#!/usr/bin/env python3
"""
Compare the demand the game would show against real-world weekly traffic.

Read-only spot check over a fixed benchmark set covering anchored and modelled
routes on every continent we have data for, so a change to the gravity fit can be
judged on what players actually see rather than on regression diagnostics.

Usage:
    python3 Intl_Route/check_routes.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

# Real weekly one-way pax, from published traffic reports. Round numbers on
# purpose -- these are order-of-magnitude benchmarks, not precise measurements.
BENCHMARKS = [
    ("HND", "FUK", 84_000),
    ("HND", "CTS", 90_000),
    ("HND", "KIX", 30_000),
    ("ICN", "NRT", 31_000),
    ("ICN", "KIX", 27_000),
    ("ICN", "BKK", 30_000),
    ("ATL", "LAX", 30_000),
    ("SYD", "MEL", 70_000),
    ("CDG", "LHR", 15_000),
    ("LHR", "SIN", 20_000),
    ("HKG", "TPE", 50_000),
    ("PEK", "PVG", 60_000),
    ("BOM", "DEL", 45_000),
    ("SIN", "SGN", 19_000),
    ("SGN", "PEK", 4_000),
    ("DLI", "HPH", 3_000),
    ("HAN", "BOM", 1_000),
]


def main() -> int:
    from helpers import FreshGame  # noqa: PLC0415

    with FreshGame() as world:
        from engine.route_demand import (  # noqa: PLC0415
            compute_base_demand,
            effective_demand_multiplier,
        )

        eff = effective_demand_multiplier()
        print(f"{'route':<10}{'source':<10}{'pool/wk':>10}{'real/wk':>10}{'ratio':>8}")
        modelled: list[float] = []
        anchored: list[float] = []
        for origin, dest, real in BENCHMARKS:
            ao = world.fetch_one("SELECT * FROM airports WHERE iata=?", (origin,))
            ad = world.fetch_one("SELECT * FROM airports WHERE iata=?", (dest,))
            if not ao or not ad:
                print(f"{origin}-{dest:<6}{'MISSING':<10}")
                continue
            ao, ad = dict(ao), dict(ad)
            dist = haversine(ao["lat"], ao["lon"], ad["lat"], ad["lon"])
            info = compute_base_demand(dist, ao, ad)
            pool = info["base_total"] * eff
            ratio = pool / real if real else 0.0
            (anchored if info["demand_source"] == "BTS" else modelled).append(ratio)
            flag = " floor" if info["market_floor_applied"] else ""
            print(
                f"{origin}-{dest:<6}{info['demand_source']:<10}{pool:>10,.0f}"
                f"{real:>10,.0f}{ratio:>8.2f}{flag}"
            )

        for label, vals in (("anchored", anchored), ("modelled", modelled)):
            if vals:
                vals.sort()
                med = vals[len(vals) // 2]
                print(f"\n{label:<10} n={len(vals):<3} median ratio {med:.2f}")
    return 0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = r2 - r1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


if __name__ == "__main__":
    raise SystemExit(main())
