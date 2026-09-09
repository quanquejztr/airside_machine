"""Tests for US_Route/bts_calibrate.py output."""

from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from US_Route.bts_calibrate import weighted_median, trend_adjust, year_weight, REF_YEAR


class TestBtsCalibrate(unittest.TestCase):
    ANCHORS = ROOT / "data" / "bts_demand_anchors.csv"
    GRAVITY = ROOT / "data" / "bts_gravity_params.json"

    def test_weighted_median_basic(self):
        self.assertEqual(weighted_median([10.0, 20.0, 30.0], [1.0, 1.0, 1.0]), 20.0)

    def test_trend_adjust_to_ref_year(self):
        self.assertAlmostEqual(
            trend_adjust(100.0, REF_YEAR, ref_year=REF_YEAR, growth=0.02), 100.0
        )
        self.assertGreater(
            trend_adjust(100.0, REF_YEAR - 10, ref_year=REF_YEAR, growth=0.02), 100.0
        )

    def test_covid_years_zero_weight(self):
        self.assertEqual(year_weight(2020, ref_year=REF_YEAR, decay=0.85), 0.0)
        self.assertEqual(year_weight(2021, ref_year=REF_YEAR, decay=0.85), 0.0)

    def test_anchor_outputs_exist(self):
        self.assertTrue(self.ANCHORS.is_file(), "run US_Route/bts_calibrate.py first")
        self.assertTrue(self.GRAVITY.is_file())

    def test_atl_lax_anchor_sane(self):
        if not self.ANCHORS.is_file():
            self.skipTest("anchors csv missing")
        hit = None
        with self.ANCHORS.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["origin_iata"] == "ATL" and row["dest_iata"] == "LAX":
                    hit = row
                    break
        self.assertIsNotNone(hit)
        weekly = float(hit["anchor_weekly"])
        self.assertGreater(weekly, 15_000)
        self.assertLess(weekly, 30_000)
        self.assertGreaterEqual(int(hit["years_used"]), 10)

    def test_gravity_params_sane(self):
        if not self.GRAVITY.is_file():
            self.skipTest("gravity json missing")
        params = json.loads(self.GRAVITY.read_text(encoding="utf-8"))
        self.assertGreater(params["k"], 0)
        self.assertGreater(params["alpha"], 0)
        self.assertGreater(params["beta"], 0)
        self.assertIn("2023.csv", params.get("skipped_duplicate_files", []))


class TestBtsDbLoad(unittest.TestCase):
    def test_migrations_load_anchors_into_db(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from db import db

            n = db.bts_demand_anchors_loaded()
            self.assertGreater(n, 50_000)
            atl_lax = db.lookup_bts_anchor_weekly("ATL", "LAX")
            self.assertIsNotNone(atl_lax)
            self.assertGreater(atl_lax, 15_000)
            self.assertLess(atl_lax, 30_000)
            gk = db.get_financial_constant("bts_gravity_k")
            self.assertIsNotNone(gk)
            self.assertGreater(float(gk), 0)

    def test_compute_base_demand_bts_and_gravity(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from engine.airports import get_airport
            from engine.route_demand import (
                compute_base_demand,
                effective_demand_multiplier,
            )
            from engine.routes import haversine_distance, open_route
            from engine.demand import preview_weekly_demand_before_open
            from db import db

            self.assertAlmostEqual(
                float(db.get_financial_constant("bts_target_market_share") or 0),
                1.30,
                places=2,
            )
            # The floor keeps ultra-thin OD pairs flyable without inventing markets.
            # It has to stay well clear of both failure modes: too low and a thin
            # route cannot fill anything, too high and it overrides the real market
            # it is supposed to be protecting (at 1500 TPA-SAN got 240% of actual).
            min_pool = float(db.get_financial_constant("bts_min_weekly_pool") or 0)
            self.assertGreaterEqual(min_pool, 500)
            self.assertLessEqual(min_pool, 1000)

            atl = get_airport("ATL")
            lax = get_airport("LAX")
            self.assertIsNotNone(atl)
            self.assertIsNotNone(lax)

            dist = haversine_distance(atl["lat"], atl["lon"], lax["lat"], lax["lon"])
            info = compute_base_demand(dist, atl, lax)
            self.assertEqual(info["demand_source"], "BTS")
            self.assertGreater(info["base_demand_business"] + info["base_demand_leisure"], 100)

            # Phase 6: T-100 International anchors cover US<->foreign pairs, so the
            # busiest transatlantic market resolves from real data instead of the
            # LEGACY category buckets it used to fall through to.
            jfk = get_airport("JFK")
            lhr = get_airport("LHR")
            self.assertIsNotNone(jfk)
            self.assertIsNotNone(lhr)
            d_intl = haversine_distance(jfk["lat"], jfk["lon"], lhr["lat"], lhr["lon"])
            intl = compute_base_demand(d_intl, jfk, lhr)
            self.assertEqual(intl["demand_source"], "BTS")
            tot = intl["base_demand_business"] + intl["base_demand_leisure"]
            self.assertGreater(tot, 50)
            # It must also outrank a mid-size long-haul, which LEGACY could not do:
            # its distance buckets gave every 3000nm+ pair the same answer.
            gru = get_airport("GRU")
            self.assertIsNotNone(gru)
            d_gru = haversine_distance(jfk["lat"], jfk["lon"], gru["lat"], gru["lon"])
            gru_info = compute_base_demand(d_gru, jfk, gru)
            self.assertGreater(float(intl["base_total"]), float(gru_info["base_total"]))

            # Foreign<->foreign with no anchor must come from banded gravity,
            # never LEGACY, and must not out-market a real anchored trunk route.
            # Chinese domestic is the safe example: LHR-CDG used to serve here
            # but Eurostat now anchors it, and no free source covers China.
            pek = get_airport("PEK")
            pvg = get_airport("PVG")
            self.assertIsNotNone(pek)
            self.assertIsNotNone(pvg)
            d_ff = haversine_distance(pek["lat"], pek["lon"], pvg["lat"], pvg["lon"])
            ff = compute_base_demand(d_ff, pek, pvg)
            self.assertEqual(ff["demand_source"], "GRAVITY")
            self.assertGreater(
                ff["base_demand_business"] + ff["base_demand_leisure"], 0
            )
            self.assertLess(float(ff["base_total"]), float(intl["base_total"]))

            # Thin US pair: soft weekly pool keeps week-1 market playable.
            tpa = get_airport("TPA")
            san = get_airport("SAN")
            self.assertIsNotNone(tpa)
            self.assertIsNotNone(san)
            d_thin = haversine_distance(tpa["lat"], tpa["lon"], san["lat"], san["lon"])
            thin = compute_base_demand(d_thin, tpa, san)
            self.assertEqual(thin["demand_source"], "BTS")
            eff = effective_demand_multiplier()
            pool = float(thin["base_total"]) * eff
            # The floor scales down with airport size, so compare against this
            # pair's own floor rather than the configured hub-reference value.
            from engine.route_demand import min_weekly_pool

            pair_floor, _ = min_weekly_pool(tpa, san)
            self.assertLessEqual(pair_floor, min_pool)
            self.assertGreaterEqual(pool, pair_floor * 0.99)
            # Playable (fills a narrowbody daily) without running far past the real
            # market -- TPA-SAN carries ~540 pax/wk for real.
            week1 = int(preview_weekly_demand_before_open(tpa, san, d_thin)["total_pax"])
            self.assertGreaterEqual(week1, 500)
            anchor = db.lookup_bts_anchor_weekly("TPA", "SAN")
            self.assertTrue(anchor)
            self.assertLess(week1 / anchor, 1.6)

            route = open_route("ATL", "LAX", silent=True)
            self.assertEqual(route.get("demand_source"), "BTS")
            row = world.fetch_one(
                "SELECT demand_source, base_demand_business, base_demand_leisure "
                "FROM routes WHERE route_id='ATL-LAX'"
            )
            self.assertEqual(str(row["demand_source"]), "BTS")
            self.assertGreater(
                int(row["base_demand_business"]) + int(row["base_demand_leisure"]), 100
            )

            open_route("JFK", "LHR", silent=True)
            intl_row = world.fetch_one(
                "SELECT demand_source, base_demand_business, base_demand_leisure "
                "FROM routes WHERE route_id='JFK-LHR'"
            )
            self.assertIsNotNone(intl_row)
            self.assertEqual(str(intl_row["demand_source"]), "BTS")
            self.assertGreater(
                int(intl_row["base_demand_business"]) + int(intl_row["base_demand_leisure"]),
                50,
            )

    def test_banded_gravity_is_continuous_and_capped(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from engine.route_demand import (
                _band_decay,
                gravity_band_params,
                gravity_cap_weekly,
                gravity_weekly,
            )

            params = gravity_band_params()
            self.assertTrue(params, "data/gravity_bands.json should be present")
            d1 = float(params["band1_nm"])
            d2 = float(params["band2_nm"])

            # Decay must not jump across a band edge, otherwise two near-identical
            # routes would show materially different demand. Step either side by a
            # hair so any remaining gap is a discontinuity, not ordinary decay.
            eps = 1e-6
            for edge in (d1, d2):
                below = _band_decay(edge - eps, params)
                above = _band_decay(edge + eps, params)
                self.assertAlmostEqual(below / above, 1.0, places=8)

            # Decay is monotonic: further is never worth more.
            prev = 0.0
            for nm in (100, 400, 799, 801, 1500, 2999, 3001, 5000, 7000):
                cur = _band_decay(float(nm), params)
                self.assertGreater(cur, prev)
                prev = cur

            # Long-haul decays fastest. Short and medium came out nearly equal
            # once European short-haul entered the fit, so their relative order
            # is not a property worth asserting -- only that both are positive
            # and below the long-haul rate.
            short = float(params["beta_short"])
            medium = float(params["beta_medium"])
            long_ = float(params["beta_long"])
            self.assertGreater(short, 0.0)
            self.assertGreater(medium, 0.0)
            self.assertGreater(long_, short)
            self.assertGreater(long_, medium)

            # No modelled market may exceed the per-band plausibility cap.
            def get(iata):
                row = world.fetch_one("SELECT * FROM airports WHERE iata=?", (iata,))
                return dict(row) if row else None

            # Caps are stratified by the smaller of the two scores as well as by
            # distance, so the ceiling must be looked up for this specific pair --
            # calling it without a score asks for the thin-tier cap, which a pair
            # of hubs is entitled to exceed.
            for o, d, nm in (("LHR", "CDG", 187.0), ("NRT", "ICN", 679.0), ("SYD", "SIN", 3399.0)):
                a, b = get(o), get(d)
                self.assertIsNotNone(a, f"{o} missing")
                self.assertIsNotNone(b, f"{d} missing")
                weekly = gravity_weekly(a, b, nm)
                self.assertGreater(weekly, 0.0)
                pair_cap = gravity_cap_weekly(
                    nm, min_score=min(float(a["score"]), float(b["score"]))
                )
                self.assertGreater(pair_cap, 0.0)
                self.assertLessEqual(weekly, pair_cap + 1e-6)
                # A hub pair must not be held to the thin-tier ceiling.
                self.assertGreaterEqual(pair_cap, gravity_cap_weekly(nm, min_score=0.0))

    def test_quantile_mapping_widens_spread_without_reordering(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame():
            from engine.route_demand import apply_quantile_mapping, gravity_band_params

            params = gravity_band_params()
            self.assertTrue(params.get("quantile_model"))
            self.assertEqual(
                len(params["quantile_model"]), len(params["quantile_real"])
            )

            # Monotonic: a busier raw prediction can never map below a quieter one.
            samples = [1.0, 10.0, 50.0, 200.0, 537.0, 900.0, 2_500.0, 10_000.0, 1e6]
            mapped = [apply_quantile_mapping(v, params) for v in samples]
            for prev, cur in zip(mapped, mapped[1:]):
                self.assertGreater(cur, prev)
            for m in mapped:
                self.assertGreater(m, 0.0)

            # The point of the correction: trunk-scale predictions get lifted and
            # thin ones pulled down, widening a span that was far too narrow.
            low_in, high_in = 50.0, 2_500.0
            low_out = apply_quantile_mapping(low_in, params)
            high_out = apply_quantile_mapping(high_in, params)
            self.assertLess(low_out, low_in)
            self.assertGreater(high_out, high_in)
            self.assertGreater(high_out / low_out, high_in / low_in)

    def test_backfilled_international_airports_present(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            # These carry ~22% of T-100 international traffic; without them the
            # busiest intercontinental routes silently drop to gravity or LEGACY.
            for iata in ("FRA", "MAD", "IST", "MUC", "GRU", "FCO", "DOH", "DEL"):
                row = world.fetch_one(
                    "SELECT score, category, lat, lon FROM airports WHERE iata=?", (iata,)
                )
                self.assertIsNotNone(row, f"{iata} missing from airports.csv")
                self.assertGreater(float(row["score"]), 0)
                self.assertNotEqual(float(row["lat"]), 0.0)

    def test_korean_routes_are_anchored_not_modelled(self):
        """Intra-Asian pairs from airportal.go.kr must resolve to real anchors."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from engine.route_demand import compute_base_demand

            # Before the Korean import every one of these fell through to gravity,
            # which under-predicts Asian trunk routes by an order of magnitude.
            for origin, dest, low, high in (
                ("ICN", "NRT", 20_000, 45_000),
                ("ICN", "KIX", 20_000, 45_000),
                ("ICN", "BKK", 15_000, 40_000),
            ):
                ao = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (origin,)))
                ad = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (dest,)))
                info = compute_base_demand(_nm(ao, ad), ao, ad)
                self.assertEqual(
                    info["demand_source"], "BTS", f"{origin}-{dest} lost its anchor"
                )
                self.assertFalse(info["market_floor_applied"])
                weekly = info["anchor_weekly"]
                self.assertIsNotNone(weekly)
                self.assertGreaterEqual(weekly, low)
                self.assertLessEqual(weekly, high)

    def test_japanese_and_european_trunks_are_anchored(self):
        """e-Stat and Eurostat must cover the busiest domestic/intra-EU markets."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from engine.route_demand import compute_base_demand

            # HND-FUK and HND-CTS are among the busiest routes anywhere; gravity
            # priced them at a few hundred a week before the Japanese import.
            for origin, dest, low in (
                ("HND", "FUK", 50_000),
                ("HND", "CTS", 50_000),
                ("LHR", "CDG", 8_000),
                ("MAD", "BCN", 8_000),
            ):
                ao = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (origin,)))
                ad = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (dest,)))
                info = compute_base_demand(_nm(ao, ad), ao, ad)
                self.assertEqual(
                    info["demand_source"], "BTS", f"{origin}-{dest} is not anchored"
                )
                self.assertGreaterEqual(info["anchor_weekly"], low)

    def test_junk_anchors_yield_to_gravity_only_when_far_apart(self):
        """A charter-sized anchor must not beat the model, unless people can drive."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from helpers import FreshGame

        with FreshGame() as world:
            from engine.route_demand import compute_base_demand

            def info(origin, dest):
                ao = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (origin,)))
                ad = dict(world.fetch_one("SELECT * FROM airports WHERE iata=?", (dest,)))
                return compute_base_demand(_nm(ao, ad), ao, ad)

            # HND-PUS carries an anchor of ~3/wk, which used to collapse to the
            # playability floor. Over water at 531nm there is no ground
            # alternative, so the model should take over.
            far = info("HND", "PUS")
            self.assertEqual(far["demand_source"], "GRAVITY")
            self.assertFalse(far["market_floor_applied"])
            self.assertGreater(far["base_total"], 0)

            # These are correct near-zero anchors, not junk: ORD-MDW is 13nm and
            # BWI-IAD a short drive. Gravity wants thousands a week on both, so
            # the anchor must win however small it is.
            for origin, dest in (("ORD", "MDW"), ("BWI", "IAD"), ("SJC", "SMF")):
                near = info(origin, dest)
                self.assertEqual(
                    near["demand_source"],
                    "BTS",
                    f"{origin}-{dest} is short enough to drive; anchor must hold",
                )
                self.assertTrue(near["market_floor_applied"])

            # Anchors above the credibility threshold are untouched at any range.
            for origin, dest in (("ICN", "PUS"), ("ATL", "LAX")):
                keep = info(origin, dest)
                self.assertEqual(keep["demand_source"], "BTS")
                self.assertFalse(keep["market_floor_applied"])

    def test_russian_far_east_is_asian_not_european(self):
        """Vladivostok trades with Seoul, so ICN-VVO must not get a Europe prior."""
        from engine.route_demand import _region

        self.assertEqual(_region("RU", 131.9), "AS")  # VVO
        self.assertEqual(_region("RU", 37.4), "EU")  # SVO
        # Without a longitude we cannot tell them apart; keep the old default.
        self.assertEqual(_region("RU"), "EU")


def _nm(a: dict, b: dict) -> float:
    import math

    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    )
    return 2 * 3440.065 * math.asin(math.sqrt(h))


if __name__ == "__main__":
    unittest.main()
