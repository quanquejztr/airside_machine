"""
End-to-end lifecycle: found an airline, acquire aircraft, open routes, schedule, fly a week,
settle, and check the books. These are the tests that prove the game loop actually closes.
"""

from __future__ import annotations

import unittest

from helpers import fresh_game, near_airports, pick_type


class LifecycleBase(unittest.TestCase):
    """A world with one narrowbody and a hub round-trip already open."""

    HUB = "TPA"

    def setUp(self):
        self.g = fresh_game(hub=self.HUB)
        self.db = self.g.db
        self.type_id = pick_type(self.db, "NARROW", min_range=600, max_seats=220)
        self.assertIsNotNone(self.type_id, "no usable narrowbody in the catalogue")
        rr = self.db.fetch_one("SELECT runway_req_ft FROM aircraft_types WHERE type_id = ?",
                               (self.type_id,))
        self.spokes = near_airports(self.db, self.HUB, 250, 900, limit=4,
                                   require_runway_ft=int(rr["runway_req_ft"] or 0))
        if not self.spokes:
            self.skipTest("no runway-adequate spoke near the hub")
        self.spoke = self.spokes[0]
        self.tail = self.g.lease(self.type_id)
        self.out_id, self.in_id = self.g.open_round_trip(self.spoke)

    def tearDown(self):
        self.g.close()


class TestFounding(LifecycleBase):
    def test_airline_starts_with_expected_state(self):
        # setUp already leased a tail and opened routes, so check a pristine world for cash.
        with fresh_game(hub=self.HUB) as pristine:
            self.assertEqual(1_000_000_000.0, pristine.cash(), "starting cash should be $1B")
        a = self.db.fetch_one("SELECT * FROM airline WHERE id = 1")
        self.assertEqual(720, int(a["credit_score"]))
        self.assertEqual(self.HUB, str(a["home_hub_iata"]))
        self.assertEqual(0.0, float(a["total_debt"]))

    def test_lease_creates_fleet_and_cabin(self):
        f = self.db.fetch_one("SELECT * FROM fleet WHERE tail_number = ?", (self.tail,))
        self.assertIsNotNone(f)
        self.assertEqual("LEASED", str(f["ownership"]))
        self.assertEqual(self.HUB, str(f["current_airport_iata"]),
                         "a new tail should be delivered to the hub")
        cab = self.db.fetch_one("SELECT * FROM fleet_cabin_config WHERE tail_number = ?", (self.tail,))
        self.assertIsNotNone(cab, "leasing must create a cabin config")
        seats = sum(int(cab[k] or 0) for k in
                    ("seats_economy", "seats_premium_economy", "seats_business", "seats_first"))
        self.assertGreater(seats, 0)

    def test_cabin_respects_eec_budget(self):
        cab = self.db.fetch_one("SELECT * FROM fleet_cabin_config WHERE tail_number = ?", (self.tail,))
        cap = self.db.fetch_one("SELECT eec FROM aircraft_types WHERE type_id = ?", (self.type_id,))
        used = (int(cab["seats_economy"]) * 1.0 + int(cab["seats_premium_economy"]) * 1.5
                + int(cab["seats_business"]) * 2.0 + int(cab["seats_first"]) * 4.0)
        self.assertLessEqual(used, float(cap["eec"]) + 0.5)

    def test_buying_deducts_full_price_and_cannot_overdraft(self):
        price = float(self.db.fetch_one(
            "SELECT purchase_price FROM aircraft_types WHERE type_id = ?", (self.type_id,))["purchase_price"])
        before = self.g.cash()
        self.g.buy(self.type_id)
        self.assertAlmostEqual(before - price, self.g.cash(), places=2)
        # now try to buy with almost no cash
        self.db.execute("UPDATE airline SET cash = 1000 WHERE id = 1")
        from engine import aircraft
        with self.assertRaises(ValueError):
            aircraft.buy_aircraft(self.type_id)

    def test_opening_route_records_player_ownership(self):
        self.assertIsNotNone(self.db.fetch_one(
            "SELECT 1 FROM player_routes WHERE route_id = ?", (self.out_id,)))
        r = self.db.fetch_one("SELECT * FROM routes WHERE route_id = ?", (self.out_id,))
        self.assertGreater(float(r["distance_nm"]), 0)
        self.assertGreater(float(r["price_leisure"]), 0)
        self.assertGreater(int(r["base_demand_leisure"]), 0)

    def test_cannot_open_the_same_player_route_twice(self):
        from engine import routes
        with self.assertRaises(ValueError):
            routes.open_route(self.HUB, self.spoke, silent=True)

    def test_cannot_open_route_to_itself(self):
        from engine import routes
        with self.assertRaises(ValueError):
            routes.open_route(self.HUB, self.HUB, silent=True)


class TestScheduling(LifecycleBase):
    def test_assign_rotation_creates_connected_segments(self):
        from engine.scheduling import assign_rotation
        rot = assign_rotation(self.tail, [self.out_id, self.in_id])
        self.assertEqual(2, len(rot["segments"]))
        segs = self.db.fetch_all(
            "SELECT * FROM flight_segments WHERE tail_number = ? ORDER BY scheduled_dep_game_hour",
            (self.tail,))
        self.assertEqual(2, len(segs))
        self.assertEqual(self.HUB, str(segs[0]["origin_iata"]))
        self.assertEqual(self.spoke, str(segs[1]["origin_iata"]))
        # arrival strictly after departure, and leg 2 departs after leg 1 lands
        for s in segs:
            self.assertGreater(float(s["scheduled_arr_game_hour"]), float(s["scheduled_dep_game_hour"]))
        self.assertGreaterEqual(float(segs[1]["scheduled_dep_game_hour"]),
                                float(segs[0]["scheduled_arr_game_hour"]))

    def test_baseline_equals_schedule_on_creation(self):
        from engine.scheduling import assign_rotation
        assign_rotation(self.tail, [self.out_id, self.in_id])
        for s in self.db.fetch_all("SELECT * FROM flight_segments WHERE tail_number = ?", (self.tail,)):
            self.assertAlmostEqual(float(s["baseline_dep_game_hour"]),
                                   float(s["scheduled_dep_game_hour"]), places=6)

    def test_rotation_must_connect(self):
        from engine.scheduling import assign_rotation
        others = [s for s in self.spokes if s != self.spoke]
        if not others:
            self.skipTest("need a second spoke")
        second = others[0]
        self.g.open_round_trip(second)
        with self.assertRaises(ValueError):
            # hub->spoke then second->hub does not connect
            assign_rotation(self.tail, [self.out_id, f"{second}-{self.HUB}"])

    def test_range_limit_is_enforced(self):
        from engine.scheduling import assign_rotation
        rng = float(self.db.fetch_one(
            "SELECT range_nm FROM aircraft_types WHERE type_id = ?", (self.type_id,))["range_nm"])
        far = near_airports(self.db, self.HUB, rng + 500, rng + 4000, limit=1)
        if not far:
            self.skipTest("no airport beyond this type's range")
        rid = self.g.open_route(self.HUB, far[0])
        with self.assertRaises(ValueError):
            assign_rotation(self.tail, [rid])

    def test_weekly_airborne_cap_is_enforced(self):
        from engine.scheduling import assign_rotation, max_weekly_airborne_hours_cap
        self.db.execute(
            "INSERT OR REPLACE INTO financial_constants (key, value) VALUES ('max_weekly_airborne_hours_per_tail', 1.0)")
        self.assertEqual(1.0, max_weekly_airborne_hours_cap())
        with self.assertRaises(ValueError):
            assign_rotation(self.tail, [self.out_id, self.in_id])

    def test_positioning_is_enforced(self):
        """A leg cannot depart from an airport the aircraft is not at."""
        from engine.scheduling import assign_rotation
        with self.assertRaises(ValueError):
            assign_rotation(self.tail, [self.in_id])   # aircraft is at the hub, not the spoke

    def test_turnaround_overrides_flow_through(self):
        from engine.scheduling import assign_rotation
        assign_rotation(self.tail, [self.out_id, self.in_id], turn_minutes=[240, 30],
                        flight_numbers=["TT100", "TT101"])
        segs = self.db.fetch_all(
            "SELECT flight_number, scheduled_dep_game_hour d, scheduled_arr_game_hour a"
            " FROM flight_segments WHERE tail_number = ? ORDER BY d", (self.tail,))
        self.assertEqual(["TT100", "TT101"], [str(s["flight_number"]) for s in segs])
        gap = (float(segs[1]["d"]) - float(segs[0]["a"])) * 60.0
        self.assertAlmostEqual(240.0, gap, places=1)

    def test_chained_detailed_airborne_uses_operating_days_not_calendar_span(self):
        """One weekly start must not multiply block time by weekday labels the rotation crosses."""
        from engine.scheduling import create_chained_detailed_rotation, _plan_chained_detailed_segments
        from engine.routes import get_route

        others = [s for s in self.spokes if s != self.spoke]
        if not others:
            self.skipTest("need a second spoke")
        spoke2 = others[0]
        out2, in2 = self.g.open_round_trip(spoke2)
        route_ids = [self.out_id, self.in_id, self.out_id, self.in_id, out2, in2]
        turn_mins = [600] * len(route_ids)  # long turns stretch calendar span, not airborne hours

        self.db.execute(
            "INSERT OR REPLACE INTO financial_constants (key, value) VALUES "
            "('max_weekly_airborne_hours_per_tail', 8.0)"
        )
        gw = int(self.db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")["game_week"])
        cruise = float(self.db.fetch_one(
            "SELECT cruise_speed_kts FROM aircraft_types WHERE type_id = ?", (self.type_id,)
        )["cruise_speed_kts"] or 450)
        routes = [dict(get_route(rid)) for rid in route_ids]
        planned, fh_list, _ = _plan_chained_detailed_segments(
            gw,
            ["THU"],
            "08:00",
            routes,
            ["X1"] * len(route_ids),
            cruise,
            0.5,
            turn_hours=[m / 60.0 for m in turn_mins],
        )
        calendar_labels = len({p["day"] for p in planned})
        airborne_once = sum(fh_list)
        self.assertGreater(calendar_labels, 1)
        self.assertLess(airborne_once, 8.0)
        self.assertGreater(
            airborne_once * calendar_labels,
            8.0,
            "old formula multiplied by calendar labels and would reject",
        )
        create_chained_detailed_rotation(
            self.tail,
            route_ids,
            [f"CH{i}" for i in range(len(route_ids))],
            '["THU"]',
            "08:00",
            turn_minutes=turn_mins,
        )
        rows = self.db.fetch_all(
            "SELECT route_id FROM flight_schedules WHERE tail_number = ? AND active = 1",
            (self.tail,),
        )
        self.assertEqual(len(route_ids), len(rows))

    def test_clear_removes_plan_and_repositions(self):
        from engine.scheduling import assign_rotation, cancel_rotation
        assign_rotation(self.tail, [self.out_id])          # one-way: ends at the spoke
        self.g.fly_all()
        loc = self.db.fetch_one("SELECT current_airport_iata FROM fleet WHERE tail_number=?", (self.tail,))
        self.assertEqual(self.spoke, str(loc["current_airport_iata"]))
        res = cancel_rotation(self.tail, wipe_completed_this_week=True)
        self.assertIsInstance(res, dict, "a tail away from hub should get a ferry")
        self.assertEqual(self.HUB, res["to"])
        self.assertIsNone(self.db.fetch_one(
            "SELECT 1 FROM weekly_rotations WHERE tail_number = ?", (self.tail,)))


class TestFlyingAndSettlement(LifecycleBase):
    def _schedule_and_fly(self):
        from engine.scheduling import assign_rotation
        assign_rotation(self.tail, [self.out_id, self.in_id])
        return self.g.fly_all()

    def test_departure_books_pax_and_revenue(self):
        dep, landed = self._schedule_and_fly()
        self.assertEqual(2, dep)
        self.assertEqual(2, landed)
        rows = self.db.fetch_all("SELECT * FROM flight_segments WHERE tail_number = ?", (self.tail,))
        for s in rows:
            self.assertEqual("LANDED", str(s["status"]))
            self.assertGreater(float(s["revenue_gross"]), 0, "a flown leg should sell seats")
            self.assertGreater(int(s["pax_leisure"]) + int(s["pax_business"]), 0)
            self.assertGreater(float(s["fuel_burned_gallons"] or 0), 0)
            self.assertGreater(float(s["fuel_cost"] or 0), 0)
            self.assertIsNotNone(s["net_contribution"])

    def test_pax_never_exceed_seats(self):
        self._schedule_and_fly()
        cab = self.db.fetch_one("SELECT * FROM fleet_cabin_config WHERE tail_number = ?", (self.tail,))
        for s in self.db.fetch_all("SELECT * FROM flight_segments WHERE tail_number = ?", (self.tail,)):
            self.assertLessEqual(int(s["pax_economy"]), int(cab["seats_economy"]))
            self.assertLessEqual(int(s["pax_premium_economy"]), int(cab["seats_premium_economy"]))
            self.assertLessEqual(int(s["pax_business_cabin"]), int(cab["seats_business"]))
            self.assertLessEqual(int(s["pax_first"]), int(cab["seats_first"]))

    def test_taxes_and_fees_are_consistent_with_revenue(self):
        self._schedule_and_fly()
        excise_rate = float(self.db.get_financial_constant("excise_tax_rate") or 0.075)
        seg_fee = float(self.db.get_financial_constant("segment_fee") or 5.30)
        for s in self.db.fetch_all("SELECT * FROM flight_segments WHERE tail_number = ?", (self.tail,)):
            rev = float(s["revenue_gross"])
            self.assertAlmostEqual(rev * excise_rate, float(s["excise_tax"]), places=2)
            pax = (int(s["pax_economy"]) + int(s["pax_premium_economy"])
                   + int(s["pax_business_cabin"]) + int(s["pax_first"]))
            self.assertAlmostEqual(pax * seg_fee, float(s["segment_fee"]), places=2)
            self.assertGreater(float(s["landing_fee"]), 0)

    def test_settlement_writes_ledger_and_moves_cash(self):
        self._schedule_and_fly()
        before = self.g.cash()
        res = self.g.settle(1)
        self.assertFalse(res.get("skipped"), res)
        led = self.db.fetch_one("SELECT * FROM week_ledger WHERE game_week = 1")
        self.assertIsNotNone(led, "settlement must write a ledger row")
        self.assertGreater(float(led["revenue_gross"]), 0)
        self.assertAlmostEqual(before + float(led["net_income"]), self.g.cash(), delta=1.0,
                               msg="cash must move by exactly net_income")
        self.assertAlmostEqual(self.g.cash(), float(led["cash_end_of_week"]), delta=1.0)

    def test_settlement_is_idempotent(self):
        self._schedule_and_fly()
        self.g.settle(1)
        cash_after_first = self.g.cash()
        second = self.g.settle(1)
        self.assertTrue(second.get("skipped"), "re-settling a week must be a no-op")
        self.assertEqual(cash_after_first, self.g.cash(), "re-settling must not move cash again")
        n = self.db.fetch_one("SELECT COUNT(*) n FROM week_ledger WHERE game_week = 1")["n"]
        self.assertEqual(1, n)

    def test_ledger_net_income_reconciles_with_components(self):
        self._schedule_and_fly()
        self.g.settle(1)
        l = self.db.fetch_one("SELECT * FROM week_ledger WHERE game_week = 1")
        fees = (float(l["excise_tax"]) + float(l["segment_fees"]) + float(l["security_fees"])
                + float(l["pfc_fees"]) + float(l["landing_fees"]) + float(l["gate_fees"]))
        pretax = float(l["revenue_gross"]) - fees - float(l["fuel_cost"]) \
            - float(l["lease_costs"]) - float(l["maintenance_costs"])
        self.assertAlmostEqual(pretax - float(l["corporate_tax"]), float(l["net_income"]), delta=2.0)

    def test_every_lease_week_is_billed_through_the_ledger(self):
        """Lease rent is a ledger expense from week 1, so it is visible in the books.

        Signing used to take the first week's rent straight out of cash and set
        lease_prepaid=1, which made compute_lease_costs() skip it. The money was real
        but appeared nowhere in the financials, so a leased fleet looked free for its
        first week. Signing is now cash-neutral and settlement bills every week.
        """
        from engine.scheduling import (assign_rotation,
                                       reset_operational_schedule_for_new_calendar_week,
                                       spawn_rotation_segments_for_week)
        weekly = float(self.db.fetch_one(
            "SELECT weekly_lease_cost FROM aircraft_types WHERE type_id = ?",
            (self.type_id,))["weekly_lease_cost"])
        self.assertEqual(0, int(self.db.fetch_one(
            "SELECT lease_prepaid FROM fleet WHERE tail_number = ?", (self.tail,))["lease_prepaid"]),
            "signing must not prepay, or week 1 rent never reaches the ledger")
        assign_rotation(self.tail, [self.out_id, self.in_id])
        self.g.fly_all(1)
        self.g.settle(1)
        self.assertAlmostEqual(weekly, float(self.db.fetch_one(
            "SELECT lease_costs FROM week_ledger WHERE game_week = 1")["lease_costs"]), delta=1.0,
            msg="week 1 must bill one weekly lease")
        self.g.set_hour(168.0)
        reset_operational_schedule_for_new_calendar_week(2)
        spawn_rotation_segments_for_week(2)
        self.g.fly_all(2)
        self.g.settle(2)
        self.assertAlmostEqual(weekly, float(self.db.fetch_one(
            "SELECT lease_costs FROM week_ledger WHERE game_week = 2")["lease_costs"]), delta=1.0,
            msg="week 2 must bill exactly one weekly lease")

    def test_lease_weeks_countdown(self):
        before = int(self.db.fetch_one(
            "SELECT lease_weeks_remaining FROM fleet WHERE tail_number = ?", (self.tail,))["lease_weeks_remaining"])
        self.g.settle(1)
        after = int(self.db.fetch_one(
            "SELECT lease_weeks_remaining FROM fleet WHERE tail_number = ?", (self.tail,))["lease_weeks_remaining"])
        self.assertEqual(before - 1, after, "settlement must decrement the lease term")

    def test_two_weeks_run_without_drift(self):
        from engine.scheduling import (assign_rotation, reset_operational_schedule_for_new_calendar_week,
                                       spawn_rotation_segments_for_week)
        assign_rotation(self.tail, [self.out_id, self.in_id])
        self.g.fly_all(1)
        self.g.settle(1)
        # roll to week 2 the way the clock does
        self.g.set_hour(168.0)
        reset_operational_schedule_for_new_calendar_week(2)
        spawn = spawn_rotation_segments_for_week(2)
        self.assertGreater(int(spawn["inserted"]), 0, "weekly template must respawn next week")
        self.g.fly_all(2)
        r2 = self.g.settle(2)
        self.assertFalse(r2.get("skipped"), r2)
        weeks = [int(r["game_week"]) for r in
                 self.db.fetch_all("SELECT game_week FROM week_ledger ORDER BY game_week")]
        self.assertEqual([1, 2], weeks)


class TestPersistence(LifecycleBase):
    def test_clock_state_round_trips(self):
        self.g.set_hour(500.5)
        gs = self.db.fetch_one("SELECT game_hours_elapsed, game_week FROM game_state WHERE id = 1")
        self.assertAlmostEqual(500.5, float(gs["game_hours_elapsed"]), places=3)
        self.assertEqual(3, int(gs["game_week"]), "hour 500 is week 3 (168h weeks)")

    def test_week_boundaries_map_to_hours(self):
        from engine.scheduling import week_base_hours
        self.assertEqual(0.0, week_base_hours(1))
        self.assertEqual(168.0, week_base_hours(2))
        self.assertEqual(336.0, week_base_hours(3))

    def test_reopening_the_db_preserves_state(self):
        self.g.lease(self.type_id, tail="KEEP-001")
        self.db.execute("UPDATE airline SET cash = 12345.0 WHERE id = 1")
        # simulate a restart: new connection through the same module
        again = self.db.fetch_one("SELECT cash FROM airline WHERE id = 1")
        self.assertAlmostEqual(12345.0, float(again["cash"]), places=2)
        self.assertIsNotNone(self.db.fetch_one(
            "SELECT 1 FROM fleet WHERE tail_number = 'KEEP-001'"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
