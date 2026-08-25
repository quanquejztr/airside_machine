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