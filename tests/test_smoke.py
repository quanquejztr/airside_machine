"""
Regression suite for airline_sim.

Run:  python3 -m unittest discover -s tests -v
  or: python3 tests/test_smoke.py

Design rules:
  * NEVER touches db/airline_sim.db. Integration tests run against a throwaway copy in
    a temp dir, with db.DB_FILE repointed. Your save is read once and never written.
  * No pytest dependency (stdlib unittest only) and no `rich` dependency, so this runs
    on any interpreter that can import the engine.
  * Targets the bug classes this project has actually produced: missing module-level
    imports, dead API endpoints, malformed seed CSV rows, doc-vs-code drift, and
    schedule/slot maths.
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub_rich() -> None:
    """ui/* imports rich at module scope; the web path must not need it (and CI may lack it)."""
    try:
        import rich  # noqa: F401
        return
    except ImportError:
        pass
    names = ("rich", "rich.console", "rich.table", "rich.panel", "rich.live",
             "rich.text", "rich.box", "rich.align", "rich.progress")
    for n in names:
        sys.modules.setdefault(n, types.ModuleType(n))
    attrs = {"rich.console": ["Console", "Group"], "rich.table": ["Table"],
             "rich.panel": ["Panel"], "rich.live": ["Live"], "rich.text": ["Text"],
             "rich.align": ["Align"]}
    for mod, syms in attrs.items():
        for s in syms:
            setattr(sys.modules[mod], s,
                    type(s, (object,), {"__init__": lambda self, *a, **k: None}))
    sys.modules["rich.box"].SIMPLE = None
    sys.modules["rich.box"].ROUNDED = None


_stub_rich()

LIVE_DB = ROOT / "db" / "airline_sim.db"
DATA = ROOT / "data"


# Shared fixtures own the DB redirection: they also reset db.py's cached per-thread
# connection, without which every test after the first reads the previous world.
from helpers import live_copy as TempSave  # noqa: E402
from helpers import FreshGame, fresh_game, near_airports, pick_type  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Imports. Catches a module-level NameError / missing import / py-version issue.
# ---------------------------------------------------------------------------
class TestImports(unittest.TestCase):
    MODULES = [
        "db.db", "db.seed",
        "engine.setup", "engine.airports", "engine.routes", "engine.aircraft",
        "engine.cabin", "engine.demand", "engine.scheduling", "engine.clock",
        "engine.settlement", "engine.slots", "engine.gates", "engine.fuel",
        "engine.events", "engine.banking", "engine.reputation", "engine.analytics",
        "engine.environment", "engine.ai", "engine.ai_economics", "engine.ai_flights",
        "engine.ai_gates", "engine.ai_log", "engine.flight_map_data",
        "server.game_api", "server.game_http",
    ]

    def test_all_modules_import(self):
        failed = []
        for m in self.MODULES:
            try:
                __import__(m)
            except Exception as e:
                failed.append(f"{m}: {type(e).__name__}: {e}")
        self.assertEqual([], failed, "modules failed to import:\n  " + "\n  ".join(failed))

    def test_python_version_supports_engine_syntax(self):
        # engine/cabin.py uses `str | None` annotations at runtime -> needs 3.10+.
        self.assertGreaterEqual(
            sys.version_info[:2], (3, 10),
            f"engine requires Python >= 3.10, running {sys.version.split()[0]}",
        )

    def test_web_path_does_not_need_rich(self):
        """json_tail_schedule_view feeds the overlay; it must not pull the CLI renderer."""
        import ast
        src = (ROOT / "ui" / "tail_schedule_grid.py").read_text()
        tree = ast.parse(src)
        top_level_rich = [
            n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and "rich" in (getattr(n, "module", "") or "" if isinstance(n, ast.ImportFrom)
                           else ",".join(a.name for a in n.names))
        ]
        self.assertEqual([], top_level_rich,
                         "rich must be imported lazily inside print_* helpers, not at module scope")


# ---------------------------------------------------------------------------
# 2. Seed data integrity. Would have caught the malformed C750 row.
# ---------------------------------------------------------------------------
class TestSeedData(unittest.TestCase):
    def test_csv_row_widths_match_header(self):
        problems = []
        for name in ("aircraft_types.csv", "aircraft_default_config.csv", "airports.csv",
                     "airport_categories.csv", "seasonality.csv", "financial_constants.csv"):
            p = DATA / name
            if not p.exists():
                continue
            with p.open(newline="") as fh:
                rows = [r for r in csv.reader(fh) if r and not r[0].lstrip().startswith("#")]
            width = len(rows[0])
            for i, r in enumerate(rows[1:], start=2):
                if len(r) != width:
                    problems.append(f"{name} line {i}: {len(r)} fields, header has {width}: {r[:3]}")
        self.assertEqual([], problems, "malformed CSV rows:\n  " + "\n  ".join(problems))

    def test_aircraft_economics_are_sane(self):
        bad = []
        with (DATA / "aircraft_types.csv").open(newline="") as fh:
          for r in csv.DictReader(fh):
            tid = r.get("type_id")
            if not tid or tid.lstrip().startswith("#"):
                continue
            try:
                price = float(r["purchase_price"])
                lease = float(r["weekly_lease_cost"])
            except (TypeError, ValueError):
                bad.append(f"{tid}: unparseable price/lease ({r.get('purchase_price')}/{r.get('weekly_lease_cost')})")
                continue
            if price < 1_000_000:
                bad.append(f"{tid}: purchase_price {price:,.0f} is implausibly low")
            if lease <= 0:
                bad.append(f"{tid}: weekly_lease_cost is {lease}")
        self.assertEqual([], bad, "aircraft economics:\n  " + "\n  ".join(bad))

    def test_default_cabins_fit_eec_budget(self):
        """Part C.1: (eco*1)+(prem*1.5)+(biz*2)+(first*4) must be <= aircraft_types.eec."""
        types = {}
        for r in csv.DictReader((DATA / "aircraft_types.csv").open()):
            if r.get("type_id") and not r["type_id"].lstrip().startswith("#"):
                types[r["type_id"]] = r
        over = []
        for c in csv.DictReader((DATA / "aircraft_default_config.csv").open()):
            tid = (c.get("type_id") or "").strip()
            if not tid or tid.startswith("#") or tid not in types:
                continue
            try:
                cap = float(types[tid].get("eec") or 0)
            except (TypeError, ValueError):
                continue
            if cap <= 0:
                continue
            used = (float(c["seats_economy"]) * 1.0 + float(c["seats_premium_economy"]) * 1.5
                    + float(c["seats_business"]) * 2.0 + float(c["seats_first"]) * 4.0)
            if used > cap + 0.5:
                over.append(f"{tid}: seats need {used:.0f} EEC, cap {cap:.0f}")
        self.assertEqual([], over,
                         "default cabin exceeds EEC budget, so validate_config() rejects the "
                         "stock layout:\n  " + "\n  ".join(over))

    def test_constants_referenced_in_code_exist(self):
        """A typo'd constant key silently falls back to a default; catch drift early."""
        keys = {r["key"] for r in csv.DictReader((DATA / "financial_constants.csv").open())
                if r.get("key") and not r["key"].startswith("#")}
        required = {
            "mtt_minutes", "excise_tax_rate", "segment_fee", "security_fee", "pfc_fee",
            "fuel_tax_per_gallon", "fuel_base_price_bbl", "passenger_demand_multiplier",
            "slot_utilization_threshold", "slot_grace_weeks", "slot_season_weeks",
            "slot_enforcement_weekly", "gate_auction_score_threshold",
            "max_weekly_airborne_hours_per_tail",
        }
        self.assertEqual(set(), required - keys,
                         f"missing financial_constants keys: {sorted(required - keys)}")


# ---------------------------------------------------------------------------
# 3. Migrations must be non-destructive and idempotent.
# ---------------------------------------------------------------------------
class TestMigrations(unittest.TestCase):
    def _counts(self, path):
        con = sqlite3.connect(path)
        try:
            out = {}
            for t in ("flight_segments", "routes", "fleet", "competitors", "competitor_routes"):
                try:
                    out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    out[t] = None
            return out
        finally:
            con.close()

    def test_migration_preserves_rows_and_is_idempotent(self):
        with TempSave() as db:
            before = self._counts(db.DB_FILE)
            db.ensure_schema_migrations()
            after = self._counts(db.DB_FILE)
            self.assertEqual(before, after, "migration changed row counts")
            db._migrations_done = False
            db.ensure_schema_migrations()  # must not raise on a second pass
            self.assertEqual(before, self._counts(db.DB_FILE))

    def test_is_ferry_column_present_after_migration(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(flight_segments)")}
            self.assertIn("is_ferry", cols)

    def test_schema_sql_has_no_duplicate_table_definitions(self):
        src = (ROOT / "db" / "schema.sql").read_text()
        import re
        names = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", src)
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(set(), dupes, f"duplicate CREATE TABLE blocks (first one silently wins): {dupes}")


# ---------------------------------------------------------------------------
# 4. Scheduling: turnaround handling.
# ---------------------------------------------------------------------------
class TestTurnarounds(unittest.TestCase):
    def test_normalize_turn_minutes(self):
        with TempSave():
            from engine.scheduling import normalize_turn_minutes
            rids = ["A-B", "B-C", "C-A"]
            self.assertEqual([30.0, 30.0, 30.0], normalize_turn_minutes(rids, None))
            self.assertEqual([90.0, 90.0, 90.0], normalize_turn_minutes(rids, 90))
            self.assertEqual([45.0, 60.0, 30.0], normalize_turn_minutes(rids, [45, 60, 30]))
            self.assertEqual([90.0, 30.0, 30.0], normalize_turn_minutes(rids, [90]))
            # a blank box means "default", not an error
            self.assertEqual([120.0, 30.0, 30.0], normalize_turn_minutes(rids, [120, None, None]))
            self.assertEqual([30.0, 30.0, 30.0], normalize_turn_minutes(rids, ["", "", ""]))
            for bad in (5, 5000, ["x", 30, 30]):
                with self.assertRaises(ValueError):
                    normalize_turn_minutes(rids, bad)

    def test_chaining_uses_per_leg_turnaround(self):
        with TempSave() as db:
            from engine.scheduling import _plan_chained_detailed_segments
            routes = [dict(r) for r in db.fetch_all(
                "SELECT route_id, origin_iata, dest_iata, distance_nm FROM routes LIMIT 3")]
            if len(routes) < 3:
                self.skipTest("need 3 routes in the save")
            planned, _, _ = _plan_chained_detailed_segments(
                1, ["MON"], "08:00", routes, ["X1", "X2", "X3"], 450.0, 0.5,
                turn_hours=[45 / 60, 180 / 60, 30 / 60])
            gap1 = (planned[1]["dep_abs"] - planned[0]["arr_abs"]) * 60.0
            gap2 = (planned[2]["dep_abs"] - planned[1]["arr_abs"]) * 60.0
            self.assertAlmostEqual(45.0, gap1, places=2)
            self.assertAlmostEqual(180.0, gap2, places=2)

    def test_chaining_defaults_to_mtt(self):
        with TempSave() as db:
            from engine.scheduling import _plan_chained_detailed_segments
            routes = [dict(r) for r in db.fetch_all(
                "SELECT route_id, origin_iata, dest_iata, distance_nm FROM routes LIMIT 2")]
            if len(routes) < 2:
                self.skipTest("need 2 routes in the save")
            planned, _, _ = _plan_chained_detailed_segments(
                1, ["MON"], "08:00", routes, ["X1", "X2"], 450.0, 0.5)
            gap = (planned[1]["dep_abs"] - planned[0]["arr_abs"]) * 60.0
            self.assertAlmostEqual(30.0, gap, places=2, msg="no turn_hours must fall back to MTT")


# ---------------------------------------------------------------------------
# 5. Slot use-it-or-lose-it.
# ---------------------------------------------------------------------------
class TestSlotUtilization(unittest.TestCase):
    def _alloc(self, db, week, iata, held, used, below=0, holder="PLAYER"):
        import uuid
        db.execute("DELETE FROM slot_allocations WHERE airport_iata=? AND holder_id=? AND game_week=?",
                   (iata, holder, week))
        db.execute("""INSERT INTO slot_allocations
                      (allocation_id, airport_iata, holder_id, game_week, slots_held,
                       used_this_week, below_threshold_weeks) VALUES (?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), iata, holder, week, held, used, below))

    def _held(self, db, week, iata, holder="PLAYER"):
        r = db.fetch_one("""SELECT slots_held, below_threshold_weeks FROM slot_allocations
                            WHERE airport_iata=? AND holder_id=? AND game_week=?""",
                         (iata, holder, week))
        return (r["slots_held"], r["below_threshold_weeks"]) if r else (None, None)

    def test_mid_season_warns_but_does_not_confiscate(self):
        with TempSave() as db:
            import engine.slots as S
            self._alloc(db, 6, "JFK", 8, 0, below=1)
            S.enforce_slot_utilization(6)          # season = 12, so week 6 is mid-season
            held, below = self._held(db, 6, "JFK")
            self.assertEqual(8, held, "mid-season must not take units")
            self.assertEqual(2, below, "low-use streak should still accumulate")
            notes = db.fetch_all("SELECT body FROM player_notifications WHERE game_week=6")
            self.assertTrue(any("JFK" in str(n["body"]) for n in notes),
                            "player must be warned before anything is taken")

    def test_season_boundary_scales_loss(self):
        with TempSave() as db:
            import engine.slots as S
            self._alloc(db, 12, "JFK", 8, 0, below=2)      # 0% used
            S.enforce_slot_utilization(12)
            self.assertEqual(6, self._held(db, 12, "JFK")[0], "0% used should shed 2 (capped)")
            self._alloc(db, 12, "LAX", 8, 5, below=2)      # 62.5%, small shortfall
            S.enforce_slot_utilization(12)
            self.assertEqual(7, self._held(db, 12, "LAX")[0], "small shortfall should shed 1")

    def test_home_hub_keeps_its_last_unit(self):
        with TempSave() as db:
            import engine.slots as S
            row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
            if not row or not row["home_hub_iata"]:
                self.skipTest("no airline in the save")
            hub = str(row["home_hub_iata"]).upper()
            self._alloc(db, 12, hub, 1, 0, below=2)
            S.enforce_slot_utilization(12)
            self.assertEqual(1, self._held(db, 12, hub)[0], "must never strip the last hub unit")
            self._alloc(db, 12, "ORD" if hub != "ORD" else "DFW", 1, 0, below=2)
            S.enforce_slot_utilization(12)
            other = "ORD" if hub != "ORD" else "DFW"
            self.assertEqual(0, self._held(db, 12, other)[0], "non-hub may go to zero")

    def test_cancelled_movements_are_credited(self):
        """Disruption must not cost paid quota."""
        with TempSave() as db:
            import uuid
            import engine.slots as S
            rt = db.fetch_one("SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1")
            tail = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            if not rt or not tail:
                self.skipTest("need a route and a tail in the save")
            iata = str(rt["origin_iata"]).upper()
            for _ in range(6):
                db.execute("""INSERT INTO flight_segments
                    (segment_id,game_week,day_of_week,tail_number,route_id,origin_iata,dest_iata,
                     flight_number,scheduled_dep_time,scheduled_dep_game_hour,scheduled_arr_time,
                     scheduled_arr_game_hour,baseline_dep_game_hour,baseline_arr_game_hour,status,
                     pax_business,pax_leisure,revenue_gross,excise_tax,segment_fee,security_fee,
                     pfc_fee,landing_fee,gate_fee)
                    VALUES (?,12,'MON',?,?,?,?,'CX1','08:00',1850.0,'10:00',1852.0,1850.0,1852.0,
                            'CANCELLED',0,0,0,0,0,0,0,0,0)""",
                    (f"cx-{uuid.uuid4().hex[:10]}", tail["tail_number"], rt["route_id"],
                     rt["origin_iata"], rt["dest_iata"]))
            self._alloc(db, 12, iata, 8, 0, below=2)   # nothing flown, 6 cancelled
            S.enforce_slot_utilization(12)
            held, below = self._held(db, 12, iata)
            self.assertEqual(8, held, "cancelled movements should count as used")
            self.assertEqual(0, below, "crediting them should reset the streak")


# ---------------------------------------------------------------------------
# 6. Ferry / repositioning.
# ---------------------------------------------------------------------------
class TestClockSpeed(unittest.TestCase):
    def test_switching_20x_to_1x_keeps_interpolated_time(self):
        """Regression: speed change used the new multiplier from an old snapshot, rewinding hours."""
        import time

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            clk = GameClock()
            clk.running = False
            clk.speed_multiplier = 20
            clk.game_hours_elapsed = 10.0
            clk._snapshot_ghe = 10.0
            clk._snapshot_wall_time = time.time() - 3.0
            expected = 10.0 + (3.0 / 30.0) * 20.0
            before = clk.get_interpolated_game_hours()
            self.assertAlmostEqual(expected, before, places=2)
            ok, _ = clk.set_speed(1, player_initiated=True)
            self.assertTrue(ok)
            after = clk.get_interpolated_game_hours()
            self.assertAlmostEqual(before, after, delta=0.05)
            self.assertEqual(1, clk.speed_multiplier)

    def test_switching_1x_to_20x_keeps_interpolated_time(self):
        import time

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            clk = GameClock()
            clk.running = False
            clk.speed_multiplier = 1
            clk.game_hours_elapsed = 50.0
            clk._snapshot_ghe = 50.0
            clk._snapshot_wall_time = time.time() - 6.0
            before = clk.get_interpolated_game_hours()
            clk.set_speed(20, player_initiated=True)
            after = clk.get_interpolated_game_hours()
            self.assertAlmostEqual(before, after, delta=0.05)
            self.assertEqual(20, clk.speed_multiplier)

    def test_dead_clock_reports_paused_speed_for_api(self):
        """Regression: stale DB speed 60 + dead thread made the HUD extrapolate weeks ahead."""
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine import clock as clock_mod
            from engine.clock import GameClock, get_api_clock_status

            clk = GameClock()
            clk.running = False
            clk.set_speed(60, player_initiated=True)
            clk._persist_game_state_unlocked()
            clock_mod._global_clock = clk
            st = get_api_clock_status()
            self.assertFalse(st["clock_alive"])
            self.assertEqual(0, st["speed_multiplier"])
            self.assertTrue(st["is_paused"])


class TestFlightMapVisibility(unittest.TestCase):
    def test_future_legs_are_not_all_drawn(self):
        """Map used to draw every SCHEDULED player leg for the week as a flying aircraft."""
        from engine.flight_map_data import _one_segment_per_tail, _segment_visible_on_flight_map

        now = 100.0
        self.assertTrue(_segment_visible_on_flight_map("IN_AIR", 90.0, now, player=True))
        self.assertTrue(_segment_visible_on_flight_map("SCHEDULED", 100.5, now, player=True))
        self.assertFalse(_segment_visible_on_flight_map("SCHEDULED", 120.0, now, player=True))
        self.assertFalse(_segment_visible_on_flight_map("LANDED", 90.0, now, player=True))

        class R(dict):
            def __getitem__(self, k):
                return dict.__getitem__(self, k)

        rows = [
            R(tail_number="T1", status="SCHEDULED", scheduled_dep_game_hour=101.0),
            R(tail_number="T1", status="SCHEDULED", scheduled_dep_game_hour=140.0),
            R(tail_number="T2", status="IN_AIR", scheduled_dep_game_hour=98.0),
            R(tail_number="T2", status="SCHEDULED", scheduled_dep_game_hour=110.0),
        ]
        picked = _one_segment_per_tail(rows)
        by_tail = {r["tail_number"]: r for r in picked}
        self.assertEqual(101.0, by_tail["T1"]["scheduled_dep_game_hour"])
        self.assertEqual("IN_AIR", by_tail["T2"]["status"])


class TestFerry(unittest.TestCase):
    @staticmethod
    def _ensure_player_gates(db, iata: str, units: int = 8) -> None:
        """Live-copy saves can be gate-tight; ferry tests need spare stands."""
        import uuid

        iata = str(iata).upper()
        row = db.fetch_one(
            """
            SELECT allocation_id, gate_units FROM airport_gate_allocations
            WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'
            """,
            (iata,),
        )
        if row:
            cur = int(row["gate_units"] or 0)
            if cur < units:
                db.execute(
                    "UPDATE airport_gate_allocations SET gate_units = ? WHERE allocation_id = ?",
                    (units, str(row["allocation_id"])),
                )
            return
        db.execute(
            """
            INSERT INTO airport_gate_allocations (
                allocation_id, airport_iata, holder_id, gate_units,
                used_this_week, scheduled_this_week, below_threshold_weeks, effective_week, status
            ) VALUES (?, ?, 'PLAYER', ?, 0, 0, 0, 1, 'ACTIVE')
            """,
            (str(uuid.uuid4()), iata, units),
        )

    def test_clearing_a_plan_sends_the_tail_home_with_no_revenue(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from server import game_api as api
            from engine.scheduling import on_arrival, on_departure
            hub_row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
            tail_row = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            if not hub_row or not tail_row:
                self.skipTest("need an airline and a tail in the save")
            hub = str(hub_row["home_hub_iata"]).upper()
            tail = tail_row["tail_number"]
            away = "SFO" if hub != "SFO" else "JFK"
            self._ensure_player_gates(db, hub)
            self._ensure_player_gates(db, away)
            db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
            db.execute("UPDATE fleet SET current_airport_iata=?, status='IDLE' WHERE tail_number=?",
                       (away, tail))

            out = api.assign_schedule({"tail_number": tail, "mode": "clear"})
            self.assertTrue(out.get("ok"), out.get("error"))
            ferry = out.get("ferry")
            self.assertIsNotNone(ferry, "a tail away from hub should be repositioned")
            self.assertEqual(hub, ferry["to"])

            seg = db.fetch_one(
                "SELECT segment_id FROM flight_segments WHERE tail_number=? AND is_ferry=1", (tail,))
            self.assertIsNotNone(seg, "ferry segment should exist")
            on_departure(seg["segment_id"])
            on_arrival(seg["segment_id"])
            r = db.fetch_one("""SELECT pax_leisure,pax_business,revenue_gross,landing_fee,
                                gate_fee,fuel_cost FROM flight_segments WHERE segment_id=?""",
                             (seg["segment_id"],))
            self.assertEqual(0, int(r["pax_leisure"]))
            self.assertEqual(0, int(r["pax_business"]))
            self.assertEqual(0.0, float(r["revenue_gross"]),
                             "a ferry must not sell seats on a market the player never opened")
            self.assertGreater(float(r["landing_fee"] or 0) + float(r["gate_fee"] or 0), 0,
                               "repositioning should still cost fees")
            self.assertGreater(float(r["fuel_cost"] or 0), 0, "repositioning should burn fuel")
            loc = db.fetch_one("SELECT current_airport_iata FROM fleet WHERE tail_number=?", (tail,))
            self.assertEqual(hub, str(loc["current_airport_iata"]).upper())

    def test_no_ferry_when_already_at_hub(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from server import game_api as api
            hub_row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
            tail_row = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            if not hub_row or not tail_row:
                self.skipTest("need an airline and a tail in the save")
            hub = str(hub_row["home_hub_iata"]).upper()
            tail = tail_row["tail_number"]
            db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
            db.execute("UPDATE fleet SET current_airport_iata=?, status='IDLE' WHERE tail_number=?",
                       (hub, tail))
            out = api.assign_schedule({"tail_number": tail, "mode": "clear"})
            self.assertTrue(out.get("ok"), out.get("error"))
            self.assertIsNone(out.get("ferry"))

    def test_manual_reposition_to_non_hub(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from server import game_api as api
            from engine.scheduling import (
                hhmm_from_absolute_game_hour,
                on_arrival,
                on_departure,
                tail_position_and_free_hour,
                _day_of_week_label,
            )
            hub_row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
            tail_row = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            if not hub_row or not tail_row:
                self.skipTest("need an airline and a tail in the save")
            hub = str(hub_row["home_hub_iata"]).upper()
            tail = tail_row["tail_number"]
            away = "SFO" if hub != "SFO" else "JFK"
            self._ensure_player_gates(db, hub)
            self._ensure_player_gates(db, away)
            db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
            db.execute("UPDATE fleet SET current_airport_iata=?, status='IDLE' WHERE tail_number=?",
                       (away, tail))
            # Leave an old passenger leg on the books so reposition must clear it.
            db.execute(
                """
                INSERT INTO flight_segments (
                    segment_id, game_week, day_of_week, tail_number, route_id,
                    origin_iata, dest_iata, flight_number,
                    scheduled_dep_time, scheduled_dep_game_hour,
                    scheduled_arr_time, scheduled_arr_game_hour,
                    baseline_dep_game_hour, baseline_arr_game_hour,
                    status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee, is_ferry
                ) VALUES ('legacy-atl-sfo', 99, 'SAT', ?, 'ATL-SFO', 'ATL', 'SFO', 'OLD1',
                          '12:00', 9999.0, '16:00', 10003.0, 9999.0, 10003.0,
                          'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                """,
                (tail,),
            )

            pos, free = tail_position_and_free_hour(tail)
            self.assertEqual(away, pos)
            free = float(free) + (2.0 / 60.0)  # avoid HH:MM truncation landing before MTT
            gw = int(free // 168.0) + 1
            day = _day_of_week_label(gw, free)
            dep = hhmm_from_absolute_game_hour(free)

            out = api.reposition_aircraft({
                "tail_number": tail,
                "dest_iata": hub,
                "day": day,
                "departure_time": dep,
            })
            self.assertTrue(out.get("ok"), out.get("error"))
            ferry = out.get("ferry")
            self.assertIsNotNone(ferry)
            self.assertEqual(away, ferry["from"])
            self.assertEqual(hub, ferry["to"])
            self.assertEqual(day, ferry["day"])
            leftover = db.fetch_one(
                "SELECT 1 FROM flight_segments WHERE segment_id='legacy-atl-sfo'"
            )
            self.assertIsNone(leftover, "reposition must clear leftover passenger legs")

            seg = db.fetch_one(
                "SELECT segment_id FROM flight_segments WHERE tail_number=? AND is_ferry=1 ORDER BY scheduled_dep_game_hour DESC LIMIT 1",
                (tail,),
            )
            self.assertIsNotNone(seg)
            on_departure(seg["segment_id"])
            on_arrival(seg["segment_id"])
            loc = db.fetch_one("SELECT current_airport_iata FROM fleet WHERE tail_number=?", (tail,))
            self.assertEqual(hub, str(loc["current_airport_iata"]).upper())


# ---------------------------------------------------------------------------
# Settlement catch-up. Saves that advanced calendar weeks without week_ledger
# must heal on boot / Books open.
# ---------------------------------------------------------------------------
class TestSettlementCatchUp(unittest.TestCase):
    def _stub_ledger(self, db, week: int) -> None:
        db.execute(
            """
            INSERT OR IGNORE INTO week_ledger (
                game_week, revenue_gross, excise_tax, segment_fees, security_fees,
                pfc_fees, landing_fees, gate_fees, fuel_cost, lease_costs,
                maintenance_costs, loan_payments, corporate_tax, net_income,
                cash_end_of_week
            ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (week,),
        )

    def test_catch_up_fills_missing_ledger_and_moves_reputation(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation
            from engine.settlement import (
                catch_up_missing_settlements,
                last_settled_week,
                missing_settlement_weeks,
            )
            from engine.reputation import preview_reputation

            tid = pick_type(g.db, "NARROW", min_range=600, max_seats=220)
            if not tid:
                self.skipTest("no usable narrowbody")
            rr = g.db.fetch_one(
                "SELECT runway_req_ft FROM aircraft_types WHERE type_id = ?", (tid,)
            )
            dests = near_airports(
                g.db, "TPA", 250, 900, limit=4,
                require_runway_ft=int(rr["runway_req_ft"] or 0),
            )
            if not dests:
                self.skipTest("no runway-adequate spoke near TPA")
            other = dests[0]
            out_id, in_id = g.open_round_trip(other)
            tail = g.lease(tid)
            assign_rotation(tail, [out_id, in_id])
            g.fly_all(week=1)

            # Advance calendar without settling (simulates missed on_week).
            g.set_hour(168 * 2 + 10)  # week 3
            g.db.execute("UPDATE airline SET reputation_score = 50 WHERE id = 1")
            self.assertEqual([1, 2], missing_settlement_weeks())
            self.assertEqual(0, last_settled_week())

            out = catch_up_missing_settlements()
            self.assertEqual([1, 2], out["settled"])
            self.assertEqual([1, 2], out["missing_before"])
            self.assertEqual([], out["errors"])
            self.assertEqual(2, last_settled_week())
            self.assertEqual([], missing_settlement_weeks())
            led1 = g.db.fetch_one("SELECT game_week, net_income FROM week_ledger WHERE game_week = 1")
            self.assertIsNotNone(led1)
            # Week 1 on-time → +2; week 2 empty → 0 (no OTP inflate).
            rep = preview_reputation(3)
            self.assertAlmostEqual(52.0, float(rep["reputation_score"]), places=5)

    def test_catch_up_is_idempotent(self):
        with fresh_game(hub="TPA") as g:
            from engine.settlement import catch_up_missing_settlements, last_settled_week

            g.set_hour(168 + 5)  # week 2
            a = catch_up_missing_settlements()
            self.assertEqual([1], a["settled"])
            n = last_settled_week()
            cash1 = g.cash()
            led = g.db.fetch_one("SELECT game_week FROM week_ledger WHERE game_week = 1")
            self.assertIsNotNone(led)
            b = catch_up_missing_settlements()
            self.assertEqual([], b["settled"])
            self.assertEqual(n, last_settled_week())
            self.assertAlmostEqual(cash1, g.cash(), places=2)

    def test_missing_weeks_detects_gaps(self):
        with fresh_game(hub="TPA") as g:
            from engine.settlement import missing_settlement_weeks, last_settled_week

            g.set_hour(168 * 4 + 1)  # week 5
            self._stub_ledger(g.db, 1)
            self._stub_ledger(g.db, 3)
            self.assertEqual([2, 4], missing_settlement_weeks())
            self.assertEqual(3, last_settled_week())  # MAX, even with a gap

    def test_catch_up_respects_limit(self):
        with fresh_game(hub="TPA") as g:
            from engine.settlement import (
                catch_up_missing_settlements,
                missing_settlement_weeks,
                last_settled_week,
            )

            g.set_hour(168 * 3 + 1)  # week 4 → missing 1,2,3
            out = catch_up_missing_settlements(limit=2)
            self.assertEqual([1, 2, 3], out["missing_before"])
            self.assertEqual([1, 2], out["attempted"])
            self.assertEqual([1, 2], out["settled"])
            self.assertEqual([3], missing_settlement_weeks())
            self.assertEqual(2, last_settled_week())

    def test_books_status_includes_settlement_and_reputation(self):
        with fresh_game(hub="TPA") as g:
            from server import game_api as api

            g.set_hour(10)
            out = api.books_status()
            self.assertTrue(out.get("ok"), out.get("error"))
            self.assertIn("week", out)
            self.assertIn("fuel", out)
            self.assertIn("settlement", out)
            self.assertIn("reputation", out)
            st = out["settlement"]
            self.assertEqual(1, int(st["calendar_week"]))
            self.assertEqual([], st["missing_weeks"])
            rep = out["reputation"]
            self.assertIn("reputation_score", rep)
            self.assertIn("brand_power", rep)
            self.assertIn("projected_delta", rep)


# ---------------------------------------------------------------------------
# Reputation OTP math (settlement + Books preview share _week_otp_stats).
# ---------------------------------------------------------------------------
class TestReputationOtp(unittest.TestCase):
    def _insert_landed(self, g, *, week, delay, seg_id, route_id, tail):
        g.db.execute(
            """
            INSERT INTO flight_segments (
                segment_id, game_week, day_of_week, tail_number, route_id,
                origin_iata, dest_iata, flight_number,
                scheduled_dep_time, scheduled_dep_game_hour,
                scheduled_arr_time, scheduled_arr_game_hour,
                baseline_dep_game_hour, baseline_arr_game_hour,
                status, delay_minutes, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee, is_ferry
            ) VALUES (?, ?, 'MON', ?, ?, 'TPA', 'MCO', 'X',
                      '08:00', 8.0, '09:00', 9.0, 8.0, 9.0,
                      'LANDED', ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (seg_id, week, tail, route_id, delay),
        )

    def test_empty_week_does_not_boost_reputation(self):
        with fresh_game(hub="TPA") as g:
            from engine.reputation import update_reputation, preview_reputation

            g.db.execute("UPDATE airline SET reputation_score = 50 WHERE id = 1")
            delta, score = update_reputation(1)
            self.assertEqual(0.0, delta)
            self.assertEqual(50.0, score)
            prev = preview_reputation(1)
            self.assertEqual(0, int(prev["flights_counted"]))
            self.assertEqual(0.0, float(prev["projected_delta"]))

    def test_empty_week_still_applies_aog(self):
        with fresh_game(hub="TPA") as g:
            from engine.reputation import update_reputation

            g.db.execute("UPDATE airline SET reputation_score = 50 WHERE id = 1")
            g.db.execute(
                """
                INSERT INTO event_log (
                    event_id, game_week, game_time_hours, event_type,
                    affected_iata, affected_tail, description, financial_impact, resolved
                ) VALUES ('aog-empty', 1, 10.0, 'AOG', NULL, NULL, 'Grounded', 0, 0)
                """
            )
            delta, score = update_reputation(1)
            self.assertAlmostEqual(-0.5, delta, places=5)
            self.assertAlmostEqual(49.5, score, places=5)

    def test_high_otp_and_aog_penalty(self):
        with fresh_game(hub="TPA") as g:
            from engine.reputation import update_reputation, apply_brand_power_from_reputation
            from engine.reputation import reputation_to_brand_power

            tid = pick_type(g.db, "NARROW", min_range=200, max_seats=220)
            if not tid:
                self.skipTest("no usable narrowbody")
            out_id, _ = g.open_round_trip("MCO")
            tail = g.lease(tid)
            g.db.execute("UPDATE airline SET reputation_score = 50, marketing_brand_bonus = 0.05 WHERE id = 1")
            for i, delay in enumerate((0, 0)):
                self._insert_landed(
                    g, week=1, delay=delay, seg_id=f"otp-{i}", route_id=out_id, tail=tail,
                )
            g.db.execute(
                """
                INSERT INTO event_log (
                    event_id, game_week, game_time_hours, event_type,
                    affected_iata, affected_tail, description, financial_impact, resolved
                ) VALUES ('aog-1', 1, 10.0, 'AOG', NULL, ?, 'Engine', 0, 0)
                """,
                (tail,),
            )
            delta, score = update_reputation(1)
            # +2 OTP, -0.5 AOG
            self.assertAlmostEqual(1.5, delta, places=5)
            self.assertAlmostEqual(51.5, score, places=5)
            bp = apply_brand_power_from_reputation()
            self.assertAlmostEqual(reputation_to_brand_power(51.5) + 0.05, bp, places=5)

    def test_poor_otp_drops_reputation(self):
        with fresh_game(hub="TPA") as g:
            from engine.reputation import update_reputation

            tid = pick_type(g.db, "NARROW", min_range=200, max_seats=220)
            if not tid:
                self.skipTest("no usable narrowbody")
            out_id, _ = g.open_round_trip("MCO")
            tail = g.lease(tid)
            g.db.execute("UPDATE airline SET reputation_score = 50 WHERE id = 1")
            for i, delay in enumerate((30, 40, 50, 60)):  # 0% on-time
                self._insert_landed(
                    g, week=1, delay=delay, seg_id=f"late-{i}", route_id=out_id, tail=tail,
                )
            delta, score = update_reputation(1)
            self.assertEqual(-5.0, delta)
            self.assertEqual(45.0, score)


# ---------------------------------------------------------------------------
# 7. Overlay API. Every dock the UI can open must return a payload.
# ---------------------------------------------------------------------------
class TestOverlayAPI(unittest.TestCase):
    def test_every_read_endpoint_returns_ok(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from server import game_api as api
            rt = db.fetch_one("SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1")
            tail = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            comp = db.fetch_one("SELECT competitor_id FROM competitors LIMIT 1")
            # Avoid a multi-week settlement catch-up during endpoint smoke (slow AI turns).
            gw_row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
            gw = int(gw_row["game_week"] or 1) if gw_row else 1
            for w in range(1, max(1, gw)):
                db.execute(
                    """
                    INSERT OR IGNORE INTO week_ledger (
                        game_week, revenue_gross, excise_tax, segment_fees, security_fees,
                        pfc_fees, landing_fees, gate_fees, fuel_cost, lease_costs,
                        maintenance_costs, loan_payments, corporate_tax, net_income,
                        cash_end_of_week
                    ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                    """,
                    (w,),
                )
            # route_detail is player-scoped: the routes table also holds AI-only markets.
            prow = db.fetch_one(
                "SELECT r.route_id, r.origin_iata, r.dest_iata FROM routes r "
                "JOIN player_routes pr ON pr.route_id = r.route_id LIMIT 1")
            rid = prow["route_id"] if prow else (rt["route_id"] if rt else "SFO-LAX")
            o = rt["origin_iata"] if rt else "SFO"
            d = rt["dest_iata"] if rt else "LAX"
            cases = {
                "get_state": lambda: api.get_state(),
                "flight_map": lambda: api.flight_map(),
                "search_airports": lambda: api.search_airports("SF"),
                "list_catalog": lambda: api.list_catalog(None),
                "cabin_layout": lambda: api.cabin_layout("A320"),
                "list_fleet": lambda: api.list_fleet(),
                "list_player_routes": lambda: api.list_player_routes(),
                "player_routes_overview": lambda: api.player_routes_overview(),
                "preview_route": lambda: api.preview_route(o, d),
                "route_suggestions": lambda: api.route_suggestions(o, limit=5),
            }
            if prow:
                cases["route_detail"] = lambda: api.route_detail(rid)
            cases.update({
                "gate_auctions": lambda: api.gate_auctions(),
                "player_gate_bids": lambda: api.player_gate_bids(),
                "player_gates": lambda: api.player_gates(),
                "slot_status": lambda: api.slot_status(),
                "player_slot_bids": lambda: api.player_slot_bids(),
                "player_notifications": lambda: api.player_notifications(),
                "flight_board": lambda: api.flight_board(),
                "airport_flight_board": lambda: api.airport_flight_board(
                    prow["origin_iata"] if prow else (rt["origin_iata"] if rt else "ATL")
                ),
                "competitors_overview": lambda: api.competitors_overview(),
                "contested_markets": lambda: api.contested_markets(),
                "bank_status": lambda: api.bank_status("1000000"),
                "books_status": lambda: api.books_status(),
                "pop_week_summaries": lambda: api.pop_week_summaries(),
            })
            if comp:
                cases["competitor_routes"] = lambda: api.competitor_routes(comp["competitor_id"])
            if tail:
                cases["tail_schedule"] = lambda: api.tail_schedule(tail["tail_number"])
            failures = []
            for name, fn in cases.items():
                try:
                    out = fn()
                    if not isinstance(out, dict):
                        failures.append(f"{name}: returned {type(out).__name__}, expected dict")
                    elif out.get("ok") is False:
                        failures.append(f"{name}: ok=False ({out.get('error')})")
                except Exception as e:
                    failures.append(f"{name}: {type(e).__name__}: {e}")
            self.assertEqual([], failures, "overlay endpoints failing:\n  " + "\n  ".join(failures))

    def test_every_js_endpoint_has_a_route_handler(self):
        """A dock calling a path the server does not serve renders blank."""
        import re
        js = (ROOT / "web" / "app.js").read_text()
        http = (ROOT / "server" / "game_http.py").read_text()
        called = set()
        for m in re.finditer(r"""["'`](/api/[A-Za-z0-9/_-]+)""", js):
            called.add(m.group(1).rstrip("/"))
        served = set()
        for m in re.finditer(r"""path == ["'](/api/[A-Za-z0-9/_-]+)""", http):
            served.add(m.group(1).rstrip("/"))
        for m in re.finditer(r"""path\.startswith\(["'](/api/[A-Za-z0-9/_-]+)""", http):
            served.add(m.group(1).rstrip("/"))
        missing = sorted(c for c in called if c not in served)
        self.assertEqual([], missing, f"web/app.js calls unserved endpoints: {missing}")


# ---------------------------------------------------------------------------
# 8. AI wiring. The competitors must actually fly, or they are invisible.
# ---------------------------------------------------------------------------
class TestAIWiring(unittest.TestCase):
    def test_bootstrap_produces_a_flying_ai(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ai_bootstrap_if_needed, ensure_competitors_seeded
            from engine.ai_flights import spawn_ai_segments_for_week
            ensure_competitors_seeded()
            n_comp = db.fetch_one("SELECT COUNT(*) AS n FROM competitors")["n"]
            self.assertGreater(n_comp, 0, "roster did not seed")
            self.assertGreater(db.fetch_one("SELECT COUNT(*) AS n FROM ai_fleet")["n"], 0,
                               "ai_fleet is empty; aircraft choice and lease charges depend on it")
            week = int(db.fetch_one("SELECT game_week FROM game_state WHERE id=1")["game_week"] or 1)
            ai_bootstrap_if_needed(week)
            spawn_ai_segments_for_week(week)
            segs = db.fetch_one("SELECT COUNT(*) AS n FROM ai_flight_segments WHERE game_week=?",
                                (week,))["n"]
            self.assertGreater(segs, 0, "AI has no segments, so nothing appears on the map/board")

    def test_ai_weekly_turn_records_no_errors(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ai_weekly_turn, ensure_competitors_seeded
            ensure_competitors_seeded()
            week = int(db.fetch_one("SELECT game_week FROM game_state WHERE id=1")["game_week"] or 1)
            out = ai_weekly_turn(week)
            self.assertEqual(0, int(out.get("errors") or 0),
                             f"ai_weekly_turn reported errors: {out}")
            errs = db.fetch_all(
                "SELECT competitor_id, error FROM ai_turn_log WHERE game_week=? AND error IS NOT NULL",
                (week,))
            self.assertEqual([], [f"{r['competitor_id']}: {r['error']}" for r in errs])

    def test_international_ai_reaches_intercontinental_radius(self):
        with TempSave() as db:
            from engine.ai import _hub_radius_for_competitor, ai_generate_candidates, load_competitor_specs
            from engine.routes import get_route

            titan = next(s for s in load_competitor_specs() if s["competitor_id"] == "AI_TITAN")
            self.assertTrue(titan.get("international"))
            self.assertGreaterEqual(_hub_radius_for_competitor("AI_TITAN", "HUBSPOKE"), 5600.0)
            self.assertLessEqual(_hub_radius_for_competitor("AI_TEMPEST", "BUDGET"), 1500.0)

            pairs = ai_generate_candidates("AI_TITAN")
            self.assertTrue(any("LHR" in p or "CDG" in p or "FRA" in p for p in pairs),
                            f"expected European pairs in pool, got sample {pairs[:8]}")

            from engine.ai import ai_resolve_aircraft_type, _wide_type_ids
            rt = get_route("ATL-LHR")
            if rt and float(rt["distance_nm"] or 0) >= 3000:
                db.execute("DELETE FROM ai_fleet WHERE competitor_id = 'AI_TITAN'")
                db.execute(
                    "INSERT INTO ai_fleet (ai_tail, competitor_id, type_id, status) VALUES (?, ?, ?, 'ACTIVE')",
                    ("TN-001", "AI_TITAN", "A320"),
                )
                picked = ai_resolve_aircraft_type("AI_TITAN", "ATL-LHR")
                self.assertIn(picked, _wide_type_ids())

    def test_reset_airline_reseeds_competitors_from_json(self):
        with FreshGame(hub="SGN", callsign="VNA", name="VNA") as world:
            from engine import setup
            from engine.ai import load_competitor_specs

            spec = next(s for s in load_competitor_specs() if s["competitor_id"] == "AI_TITAN")
            world.execute(
                "UPDATE competitors SET cash = ?, fleet_size = ?, stance = 'CONSOLIDATE' WHERE competitor_id = ?",
                (8_500_000_000.0, 120, "AI_TITAN"),
            )
            setup.reset_airline()
            row = world.fetch_one(
                "SELECT cash, fleet_size, stance FROM competitors WHERE competitor_id = 'AI_TITAN'"
            )
            self.assertIsNotNone(row)
            self.assertAlmostEqual(float(row["cash"]), float(spec["cash"]), places=0)
            self.assertEqual(int(row["fleet_size"]), int(spec["fleet_size"]))
            self.assertEqual(str(row["stance"]).upper(), "GROW")
            routes = world.fetch_one(
                "SELECT COUNT(*) AS n FROM competitor_routes WHERE competitor_id = 'AI_TITAN'"
            )["n"]
            self.assertGreater(routes, 0, "starter network should be re-seeded after reset")


# ---------------------------------------------------------------------------
# 8a2. Player hub starter capacity + raised runway hourly caps.
# ---------------------------------------------------------------------------
class TestPlayerHubStarters(unittest.TestCase):
    def test_create_airline_at_slot_hub_gets_free_gates_and_slots(self):
        from tests.helpers import fresh_game
        from engine.gates import is_auctioned_airport
        from engine.slots import declared_hourly_cap, is_slot_controlled, slots_held

        with fresh_game(hub="ICN", name="Test Air", callsign="TST"):
            from db import db

            self.assertTrue(is_auctioned_airport("ICN"))
            self.assertTrue(is_slot_controlled("ICN"))
            gates = db.fetch_one(
                """
                SELECT gate_units FROM airport_gate_allocations
                WHERE airport_iata = 'ICN' AND holder_id = 'PLAYER' AND status = 'ACTIVE'
                """
            )
            self.assertIsNotNone(gates)
            self.assertEqual(int(gates["gate_units"] or 0), 2)
            self.assertEqual(slots_held("ICN", "PLAYER", 1), 12)
            self.assertEqual(slots_held("ICN", "PLAYER", 12), 12)
            self.assertGreaterEqual(declared_hourly_cap("ICN"), 26)

    def test_create_airline_at_non_slot_hub_skips_slots(self):
        from tests.helpers import fresh_game
        from engine.slots import is_slot_controlled

        with fresh_game(hub="SGN", name="VNA Test", callsign="VNT"):
            from db import db

            self.assertFalse(is_slot_controlled("SGN"))
            slots = db.fetch_one(
                """
                SELECT slots_held FROM slot_allocations
                WHERE airport_iata = 'SGN' AND holder_id = 'PLAYER' AND game_week = 1
                """
            )
            self.assertIsNone(slots)

    def test_slot_hourly_caps_raise_on_existing_rows(self):
        from tests.helpers import fresh_game
        from engine.slots import STAGE1_SLOT_AIRPORTS, declared_hourly_cap, seed_slot_controlled_airports

        with fresh_game(hub="ATL", name="Cap Test", callsign="CAP"):
            from db import db

            db.execute(
                "UPDATE slot_controlled_airports SET declared_hourly_cap = 3 WHERE iata = 'LHR'"
            )
            self.assertEqual(declared_hourly_cap("LHR"), 3)
            seed_slot_controlled_airports()
            self.assertEqual(declared_hourly_cap("LHR"), int(STAGE1_SLOT_AIRPORTS["LHR"]))


# ---------------------------------------------------------------------------
# 8b. AI Phase 1 — load streak column, gate planning, distress reactivation.
# ---------------------------------------------------------------------------
class TestAiPhase1(unittest.TestCase):
    def test_load_streaks_write_low_lf_weeks_not_consecutive_loss(self):
        """Regression: LF streak overwrote consecutive_loss_weeks and triggered premature CLOSING."""
        with TempSave() as db:
            db.ensure_schema_migrations()
            from unittest.mock import patch
            from engine.ai import ai_update_load_streaks, ensure_competitors_seeded

            ensure_competitors_seeded()
            row = db.fetch_one(
                """
                SELECT competitor_id, route_pair_id, outbound_route_id
                FROM competitor_routes
                WHERE status = 'ACTIVE'
                LIMIT 1
                """
            )
            self.assertIsNotNone(row)
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                """
                UPDATE competitor_routes
                SET consecutive_loss_weeks = 0, low_lf_weeks = 0,
                    actual_weekly_revenue_avg = 50000
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            with patch(
                "engine.demand.estimate_competitor_route_load_factor",
                return_value=0.40,
            ):
                ai_update_load_streaks(cid, 2, 1)
            after = db.fetch_one(
                """
                SELECT consecutive_loss_weeks, low_lf_weeks
                FROM competitor_routes
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            self.assertEqual(1, int(after["low_lf_weeks"]))
            self.assertEqual(0, int(after["consecutive_loss_weeks"]))

    def test_planned_gate_freqs_ignore_suspended_routes(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ensure_competitors_seeded
            from engine.ai_gates import _planned_freqs

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes WHERE status='ACTIVE' LIMIT 1"
            )
            self.assertIsNotNone(row)
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"]).upper()
            db.execute(
                """
                UPDATE competitor_routes
                SET status = 'SUSPENDED', frequency_per_week = 99
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, row["route_pair_id"]),
            )
            freqs = _planned_freqs(cid, [])
            self.assertNotIn(pair, freqs)

    def test_reactivate_suspended_when_cash_recovered(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from unittest.mock import patch
            from engine.ai import _reactivate_suspended_routes, ensure_competitors_seeded

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes LIMIT 1"
            )
            self.assertIsNotNone(row)
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                "UPDATE competitor_routes SET status = 'SUSPENDED' WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )
            db.execute(
                "UPDATE competitors SET cash = 50000000, stance = 'GROW' WHERE competitor_id = ?",
                (cid,),
            )
            with patch("engine.ai_gates.ai_gate_shortfall", return_value=0):
                _reactivate_suspended_routes(cid, 5)
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )["status"]
            self.assertEqual("ACTIVE", str(st))

    def test_reactivate_skipped_under_consolidate_or_negative_cash(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import _reactivate_suspended_routes, ensure_competitors_seeded

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes LIMIT 1"
            )
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                "UPDATE competitor_routes SET status = 'SUSPENDED' WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )
            db.execute(
                "UPDATE competitors SET cash = -1000, stance = 'GROW' WHERE competitor_id = ?",
                (cid,),
            )
            _reactivate_suspended_routes(cid, 5)
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )["status"]
            self.assertEqual("SUSPENDED", str(st))

            db.execute(
                "UPDATE competitors SET cash = 50000000, stance = 'CONSOLIDATE' WHERE competitor_id = ?",
                (cid,),
            )
            _reactivate_suspended_routes(cid, 5)
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )["status"]
            self.assertEqual("SUSPENDED", str(st))

    def test_light_pass_reactivates_after_distress_recovery(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from unittest.mock import patch
            from engine.ai import ai_light_pass, ensure_competitors_seeded

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes LIMIT 1"
            )
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                "UPDATE competitor_routes SET status = 'SUSPENDED' WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )
            db.execute(
                "UPDATE competitors SET cash = 500000000, stance = 'GROW', consecutive_loss_weeks = 0"
                " WHERE competitor_id = ?",
                (cid,),
            )
            with patch("engine.ai_gates.ai_gate_shortfall", return_value=0):
                ai_light_pass(cid, 10)
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )["status"]
            self.assertEqual("ACTIVE", str(st))


# ---------------------------------------------------------------------------
# 8b2. AI departure banks — 7-day / 24-hour spread.
# ---------------------------------------------------------------------------
class TestAiFlightBanks(unittest.TestCase):
    def test_frequency_seven_covers_every_weekday(self):
        from engine.ai_flights import bank_dep_hours

        hours = bank_dep_hours(7, "HUBSPOKE", 0.0, pair_id="ATL-JFK")
        days = sorted({int(h // 24.0) % 7 for h in hours})
        self.assertEqual(days, [0, 1, 2, 3, 4, 5, 6])
        clock_hours = [h % 24.0 for h in hours]
        self.assertGreater(max(clock_hours) - min(clock_hours), 6.0)

    def test_hubspoke_is_not_monday_morning_only(self):
        from engine.ai_flights import bank_dep_hours

        hours = bank_dep_hours(5, "HUBSPOKE", 336.0, pair_id="ICN-NRT")
        offsets = [h - 336.0 for h in hours]
        self.assertGreater(max(offsets), 96.0, "should reach later weekdays, not Mon–Tue banks")
        self.assertFalse(all(abs((h % 24.0) - 7.0) < 0.5 or abs((h % 24.0) - 15.0) < 0.5 for h in hours))


# ---------------------------------------------------------------------------
# 8c. AI Phase 2 — hub gate scaling, spawn reconcile, mega starter balance.
# ---------------------------------------------------------------------------
class TestAiPhase2(unittest.TestCase):
    def test_hub_gates_scale_to_peak_concurrency(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ensure_competitors_seeded
            from engine.ai_gates import ai_gates_held, ai_peak_concurrent_at, sync_hub_gates_to_network

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, home_hub_iata FROM competitors WHERE competitor_id = 'AI_TITAN'"
            )
            if not row:
                self.skipTest("AI_TITAN not in roster")
            cid = str(row["competitor_id"])
            hub = str(row["home_hub_iata"])
            peak = ai_peak_concurrent_at(cid, hub, [], 0.5)
            db.execute(
                """
                UPDATE airport_gate_allocations
                SET gate_units = 1
                WHERE holder_id = ? AND airport_iata = ?
                """,
                (cid, hub),
            )
            added = sync_hub_gates_to_network(cid)
            held = ai_gates_held(cid, hub)
            self.assertGreater(added, 0)
            self.assertGreaterEqual(held, min(24, peak + 1))

    def test_reconcile_thins_route_with_zero_spawned_legs(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ensure_competitors_seeded
            from engine.ai_flights import _reconcile_unspawned_routes

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes WHERE status='ACTIVE' LIMIT 1"
            )
            self.assertIsNotNone(row)
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                """
                UPDATE competitor_routes SET frequency_per_week = 3
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            spawned = {
                (str(r["competitor_id"]), str(r["route_pair_id"])): 2
                for r in db.fetch_all(
                    "SELECT competitor_id, route_pair_id FROM competitor_routes WHERE status='ACTIVE'"
                )
            }
            spawned.pop((cid, pair), None)
            changed = _reconcile_unspawned_routes(2, spawned)
            self.assertEqual(1, changed)
            freq = int(
                db.fetch_one(
                    "SELECT frequency_per_week FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                    (cid, pair),
                )["frequency_per_week"]
            )
            self.assertEqual(2, freq)

    def test_reconcile_suspends_route_at_one_frequency(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ensure_competitors_seeded
            from engine.ai_flights import _reconcile_unspawned_routes

            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id, route_pair_id FROM competitor_routes WHERE status='ACTIVE' LIMIT 1"
            )
            cid = str(row["competitor_id"])
            pair = str(row["route_pair_id"])
            db.execute(
                """
                UPDATE competitor_routes SET frequency_per_week = 1
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            spawned = {
                (str(r["competitor_id"]), str(r["route_pair_id"])): 2
                for r in db.fetch_all(
                    "SELECT competitor_id, route_pair_id FROM competitor_routes WHERE status='ACTIVE'"
                )
            }
            spawned.pop((cid, pair), None)
            _reconcile_unspawned_routes(2, spawned)
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )["status"]
            self.assertEqual("SUSPENDED", str(st))

    def test_spawn_creates_segments_for_titan_after_gate_sync(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import ensure_competitors_seeded
            from engine.ai_flights import spawn_ai_segments_for_week
            from engine.ai_gates import sync_hub_gates_to_network

            ensure_competitors_seeded()
            sync_hub_gates_to_network("AI_TITAN")
            db.execute("UPDATE game_state SET game_week = 2, game_hours_elapsed = 168 WHERE id = 1")
            out = spawn_ai_segments_for_week(2)
            self.assertGreater(int(out.get("inserted") or 0), 0)
            titan = db.fetch_one(
                """
                SELECT COUNT(*) AS n FROM ai_flight_segments
                WHERE competitor_id = 'AI_TITAN' AND game_week = 2
                """
            )["n"]
            self.assertGreater(int(titan), 0)


# ---------------------------------------------------------------------------
# 8d. Phase 3 — clock/map reliability and synchronous week-boundary spawn.
# ---------------------------------------------------------------------------
class TestAiPhase3(unittest.TestCase):
    def tearDown(self):
        try:
            from engine.clock import stop_game_clock

            stop_game_clock()
        except Exception:
            pass

    def test_dead_clock_reports_zero_speed_not_db_stale_value(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import get_api_clock_status, stop_game_clock

            stop_game_clock()
            db.execute(
                "UPDATE game_state SET speed_multiplier = 60, game_hours_elapsed = 100 WHERE id = 1"
            )
            status = get_api_clock_status()
            self.assertFalse(status["clock_alive"])
            self.assertEqual(0, status["speed_multiplier"])
            self.assertTrue(status["is_paused"])
            self.assertAlmostEqual(100.0, float(status["current_game_hour"]), places=2)

    def test_flight_map_payload_uses_live_calendar_week(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import stop_game_clock
            from engine.flight_map_data import get_flight_map_payload

            stop_game_clock()
            db.execute(
                """
                UPDATE game_state
                SET game_week = 1, game_hours_elapsed = 200, speed_multiplier = 60
                WHERE id = 1
                """
            )
            payload = get_flight_map_payload()
            self.assertEqual(2, payload["game_week"])
            self.assertEqual(0, payload["speed_multiplier"])
            self.assertFalse(payload.get("clock_alive"))

    def test_calendar_week_from_interpolated_clock_hours(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            import engine.clock as clock_mod
            from engine.scheduling.time_helpers import calendar_game_week_from_state

            class _FakeClk:
                def is_alive(self):
                    return True

                def get_interpolated_game_hours(self):
                    return 200.0

            old = clock_mod._global_clock
            clock_mod._global_clock = _FakeClk()
            try:
                self.assertEqual(2, calendar_game_week_from_state())
            finally:
                clock_mod._global_clock = old

    def test_spawn_segments_for_calendar_week_is_immediate(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation
            from engine.settlement import spawn_segments_for_calendar_week

            tid, other, out_id, in_id = TestFlightNumbers._rt_setup(self, g)
            tail = g.lease(tid)
            assign_rotation(tail, [out_id, in_id])
            g.db.execute(
                "UPDATE game_state SET game_week = 2, game_hours_elapsed = 168 WHERE id = 1"
            )
            before = int(
                g.db.fetch_one("SELECT COUNT(*) AS n FROM flight_segments WHERE game_week = 2")["n"]
            )
            self.assertEqual(0, before)
            out = spawn_segments_for_calendar_week(2)
            self.assertGreater(int((out.get("spawn") or {}).get("inserted") or 0), 0)
            after = int(
                g.db.fetch_one("SELECT COUNT(*) AS n FROM flight_segments WHERE game_week = 2")["n"]
            )
            self.assertGreater(after, 0)

    def test_ensure_clock_running_restarts_dead_thread(self):
        with fresh_game(hub="TPA") as g:
            from engine.clock import get_global_clock, stop_game_clock
            from server.game_http import ensure_clock_running

            stop_game_clock()
            self.assertIsNone(get_global_clock())
            clk = ensure_clock_running()
            self.assertIsNotNone(clk)
            self.assertTrue(clk.is_alive())


# ---------------------------------------------------------------------------
# 8e. Phase 4 — slot seeding balance, exit policy, settlement AI retry.
# ---------------------------------------------------------------------------
class TestAiPhase4(unittest.TestCase):
    def test_airport_freq_map_sums_routes_at_hub(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import _airport_freq_map

            cid = "AI_TEST_FREQ"
            db.execute(
                """
                INSERT INTO competitors (
                    competitor_id, name, callsign, home_hub_iata, cash, strategy,
                    aggressiveness, risk_tolerance, expansion_rate, fleet_size,
                    max_fleet_size, weekly_route_budget, bid_probability,
                    weekly_slot_budget, reputation, brand_power
                ) VALUES (
                    ?, 'Test', 'TST', 'ATL', 50000000, 'HUBSPOKE',
                    1.0, 0.5, 2, 3, 10, 100000, 0.5, 50000, 50, 1.0
                )
                """,
                (cid,),
            )
            for pair, freq in (("ATL-JFK", 3), ("ATL-MIA", 2), ("ORD-ATL", 4)):
                o, d = pair.split("-")
                db.execute(
                    """
                    INSERT INTO competitor_routes (
                        competitor_id, route_pair_id, outbound_route_id, inbound_route_id,
                        status, frequency_per_week, fare_leisure, fare_business,
                        aircraft_type_id, opened_week
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, 120, 300, 'A320', 1)
                    """,
                    (cid, pair, pair, f"{d}-{o}", freq),
                )
            freq_map = _airport_freq_map(cid)
            self.assertEqual(9, int(freq_map.get("ATL", 0)))

    def test_slot_seed_scales_with_summed_hub_frequency(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import _sync_competitor_slots, ensure_competitors_seeded
            from engine.slots import is_slot_controlled, seed_slot_controlled_airports, slots_held

            seed_slot_controlled_airports()
            ensure_competitors_seeded()
            row = db.fetch_one(
                "SELECT competitor_id FROM competitors WHERE home_hub_iata = 'ICN'"
            )
            if not row:
                self.skipTest("no ICN hub carrier")
            cid = str(row["competitor_id"])
            if not is_slot_controlled("ICN"):
                self.skipTest("ICN not slot controlled")
            _sync_competitor_slots(cid, 1)
            held = slots_held("ICN", cid, 1)
            self.assertGreaterEqual(held, 8)

    def test_require_flown_pnl_blocks_lf_only_close(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.ai import _growth_profile, _update_loss_tracking_and_status, ensure_competitors_seeded

            ensure_competitors_seeded()
            db.execute("UPDATE game_state SET game_week = 20 WHERE id = 1")
            self.assertTrue(_growth_profile("AI_MERIDIAN")["require_flown_pnl_to_exit"])
            self.assertFalse(_growth_profile("AI_TITAN")["require_flown_pnl_to_exit"])

            def _prime_route(cid: str, pair: str) -> None:
                db.execute(
                    """
                    UPDATE competitor_routes
                    SET status = 'ACTIVE', opened_week = 1, estimated_weekly_profit = 5000,
                        consecutive_loss_weeks = 0, low_lf_weeks = 6, paper_loss_weeks = 0,
                        actual_weekly_net_avg = 500.0, actual_lf_avg = 0.20
                    WHERE competitor_id = ? AND route_pair_id = ?
                    """,
                    (cid, pair),
                )

            row = db.fetch_one(
                "SELECT route_pair_id FROM competitor_routes WHERE competitor_id = 'AI_MERIDIAN' LIMIT 1"
            )
            self.assertIsNotNone(row)
            pair = str(row["route_pair_id"])
            _prime_route("AI_MERIDIAN", pair)
            _update_loss_tracking_and_status("AI_MERIDIAN")
            st = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = 'AI_MERIDIAN' AND route_pair_id = ?",
                (pair,),
            )["status"]
            self.assertEqual("ACTIVE", str(st))

            row2 = db.fetch_one(
                "SELECT route_pair_id FROM competitor_routes WHERE competitor_id = 'AI_TITAN' LIMIT 1"
            )
            self.assertIsNotNone(row2)
            pair2 = str(row2["route_pair_id"])
            _prime_route("AI_TITAN", pair2)
            _update_loss_tracking_and_status("AI_TITAN")
            st2 = db.fetch_one(
                "SELECT status FROM competitor_routes WHERE competitor_id = 'AI_TITAN' AND route_pair_id = ?",
                (pair2,),
            )["status"]
            self.assertEqual("CLOSING", str(st2))

    def test_retry_ai_turn_only_marks_post_ops_when_clean(self):
        with fresh_game(hub="TPA") as g:
            from engine.settlement import _retry_ai_turn_only, _settlement_flags

            _settlement_flags(3)
            g.db.execute(
                """
                INSERT INTO week_ledger (
                    game_week, revenue_gross, excise_tax, segment_fees, security_fees,
                    pfc_fees, landing_fees, gate_fees, fuel_cost, lease_costs,
                    maintenance_costs, loan_payments, corporate_tax, net_income,
                    cash_end_of_week
                ) VALUES (3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                """
            )
            g.db.execute(
                "UPDATE settlement_flags SET cash_applied = 1, post_ops_done = 0 WHERE game_week = 3"
            )
            import unittest.mock as mock

            with mock.patch("engine.ai.ai_weekly_turn", return_value={"errors": 0}):
                out = _retry_ai_turn_only(3)
            self.assertEqual(0, int(out.get("ai_errors") or 0))
            flags = _settlement_flags(3)
            self.assertEqual(1, flags["post_ops_done"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# 9. Gate use-it-or-lose-it. A won stand must be usable before it can be judged.
# ---------------------------------------------------------------------------
class TestGateRetention(unittest.TestCase):
    def _jfk(self, db):
        return db.fetch_one(
            "SELECT gate_units, below_threshold_weeks FROM airport_gate_allocations"
            " WHERE airport_iata='JFK' AND holder_id='PLAYER'")

    def test_won_gate_is_not_revoked_before_it_can_be_used(self):
        """
        Regression: a unit won at week N is effective at N+1, and the old guard
        (effective_week > settled_week) judged it during N+1 — the first week it could
        possibly be scheduled — so a new station was always revoked unused.
        """
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.gates import (reset_weekly_gate_counters_and_enforce,
                                      resolve_closing_gate_auctions)
            if not db.fetch_one("SELECT 1 FROM airport_gate_bids b JOIN airport_gate_auctions a"
                                " ON a.auction_id=b.auction_id WHERE b.bidder_id='PLAYER'"
                                " AND a.airport_iata='JFK'"):
                self.skipTest("save has no PLAYER bid on JFK")
            resolve_closing_gate_auctions(1)
            row = self._jfk(db)
            self.assertIsNotNone(row, "PLAYER should have won a JFK unit")
            self.assertGreater(int(row["gate_units"]), 0)
            for wk in (1, 2):
                db.execute("UPDATE game_state SET game_week=?, game_hours_elapsed=? WHERE id=1",
                           (wk, (wk - 1) * 168.0))
                reset_weekly_gate_counters_and_enforce(wk)
                self.assertGreater(
                    int(self._jfk(db)["gate_units"]), 0,
                    f"gate was revoked at week {wk} before it could be used")

    def test_utilization_thresholds_are_on_the_right_scale(self):
        """
        util = movements * MTT / (units * 168) is a time-occupancy ratio, so a busy stand
        scores ~0.06, not ~0.65. A hub threshold above ~0.15 is unreachable and silently
        bleeds a stand every week.
        """
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.gates import (gate_utilization_threshold_hub,
                                      gate_utilization_threshold_nonhub)
            mtt = float(db.get_financial_constant("mtt_minutes") or 30) / 60.0
            for name, thr in (("hub", gate_utilization_threshold_hub()),
                              ("non-hub", gate_utilization_threshold_nonhub())):
                flights_per_day = (thr * 168.0 / mtt) / 2.0 / 7.0
                self.assertLess(
                    flights_per_day, 12.0,
                    f"{name} threshold {thr} demands {flights_per_day:.0f} flights/stand/day")

    def test_turnaround_uses_one_gate_not_two(self):
        """One aircraft arriving then departing should not double-count as 2 concurrent gates."""
        from engine.gates import _gate_intervals_at_airport, _mtt_hours, _peak_concurrency

        with fresh_game():
            mtt = _mtt_hours()
            arr = 100.0
            dep_tight = arr + (20.0 / 60.0)  # 20 min turn — shorter than default MTT
            peak_old_style = _peak_concurrency([(arr, arr + mtt), (dep_tight, dep_tight + mtt)])
            self.assertEqual(2, peak_old_style, "sanity: overlapping arr/dep windows double-count")
            peak = _peak_concurrency(
                _gate_intervals_at_airport(
                    "SFO",
                    1,
                    extra_segments=[
                        {
                            "origin_iata": "ATL",
                            "dest_iata": "SFO",
                            "dep_abs": arr - 4.0,
                            "arr_abs": arr,
                        },
                        {
                            "origin_iata": "SFO",
                            "dest_iata": "ATL",
                            "dep_abs": dep_tight,
                            "arr_abs": dep_tight + 4.0,
                        },
                    ],
                )
            )
            self.assertEqual(1, peak, "one tail turn at SFO should need only one stand")

    def test_scheduled_turn_minutes_drive_gate_occupancy(self):
        """Per-leg turnaround from scheduling is gate MTT, not the global 30 min default."""
        from engine.gates import _gate_intervals_at_airport

        with fresh_game():
            arr = 200.0
            turn_h = 180.0 / 60.0
            dep = arr + turn_h
            inbound = {
                "tail_number": "N900",
                "origin_iata": "ONT",
                "dest_iata": "ICN",
                "dep_abs": arr - 11.0,
                "arr_abs": arr,
            }
            outbound = {
                "tail_number": "N900",
                "origin_iata": "ICN",
                "dest_iata": "ONT",
                "dep_abs": dep,
                "arr_abs": dep + 11.0,
            }
            busy_default = sum(
                e - s
                for s, e in _gate_intervals_at_airport("ICN", 2, extra_segments=[inbound, outbound])
            )
            busy_long = sum(
                e - s
                for s, e in _gate_intervals_at_airport(
                    "ICN",
                    2,
                    extra_segments=[inbound, {**outbound, "turn_minutes": 180}],
                )
            )
            self.assertGreater(busy_long, busy_default + 1.0)
            # [arr, dep + turn): dep = arr + turn, so occupancy = 2 × turn
            self.assertAlmostEqual(busy_long, turn_h * 2.0, places=2)

    def test_airport_board_coalesces_route_endpoints(self):
        with TempSave() as db:
            db.ensure_schema_migrations()
            from ui.airport_board import get_airport_board_rows

            rt = db.fetch_one(
                "SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1"
            )
            if not rt:
                self.skipTest("need a route in save")
            db.execute(
                """
                INSERT INTO flight_segments (
                    segment_id, game_week, day_of_week, tail_number, route_id,
                    flight_number, scheduled_dep_time, scheduled_dep_game_hour,
                    scheduled_arr_time, scheduled_arr_game_hour,
                    baseline_dep_game_hour, baseline_arr_game_hour,
                    status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee,
                    landing_fee, gate_fee
                ) VALUES (
                    'seg-null-od', 1, 'MON', 'N777', ?, 'TST001',
                    '08:00', 8.0, '16:00', 16.0, 8.0, 16.0, 'SCHEDULED',
                    0, 0, 0, 0, 0, 0, 0, 0, 0
                )
                """,
                (str(rt["route_id"]),),
            )
            deps = [
                r
                for r in get_airport_board_rows(str(rt["origin_iata"]), 1)
                if r.get("direction") == "DEP"
            ]
            self.assertTrue(any(r.get("origin_iata") == rt["origin_iata"] for r in deps))

    def test_list_open_resolves_overdue_instead_of_cancelling_bids(self):
        """
        Regression: after the clock rolls to week N+1, opening Gate auctions used to
        CANCEL still-OPEN week-N auctions (and wipe PLAYER bids) before settlement
        could award the stands.
        """
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.gates import (
                ensure_weekly_airport_auctions,
                list_open_gate_auctions,
                submit_gate_bid,
            )
            db.execute(
                "UPDATE game_state SET game_week=1, game_hours_elapsed=0 WHERE id=1"
            )
            ensure_weekly_airport_auctions(1)
            a = db.fetch_one(
                "SELECT auction_id, units_available FROM airport_gate_auctions "
                "WHERE airport_iata='ATL' AND status='OPEN' AND opens_week=1"
            )
            if not a:
                self.skipTest("no ATL auction in week 1")
            if int(a["units_available"] or 0) < 1:
                self.skipTest("ATL has no units available in week 1")
            aid = str(a["auction_id"])
            submit_gate_bid(aid, 2, 20000.0, bidder_id="PLAYER")
            # Simulate week rollover before settlement resolves the auction.
            db.execute(
                "UPDATE game_state SET game_week=2, game_hours_elapsed=? WHERE id=1",
                (168.0,),
            )
            list_open_gate_auctions()
            st = db.fetch_one(
                "SELECT status FROM airport_gate_auctions WHERE auction_id=?", (aid,)
            )
            self.assertEqual(
                str(st["status"]),
                "RESOLVED",
                "overdue auction must be resolved, not silently cancelled",
            )
            alloc = db.fetch_one(
                "SELECT gate_units FROM airport_gate_allocations "
                "WHERE airport_iata='ATL' AND holder_id='PLAYER' AND status='ACTIVE'"
            )
            self.assertIsNotNone(alloc)
            self.assertGreaterEqual(int(alloc["gate_units"] or 0), 2)


class TestConcurrentGates(unittest.TestCase):
    """Player + AI peak-concurrency at auctioned airports (HNL, LAX, SFO, etc.)."""

    def setUp(self):
        self._world = fresh_game()
        self._world.__enter__()

    def tearDown(self):
        self._world.__exit__(None, None, None)

    def _mtt(self):
        from engine.gates import _mtt_hours

        return _mtt_hours()

    def test_two_tails_overlapping_need_two_gates(self):
        from engine.gates import _gate_intervals_at_airport, peak_concurrency

        mtt = self._mtt()
        base = 50.0
        peak = peak_concurrency(
            _gate_intervals_at_airport(
                "LAX",
                1,
                extra_segments=[
                    {
                        "tail_number": "N101",
                        "origin_iata": "LAX",
                        "dest_iata": "SFO",
                        "dep_abs": base,
                        "arr_abs": base + 1.5,
                    },
                    {
                        "tail_number": "N102",
                        "origin_iata": "LAX",
                        "dest_iata": "HNL",
                        "dep_abs": base + (mtt / 3.0),
                        "arr_abs": base + 6.0,
                    },
                ],
            )
        )
        self.assertEqual(2, peak)

    def test_two_tails_spaced_need_one_gate(self):
        from engine.gates import _gate_intervals_at_airport, peak_concurrency

        mtt = self._mtt()
        base = 80.0
        gap = mtt + 2.0
        peak = peak_concurrency(
            _gate_intervals_at_airport(
                "HNL",
                1,
                extra_segments=[
                    {
                        "tail_number": "N201",
                        "origin_iata": "HNL",
                        "dest_iata": "LAX",
                        "dep_abs": base,
                        "arr_abs": base + 5.0,
                    },
                    {
                        "tail_number": "N202",
                        "origin_iata": "HNL",
                        "dest_iata": "SFO",
                        "dep_abs": base + gap + 5.0,
                        "arr_abs": base + gap + 10.0,
                    },
                ],
            )
        )
        self.assertEqual(1, peak)

    def test_depart_only_does_not_double_count(self):
        from engine.gates import _gate_intervals_at_airport, peak_concurrency

        base = 20.0
        mtt = self._mtt()
        peak = peak_concurrency(
            _gate_intervals_at_airport(
                "SFO",
                1,
                extra_segments=[
                    {
                        "tail_number": "N301",
                        "origin_iata": "SFO",
                        "dest_iata": "LAX",
                        "dep_abs": base,
                        "arr_abs": base + 1.0,
                    }
                ],
            )
        )
        self.assertEqual(1, peak)
        intervals = _gate_intervals_at_airport(
            "SFO",
            1,
            extra_segments=[
                {
                    "tail_number": "N301",
                    "origin_iata": "SFO",
                    "dest_iata": "LAX",
                    "dep_abs": base,
                    "arr_abs": base + 1.0,
                }
            ],
        )
        self.assertEqual([(base, base + mtt)], intervals)

    def test_exclude_tail_avoids_double_count_on_reschedule(self):
        import uuid

        from engine.gates import _upsert_allocation, player_gate_peak_at_airport

        tail = "N999"
        dep = 40.0
        arr = 44.0
        with TempSave() as db:
            db.ensure_schema_migrations()
            rt = db.fetch_one("SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1")
            if not rt:
                self.skipTest("need a route in save")
            route_id = str(rt["route_id"])
            oi = str(rt["origin_iata"]).upper()
            di = str(rt["dest_iata"]).upper()
            _upsert_allocation(oi, "PLAYER", 2, effective_week=1)
            db.execute(
                """
                INSERT INTO flight_segments (
                    segment_id, game_week, day_of_week, tail_number, route_id,
                    origin_iata, dest_iata, flight_number,
                    scheduled_dep_time, scheduled_dep_game_hour,
                    scheduled_arr_time, scheduled_arr_game_hour,
                    baseline_dep_game_hour, baseline_arr_game_hour,
                    status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee
                ) VALUES (?, 1, 'MON', ?, ?, ?, ?, 'TST001',
                          '08:00', ?, '09:30', ?, ?, ?, 'SCHEDULED',
                          0, 0, 0, 0, 0, 0, 0, 0, 0)
                """,
                (str(uuid.uuid4()), tail, route_id, oi, di, dep, arr, dep, arr),
            )
            replanned = [
                {
                    "tail_number": tail,
                    "origin_iata": oi,
                    "dest_iata": di,
                    "dep_abs": dep + 0.1,
                    "arr_abs": arr + 0.1,
                }
            ]
            without_exclude = player_gate_peak_at_airport(oi, 1, extra_segments=replanned)
            with_exclude = player_gate_peak_at_airport(
                oi, 1, extra_segments=replanned, exclude_tails={tail}
            )
            self.assertEqual(2, without_exclude, "old + new overlap should double-count without exclude")
            self.assertEqual(1, with_exclude)

    def test_spawn_gate_check_excludes_same_tail_from_db(self):
        """Weekly re-spawn must not double-count existing segments for the same tail."""
        import uuid

        from engine.gates import _upsert_allocation, assert_player_gate_capacity_for_new_segments

        tail = "N888"
        turn = 150
        arr1 = 360.0
        dep1 = arr1 + (turn / 60.0)
        arr2 = 453.5
        dep2 = arr2 + (turn / 60.0)
        with TempSave() as db:
            db.ensure_schema_migrations()
            _upsert_allocation("IST", "PLAYER", 1, effective_week=1)
            for rid, oi, di, dep, arr in (
                ("MNL-IST", "MNL", "IST", arr1 - 10.0, arr1),
                ("IST-MNL", "IST", "MNL", dep1, dep1 + 10.0),
                ("MNL-IST", "MNL", "IST", arr2 - 10.0, arr2),
                ("IST-MNL", "IST", "MNL", dep2, dep2 + 10.0),
            ):
                if not db.fetch_one("SELECT 1 FROM routes WHERE route_id=?", (rid,)):
                    self.skipTest(f"need route {rid}")
                db.execute(
                    """
                    INSERT INTO flight_segments (
                        segment_id, game_week, day_of_week, tail_number, route_id,
                        origin_iata, dest_iata, flight_number,
                        scheduled_dep_time, scheduled_dep_game_hour,
                        scheduled_arr_time, scheduled_arr_game_hour,
                        baseline_dep_game_hour, baseline_arr_game_hour,
                        turn_minutes, status, pax_business, pax_leisure, revenue_gross,
                        excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee
                    ) VALUES (?, 3, 'MON', ?, ?, ?, ?, 'TST01', '08:00', ?, '18:00', ?, ?, ?, ?, 'SCHEDULED',
                              0, 0, 0, 0, 0, 0, 0, 0, 0)
                    """,
                    (str(uuid.uuid4()), tail, rid, oi, di, dep, arr, dep, arr, turn),
                )
            planned = [
                {
                    "tail_number": tail,
                    "origin_iata": oi,
                    "dest_iata": di,
                    "dep_abs": dep,
                    "arr_abs": arr,
                    "turn_minutes": turn,
                }
                for _rid, oi, di, dep, arr in (
                    ("MNL-IST", "MNL", "IST", arr1 - 10.0, arr1),
                    ("IST-MNL", "IST", "MNL", dep1, dep1 + 10.0),
                    ("MNL-IST", "MNL", "IST", arr2 - 10.0, arr2),
                    ("IST-MNL", "IST", "MNL", dep2, dep2 + 10.0),
                )
            ]
            assert_player_gate_capacity_for_new_segments(3, planned)

    def test_inbound_turn_minutes_extend_dest_gate_hold(self):
        """Per-leg turnaround applies after landing at the destination airport."""
        from engine.gates import _gate_intervals_at_airport

        with fresh_game():
            arr = 200.0
            turn_h = 150.0 / 60.0
            inbound = {
                "tail_number": "N900",
                "origin_iata": "MNL",
                "dest_iata": "IST",
                "dep_abs": arr - 10.0,
                "arr_abs": arr,
                "turn_minutes": 150,
            }
            busy = sum(
                e - s
                for s, e in _gate_intervals_at_airport("IST", 2, extra_segments=[inbound])
            )
            self.assertAlmostEqual(busy, turn_h, places=2)

    def test_ai_turnaround_at_dest_uses_one_gate(self):
        from engine.gates import gate_intervals_for_ai_at_airport, peak_concurrency

        mtt = self._mtt()
        cid = "COMP_TEST"
        dep_out = 60.0
        arr_out = dep_out + 5.0
        dep_in = arr_out + (20.0 / 60.0)
        arr_in = dep_in + 5.0
        peak_old = peak_concurrency([(arr_out, arr_out + mtt), (dep_in, dep_in + mtt)])
        self.assertEqual(2, peak_old, "sanity: old model double-counts turnaround")
        peak = peak_concurrency(
            gate_intervals_for_ai_at_airport(
                cid,
                "HNL",
                1,
                extra_cycles=[
                    {
                        "tail": f"{cid}:001",
                        "origin_iata": "LAX",
                        "dest_iata": "HNL",
                        "dep_out": dep_out,
                        "arr_out": arr_out,
                        "dep_in": dep_in,
                        "arr_in": arr_in,
                    }
                ],
            )
        )
        self.assertEqual(1, peak)

    def test_ai_cycle_fits_with_tight_turn(self):
        from engine.ai_gates import cycle_fits_ai_gates
        from engine.gates import _upsert_allocation

        with TempSave() as db:
            db.ensure_schema_migrations()
            cid = "COMP_A"
            _upsert_allocation("LAX", cid, 1, effective_week=1)
            _upsert_allocation("HNL", cid, 1, effective_week=1)
            mtt = self._mtt()
            dep_out = 10.0
            arr_out = dep_out + 5.0
            dep_in = arr_out + (20.0 / 60.0)
            arr_in = dep_in + 5.0
            self.assertTrue(
                cycle_fits_ai_gates(
                    cid, "LAX", "HNL", dep_out, arr_out, dep_in, arr_in, 1, mtt
                )
            )

    def test_ai_existing_segments_merge_round_trip(self):
        import uuid

        from engine.gates import gate_intervals_for_ai_at_airport, peak_concurrency

        with TempSave() as db:
            db.ensure_schema_migrations()
            cid = "COMP_B"
            dep_out, arr_out = 30.0, 35.0
            dep_in, arr_in = 35.5, 40.5
            db.execute(
                """
                INSERT INTO ai_flight_segments (
                    segment_id, competitor_id, route_id, game_week, flight_number,
                    origin_iata, dest_iata,
                    scheduled_dep_game_hour, scheduled_arr_game_hour,
                    frequency, status
                ) VALUES (?, ?, 'LAX-HNL', 1, 'AI001', 'LAX', 'HNL', ?, ?, 1, 'SCHEDULED')
                """,
                (str(uuid.uuid4()), cid, dep_out, arr_out),
            )
            db.execute(
                """
                INSERT INTO ai_flight_segments (
                    segment_id, competitor_id, route_id, game_week, flight_number,
                    origin_iata, dest_iata,
                    scheduled_dep_game_hour, scheduled_arr_game_hour,
                    frequency, status
                ) VALUES (?, ?, 'HNL-LAX', 1, 'AI001R', 'HNL', 'LAX', ?, ?, 1, 'SCHEDULED')
                """,
                (str(uuid.uuid4()), cid, dep_in, arr_in),
            )
            peak_lax = peak_concurrency(gate_intervals_for_ai_at_airport(cid, "LAX", 1))
            peak_hnl = peak_concurrency(gate_intervals_for_ai_at_airport(cid, "HNL", 1))
            self.assertEqual(1, peak_lax)
            self.assertEqual(1, peak_hnl)

    def test_auctioned_airports_player_peak_within_capacity(self):
        """Sanity on live save: player peak must not exceed allocated gates."""
        from engine.gates import (
            _allocated_gates,
            assert_player_gate_capacity_for_week,
            is_auctioned_airport,
            player_gate_peak_at_airport,
        )

        if not LIVE_DB.exists():
            self.skipTest("no live save")
        with TempSave() as db:
            db.ensure_schema_migrations()
            gw_row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
            gw = int(gw_row["game_week"] or 1) if gw_row else 1
            for ap in ("HNL", "LAX", "SFO", "ATL", "JFK"):
                if not is_auctioned_airport(ap):
                    continue
                cap = _allocated_gates(ap, "PLAYER")
                if cap <= 0:
                    continue
                peak = player_gate_peak_at_airport(ap, gw)
                self.assertLessEqual(
                    peak,
                    cap,
                    f"{ap} week {gw}: peak {peak} > cap {cap}",
                )
            assert_player_gate_capacity_for_week(gw)


class TestRouteSuggestions(unittest.TestCase):
    def test_suggestions_are_bidirectional_and_sorted(self):
        from engine.route_suggestions import popular_destinations_from_origin

        with fresh_game(hub="ATL"):
            rows = popular_destinations_from_origin("ATL", limit=10)
            self.assertGreater(len(rows), 0)
            for r in rows:
                self.assertIn("outbound", r)
                self.assertIn("inbound", r)
                self.assertEqual(r["outbound"]["from"], "ATL")
                self.assertEqual(r["inbound"]["to"], "ATL")
                self.assertGreaterEqual(int(r["outbound"]["weekly_demand"]), 0)
                self.assertGreaterEqual(int(r["inbound"]["weekly_demand"]), 0)
                self.assertGreater(float(r["distance_nm"]), 0)
            ranks = [int(r["rank_demand"]) for r in rows]
            self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_suggestions_api_requires_origin(self):
        from server import game_api as api

        with fresh_game(hub="ATL"):
            bad = api.route_suggestions("")
            self.assertFalse(bad.get("ok"))
            good = api.route_suggestions("ATL", limit=3)
            self.assertTrue(good.get("ok"))
            self.assertEqual(good.get("origin_iata"), "ATL")
            self.assertGreater(len(good.get("suggestions") or []), 0)


class TestSpawnIdempotency(unittest.TestCase):
    def test_quick_spawn_skips_duplicate_tail_route_dep(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation, spawn_rotation_segments_for_week
            from engine.scheduling.segments import dedupe_duplicate_spawn_segments

            tid, other, out_id, in_id = TestFlightNumbers._rt_setup(self, g)
            tail = g.lease(tid)
            assign_rotation(tail, [out_id, in_id])
            out1 = spawn_rotation_segments_for_week(2)
            out2 = spawn_rotation_segments_for_week(2)
            self.assertGreater(int(out1.get("inserted") or 0), 0)
            self.assertEqual(0, int(out2.get("inserted") or 0))
            n = g.db.fetch_one(
                "SELECT COUNT(*) AS n FROM flight_segments WHERE tail_number=? AND game_week=2",
                (tail,),
            )["n"]
            self.assertEqual(int(out1.get("inserted") or 0), int(n))

    def test_dedupe_removes_duplicate_spawn_rows(self):
        import uuid

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.scheduling.segments import dedupe_duplicate_spawn_segments

            rt = db.fetch_one("SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1")
            if not rt:
                self.skipTest("need a route")
            route_id = str(rt["route_id"])
            oi = str(rt["origin_iata"])
            di = str(rt["dest_iata"])
            tail = "N777"
            dep = 50.0
            arr = 54.0
            for i in range(3):
                db.execute(
                    """
                    INSERT INTO flight_segments (
                        segment_id, game_week, day_of_week, tail_number, route_id,
                        origin_iata, dest_iata, flight_number,
                        scheduled_dep_time, scheduled_dep_game_hour,
                        scheduled_arr_time, scheduled_arr_game_hour,
                        baseline_dep_game_hour, baseline_arr_game_hour,
                        status, pax_business, pax_leisure, revenue_gross,
                        excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee
                    ) VALUES (?, 2, 'MON', ?, ?, ?, ?, ?, '08:00', ?, '09:00', ?, ?, ?, 'SCHEDULED',
                              0, 0, 0, 0, 0, 0, 0, 0, 0)
                    """,
                    (str(uuid.uuid4()), tail, route_id, oi, di, f"FN{i}", dep, arr, dep, arr),
                )
            removed = dedupe_duplicate_spawn_segments(2)
            self.assertEqual(2, removed)
            left = db.fetch_one(
                "SELECT COUNT(*) AS n FROM flight_segments WHERE tail_number=? AND game_week=2",
                (tail,),
            )["n"]
            self.assertEqual(1, int(left))

    def test_duplicate_db_rows_do_not_inflate_gate_peak(self):
        import uuid

        from engine.gates import _gate_intervals_at_airport, peak_concurrency, _upsert_allocation

        with TempSave() as db:
            db.ensure_schema_migrations()
            rt = db.fetch_one("SELECT route_id, origin_iata, dest_iata FROM routes LIMIT 1")
            if not rt:
                self.skipTest("need a route")
            route_id = str(rt["route_id"])
            oi = str(rt["origin_iata"]).upper()
            di = str(rt["dest_iata"]).upper()
            _upsert_allocation(oi, "PLAYER", 2, effective_week=1)
            tail = "N555"
            dep = 60.0
            arr = 64.0
            for _ in range(4):
                db.execute(
                    """
                    INSERT INTO flight_segments (
                        segment_id, game_week, day_of_week, tail_number, route_id,
                        origin_iata, dest_iata, flight_number,
                        scheduled_dep_time, scheduled_dep_game_hour,
                        scheduled_arr_time, scheduled_arr_game_hour,
                        baseline_dep_game_hour, baseline_arr_game_hour,
                        status, pax_business, pax_leisure, revenue_gross,
                        excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee
                    ) VALUES (?, 1, 'MON', ?, ?, ?, ?, 'TST', '08:00', ?, '09:00', ?, ?, ?, 'SCHEDULED',
                              0, 0, 0, 0, 0, 0, 0, 0, 0)
                    """,
                    (str(uuid.uuid4()), tail, route_id, oi, di, dep, arr, dep, arr),
                )
            peak = peak_concurrency(_gate_intervals_at_airport(oi, 1))
            self.assertEqual(1, peak)


# ---------------------------------------------------------------------------
# Flight numbers: sticky-by-route, random (not 001), overlap rules, spawn persist
# ---------------------------------------------------------------------------
class TestFlightNumbers(unittest.TestCase):
    def _rt_setup(self, g):
        tid = pick_type(g.db, "NARROW", min_range=200, max_seats=220)
        if not tid:
            self.skipTest("no usable narrowbody")
        dests = near_airports(g.db, "TPA", 150, 800, limit=4)
        if not dests:
            self.skipTest("no nearby spoke")
        other = dests[0]
        out_id, in_id = g.open_round_trip(other)
        return tid, other, out_id, in_id

    def test_auto_assign_is_not_sequential_001(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation

            tid, _other, out_id, in_id = self._rt_setup(g)
            t1 = g.lease(tid)
            assign_rotation(t1, [out_id, in_id])
            rows = g.db.fetch_all(
                "SELECT flight_number FROM flight_segments WHERE tail_number = ? ORDER BY scheduled_dep_game_hour",
                (t1,),
            )
            fns = [str(r["flight_number"]) for r in rows]
            self.assertEqual(2, len(fns))
            self.assertNotEqual(fns[0], fns[1])  # round-trip pair differs
            for fn in fns:
                self.assertFalse(fn.endswith("001"), f"got sequential-style {fn}")
                self.assertFalse(fn.endswith("002"), f"got sequential-style {fn}")
                # 4-digit product numbers
                self.assertRegex(fn, r"^[A-Z]{2,3}\d{4}$")

    def test_sticky_reuses_number_for_same_route(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation, cancel_rotation
            from engine.scheduling.flight_numbers import get_sticky_flight_number

            tid, _other, out_id, in_id = self._rt_setup(g)
            t1 = g.lease(tid)
            assign_rotation(t1, [out_id, in_id])
            first = g.db.fetch_one(
                "SELECT flight_number FROM flight_segments WHERE route_id = ? LIMIT 1",
                (out_id,),
            )
            sticky = get_sticky_flight_number(out_id)
            self.assertEqual(str(first["flight_number"]), sticky)
            cancel_rotation(t1, wipe_completed_this_week=True)
            t2 = g.lease(tid)
            assign_rotation(t2, [out_id, in_id])
            second = g.db.fetch_one(
                "SELECT flight_number FROM flight_segments WHERE route_id = ? AND tail_number = ? LIMIT 1",
                (out_id, t2),
            )
            self.assertEqual(sticky, str(second["flight_number"]))

    def test_overlap_same_number_is_rejected(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation
            from engine.scheduling.flight_numbers import assert_flight_number_ok

            tid, _other, out_id, in_id = self._rt_setup(g)
            t1 = g.lease(tid)
            assign_rotation(t1, [out_id, in_id], flight_numbers=["TST1992", "TST1993"])
            seg = g.db.fetch_one(
                "SELECT scheduled_dep_game_hour d, scheduled_arr_game_hour a FROM flight_segments "
                "WHERE flight_number = 'TST1992' LIMIT 1"
            )
            with self.assertRaises(ValueError):
                assert_flight_number_ok(
                    "TST1992",
                    [(float(seg["d"]), float(seg["a"]))],
                    1,
                )
            # Non-overlapping window with same number is OK
            assert_flight_number_ok(
                "TST1992",
                [(float(seg["a"]) + 2.0, float(seg["a"]) + 4.0)],
                1,
            )

    def test_manual_override_persists_in_quick_template(self):
        with fresh_game(hub="TPA") as g:
            import json
            from engine.scheduling import assign_rotation

            tid, _other, out_id, in_id = self._rt_setup(g)
            tail = g.lease(tid)
            assign_rotation(tail, [out_id, in_id], flight_numbers=["ABC4242", "ABC4243"])
            row = g.db.fetch_one(
                "SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail,)
            )
            blob = json.loads(row["legs_json"])
            legs = blob if isinstance(blob, list) else blob.get("legs") or []
            self.assertEqual("ABC4242", legs[0].get("flight_number"))
            self.assertEqual("ABC4243", legs[1].get("flight_number"))

    def test_week_spawn_keeps_template_flight_numbers(self):
        with fresh_game(hub="TPA") as g:
            from engine.scheduling import assign_rotation, spawn_rotation_segments_for_week

            tid, _other, out_id, in_id = self._rt_setup(g)
            tail = g.lease(tid)
            assign_rotation(tail, [out_id, in_id], flight_numbers=["ZZZ7777", "ZZZ7778"])
            g.db.execute("DELETE FROM flight_segments WHERE game_week = 1")
            g.set_hour(168 + 10)  # week 2
            out = spawn_rotation_segments_for_week(2)
            self.assertGreater(int(out.get("inserted") or 0), 0)
            fns = {
                str(r["flight_number"])
                for r in g.db.fetch_all(
                    "SELECT flight_number FROM flight_segments WHERE game_week = 2 AND tail_number = ?",
                    (tail,),
                )
            }
            self.assertEqual({"ZZZ7777", "ZZZ7778"}, fns)
