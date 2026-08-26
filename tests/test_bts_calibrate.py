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


if __name__ == "__main__":
    unittest.main()
