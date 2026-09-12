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
import json
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

    def test_holdings_carry_forward_without_settlement(self):
        """A holding read before settlement runs must not read as zero."""
        with TempSave() as db:
            import engine.slots as S
            self._alloc(db, 2, "IAD", 30, 14, below=1)
            db.execute("DELETE FROM slot_allocations WHERE airport_iata='IAD' AND game_week=3")
            self.assertEqual(30, S.slots_held("IAD", "PLAYER", 3),
                             "week 3 must inherit week 2's entitlement")
            self.assertEqual(30, self._held(db, 3, "IAD")[0],
                             "the inherited row should be materialised")

    def test_grandfather_does_not_rebaseline_an_existing_holder(self):
        """Regression: partial spawn + historic grant capped a 30-unit holder at 4."""
        with TempSave() as db:
            import uuid
            import engine.slots as S
            S.seed_slot_controlled_airports()
            self._alloc(db, 2, "IAD", 30, 14, below=1)
            db.execute("DELETE FROM slot_allocations WHERE airport_iata='IAD' AND game_week=3")
            # Four week-3 movements already on the board when grandfathering runs.
            for i in range(4):
                db.execute("""INSERT INTO slot_usages
                              (usage_id, airport_iata, holder_id, game_week,
                               segment_id, movement_type, clock_hour)
                              VALUES (?,?,?,?,?,?,?)""",
                           (str(uuid.uuid4()), "IAD", "PLAYER", 3, f"seg-{i}", "DEP", i))
            S.grandfather_historic_slot_holdings(3)
            self.assertEqual(30, S.slots_held("IAD", "PLAYER", 3),
                             "an established holder must not be re-baselined to movements flown")

    def test_carry_forward_wins_over_a_pre_created_row(self):
        """ensure_slot_allocations_for_week must upsert, not silently ignore."""
        with TempSave() as db:
            import engine.slots as S
            self._alloc(db, 2, "IAD", 30, 14, below=1)
            self._alloc(db, 3, "IAD", 4, 4, below=0)   # grandfathered stub
            S.ensure_slot_allocations_for_week(3)
            self.assertEqual(30, self._held(db, 3, "IAD")[0],
                             "carried entitlement must overwrite a smaller stub row")


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

    def test_status_week_uses_committed_not_interpolated(self):
        """HUD week/day must not race ahead of catch-up'd hours near a week boundary."""
        import time

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            clk = GameClock()
            clk.running = False
            # ~1.1 game hours shy of week 9 (1344).
            clk.game_hours_elapsed = 1342.9
            clk._snapshot_ghe = 1342.9
            clk.speed_multiplier = 60
            # Pretend 1 real second has passed → interpolates +2.0h → past 1344.
            clk._snapshot_wall_time = time.time() - 1.0
            live = clk.get_interpolated_game_hours()
            self.assertGreaterEqual(live, 1344.0)
            st = clk.get_status()
            self.assertEqual(8, st["current_week"])
            self.assertEqual(7, st["current_day"])
            self.assertAlmostEqual(1342.9, float(st["committed_game_hour"]), places=2)
            self.assertAlmostEqual(1342.9, float(st["game_hours_elapsed"]), places=2)
            self.assertGreaterEqual(float(st["current_game_hour"]), 1344.0)

    def test_stop_flushes_catch_up_to_db(self):
        """stop() must persist catch-up'd hours so restart does not snap backward."""
        import time

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            clk = GameClock()
            clk.running = False
            clk.game_hours_elapsed = 1342.0
            clk._snapshot_ghe = 1342.0
            clk.speed_multiplier = 60
            clk._snapshot_wall_time = time.time() - 1.5  # +3.0h → 1345 (week 9)
            clk.stop()
            row = db.fetch_one(
                "SELECT game_hours_elapsed, game_week FROM game_state WHERE id = 1"
            )
            self.assertGreaterEqual(float(row["game_hours_elapsed"]), 1344.0)
            self.assertEqual(9, int(row["game_week"]))

    def test_milestones_see_persisted_hours(self):
        """Persist-before-on_week: crossing the boundary writes week 9 before the hook."""
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            seen = []

            def on_week(w):
                row = db.fetch_one(
                    "SELECT game_hours_elapsed, game_week FROM game_state WHERE id = 1"
                )
                seen.append(
                    (
                        int(w),
                        float(row["game_hours_elapsed"]),
                        int(row["game_week"]),
                    )
                )

            clk = GameClock(on_week=on_week)
            clk.running = False
            clk.game_hours_elapsed = 1343.5
            clk._snapshot_ghe = 1343.5
            clk.last_week = 8
            clk.speed_multiplier = 0
            # Simulate the post-tick catch-up + persist + milestones path.
            clk.game_hours_elapsed = 1344.1
            clk._snapshot_ghe = 1344.1
            clk._persist_game_state_unlocked()
            with clk.lock:
                clk._check_time_milestones_locked()
            self.assertEqual(1, len(seen))
            self.assertEqual(9, seen[0][0])
            self.assertGreaterEqual(seen[0][1], 1344.0)
            self.assertEqual(9, seen[0][2])

    def test_callback_reentering_clock_does_not_deadlock(self):
        """Regression: on_departure -> trigger_aog -> request_auto_pause wedged the clock.

        request_auto_pause() takes GameClock.lock, and the old run loop held that
        same non-reentrant lock across the callback, so the clock thread blocked on
        itself forever and every HTTP reader queued behind it.
        """
        import threading

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            done = threading.Event()
            ran_on = {}

            def on_week(w):
                ran_on["thread"] = threading.current_thread().name
                clk.request_auto_pause(f"Aircraft N1 is AOG (week {w})")
                done.set()

            clk = GameClock(on_week=on_week)
            clk.running = False
            clk.speed_multiplier = 4
            clk._start_worker()
            clk._enqueue("week", 9)
            try:
                self.assertTrue(done.wait(5), "callback deadlocked re-entering the clock")
                self.assertEqual("game-clock-callbacks", ran_on["thread"])
                self.assertEqual(0, clk.speed_multiplier)
                self.assertIn("AOG", clk.last_auto_pause_reason or "")
            finally:
                clk.stop()

    def test_slow_callback_never_blocks_status_or_speed(self):
        """A long week-roll must not stall /api/clock, /api/state or the map poll."""
        import threading
        import time

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            entered = threading.Event()

            def on_week(w):
                entered.set()
                time.sleep(3.0)

            clk = GameClock(on_week=on_week)
            clk.running = False
            clk._start_worker()
            clk._enqueue("week", 9)
            try:
                self.assertTrue(entered.wait(2), "callback never ran")
                t0 = time.time()
                status = clk.get_status()
                ok, _ = clk.set_speed(4, player_initiated=True)
                elapsed = time.time() - t0
                self.assertTrue(ok)
                self.assertIsNotNone(status["time_display"])
                self.assertLess(
                    elapsed, 0.5, "clock lock was held across the callback"
                )
            finally:
                clk.stop()

    def test_interpolation_is_bounded_while_thread_is_wedged(self):
        """A stale snapshot must hold time still, not teleport the HUD hours ahead."""
        import time

        from engine.clock import MAX_EXTRAPOLATION_REAL_SECONDS

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            clk = GameClock()
            clk.running = False
            clk.is_alive = lambda: True  # a live thread that stopped catching up
            clk.game_hours_elapsed = 1470.0
            clk._snapshot_ghe = 1470.0
            clk.speed_multiplier = 60
            # A wedged loop leaves both stamps stale; _catch_up_locked sets them together.
            clk._snapshot_wall_time = time.time() - 18.5  # the observed ~37h skew
            clk._last_tick = clk._snapshot_wall_time

            live = clk.get_interpolated_game_hours()
            uncapped = 1470.0 + (18.5 / 30.0) * 60.0
            self.assertAlmostEqual(1507.0, uncapped, places=0)
            ceiling = 1470.0 + (MAX_EXTRAPOLATION_REAL_SECONDS / 30.0) * 60.0
            self.assertLessEqual(live, ceiling + 1e-6)
            self.assertTrue(clk.is_stalled())

    def test_flight_map_payload_sends_committed_hour(self):
        """The map payload must not alias game_hours_elapsed to the interpolated hour."""
        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import stop_game_clock
            from engine.flight_map_data import get_flight_map_payload

            stop_game_clock()
            db.execute(
                "UPDATE game_state SET game_hours_elapsed = 1470, speed_multiplier = 60 WHERE id = 1"
            )
            payload = get_flight_map_payload()
            self.assertIn("committed_game_hour", payload)
            self.assertAlmostEqual(
                float(payload["committed_game_hour"]),
                float(payload["game_hours_elapsed"]),
                places=6,
            )
            self.assertAlmostEqual(1470.0, float(payload["committed_game_hour"]), places=2)

    def test_worker_preserves_week_before_flight_event_order(self):
        """Week spawn must land before the flight events for that week are dispatched."""
        import threading

        with TempSave() as db:
            db.ensure_schema_migrations()
            from engine.clock import GameClock

            order = []
            both = threading.Event()

            def on_week(w):
                order.append("week")

            def on_departure(seg):
                order.append("departure")

            clk = GameClock(on_week=on_week, on_departure=on_departure)
            clk.running = False
            clk._dispatch_flight_events = lambda t0, t1: (
                order.append("flights"),
                both.set(),
            )
            clk._start_worker()
            clk._enqueue("week", 9)
            clk._enqueue("flights", (1343.0, 1345.0))
            try:
                self.assertTrue(both.wait(5))
                self.assertEqual(["week", "flights"], order)
            finally:
                clk.stop()


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

    def test_clearing_a_plan_leaves_the_tail_where_it_is(self):
        """Clearing must not reposition: a rotation may deliberately start away from base."""
        with TempSave() as db:
            db.ensure_schema_migrations()
            from server import game_api as api
            hub_row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
            tail_row = db.fetch_one("SELECT tail_number FROM fleet LIMIT 1")
            if not hub_row or not tail_row:
                self.skipTest("need an airline and a tail in the save")
            hub = str(hub_row["home_hub_iata"]).upper()
            tail = tail_row["tail_number"]
            away = "SFO" if hub != "SFO" else "JFK"
            db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
            db.execute("UPDATE fleet SET current_airport_iata=?, status='IDLE' WHERE tail_number=?",
                       (away, tail))

            out = api.assign_schedule({"tail_number": tail, "mode": "clear"})
            self.assertTrue(out.get("ok"), out.get("error"))
            self.assertIsNone(
                db.fetch_one("SELECT 1 FROM flight_segments WHERE tail_number=? AND is_ferry=1", (tail,)),
                "clearing must not create a ferry")
            loc = db.fetch_one("SELECT current_airport_iata FROM fleet WHERE tail_number=?", (tail,))
            self.assertEqual(away, str(loc["current_airport_iata"]).upper(),
                             "the aircraft stays where the player left it")

    def test_an_explicit_ferry_carries_no_revenue_but_still_costs(self):
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

            from engine.scheduling import schedule_ferry_to_hub

            ferry = schedule_ferry_to_hub(tail, hub)
            self.assertIsNotNone(ferry, "an explicit reposition must still be possible")
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
                "airports_map": lambda: api.airports_map(),
                "airport_routes": lambda: api.airport_routes(
                    prow["origin_iata"] if prow else (rt["origin_iata"] if rt else "ATL")
                ),
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
        from helpers import fresh_game
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
        from helpers import fresh_game
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
        from helpers import fresh_game
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

    def test_stand_is_released_at_pushback_not_a_turn_later(self):
        """Regression: [arr, dep + MTT) double-counted the turn and invented collisions.

        The rotation planner already places a departure at arrival + turn_minutes, so the
        arrival-to-departure window *is* the turnaround. Adding MTT again left an aircraft
        holding its stand for 45 minutes after takeoff, doubling every visit and putting
        two tails on one stand when they never overlap in reality.
        """
        from engine.gates import _peak_concurrency, visit_intervals_for_tail

        turn = 45.0 / 60.0
        arr, dep = 346.12, 346.12 + turn
        iv = visit_intervals_for_tail(turn, [(arr, "A"), (dep, "D")])
        self.assertEqual(1, len(iv))
        self.assertAlmostEqual(arr, iv[0][0], places=6)
        self.assertAlmostEqual(dep, iv[0][1], places=6,
                               msg="the stand must be free at pushback, not a turn later")

        # The real ABB-007 / ABB-002 collision at MCO: 1.8 minutes of phantom overlap.
        other = visit_intervals_for_tail(turn, [(344.65, "A"), (345.40, "D")])
        self.assertEqual(1, _peak_concurrency(iv + other),
                         "consecutive visits 42 minutes apart must not need two stands")

    def test_orphan_departure_occupies_the_stand_before_pushback(self):
        """A departure whose arrival is outside the window was parked, so occupancy precedes it."""
        from engine.gates import visit_intervals_for_tail

        turn = 45.0 / 60.0
        (start, end), = visit_intervals_for_tail(turn, [(500.0, "D")])
        self.assertAlmostEqual(500.0 - turn, start, places=6)
        self.assertAlmostEqual(500.0, end, places=6)

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
        """Occupancy tracks the real ground time between arrival and departure.

        The per-leg turnaround reaches the gate model through the schedule itself: the
        rotation planner places the departure at arrival + turn_minutes, so a 3-hour turn
        produces a 3-hour stand visit. `turn_minutes` is no longer added on top of that
        window — doing so counted the turn twice.
        """
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
            short_h = 30.0 / 60.0
            busy_short = sum(
                e - s
                for s, e in _gate_intervals_at_airport(
                    "ICN",
                    2,
                    extra_segments=[
                        inbound,
                        {**outbound, "dep_abs": arr + short_h, "arr_abs": arr + short_h + 11.0},
                    ],
                )
            )
            busy_long = sum(
                e - s
                for s, e in _gate_intervals_at_airport("ICN", 2, extra_segments=[inbound, outbound])
            )
            self.assertAlmostEqual(busy_short, short_h, places=2,
                                   msg="a 30-minute turn occupies the stand for 30 minutes")
            self.assertAlmostEqual(busy_long, turn_h, places=2,
                                   msg="a 3-hour turn occupies the stand for 3 hours, not 6")
            self.assertGreater(busy_long, busy_short + 1.0)

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
        # A departure whose inbound leg is outside the window: the aircraft was already
        # parked, so the stand was busy for the turn *leading up to* pushback and is free
        # the moment it leaves. The window runs [dep - mtt, dep), not [dep, dep + mtt).
        self.assertEqual([(base - mtt, base)], intervals)

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

        # FreshGame, not a copy of the developer's live save. This asserts an exact peak,
        # so it needs a world with no other traffic at the airport; against a real save it
        # measured whatever that save happened to have parked there (55 segments at ATL in
        # week 1, in one instance) and failed for reasons unrelated to duplicate rows.
        with FreshGame(hub="TPA") as db:
            dests = near_airports(db.db, "TPA", min_nm=200, max_nm=1100, limit=10)
            if not dests:
                self.skipTest("need a destination")
            route_id = db.open_route("TPA", dests[0])
            rt = db.fetch_one(
                "SELECT route_id, origin_iata, dest_iata FROM routes WHERE route_id = ?",
                (route_id,),
            )
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
# Route-open preview must predict what the route will actually carry
# ---------------------------------------------------------------------------
class TestPreviewMatchesReality(unittest.TestCase):
    """The preview promised the whole market; the route then delivered a fraction.

    compute_demand() takes two things off the market that the preview ignored: the
    connecting share a small network has not earned, and the slice competitors win.
    A player opening a route on the strength of the preview was being misled by up to 7x.
    """

    def _pair(self, g, hub):
        from engine.gates import is_auctioned_airport
        from engine.routes import haversine_distance

        dests = [
            a
            for a in near_airports(g.db, hub, min_nm=400, max_nm=2000, limit=25, require_runway_ft=7500)
            if not is_auctioned_airport(a)
        ]
        self.assertTrue(dests, "need a destination")
        o = dict(g.fetch_one("SELECT * FROM airports WHERE iata = ?", (hub,)))
        d = dict(g.fetch_one("SELECT * FROM airports WHERE iata = ?", (dests[0],)))
        dist = haversine_distance(float(o["lat"]), float(o["lon"]), float(d["lat"]), float(d["lon"]))
        return o, d, dist

    def test_preview_matches_compute_demand_after_opening(self):
        """Uses a hub-like destination on purpose.

        Against a plain O&D destination the capture factor is 1.0 and no competitor is
        present, so preview and reality agree even with the bug in place — the test would
        pass without detecting anything.
        """
        from engine.demand import compute_demand, preview_weekly_demand_before_open
        from engine.hub_profile import od_share_for
        from engine.routes import haversine_distance

        with FreshGame(hub="BOS") as g:
            row = g.fetch_one("SELECT * FROM airports WHERE iata = 'ORD'")
            if not row:
                self.skipTest("ORD not in catalog")
            o = dict(g.fetch_one("SELECT * FROM airports WHERE iata = 'BOS'"))
            d = dict(row)
            self.assertLess(od_share_for("ORD"), 0.70, "destination must withhold something")
            dist = haversine_distance(float(o["lat"]), float(o["lon"]), float(d["lat"]), float(d["lon"]))

            pv = preview_weekly_demand_before_open(o, d, dist)
            g.open_route("BOS", "ORD")
            ac = compute_demand("BOS-ORD", 1, 1)
            # Fares are untouched, so the only remaining difference is integer rounding.
            self.assertGreater(ac["total_pax"], 0)
            self.assertLess(pv["total_pax"], pv["weekly_market_total"], "capture must bite")
            gap = abs(pv["total_pax"] - ac["total_pax"]) / ac["total_pax"]
            self.assertLess(gap, 0.05, f"preview {pv['total_pax']} vs actual {ac['total_pax']}")

    def test_suspended_competitor_stops_taking_share(self):
        """A rival that has stopped flying a route must not keep its passengers.

        The share query counted SUSPENDED routes alongside ACTIVE ones, so a competitor
        withdrawing from a market left the player's traffic unchanged — the opposite of
        what withdrawing should mean.
        """
        from engine.demand import _fetch_competitor_rows_for_route

        with FreshGame(hub="BOS") as g:
            row = g.fetch_one(
                "SELECT competitor_id, route_pair_id, outbound_route_id FROM competitor_routes"
                " WHERE status = 'ACTIVE' AND outbound_route_id IS NOT NULL LIMIT 1"
            )
            if not row:
                self.skipTest("no active competitor route to suspend")
            rid = str(row["outbound_route_id"])
            self.assertTrue(_fetch_competitor_rows_for_route(rid), "should compete while active")

            g.execute(
                "UPDATE competitor_routes SET status = 'SUSPENDED'"
                " WHERE competitor_id = ? AND route_pair_id = ?",
                (row["competitor_id"], row["route_pair_id"]),
            )
            still = [
                r for r in _fetch_competitor_rows_for_route(rid)
                if str(r["competitor_id"]) == str(row["competitor_id"])
            ]
            self.assertEqual([], still, "a suspended route must not take share")

    def test_preview_reports_market_and_capturable_separately(self):
        """Both numbers must survive, so the UI can explain the difference."""
        from engine.demand import preview_weekly_demand_before_open

        with FreshGame(hub="BOS") as g:
            o, d, dist = self._pair(g, "BOS")
            pv = preview_weekly_demand_before_open(o, d, dist)
            for key in ("weekly_market_total", "market_business_pax", "market_leisure_pax",
                        "connect_capture", "player_share_business", "player_share_leisure"):
                self.assertIn(key, pv)
            self.assertGreaterEqual(pv["weekly_market_total"], pv["total_pax"])
            self.assertAlmostEqual(
                pv["weekly_market_total"],
                pv["market_business_pax"] + pv["market_leisure_pax"],
                delta=1,
            )

    def test_connecting_share_is_withheld_from_the_preview_too(self):
        """A hub-like destination with no network must not be previewed at full market."""
        from engine.demand import preview_weekly_demand_before_open
        from engine.hub_profile import od_share_for
        from engine.routes import haversine_distance

        with FreshGame(hub="BOS") as g:
            o = dict(g.fetch_one("SELECT * FROM airports WHERE iata = 'BOS'"))
            row = g.fetch_one("SELECT * FROM airports WHERE iata = 'ORD'")
            if not row:
                self.skipTest("ORD not in catalog")
            d = dict(row)
            self.assertLess(od_share_for("ORD"), 0.70, "ORD should be hub-like")
            dist = haversine_distance(float(o["lat"]), float(o["lon"]), float(d["lat"]), float(d["lon"]))
            pv = preview_weekly_demand_before_open(o, d, dist)
            self.assertLess(pv["connect_capture"], 1.0)
            self.assertLess(
                pv["total_pax"], pv["weekly_market_total"],
                "connecting traffic must be withheld until a bank exists",
            )


# ---------------------------------------------------------------------------
# Hub selection: travel demand (local) vs transit power (connecting)
# ---------------------------------------------------------------------------
class TestHubMetrics(unittest.TestCase):
    """The anchors are segment traffic, so they already contain connecting passengers.

    Splitting that total by od_share is what lets a tourist city and a transit city read
    differently — a gravity model cannot tell them apart, because both carry far more
    traffic than their local catchment explains.
    """

    def test_od_share_loaded_with_sensible_defaults(self):
        with FreshGame(hub="TPA") as g:
            from engine.hub_profile import od_share_for

            self.assertLess(od_share_for("ATL"), 0.5, "ATL is a connecting hub")
            self.assertGreater(od_share_for("LAS"), 0.9, "Las Vegas is a destination")
            self.assertLess(od_share_for("CLT"), 0.3, "Charlotte is the extreme case")
            # An airport absent from the CSV still gets a usable value.
            self.assertGreater(od_share_for("ZZZ_NOT_AN_AIRPORT"), 0.0)

    def test_tourist_and_transit_cities_read_differently(self):
        """LAS and CLT carry similar total traffic and must not look alike."""
        with FreshGame(hub="TPA") as g:
            from engine.hub_profile import hub_profile

            las = hub_profile("LAS")
            clt = hub_profile("CLT")
            self.assertGreater(las["travel_demand_icons"], clt["travel_demand_icons"])
            self.assertGreater(clt["transit_power"], las["transit_power"])
            self.assertAlmostEqual(1.1, las["transit_power"], delta=0.15)
            self.assertGreaterEqual(clt["transit_power"], 4.0)

    def test_demand_icons_spread_the_candidates(self):
        """Round-number thresholds put half the realistic hubs at 5/5, which is useless."""
        from engine.hub_profile import DEMAND_ICON_THRESHOLDS, demand_icons

        self.assertEqual(5, demand_icons(DEMAND_ICON_THRESHOLDS[0] + 1))
        self.assertEqual(1, demand_icons(0))
        seen = {demand_icons(v) for v in (600_000, 350_000, 200_000, 100_000, 10_000)}
        self.assertEqual({1, 2, 3, 4, 5}, seen, "every level must be reachable")

    def test_transit_power_is_the_inverse_of_od_share(self):
        """The label promises arithmetic, so it must match."""
        with FreshGame(hub="TPA") as g:
            from engine.hub_profile import od_share_for, transit_power

            for iata in ("ATL", "CLT", "LAS", "LHR"):
                self.assertAlmostEqual(
                    1.0 / od_share_for(iata), transit_power(iata), delta=0.06
                )
            self.assertLessEqual(
                transit_power("CLT"), 5.5, "no real airport supports a 10x claim"
            )

    def test_connecting_unlock_is_threshold_then_ramp(self):
        with FreshGame(hub="TPA") as g:
            from engine.hub_profile import connecting_unlock_fraction

            self.assertEqual(0.0, connecting_unlock_fraction(0))
            self.assertEqual(0.0, connecting_unlock_fraction(3), "a few routes feed nobody")
            self.assertGreater(connecting_unlock_fraction(9), 0.0)
            self.assertLess(connecting_unlock_fraction(9), 1.0)
            self.assertEqual(1.0, connecting_unlock_fraction(20))
            ramp = [connecting_unlock_fraction(n) for n in range(4, 16)]
            self.assertEqual(ramp, sorted(ramp), "must not go backwards")

    def test_hub_capture_grows_with_the_network_but_spares_od_pairs(self):
        """The mechanic must bite at hubs without nerfing every route in the game."""
        from engine.demand import connecting_capture_factor
        from engine.gates import is_auctioned_airport

        with FreshGame(hub="ATL") as g:
            dests = near_airports(g.db, "ATL", min_nm=200, max_nm=1500, limit=20)
            g.open_route("ATL", dests[0])
            small = connecting_capture_factor("ATL", dests[0])
            self.assertLess(small, 0.5, "one route must not earn a hub's connecting feed")
            for d in dests[1:15]:
                g.open_route("ATL", d)
            big = connecting_capture_factor("ATL", dests[0])
            self.assertGreater(big, small)
            self.assertAlmostEqual(1.0, big, delta=0.01, msg="a full bank captures it all")

        with FreshGame(hub="TPA") as g2:
            # Both ends are above the O&D threshold, so nothing is withheld.
            self.assertAlmostEqual(1.0, connecting_capture_factor("LAS", "MCO"), delta=0.001)

    def test_hub_profile_reports_the_supporting_numbers(self):
        with FreshGame(hub="TPA") as g:
            from engine.hub_profile import hub_profile

            p = hub_profile("ATL")
            self.assertGreater(p["reachable_destinations"], 50)
            self.assertGreater(p["weekly_market_total"], p["travel_demand_weekly"])
            self.assertAlmostEqual(
                p["weekly_market_total"],
                p["travel_demand_weekly"] + p["connecting_weekly"],
                delta=2,
                msg="local + connecting must reconstruct the total",
            )
            self.assertTrue(p["top_destinations"])
            self.assertTrue(any(c["fleet_size"] > 0 for c in p["competitors"]),
                            "ATL has AI competitors based there")


# ---------------------------------------------------------------------------
# Fleet disposal: sell an aircraft, hand a lease back early
# ---------------------------------------------------------------------------
class TestFleetDisposal(unittest.TestCase):
    def _world(self):
        return FreshGame(hub="TPA", cash=500_000_000)

    def test_valuation_falls_with_age_usage_and_condition(self):
        from engine import aircraft as A

        with self._world() as g:
            tail = g.buy("B738")
            new = A.estimate_resale_value(tail)
            self.assertEqual(1.0, new["age_factor"])
            self.assertEqual(1.0, new["usage_factor"])
            self.assertLess(new["estimated_value"], new["purchase_price_paid"],
                            "a brand-new aircraft still loses the sale haircut")

            g.execute("UPDATE game_state SET game_week = 105 WHERE id = 1")  # ~2 years
            g.execute("UPDATE fleet SET total_airborne_hours = 2000 WHERE tail_number = ?", (tail,))
            aged = A.estimate_resale_value(tail)
            self.assertLess(aged["estimated_value"], new["estimated_value"])

            g.execute("UPDATE fleet SET weeks_since_maintenance = 999 WHERE tail_number = ?", (tail,))
            worn = A.estimate_resale_value(tail)
            self.assertTrue(worn["maintenance_overdue"])
            self.assertLess(worn["estimated_value"], aged["estimated_value"])

            g.execute("UPDATE game_state SET game_week = 5000 WHERE id = 1")
            g.execute("UPDATE fleet SET total_airborne_hours = 90000 WHERE tail_number = ?", (tail,))
            floored = A.estimate_resale_value(tail)
            self.assertTrue(floored["floor_applied"], "value must not decay below the residual")

    def test_buy_then_sell_loses_money(self):
        """Anti-exploit: a round trip must never be a source of free cash."""
        from engine import aircraft as A
        from engine.settlement import process_pending_disposals

        with self._world() as g:
            before = g.cash()
            tail = g.buy("B738")
            A.request_disposal(tail)
            process_pending_disposals()
            self.assertLess(g.cash(), before, "buy->sell round trip must lose money")

    def test_lease_appears_in_the_books_from_the_first_week(self):
        """Lease rent must move cash through week_ledger, not around it.

        The first week used to be charged directly in lease_aircraft() and the tail
        flagged `lease_prepaid`, which made compute_lease_costs() skip it. Real money left
        the account and no P&L line ever showed it, so the books reported $0 lease costs
        while cash fell.
        """
        from engine.settlement import run_settlement

        with self._world() as g:
            before = g.cash()
            g.lease("B739")
            self.assertEqual(before, g.cash(), "signing must not charge outside settlement")

            g.execute("UPDATE game_state SET game_week = 2 WHERE id = 1")
            run_settlement(1)
            led = g.fetch_one("SELECT lease_costs FROM week_ledger WHERE game_week = 1")
            self.assertGreater(
                led["lease_costs"], 0, "the first week's rent must appear in the books"
            )
            self.assertLess(g.cash(), before, "and it must actually be paid")

    def test_legacy_prepaid_lease_is_not_charged_twice(self):
        """Leases signed under the old rules already paid week one; skip it exactly once."""
        from engine.settlement import run_settlement

        with self._world() as g:
            tail = g.lease("B739")
            g.execute("UPDATE fleet SET lease_prepaid = 1 WHERE tail_number = ?", (tail,))

            g.execute("UPDATE game_state SET game_week = 2 WHERE id = 1")
            run_settlement(1)
            self.assertEqual(
                0,
                g.fetch_one("SELECT lease_costs FROM week_ledger WHERE game_week = 1")["lease_costs"],
                "a prepaid week must not be billed again",
            )

            g.execute("UPDATE game_state SET game_week = 3 WHERE id = 1")
            run_settlement(2)
            self.assertGreater(
                g.fetch_one("SELECT lease_costs FROM week_ledger WHERE game_week = 2")["lease_costs"],
                0,
                "billing must resume the week after",
            )

    def test_lease_return_charges_the_flat_penalty(self):
        from engine import aircraft as A
        from engine.settlement import process_pending_disposals

        with self._world() as g:
            tail = g.lease("B738")
            penalty = A.lease_return_penalty(tail)
            self.assertGreater(penalty, 0)
            before = g.cash()
            A.request_disposal(tail)
            out = process_pending_disposals()
            self.assertAlmostEqual(before - penalty, g.cash(), places=2)
            self.assertEqual([tail], [r["tail_number"] for r in out["returned"]])
            self.assertIsNone(
                g.fetch_one("SELECT 1 FROM fleet WHERE tail_number = ?", (tail,))
            )

    def test_settlement_reentry_does_not_pay_twice(self):
        """Settlement re-enters via retry and catch-up paths; a sale must credit once."""
        from engine import aircraft as A
        from engine.settlement import process_pending_disposals

        with self._world() as g:
            tail = g.buy("B738")
            A.request_disposal(tail)
            before = g.cash()
            first = process_pending_disposals()
            after_first = g.cash()
            second = process_pending_disposals()
            self.assertEqual(1, len(first["sold"]))
            self.assertEqual(0, len(second["sold"]))
            self.assertEqual(after_first, g.cash(), "second pass must not credit again")
            self.assertGreater(after_first, before)

    def test_away_from_hub_ferries_home_then_sells(self):
        """Auto-ferry: request from anywhere, hold until home, then execute."""
        from engine import aircraft as A
        from engine.gates import is_auctioned_airport
        from engine.scheduling import on_arrival, on_departure
        from engine.settlement import process_pending_disposals

        with self._world() as g:
            dests = [
                a
                for a in near_airports(g.db, "TPA", min_nm=200, max_nm=1100, limit=25, require_runway_ft=7500)
                if not is_auctioned_airport(a)
            ]
            tail = g.buy("B738")
            g.open_round_trip(dests[0])
            g.execute(
                "UPDATE fleet SET current_airport_iata = ? WHERE tail_number = ?",
                (dests[0], tail),
            )
            res = A.request_disposal(tail)
            self.assertIsInstance(res["ferry"], dict, "a down-route aircraft must be positioned home")

            held = process_pending_disposals()
            self.assertEqual(1, len(held["held"]), "must not sell before it is home")
            self.assertEqual(0, len(held["sold"]))

            for s in g.fetch_all(
                "SELECT segment_id FROM flight_segments WHERE tail_number = ? AND status = 'SCHEDULED'",
                (tail,),
            ):
                on_departure(s["segment_id"])
                on_arrival(s["segment_id"])

            self.assertEqual([], A.disposal_blockers(tail))
            out = process_pending_disposals()
            self.assertEqual(1, len(out["sold"]))
            self.assertEqual(0, g.fetch_one("SELECT COUNT(*) c FROM fleet")["c"])

    def test_aog_cannot_be_queued_and_cancel_works(self):
        from engine import aircraft as A
        from engine.settlement import process_pending_disposals

        with self._world() as g:
            grounded = g.buy("B738")
            g.execute(
                "UPDATE fleet SET status='AOG', aog_reason='Mechanical' WHERE tail_number = ?",
                (grounded,),
            )
            with self.assertRaises(ValueError):
                A.request_disposal(grounded)

            other = g.buy("B738")
            A.request_disposal(other)
            A.cancel_disposal(other)
            self.assertIsNone(
                g.fetch_one(
                    "SELECT pending_disposal FROM fleet WHERE tail_number = ?", (other,)
                )["pending_disposal"]
            )
            self.assertEqual(0, len(process_pending_disposals()["sold"]))

    def test_acquisition_is_recorded_and_backfill_fills_older_rows(self):
        from db.db import _add_fleet_disposal_columns

        with self._world() as g:
            tail = g.buy("B738")
            row = g.fetch_one(
                "SELECT acquired_game_week, purchase_price_paid, total_airborne_hours"
                " FROM fleet WHERE tail_number = ?",
                (tail,),
            )
            self.assertIsNotNone(row["acquired_game_week"])
            self.assertGreater(row["purchase_price_paid"], 0)
            self.assertEqual(0, row["total_airborne_hours"])

            # An aircraft predating the columns: backfill must fill it, not leave NULLs.
            g.execute(
                "UPDATE fleet SET acquired_game_week = NULL, purchase_price_paid = NULL,"
                " total_airborne_hours = 0 WHERE tail_number = ?",
                (tail,),
            )
            _add_fleet_disposal_columns()
            row = g.fetch_one(
                "SELECT acquired_game_week, purchase_price_paid FROM fleet WHERE tail_number = ?",
                (tail,),
            )
            self.assertIsNotNone(row["acquired_game_week"])
            self.assertGreater(row["purchase_price_paid"], 0)


# ---------------------------------------------------------------------------
# Airport reference data: US timezones
# ---------------------------------------------------------------------------
class TestAirportTimezones(unittest.TestCase):
    """Alaska and Pacific names were effectively swapped by the generator's band table."""

    EXPECTED = {
        "ANC": "America/Anchorage",      # Anchorage was tagged Pacific/Honolulu
        "FAI": "America/Anchorage",
        "HNL": "Pacific/Honolulu",
        "OGG": "Pacific/Honolulu",
        "SFO": "America/Los_Angeles",    # the whole west coast was tagged Alaska time
        "LAX": "America/Los_Angeles",
        "SEA": "America/Los_Angeles",
        "PDX": "America/Los_Angeles",
        "DEN": "America/Denver",
    }

    def test_csv_timezones_are_correct(self):
        with open(DATA / "airports.csv", newline="", encoding="utf-8") as f:
            by_iata = {r["iata"]: r for r in csv.DictReader(f)}
        for iata, tz in self.EXPECTED.items():
            if iata in by_iata:
                self.assertEqual(tz, by_iata[iata]["timezone"], f"{iata} timezone")

    def test_generator_separates_alaska_from_hawaii(self):
        """Longitude alone cannot: both lie far west, which is how the bug arose."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "process_airports", str(DATA / "process_airports.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self.assertEqual((-9, "America/Anchorage"), mod.get_timezone(-149.996, 61.174))  # ANC
        self.assertEqual((-10, "Pacific/Honolulu"), mod.get_timezone(-157.924, 21.319))  # HNL
        self.assertEqual((-8, "America/Los_Angeles"), mod.get_timezone(-122.375, 37.619))  # SFO

    def test_existing_saves_are_repaired_and_repair_is_idempotent(self):
        from db.db import _repair_us_airport_timezones

        with TempSave() as db:
            db.ensure_schema_migrations()
            # Re-introduce the old wrong values, then repair.
            db.execute(
                "UPDATE airports SET timezone='America/Anchorage'"
                " WHERE country='US' AND lat < 50 AND lon BETWEEN -130 AND -117"
            )
            db.execute(
                "UPDATE airports SET timezone='Pacific/Honolulu'"
                " WHERE country='US' AND lat > 50 AND lon < -129"
            )
            _repair_us_airport_timezones()

            def tz(iata):
                row = db.fetch_one("SELECT timezone FROM airports WHERE iata = ?", (iata,))
                return row["timezone"] if row else None

            first = {i: tz(i) for i in ("ANC", "SFO", "SEA", "HNL")}
            for iata, expected in self.EXPECTED.items():
                if tz(iata) is not None:
                    self.assertEqual(expected, tz(iata), f"{iata} after repair")

            # Runs on every startup, so a second pass must not swap anything back.
            _repair_us_airport_timezones()
            _repair_us_airport_timezones()
            self.assertEqual(first, {i: tz(i) for i in ("ANC", "SFO", "SEA", "HNL")})


# ---------------------------------------------------------------------------
# Departure-time suggestions for a drafted chain
# ---------------------------------------------------------------------------
class TestDepartureSuggestions(unittest.TestCase):
    def _world(self):
        from engine.gates import is_auctioned_airport

        g = FreshGame(hub="TPA")
        dests = [
            a
            for a in near_airports(g.db, "TPA", min_nm=200, max_nm=1100, limit=25, require_runway_ft=7500)
            if not is_auctioned_airport(a)
        ]
        self.assertTrue(dests, "need a usable destination")
        tail = g.buy("B738")
        return g, tail, g.open_round_trip(dests[0])

    def test_suggested_times_are_actually_creatable(self):
        """The contract: anything suggested must be accepted by creation.

        The suggester and create_chained_detailed_rotation share
        assert_chain_plan_schedulable precisely so the two cannot drift apart.
        """
        import json as _json

        from engine.scheduling import create_chained_detailed_rotation, suggest_departure_times

        g, tail, trip = self._world()
        with g:
            days = _json.dumps(["SAT"])
            create_chained_detailed_rotation(tail, list(trip), ["X1", "X2"], days, "09:00")
            out = suggest_departure_times(
                tail, list(trip), days, preferred_time="09:00", limit=3
            )
            self.assertTrue(out["suggestions"], "expected some workable time")
            # Only the first: acting on a suggestion changes the schedule, so the rest
            # were computed against a state that no longer holds. The contract is that a
            # suggestion is creatable against the state it was computed from.
            first = out["suggestions"][0]["departure_time"]
            create_chained_detailed_rotation(tail, list(trip), ["", ""], days, first)

    def test_blocked_time_is_not_suggested_and_carries_a_reason(self):
        import json as _json

        from engine.scheduling import create_chained_detailed_rotation, suggest_departure_times

        g, tail, trip = self._world()
        with g:
            days = _json.dumps(["SAT"])
            create_chained_detailed_rotation(tail, list(trip), ["X1", "X2"], days, "09:00")
            out = suggest_departure_times(
                tail, list(trip), days, preferred_time="09:00", limit=12, step_minutes=30
            )
            offered = {s["departure_time"] for s in out["suggestions"]}
            self.assertNotIn("09:00", offered, "the occupied time must not be offered")
            reasons = {r["departure_time"]: r["reason"] for r in out["rejected_sample"]}
            self.assertTrue(reasons, "rejections must explain themselves")

    def test_results_are_ranked_by_closeness_to_the_requested_time(self):
        """Earliest-first would answer 00:00 to someone who asked for the afternoon."""
        import json as _json

        from engine.scheduling import suggest_departure_times

        g, tail, trip = self._world()
        with g:
            out = suggest_departure_times(
                tail, list(trip), _json.dumps(["SAT"]), preferred_time="14:00", limit=3
            )
            times = [s["departure_time"] for s in out["suggestions"]]
            self.assertEqual("14:00", times[0], "an available preferred time comes first")
            for t in times:
                hh = int(t.split(":")[0])
                self.assertLess(abs(hh - 14), 4, f"{t} is not near the requested 14:00")

    def test_suggester_does_not_write_anything(self):
        """It probes with read-only validators and placeholder flight numbers."""
        import json as _json

        from engine.scheduling import suggest_departure_times

        g, tail, trip = self._world()
        with g:
            def counts():
                return tuple(
                    g.fetch_one(f"SELECT COUNT(*) c FROM {t}")["c"]
                    for t in ("flight_segments", "flight_schedules", "weekly_rotations")
                )

            before = counts()
            suggest_departure_times(tail, list(trip), _json.dumps(["SAT"]), limit=5)
            self.assertEqual(before, counts(), "suggesting must not mutate the schedule")

    def test_impossible_chain_reports_once_not_per_candidate(self):
        import json as _json

        from engine.scheduling import suggest_departure_times

        g, tail, trip = self._world()
        with g:
            with self.assertRaises(ValueError):
                # Reversed order does not connect, and no departure time can fix that.
                suggest_departure_times(
                    tail, [trip[1], trip[0]][::-1][:1] + [trip[0]], _json.dumps(["SAT"])
                )


# ---------------------------------------------------------------------------
# Week-boundary handling: long chains that run past the end of their anchor week
# ---------------------------------------------------------------------------
class TestChainWeekOverflow(unittest.TestCase):
    """A chain anchored late in the week finishes in the next one.

    Legs used to be stamped with the anchor week and labelled SUN regardless, so gate
    capacity compared them against the wrong week's traffic — accepting a schedule that
    conflicted, then cancelling its later legs at the rollover.
    """

    LONGHAUL = [
        ("TPA", "LHR", 4000), ("LHR", "SGN", 5800), ("SGN", "SIN", 590),
        ("SIN", "JFK", 8270), ("JFK", "SFO", 2240), ("SFO", "SLC", 520),
    ]

    def _routes(self):
        return [
            {"route_id": f"{a}-{b}", "origin_iata": a, "dest_iata": b, "distance_nm": d}
            for a, b, d in self.LONGHAUL
        ]

    def test_overflowing_legs_carry_their_own_week_and_weekday(self):
        from engine.scheduling.rotation import _plan_chained_detailed_segments

        with FreshGame(hub="TPA") as g:
            routes = self._routes()
            fns = [f"XX{i}" for i in range(len(routes))]
            gw = 9
            planned, _, _ = _plan_chained_detailed_segments(
                gw, ["SAT"], "08:00", routes, fns, 490.0, 1.5
            )
            base = (gw - 1) * 168.0
            spilled = [p for p in planned if p["dep_abs"] >= base + 168.0]
            self.assertTrue(spilled, "a SAT-anchored 51h chain must run past the week end")
            for p in planned:
                self.assertEqual(
                    int(p["dep_abs"] // 168.0) + 1,
                    p["game_week"],
                    "each leg must carry the week its own departure falls in",
                )
            # The old code clamped every overflowing leg's label to SUN.
            self.assertTrue(
                any(p["day"] != "SUN" for p in spilled),
                "legs past the week end must get their real weekday, not a clamped SUN",
            )

    def test_day_label_uses_the_hour_not_a_clamped_week(self):
        """The weekday comes from the hour itself, so overflow legs get their real day.

        This previously subtracted the passed week's base and clamped the index to 0..6,
        which labelled everything past the week end "SUN".
        """
        from engine.scheduling.time_helpers import _day_of_week_label

        base = 8 * 168.0  # start of week 9
        self.assertEqual("SAT", _day_of_week_label(9, base + 128.0))
        # 172h into week 9 is Monday of week 10 — the clamp used to force SUN.
        self.assertEqual("MON", _day_of_week_label(9, base + 172.0))
        self.assertEqual("TUE", _day_of_week_label(9, base + 196.0))

    def test_chain_longer_than_a_week_is_rejected(self):
        from engine.scheduling.rotation import _plan_chained_detailed_segments

        with FreshGame(hub="TPA") as g:
            routes = [
                {"route_id": f"L{i}", "origin_iata": "A", "dest_iata": "B", "distance_nm": 9000}
                for i in range(20)
            ]
            with self.assertRaises(ValueError) as ctx:
                _plan_chained_detailed_segments(
                    9, ["MON"], "08:00", routes, [f"F{i}" for i in range(20)], 490.0, 1.5
                )
            self.assertIn("168", str(ctx.exception))

    def test_gate_peak_counts_aircraft_by_absolute_time_not_week_tag(self):
        """Two aircraft sharing a stand must be seen, even if one is tagged to another week.

        Keying occupancy off the game_week label meant a mis-tagged leg was invisible to
        the week it physically occupied, and both weeks reported a peak of 1.
        """
        import uuid

        from engine.gates import _gate_intervals_at_airport, peak_concurrency

        with FreshGame(hub="TPA") as g:
            dests = near_airports(g.db, "TPA", min_nm=200, max_nm=1100, limit=10)
            route_id = g.open_route("TPA", dests[0])
            ap = "TPA"
            hour = 9 * 168.0 + 35.0  # inside week 10
            for tag_week, tail in ((9, "N-OVERFLOW"), (10, "N-NATIVE")):
                g.execute(
                    """
                    INSERT INTO flight_segments (
                        segment_id, game_week, day_of_week, tail_number, route_id,
                        origin_iata, dest_iata, flight_number,
                        scheduled_dep_time, scheduled_dep_game_hour,
                        scheduled_arr_time, scheduled_arr_game_hour,
                        baseline_dep_game_hour, baseline_arr_game_hour, turn_minutes,
                        status, pax_business, pax_leisure, revenue_gross,
                        excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee
                    ) VALUES (?, ?, 'SUN', ?, ?, ?, ?, 'TST', '08:00', ?, '10:00', ?, ?, ?, 90,
                              'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
                    """,
                    (str(uuid.uuid4()), tag_week, tail, route_id, ap, dests[0],
                     hour, hour + 2.0, hour, hour + 2.0),
                )
            self.assertEqual(
                2,
                peak_concurrency(_gate_intervals_at_airport(ap, 10)),
                "both aircraft occupy a week-10 stand regardless of their week tag",
            )
            self.assertEqual(
                0,
                peak_concurrency(_gate_intervals_at_airport(ap, 9)),
                "neither flight occupies a stand during week 9",
            )

    def test_operating_days_rejects_unparseable_input(self):
        """A bare day name used to become ["MON"], silently moving the whole rotation."""
        from engine.scheduling.rotation import _operating_days_list

        self.assertEqual(["WED"], _operating_days_list('["WED"]'))
        self.assertEqual(7, len(_operating_days_list("DAILY")))
        with self.assertRaises(ValueError):
            _operating_days_list("SAT")
        with self.assertRaises(ValueError):
            _operating_days_list('["FUNDAY"]')


# ---------------------------------------------------------------------------
# Schedule stacking: add flights to an existing plan without clearing it first
# ---------------------------------------------------------------------------
class TestScheduleStacking(unittest.TestCase):
    """A player must be able to add flights to an aircraft that already has a plan.

    Standalone detailed lines used to be refused whenever the tail carried a chained
    rotation, forcing a clear-and-rebuild. They now stack beside the chains in the same
    `detailed_chained` blob.
    """

    def _world(self):
        """Fresh game at TPA with a narrowbody and three non-auctioned round trips.

        Non-auctioned destinations keep these tests about stacking rather than about
        gate auctions, which would otherwise reject every leg for lack of allocation.
        """
        from engine.gates import is_auctioned_airport

        g = FreshGame(hub="TPA")
        dests = [
            a
            for a in near_airports(g.db, "TPA", min_nm=200, max_nm=1100, limit=25, require_runway_ft=7500)
            if not is_auctioned_airport(a)
        ]
        self.assertGreaterEqual(len(dests), 3, "need three usable destinations")
        tail = g.buy("B738")
        trips = [g.open_round_trip(d) for d in dests[:3]]
        return g, tail, trips

    @staticmethod
    def _days(*d):
        return json.dumps(list(d))

    def _rotation_blob(self, g, tail):
        row = g.fetch_one("SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail,))
        return json.loads(row["legs_json"]) if row and row["legs_json"] else {}

    def test_stacking_and_respawn(self):
        """Chains stack, a standalone line stacks beside them, and all survive respawn."""
        # Deliberately does NOT import assert_detailed_line_addable: this test must fail
        # with the old rejection if stacking ever regresses, not with an ImportError.
        from engine.scheduling import (
            create_chained_detailed_rotation,
            create_flight_schedule,
            merge_detailed_weekly_template,
            spawn_rotation_segments_for_week,
        )

        g, tail, trips = self._world()
        with g:
            create_chained_detailed_rotation(
                tail, list(trips[0]), ["TS100", "TS101"], self._days("MON"), "08:00"
            )
            create_chained_detailed_rotation(
                tail, list(trips[1]), ["TS200", "TS201"], self._days("WED"), "08:00"
            )
            blob = self._rotation_blob(g, tail)
            self.assertEqual("detailed_chained", blob.get("mode"))
            self.assertEqual(2, len(blob.get("chains") or []), "chain must stack onto chain")

            # The case that used to raise "already has a chained detailed rotation".
            sched = create_flight_schedule(tail, trips[2][0], "TS300", self._days("FRI"), "08:00")
            merge_detailed_weekly_template(tail, [sched["template_item"]], 1)

            blob = self._rotation_blob(g, tail)
            self.assertEqual(2, len(blob.get("chains") or []), "chains must be preserved")
            self.assertEqual(1, len(blob.get("items") or []), "line must stack beside chains")
            self.assertEqual(
                5,
                g.fetch_one(
                    "SELECT COUNT(*) c FROM flight_segments WHERE tail_number = ? AND game_week = 1",
                    (tail,),
                )["c"],
            )

            # The subtle half: a stacked line that the chained spawn ignores would fly
            # this week and then silently vanish at the next weekly respawn.
            spawn_rotation_segments_for_week(2)
            wk2 = g.fetch_all(
                "SELECT origin_iata, dest_iata FROM flight_segments"
                " WHERE tail_number = ? AND game_week = 2",
                (tail,),
            )
            self.assertEqual(5, len(wk2), "2 chains x 2 legs + 1 stacked line must respawn")

    def test_airborne_aircraft_can_still_take_a_free_time_block(self):
        """Being busy now must not block scheduling later.

        Dispatch used to require status IDLE or SCHEDULED, so an aircraft in the air
        could not be given a rotation for a completely empty day. Whether a block is
        free is decided by the overlap/turnaround rule and the position timeline; only
        AOG and MAINTENANCE genuinely stop an aircraft flying.
        """
        from engine.scheduling import create_chained_detailed_rotation, on_departure

        g, tail, trips = self._world()
        with g:
            create_chained_detailed_rotation(
                tail, list(trips[0]), ["A1", "A2"], self._days("MON"), "08:00"
            )
            first = g.fetch_one(
                "SELECT segment_id FROM flight_segments WHERE tail_number = ?"
                " ORDER BY scheduled_dep_game_hour LIMIT 1",
                (tail,),
            )
            on_departure(first["segment_id"])
            self.assertEqual(
                "IN_AIR",
                g.fetch_one("SELECT status FROM fleet WHERE tail_number = ?", (tail,))["status"],
            )

            # A free day must be accepted even though the aircraft is airborne.
            create_chained_detailed_rotation(
                tail, list(trips[1]), ["B1", "B2"], self._days("WED"), "08:00"
            )

            # A genuine clash is still refused, on its merits rather than on status.
            with self.assertRaises(ValueError):
                create_chained_detailed_rotation(
                    tail, list(trips[1]), ["C1", "C2"], self._days("MON"), "08:15"
                )

    def test_aog_aircraft_still_cannot_be_scheduled(self):
        from engine.scheduling import create_chained_detailed_rotation

        g, tail, trips = self._world()
        with g:
            g.execute(
                "UPDATE fleet SET status = 'AOG', aog_reason = 'Mechanical'"
                " WHERE tail_number = ?",
                (tail,),
            )
            with self.assertRaises(ValueError) as ctx:
                create_chained_detailed_rotation(
                    tail, list(trips[0]), ["A1", "A2"], self._days("MON"), "08:00"
                )
            self.assertIn("AOG", str(ctx.exception))

    def test_rejected_line_leaves_no_orphan_segment(self):
        """create_flight_schedule writes rows, so incompatibility must be caught first.

        Validating after the write left the rejected flight's segment orphaned in the
        week — in no template, so invisible to respawn — and holding the slot, which made
        the next attempt fail as an overlap.
        """
        from engine.scheduling import (
            assign_rotation,
            assert_detailed_line_addable,
            create_flight_schedule,
            merge_detailed_weekly_template,
        )

        g, tail, trips = self._world()
        with g:
            assign_rotation(tail, list(trips[0]))  # quick rotation — incompatible
            before = g.fetch_one(
                "SELECT COUNT(*) c FROM flight_segments WHERE tail_number = ?", (tail,)
            )["c"]
            with self.assertRaises(ValueError):
                assert_detailed_line_addable(tail)
                sched = create_flight_schedule(
                    tail, trips[1][0], "TS900", self._days("FRI"), "08:00"
                )
                merge_detailed_weekly_template(tail, [sched["template_item"]], 1)
            after = g.fetch_one(
                "SELECT COUNT(*) c FROM flight_segments WHERE tail_number = ?", (tail,)
            )["c"]
            self.assertEqual(before, after, "rejected add must not leave an orphan segment")
            self.assertEqual(
                0,
                g.fetch_one(
                    "SELECT COUNT(*) c FROM flight_schedules WHERE tail_number = ?", (tail,)
                )["c"],
            )

    def test_positioning_still_blocks_a_stranding_leg(self):
        """Stacking must not weaken the position timeline check."""
        from engine.scheduling import (
            create_chained_detailed_rotation,
            create_flight_schedule,
            merge_detailed_weekly_template,
        )

        g, tail, trips = self._world()
        with g:
            create_chained_detailed_rotation(
                tail, list(trips[0]), ["TS100", "TS101"], self._days("MON"), "08:00"
            )
            inbound = trips[1][1]  # departs the outstation, where the aircraft is not
            with self.assertRaises(ValueError):
                sched = create_flight_schedule(
                    tail, inbound, "TS400", self._days("WED"), "08:00"
                )
                merge_detailed_weekly_template(tail, [sched["template_item"]], 1)


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


# ---------------------------------------------------------------------------
# Gate shortfalls: bill instead of dropping the schedule (FEATURE-05).
# ---------------------------------------------------------------------------
class TestGateShortfall(unittest.TestCase):
    """A stand shortage bills the player rather than silently deleting their week."""

    AP = "ORD"

    def _setup(self, db, *, units=1):
        """One stand at ORD and two tails wanting it at the same moment."""
        import uuid as _uuid
        db.execute("DELETE FROM gate_shortfall_events")
        db.execute(
            "DELETE FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER'",
            (self.AP,),
        )
        if units > 0:
            db.execute(
                """INSERT INTO airport_gate_allocations
                   (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                    scheduled_this_week, below_threshold_weeks, effective_week, status)
                   VALUES (?, ?, 'PLAYER', ?, 0, 0, 0, 1, 'ACTIVE')""",
                (str(_uuid.uuid4()), self.AP, int(units)),
            )
        segs = []
        for i, tail in enumerate(("N001", "N002")):
            segs.append({"tail_number": tail, "origin_iata": "BOS", "dest_iata": self.AP,
                         "dep_abs": 400.0 + i * 0.1, "arr_abs": 402.0 + i * 0.1,
                         "turn_minutes": 45})
            segs.append({"tail_number": tail, "origin_iata": self.AP, "dest_iata": "BOS",
                         "dep_abs": 402.75 + i * 0.1, "arr_abs": 404.75 + i * 0.1,
                         "turn_minutes": 45})
        return segs

    def test_scheduling_still_refuses_but_spawn_bills(self):
        """The player can fix a plan they are still writing; a published week must fly."""
        with fresh_game() as g:
            from engine.gates import (assert_player_gate_capacity_for_new_segments,
                                      open_gate_shortfalls)
            segs = self._setup(g.db)
            with self.assertRaises(ValueError):
                assert_player_gate_capacity_for_new_segments(3, segs, replace_tails=False)
            self.assertEqual([], open_gate_shortfalls(),
                             "a refused interactive edit must not bill the player")

            assert_player_gate_capacity_for_new_segments(
                3, segs, replace_tails=False, shortfall_mode=True)
            # reconcile=False: these segments are a proposal, never inserted, so the live
            # peak is 0 and reconciliation would rightly close the event on read.
            open_now = open_gate_shortfalls(reconcile=False)
            self.assertEqual(1, len(open_now))
            self.assertEqual(self.AP, open_now[0]["airport_iata"])
            self.assertEqual(2, int(open_now[0]["peak_needed"]))

    def test_repeated_spawn_does_not_open_a_second_event(self):
        """Spawn runs at launch and again from settlement; it must not bill twice."""
        with fresh_game() as g:
            from engine.gates import (assert_player_gate_capacity_for_new_segments,
                                      open_gate_shortfalls)
            segs = self._setup(g.db)
            for _ in range(3):
                assert_player_gate_capacity_for_new_segments(
                    3, segs, replace_tails=False, shortfall_mode=True)
            self.assertEqual(1, len(open_gate_shortfalls(reconcile=False)))

    def test_no_allocation_at_all_still_hard_blocks(self):
        """Operating where you have never bid is not a shortfall — there is nothing to add to."""
        with fresh_game() as g:
            from engine.gates import (assert_player_gate_capacity_for_new_segments,
                                      open_gate_shortfalls)
            segs = self._setup(g.db, units=0)
            with self.assertRaises(ValueError):
                assert_player_gate_capacity_for_new_segments(
                    3, segs, replace_tails=False, shortfall_mode=True)
            self.assertEqual([], open_gate_shortfalls())

    def test_fee_scales_with_units_at_that_airport_and_has_a_floor(self):
        """fee = route revenue x rate x units HELD HERE, floored at the auction minimum."""
        with fresh_game() as g:
            from engine.gates import gate_shortfall_fee, emergency_gate_fee_rate
            self._setup(g.db, units=3)
            priced = gate_shortfall_fee(self.AP, 3, {("BOS", self.AP)})
            floor = float(g.db.get_financial_constant("gate_min_price_per_unit") or 5000) * 3
            self.assertEqual(3, priced["gates_held"])
            expected = max(priced["route_revenue_basis"] * emergency_gate_fee_rate() * 3, floor)
            self.assertAlmostEqual(expected, priced["fee_amount"], places=2)
            self.assertGreaterEqual(priced["fee_amount"], floor,
                                    "the auction minimum is the floor")

    def test_paying_grants_a_permanent_unit_and_stops_the_penalty(self):
        with fresh_game() as g:
            from engine.gates import (_allocated_gates, accrue_gate_shortfall_penalties,
                                      record_gate_shortfall, resolve_gate_shortfall)
            from engine.setup import get_airline
            self._setup(g.db)
            ev = record_gate_shortfall(self.AP, 3, 2, {("BOS", self.AP)})
            cash0 = float(get_airline()["cash"])
            out = resolve_gate_shortfall(ev["event_id"], accept=True)
            self.assertEqual("PAID", out["status"])
            self.assertAlmostEqual(float(ev["fee_amount"]), cash0 - float(get_airline()["cash"]),
                                   places=2, msg="the fee must actually leave cash")
            self.assertEqual(2, _allocated_gates(self.AP, "PLAYER"),
                             "paying adds a unit the player keeps")
            self.assertEqual(0.0, accrue_gate_shortfall_penalties(20)["charged"],
                             "a paid shortfall must not keep charging")

    def test_declining_accrues_daily_and_is_replay_safe(self):
        with fresh_game() as g:
            from engine.gates import (accrue_gate_shortfall_penalties, record_gate_shortfall,
                                      resolve_gate_shortfall)
            import uuid as _uuid
            self._setup(g.db)
            # Real rows on a real route, so the daily re-check sees a genuine shortfall.
            rid = g.open_route(g.hub, self.AP)
            for i, tail in enumerate(("N001", "N002")):
                g.db.execute(
                    """INSERT INTO flight_segments
                       (segment_id, game_week, day_of_week, tail_number, route_id,
                        origin_iata, dest_iata, flight_number, scheduled_dep_time,
                        scheduled_arr_time, scheduled_dep_game_hour, scheduled_arr_game_hour,
                        baseline_dep_game_hour, baseline_arr_game_hour, status,
                        pax_business, pax_leisure, revenue_gross, excise_tax, segment_fee,
                        security_fee, pfc_fee, landing_fee, gate_fee, fuel_cost,
                        net_contribution)
                       VALUES (?,?,'MON',?,?,?,?,?,'08:00','09:30',?,?,?,?,'SCHEDULED',
                               0,0,0,0,0,0,0,0,0,0,0)""",
                    (str(_uuid.uuid4()), 3, tail, rid, g.hub, self.AP, f"T{i}",
                     400.0 + i * 0.1, 402.0 + i * 0.1, 400.0 + i * 0.1, 402.0 + i * 0.1),
                )
            from engine.gates import gate_shortfall_penalty_schedule

            ev = record_gate_shortfall(self.AP, 3, 2, {("BOS", self.AP)})
            sched = gate_shortfall_penalty_schedule(
                float(ev["fee_amount"]), int(ev["penalty_days_total"]))
            resolve_gate_shortfall(ev["event_id"], accept=False)

            # The charge ramps: day two costs more than day one.
            self.assertAlmostEqual(sched[0], accrue_gate_shortfall_penalties(14)["charged"], places=2)
            self.assertAlmostEqual(sched[1], accrue_gate_shortfall_penalties(15)["charged"], places=2)
            self.assertEqual(0.0, accrue_gate_shortfall_penalties(15)["charged"],
                             "the same game day must never be charged twice")
            row = g.db.fetch_one(
                "SELECT penalty_days_charged d, penalty_accrued a FROM gate_shortfall_events"
                " WHERE event_id = ?", (ev["event_id"],))
            self.assertEqual(2, int(row["d"]))
            self.assertAlmostEqual(sched[0] + sched[1], float(row["a"]), places=2)

    def test_winning_a_gate_closes_the_shortfall_without_the_player_acting(self):
        """Fixing the underlying problem should stop the bleeding on its own."""
        with fresh_game() as g:
            from engine.gates import (accrue_gate_shortfall_penalties, record_gate_shortfall,
                                      resolve_gate_shortfall)
            self._setup(g.db)
            ev = record_gate_shortfall(self.AP, 3, 2, {("BOS", self.AP)})
            resolve_gate_shortfall(ev["event_id"], accept=False)
            g.db.execute(
                "UPDATE airport_gate_allocations SET gate_units = 2"
                " WHERE airport_iata = ? AND holder_id = 'PLAYER'", (self.AP,))
            self.assertEqual(0.0, accrue_gate_shortfall_penalties(14)["charged"])
            self.assertEqual(
                "RESOLVED",
                g.db.fetch_one("SELECT status FROM gate_shortfall_events WHERE event_id = ?",
                               (ev["event_id"],))["status"])

    def test_week_totals_feed_the_ledger(self):
        with fresh_game() as g:
            from engine.gates import (gate_shortfall_week_totals, record_gate_shortfall,
                                      resolve_gate_shortfall)
            self._setup(g.db)
            ev = record_gate_shortfall(self.AP, 3, 2, {("BOS", self.AP)})
            resolve_gate_shortfall(ev["event_id"], accept=True)
            totals = gate_shortfall_week_totals(3)
            self.assertAlmostEqual(float(ev["fee_amount"]),
                                   totals["emergency_gate_fees"], places=2)
            self.assertEqual(0.0, totals["gate_shortfall_penalties"])


class TestSpawnFailureIsVisible(unittest.TestCase):
    """A failed week-roll spawn must announce itself, not look like deleted schedules."""

    def test_spawn_failure_writes_a_notification(self):
        import engine.settlement as S

        with fresh_game() as g:
            real = S.spawn_rotation_segments_for_week if hasattr(S, "spawn_rotation_segments_for_week") else None
            import engine.scheduling as SCH
            original = SCH.spawn_rotation_segments_for_week

            def boom(_gw):
                raise RuntimeError("database is locked")

            SCH.spawn_rotation_segments_for_week = boom
            try:
                out = S.spawn_segments_for_calendar_week(3)
            finally:
                SCH.spawn_rotation_segments_for_week = original
                if real is not None:
                    S.spawn_rotation_segments_for_week = real

            self.assertIn("spawn_error", out, "the failure must still be reported to the caller")
            notes = g.db.fetch_all(
                "SELECT body FROM player_notifications WHERE type = 'SPAWN_FAILURE' AND game_week = 3"
            )
            self.assertTrue(notes, "a failed spawn must leave a durable notification")
            body = str(notes[0]["body"])
            self.assertIn("database is locked", body, "the cause must be recorded for diagnosis")
            self.assertIn("intact", body, "tell the player their rotations are not lost")

    def test_successful_spawn_writes_no_failure_notification(self):
        import engine.settlement as S

        with fresh_game() as g:
            S.spawn_segments_for_calendar_week(3)
            self.assertEqual(
                [],
                g.db.fetch_all("SELECT 1 FROM player_notifications WHERE type = 'SPAWN_FAILURE'"),
                "a clean spawn must stay silent",
            )


class TestWeekRollSkippedOnRestart(unittest.TestCase):
    """A server started inside a week whose roll already fired must still get flights.

    GameClock sets last_week = int(ghe // 168) + 1, and on_week only fires when the
    calendar week exceeds it. Starting inside week N therefore never fires on_week(N),
    so spawn_segments_for_calendar_week(N) never runs. Nothing raises — the step simply
    does not happen — which is why no error handler ever caught it.
    """

    def test_clock_started_inside_a_week_does_not_fire_that_week_roll(self):
        from engine.clock import GameClock

        with fresh_game() as g:
            g.db.execute("UPDATE game_state SET game_hours_elapsed = ?, game_week = ? WHERE id = 1",
                         (700.0, 5))
            clk = GameClock()
            clk.running = False
            self.assertEqual(5, clk.last_week,
                             "last_week initialises to the week already in progress")
            clk.game_hours_elapsed = 700.0
            fired = clk._collect_milestones_locked()
            self.assertEqual([], [k for k, _ in fired if k == "week"],
                             "the in-progress week's roll can never fire — hence the safety net")

    def test_web_bootstrap_heals_a_week_with_no_player_flights(self):
        import server.game_http as H

        with fresh_game() as g:
            tail = g.lease()
            # A non-auctioned spoke, so the rotation needs no gate allocation.
            g.open_route(g.hub, "BUF")
            g.open_route("BUF", g.hub)
            from engine.scheduling import assign_rotation
            assign_rotation(tail, [f"{g.hub}-BUF", f"BUF-{g.hub}"])
            g.db.execute("DELETE FROM flight_segments WHERE game_week = 3")
            g.db.execute("UPDATE game_state SET game_week = 3, game_hours_elapsed = 350 WHERE id = 1")
            H._player_spawn_healed_week = None

            self.assertEqual(0, g.db.fetch_one(
                "SELECT COUNT(*) n FROM flight_segments WHERE game_week=3")["n"])
            H._heal_player_segments_if_missing()
            after = g.db.fetch_one("SELECT COUNT(*) n FROM flight_segments WHERE game_week=3")["n"]
            self.assertGreater(after, 0, "a week with templates but no flights must be rebuilt")

            H._heal_player_segments_if_missing()
            self.assertEqual(after, g.db.fetch_one(
                "SELECT COUNT(*) n FROM flight_segments WHERE game_week=3")["n"],
                "the once-per-week guard must make repeat calls free")


class TestWeatherClosureDeadlock(unittest.TestCase):
    """Regression: an active weather closure deadlocked the clock thread on itself.

    `weather_closure_hits_active_flights` held the non-reentrant `_closed_lock` and called
    `is_airport_closed_at` from inside a comprehension, which acquired the same lock. The
    clock thread hung permanently and the callback worker piled up behind it in
    `expire_closures_before`. It only triggered while a closure was active, which is why
    it presented as the game freezing at an arbitrary moment.
    """

    def test_active_closure_does_not_hang_the_caller(self):
        import threading as _th
        import engine.environment as env

        with fresh_game():
            env.clear_all_closures()
            env.set_closure_until("BOS", 9_999_999.0)
            try:
                done = _th.Event()
                err = []

                def call():
                    try:
                        env.weather_closure_hits_active_flights()
                    except Exception as e:      # pragma: no cover - surfaced via err
                        err.append(e)
                    finally:
                        done.set()

                t = _th.Thread(target=call, daemon=True)
                t.start()
                self.assertTrue(
                    done.wait(timeout=10),
                    "weather_closure_hits_active_flights deadlocked with a closure active",
                )
                self.assertEqual([], err)
            finally:
                env.clear_all_closures()

    def test_closure_lock_is_reentrant(self):
        """Nine other call sites share this lock; reentrancy stops the same mistake."""
        import engine.environment as env

        with env._closed_lock:
            with env._closed_lock:
                pass

    def test_closed_set_still_correct_with_a_closure_active(self):
        """The lock fix must not change which airports read as closed."""
        import engine.environment as env

        with fresh_game():
            env.clear_all_closures()
            try:
                env.set_closure_until("BOS", 500.0)
                self.assertTrue(env.is_airport_closed_at("BOS", 400.0))
                self.assertFalse(env.is_airport_closed_at("BOS", 600.0))
                self.assertFalse(env.is_airport_closed_at("ORD", 400.0))
            finally:
                env.clear_all_closures()


class TestFleetDisposalApi(unittest.TestCase):
    """The endpoints the Fleet window drives (FEATURE-01 UI)."""

    def _ready_tail(self, g):
        """A tail at the hub with no remaining flights."""
        tail = g.lease()
        g.db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
        g.db.execute("UPDATE fleet SET current_airport_iata = ? WHERE tail_number = ?",
                     (g.hub, tail))
        return tail

    def test_quote_prices_a_lease_return_and_a_sale(self):
        from server import game_api as api

        with fresh_game() as g:
            leased = self._ready_tail(g)
            q = api.fleet_disposal_quote(leased)
            self.assertTrue(q["ok"])
            self.assertEqual("RETURN_LEASE", q["kind"])
            self.assertGreater(float(q["penalty"]), 0.0, "early return costs something")

            owned = g.buy()
            g.db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (owned,))
            q2 = api.fleet_disposal_quote(owned)
            self.assertEqual("SELL", q2["kind"])
            self.assertGreater(float(q2["valuation"]["estimated_value"]), 0.0)

    def test_queue_then_cancel_round_trip(self):
        from server import game_api as api

        with fresh_game() as g:
            tail = self._ready_tail(g)
            self.assertTrue(api.dispose_aircraft({"tail_number": tail})["ok"])
            row = g.db.fetch_one("SELECT pending_disposal p FROM fleet WHERE tail_number = ?", (tail,))
            self.assertEqual("RETURN_LEASE", row["p"])

            # The Fleet list drives the pending badge off this field.
            listed = [r for r in api.list_fleet()["fleet"] if r["tail_number"] == tail][0]
            self.assertEqual("RETURN_LEASE", listed["pending_disposal"])

            self.assertTrue(api.cancel_aircraft_disposal({"tail_number": tail})["ok"])
            self.assertIsNone(
                g.db.fetch_one("SELECT pending_disposal p FROM fleet WHERE tail_number = ?", (tail,))["p"]
            )

    def test_blockers_do_not_prevent_disposal(self):
        """Regression on the UI contract: blockers are advisory, not a gate.

        request_disposal cancels the rotation and ferries the aircraft home itself, so a
        tail away from base with flights still to operate can be disposed of. An earlier
        draft of the Fleet window disabled the confirm button whenever `blockers` was
        non-empty, which refused an action the engine supports.
        """
        from server import game_api as api

        with fresh_game() as g:
            tail = g.lease()
            g.db.execute("UPDATE fleet SET current_airport_iata = 'LAX' WHERE tail_number = ?", (tail,))
            self.assertTrue(api.fleet_disposal_quote(tail)["blockers"],
                            "away from base should report a blocker")
            out = api.dispose_aircraft({"tail_number": tail})
            self.assertTrue(out["ok"], "the engine handles the ferry home; do not refuse this")

    def test_double_queue_is_rejected(self):
        from server import game_api as api

        with fresh_game() as g:
            tail = self._ready_tail(g)
            self.assertTrue(api.dispose_aircraft({"tail_number": tail})["ok"])
            second = api.dispose_aircraft({"tail_number": tail})
            self.assertFalse(second["ok"], "already queued must be refused")
            self.assertIn("already", str(second.get("error", "")).lower())


class TestGateSale(unittest.TestCase):
    """Selling a stand back — the opposite lever to the emergency purchase."""

    AP = "ORD"

    def _hold(self, db, units, *, price=8000.0, eff=1):
        import uuid as _uuid
        db.execute("DELETE FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER'",
                   (self.AP,))
        db.execute(
            """INSERT INTO airport_gate_allocations
               (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                scheduled_this_week, below_threshold_weeks, effective_week, status,
                price_paid_per_unit, pending_sale_units)
               VALUES (?, ?, 'PLAYER', ?, 0, 0, 0, ?, 'ACTIVE', ?, 0)""",
            (str(_uuid.uuid4()), self.AP, int(units), int(eff), float(price)),
        )

    def test_quote_prices_from_what_was_paid(self):
        from engine.gates import gate_sale_haircut, gate_sale_quote

        with fresh_game() as g:
            self._hold(g.db, 2, price=8000.0)
            q = gate_sale_quote(self.AP, 1)
            self.assertEqual(2, q["gates_held"])
            self.assertEqual(1, q["gates_after"])
            self.assertAlmostEqual(8000.0 * gate_sale_haircut(), q["proceeds"], places=2)

    def test_untracked_units_fall_back_to_the_auction_floor(self):
        """Stands predating price tracking have no cost recorded."""
        from engine.gates import gate_sale_haircut, gate_sale_quote

        with fresh_game() as g:
            self._hold(g.db, 2, price=0.0)
            floor = float(g.db.get_financial_constant("gate_min_price_per_unit") or 5000)
            self.assertAlmostEqual(floor * gate_sale_haircut(),
                                   gate_sale_quote(self.AP, 1)["proceeds"], places=2)

    def test_last_stand_is_refused_while_flights_use_it(self):
        """At zero units the spawn hard-blocks with no penalty path, so this must never happen."""
        from engine.gates import gate_sale_blockers, request_gate_sale
        import uuid as _uuid

        with fresh_game() as g:
            self._hold(g.db, 1)
            rid = g.open_route(g.hub, self.AP)
            g.db.execute(
                """INSERT INTO flight_segments
                   (segment_id, game_week, day_of_week, tail_number, route_id, origin_iata,
                    dest_iata, flight_number, scheduled_dep_time, scheduled_arr_time,
                    scheduled_dep_game_hour, scheduled_arr_game_hour, baseline_dep_game_hour,
                    baseline_arr_game_hour, status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee,
                    fuel_cost, net_contribution)
                   VALUES (?,1,'MON','N1',?,?,?,'T1','08:00','09:30',10.0,11.5,10.0,11.5,
                           'SCHEDULED',0,0,0,0,0,0,0,0,0,0,0)""",
                (str(_uuid.uuid4()), rid, g.hub, self.AP),
            )
            blockers = gate_sale_blockers(self.AP, 1)
            self.assertTrue(any("no allocation" in b.lower() for b in blockers), blockers)
            with self.assertRaises(ValueError):
                request_gate_sale(self.AP, 1)

    def test_peak_above_the_remainder_is_refused(self):
        from engine.gates import gate_sale_blockers

        with fresh_game() as g:
            self._hold(g.db, 2)
            import engine.gates as G
            real = G.player_gate_peak_at_airport
            G.player_gate_peak_at_airport = lambda ap, w, **k: 2 if str(ap) == self.AP else 0
            try:
                blockers = gate_sale_blockers(self.AP, 1)
            finally:
                G.player_gate_peak_at_airport = real
            self.assertTrue(any("concurrent stands" in b for b in blockers), blockers)

    def test_home_hub_last_stand_is_protected(self):
        from engine.gates import gate_sale_blockers

        with fresh_game() as g:
            self.AP = g.hub
            self._hold(g.db, 1)
            self.assertTrue(any("home hub" in b for b in gate_sale_blockers(g.hub, 1)))

    def test_not_yet_effective_stands_cannot_be_sold(self):
        from engine.gates import gate_sale_blockers

        with fresh_game() as g:
            g.db.execute("UPDATE game_state SET game_week = 3 WHERE id = 1")
            self._hold(g.db, 2, eff=4)
            self.assertTrue(any("effective" in b for b in gate_sale_blockers(self.AP, 1)))

    def test_queue_execute_and_cancel(self):
        from engine.gates import (_allocated_gates, cancel_gate_sale, process_pending_gate_sales,
                                  request_gate_sale)
        from engine.setup import get_airline

        with fresh_game() as g:
            self._hold(g.db, 2, price=8000.0)
            request_gate_sale(self.AP, 1)
            self.assertEqual(2, _allocated_gates(self.AP, "PLAYER"),
                             "queuing must not remove the stand yet")

            cancel_gate_sale(self.AP)
            self.assertEqual({"units_sold": 0, "proceeds": 0.0, "held": []},
                             process_pending_gate_sales(),
                             "a cancelled sale must not execute")

            request_gate_sale(self.AP, 1)
            cash0 = float(get_airline()["cash"])
            out = process_pending_gate_sales()
            self.assertEqual(1, out["units_sold"])
            self.assertEqual(1, _allocated_gates(self.AP, "PLAYER"))
            self.assertAlmostEqual(out["proceeds"], float(get_airline()["cash"]) - cash0, places=2)

    def test_sale_is_held_if_the_new_week_needs_the_stand(self):
        """Re-validated on execution: a schedule grown since queuing must veto the sale."""
        from engine.gates import (_allocated_gates, process_pending_gate_sales,
                                  request_gate_sale)
        import engine.gates as G

        with fresh_game() as g:
            self._hold(g.db, 2)
            request_gate_sale(self.AP, 1)
            real = G.player_gate_peak_at_airport
            G.player_gate_peak_at_airport = lambda ap, w, **k: 2 if str(ap) == self.AP else 0
            try:
                out = process_pending_gate_sales()
            finally:
                G.player_gate_peak_at_airport = real
            self.assertEqual(0, out["units_sold"])
            self.assertIn(self.AP, out["held"])
            self.assertEqual(2, _allocated_gates(self.AP, "PLAYER"),
                             "a held sale must leave the stand with the player")

    def _insert_dupes(self, g, n=2):
        """Recreate the old broken state, which the unique index now forbids."""
        import uuid as _uuid
        g.db.execute("DROP INDEX IF EXISTS idx_gate_alloc_holder_airport_active")
        g.db.execute("DELETE FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER'",
                     (self.AP,))
        for _ in range(n):
            g.db.execute(
                """INSERT INTO airport_gate_allocations
                   (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                    scheduled_this_week, below_threshold_weeks, effective_week, status)
                   VALUES (?, ?, 'PLAYER', 1, 0, 0, 0, 1, 'ACTIVE')""",
                (str(_uuid.uuid4()), self.AP),
            )

    def test_duplicate_rows_are_summed_not_read_first(self):
        """Regression: two ACTIVE rows made _allocated_gates report only the first.

        The live save had exactly this at CDG — two rows of one unit each — so the game
        credited one stand against two paid for. Schedules were refused, and under
        FEATURE-05 a shortfall could charge for a stand already owned.
        """
        from engine.gates import _allocated_gates

        with fresh_game() as g:
            self._insert_dupes(g)
            self.assertEqual(2, _allocated_gates(self.AP, "PLAYER"),
                             "the sum, not the first row, is what the player holds")

    def test_migration_merges_duplicates(self):
        with fresh_game() as g:
            self._insert_dupes(g)
            g.db._merge_duplicate_gate_allocations()
            rows = g.db.fetch_all(
                "SELECT gate_units FROM airport_gate_allocations WHERE airport_iata=?"
                " AND holder_id='PLAYER' AND status='ACTIVE'", (self.AP,))
            self.assertEqual(1, len(rows), "duplicates must collapse into one row")
            self.assertEqual(2, int(rows[0]["gate_units"]), "units must be preserved")

    def test_duplicates_cannot_be_created_any_more(self):
        """The unique index makes the broken state unrepresentable going forward."""
        import sqlite3
        import uuid as _uuid

        with fresh_game() as g:
            self._hold(g.db, 1)
            with self.assertRaises(sqlite3.IntegrityError):
                g.db.execute(
                    """INSERT INTO airport_gate_allocations
                       (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                        scheduled_this_week, below_threshold_weeks, effective_week, status)
                       VALUES (?, ?, 'PLAYER', 1, 0, 0, 0, 1, 'ACTIVE')""",
                    (str(_uuid.uuid4()), self.AP),
                )


class TestGateAuctionDoubleAward(unittest.TestCase):
    """Regression: a 1-unit bid could win 2 stands and be charged twice.

    `resolve_closing_gate_auctions` filtered on status='OPEN', but awarding and marking
    RESOLVED were separate commits, so two callers could both read OPEN and both award.
    `player_gate_bids()` calls `resolve_overdue_gate_auctions()` on the HTTP thread every
    time the Gates window polls — outside `_settlement_lock` — so a player with that
    window open at the week roll raced the settlement thread. The live save showed ARN,
    CDG, ZRH and SVO each with 2 stands from 1-unit bids, two award notifications apiece,
    and $16,000 charged for one requested stand.
    """

    AP = "ORD"

    def _auction_with_player_bid(self, g, week=1, units_available=10):
        import uuid as _uuid
        aid = str(_uuid.uuid4())
        g.db.execute("DELETE FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER'",
                     (self.AP,))
        g.db.execute(
            """INSERT INTO airport_gate_auctions
               (auction_id, airport_iata, opens_week, closes_week, units_available,
                current_price_per_unit, status)
               VALUES (?, ?, ?, ?, ?, 8000.0, 'OPEN')""",
            (aid, self.AP, week, week, int(units_available)),
        )
        g.db.execute(
            """INSERT INTO airport_gate_bids
               (bid_id, auction_id, bidder_id, units_requested, price_per_unit, submitted_week)
               VALUES (?, ?, 'PLAYER', 1, 8000.0, ?)""",
            (str(_uuid.uuid4()), aid, week),
        )
        return aid

    def test_sequential_double_resolution_awards_once(self):
        from engine.gates import _allocated_gates, resolve_closing_gate_auctions

        with fresh_game() as g:
            self._auction_with_player_bid(g)
            resolve_closing_gate_auctions(1)
            resolve_closing_gate_auctions(1)
            self.assertEqual(1, _allocated_gates(self.AP, "PLAYER"),
                             "a 1-unit bid must yield exactly 1 stand")

    def test_concurrent_resolution_awards_once(self):
        """Two threads, as the settlement thread and the Gates poll actually raced."""
        import threading as _th
        from engine.gates import _allocated_gates, resolve_closing_gate_auctions
        from engine.setup import get_airline

        with fresh_game() as g:
            self._auction_with_player_bid(g)
            cash0 = float(get_airline()["cash"])
            start = _th.Barrier(2)

            def run():
                start.wait()
                try:
                    resolve_closing_gate_auctions(1)
                except Exception:
                    pass

            ts = [_th.Thread(target=run) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=20)

            self.assertEqual(1, _allocated_gates(self.AP, "PLAYER"),
                             "concurrent resolvers must not both award")
            spent = cash0 - float(get_airline()["cash"])
            self.assertAlmostEqual(8000.0, spent, delta=1.0,
                                   msg="the player must be charged once, not twice")
            rows = g.db.fetch_all(
                "SELECT allocation_id FROM airport_gate_allocations WHERE airport_iata=?"
                " AND holder_id='PLAYER' AND status='ACTIVE'", (self.AP,))
            self.assertEqual(1, len(rows), "concurrent inserts must not duplicate the row")

    def test_claim_is_exclusive(self):
        from engine.gates import _claim_auction

        with fresh_game() as g:
            aid = self._auction_with_player_bid(g)
            self.assertTrue(_claim_auction(aid), "first caller wins the auction")
            self.assertFalse(_claim_auction(aid), "second caller must be refused")

    def test_one_award_notification_per_auction(self):
        from engine.gates import resolve_closing_gate_auctions

        with fresh_game() as g:
            self._auction_with_player_bid(g)
            resolve_closing_gate_auctions(1)
            resolve_closing_gate_auctions(1)
            notes = g.db.fetch_all(
                "SELECT body FROM player_notifications WHERE type='GATE_AUCTION' AND body LIKE ?",
                (f"%{self.AP}%",))
            won = [n for n in notes if "WON" in str(n["body"])]
            self.assertEqual(1, len(won), f"one award, one notification; got {len(won)}")


class TestNoAutoFerryHome(unittest.TestCase):
    """Clearing a plan must never reposition the aircraft on the player's behalf.

    A rotation may deliberately begin away from the hub, so an aircraft parked at an
    outstation is a valid state. The old `cancel_rotation` ended with an unconditional
    `schedule_ferry_to_hub`, which also made the last ferry undeletable: clearing it
    simply created another.
    """

    def _away(self, g, tail, where="SFO"):
        import uuid as _uuid
        rid = g.open_route(g.hub, where)
        g.db.execute(
            """INSERT INTO flight_segments
               (segment_id, game_week, day_of_week, tail_number, route_id, origin_iata,
                dest_iata, flight_number, scheduled_dep_time, scheduled_arr_time,
                scheduled_dep_game_hour, scheduled_arr_game_hour, baseline_dep_game_hour,
                baseline_arr_game_hour, status, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee,
                fuel_cost, net_contribution)
               VALUES (?,1,'MON',?,?,?,?,'F1','08:00','11:00',8.0,11.0,8.0,11.0,
                       'LANDED',0,0,0,0,0,0,0,0,0,0,0)""",
            (str(_uuid.uuid4()), tail, rid, g.hub, where),
        )
        g.db.execute("UPDATE game_state SET game_hours_elapsed = 20.0 WHERE id = 1")
        return where

    def test_clear_does_not_create_a_ferry(self):
        from engine.scheduling import cancel_rotation

        with fresh_game() as g:
            tail = g.lease()
            self._away(g, tail)
            cancel_rotation(tail, wipe_completed_this_week=False)
            ferries = g.db.fetch_all(
                "SELECT 1 FROM flight_segments WHERE tail_number = ? AND is_ferry = 1", (tail,))
            self.assertEqual([], ferries, "clearing must not reposition the aircraft")

    def test_repeated_clear_stays_empty(self):
        """The loop: each clear used to re-create the ferry it had just removed."""
        from engine.scheduling import cancel_rotation

        with fresh_game() as g:
            tail = g.lease()
            self._away(g, tail)
            for _ in range(3):
                cancel_rotation(tail, wipe_completed_this_week=False)
                self.assertEqual(
                    [], g.db.fetch_all(
                        "SELECT 1 FROM flight_segments WHERE tail_number = ? AND is_ferry = 1", (tail,)),
                    "no ferry may reappear on any clear")

    def test_explicit_reposition_still_works(self):
        """Removing the automatic ferry must not remove the deliberate one."""
        from engine.scheduling import schedule_ferry_to_hub

        with fresh_game() as g:
            tail = g.lease()
            self._away(g, tail)
            out = schedule_ferry_to_hub(tail, g.hub)
            self.assertIsNotNone(out, "a player-initiated reposition must still be possible")
            self.assertEqual(g.hub, str(out["to"]).upper())


class TestTailLocationReconciliation(unittest.TestCase):
    """One aircraft, one position. The two records used to drift and contradict."""

    def test_stale_fleet_column_is_corrected_from_history(self):
        from engine.scheduling.ferry import reconcile_tail_location

        with fresh_game() as g:
            tail = g.lease()
            TestNoAutoFerryHome()._away(g, tail, "SFO")
            # A leg can reach LANDED without on_arrival running (settlement catch-up),
            # leaving this column behind.
            g.db.execute("UPDATE fleet SET current_airport_iata = 'MCO' WHERE tail_number = ?", (tail,))
            self.assertEqual("SFO", reconcile_tail_location(tail))
            self.assertEqual("SFO", g.db.fetch_one(
                "SELECT current_airport_iata a FROM fleet WHERE tail_number = ?", (tail,))["a"])

    def test_position_lookup_ignores_the_week_tag(self):
        """A leg departing one week and arriving the next carries the departure's week.

        Filtering on `game_week` hid such a leg from the positioning validator while
        `tail_position_and_free_hour` still saw it, so the two disagreed and repositioning
        was refused for a tail whose own schedule was consistent.
        """
        import uuid as _uuid
        from engine.scheduling.rotation import _initial_airport_before_first_event

        with fresh_game() as g:
            tail = g.lease()
            rid = g.open_route(g.hub, "SFO")
            # Departs hour 836 (week 5), arrives 841 (week 6), tagged week 5.
            g.db.execute(
                """INSERT INTO flight_segments
                   (segment_id, game_week, day_of_week, tail_number, route_id, origin_iata,
                    dest_iata, flight_number, scheduled_dep_time, scheduled_arr_time,
                    scheduled_dep_game_hour, scheduled_arr_game_hour, baseline_dep_game_hour,
                    baseline_arr_game_hour, status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee,
                    fuel_cost, net_contribution)
                   VALUES (?,5,'MON',?,?,?,'SFO','F9','08:00','13:00',836.0,841.09,836.0,841.09,
                           'LANDED',0,0,0,0,0,0,0,0,0,0,0)""",
                (str(_uuid.uuid4()), tail, rid, g.hub),
            )
            # Asked about week 6, the week-5-tagged leg must still count.
            self.assertEqual("SFO", _initial_airport_before_first_event(tail, 6, 900.0),
                             "position is a function of absolute time, not of the week label")


class TestGateShortfallEscalatingPenalty(unittest.TestCase):
    """Declining must never be the cheap option.

    A flat daily rate made waiting out the week cost 70% of the outright price, so the
    purchase branch was never worth taking. The charge now ramps so the week's total is
    the outright price — and since declining ends with no stand, buying dominates.
    """

    def test_schedule_sums_to_the_outright_price(self):
        from engine.gates import gate_shortfall_penalty_schedule

        for days in (1, 2, 4, 7, 9):
            sched = gate_shortfall_penalty_schedule(100_000.0, days)
            self.assertEqual(days, len(sched))
            self.assertAlmostEqual(100_000.0, sum(sched), delta=1.0,
                                   msg=f"{days}-day ramp must total the buy price")

    def test_schedule_escalates(self):
        from engine.gates import gate_shortfall_penalty_schedule

        sched = gate_shortfall_penalty_schedule(680_742.0, 7)
        for a, b in zip(sched, sched[1:]):
            self.assertLess(a, b, "each day must cost more than the last")
        self.assertLess(sched[0], 680_742.0 / 7, "day one is cheaper than a flat split")

    def test_multiplier_makes_declining_strictly_worse(self):
        from engine.gates import gate_shortfall_penalty_schedule

        with fresh_game() as g:
            g.db.execute(
                "INSERT OR REPLACE INTO financial_constants (key, value) VALUES (?, ?)",
                ("gate_shortfall_penalty_total_multiplier", 1.5))
            self.assertAlmostEqual(
                150_000.0, sum(gate_shortfall_penalty_schedule(100_000.0, 7)), delta=1.0)

    def test_detected_late_in_the_week_still_totals_the_price(self):
        """Fewer days left means steeper days, not a discount for being caught late."""
        from engine.gates import gate_shortfall_penalty_schedule

        late = gate_shortfall_penalty_schedule(680_742.0, 2)
        early = gate_shortfall_penalty_schedule(680_742.0, 7)
        self.assertAlmostEqual(sum(late), sum(early), delta=1.0)
        self.assertGreater(late[0], early[0], "a late shortfall bites harder per day")

    def test_accrual_follows_the_ramp(self):
        from engine.gates import (accrue_gate_shortfall_penalties, gate_shortfall_penalty_schedule,
                                  record_gate_shortfall, resolve_gate_shortfall)
        import engine.gates as G

        with fresh_game() as g:
            import uuid as _uuid
            g.db.execute(
                """INSERT INTO airport_gate_allocations
                   (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                    scheduled_this_week, below_threshold_weeks, effective_week, status)
                   VALUES (?, 'ORD', 'PLAYER', 1, 0, 0, 0, 1, 'ACTIVE')""",
                (str(_uuid.uuid4()),))
            ev = record_gate_shortfall("ORD", 1, 2, {("BOS", "ORD")})
            resolve_gate_shortfall(ev["event_id"], accept=False)
            sched = gate_shortfall_penalty_schedule(
                float(ev["fee_amount"]), int(ev["penalty_days_total"]))

            real = G.player_gate_peak_at_airport
            G.player_gate_peak_at_airport = lambda ap, w, **k: 2 if str(ap) == "ORD" else 0
            try:
                charged = [accrue_gate_shortfall_penalties(d)["charged"] for d in (1, 2, 3)]
            finally:
                G.player_gate_peak_at_airport = real
            for i, amount in enumerate(charged):
                self.assertAlmostEqual(sched[i], amount, delta=1.0,
                                       msg=f"day {i + 1} must charge the ramped amount")
            self.assertLess(charged[0], charged[-1], "the charge escalates day over day")


class TestShortfallClosesWhenFixed(unittest.TestCase):
    """A shortfall the player has already fixed must stop asking for a decision.

    Closing it used to depend entirely on the clock's daily hook, and that hook was wired
    in main.py but NOT in the web server — so under the UI it never ran: shortfalls never
    resolved and no penalty was ever charged either. The prompt kept offering a choice
    that no longer existed.
    """

    AP = "ORD"

    def _event_with_gates(self, g, units):
        import uuid as _uuid
        from engine.gates import record_gate_shortfall
        g.db.execute("DELETE FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER'",
                     (self.AP,))
        g.db.execute(
            """INSERT INTO airport_gate_allocations
               (allocation_id, airport_iata, holder_id, gate_units, used_this_week,
                scheduled_this_week, below_threshold_weeks, effective_week, status)
               VALUES (?, ?, 'PLAYER', ?, 0, 0, 0, 1, 'ACTIVE')""",
            (str(_uuid.uuid4()), self.AP, int(units)))
        # Two tails on the stand at once, so the peak is genuinely 2 and the shortfall
        # is real rather than instantly self-clearing.
        rid = g.open_route(g.hub, self.AP)
        for i, tail in enumerate(("N001", "N002")):
            g.db.execute(
                """INSERT INTO flight_segments
                   (segment_id, game_week, day_of_week, tail_number, route_id, origin_iata,
                    dest_iata, flight_number, scheduled_dep_time, scheduled_arr_time,
                    scheduled_dep_game_hour, scheduled_arr_game_hour, baseline_dep_game_hour,
                    baseline_arr_game_hour, status, pax_business, pax_leisure, revenue_gross,
                    excise_tax, segment_fee, security_fee, pfc_fee, landing_fee, gate_fee,
                    fuel_cost, net_contribution)
                   VALUES (?,1,'MON',?,?,?,?,?,'08:00','09:30',?,?,?,?,'SCHEDULED',
                           0,0,0,0,0,0,0,0,0,0,0)""",
                (str(_uuid.uuid4()), tail, rid, g.hub, self.AP, f"T{i}",
                 10.0 + i * 0.1, 12.0 + i * 0.1, 10.0 + i * 0.1, 12.0 + i * 0.1))
        return record_gate_shortfall(self.AP, 1, 2, {("BOS", self.AP)})

    def test_buying_a_stand_elsewhere_closes_the_prompt(self):
        from engine.gates import open_gate_shortfalls

        with fresh_game() as g:
            ev = self._event_with_gates(g, 1)
            self.assertEqual(1, len(open_gate_shortfalls()), "opens while short")
            # Player acquires the stand by another route (auction win, grant, or purchase).
            g.db.execute("UPDATE airport_gate_allocations SET gate_units = 2"
                         " WHERE airport_iata = ? AND holder_id = 'PLAYER'", (self.AP,))
            self.assertEqual([], open_gate_shortfalls(),
                             "a fixed shortfall must not keep prompting")
            self.assertEqual("RESOLVED", g.db.fetch_one(
                "SELECT status s FROM gate_shortfall_events WHERE event_id = ?",
                (ev["event_id"],))["s"])
            self.assertEqual(0.0, float(g.db.fetch_one(
                "SELECT penalty_accrued a FROM gate_shortfall_events WHERE event_id = ?",
                (ev["event_id"],))["a"]), "nothing may be charged for a shortfall never suffered")

    def test_web_server_wires_the_daily_hook(self):
        """The accrual hook must exist on the web path, not only in main.py."""
        import inspect
        import server.game_http as H

        self.assertTrue(hasattr(H, "_on_day_roll"), "web server needs a daily handler")
        src = inspect.getsource(H._start_runtime_clock)
        self.assertIn("on_day=", src, "the daily hook must be passed to start_game_clock")
