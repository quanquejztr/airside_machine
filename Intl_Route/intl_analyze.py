#!/usr/bin/env python3
"""
Read-only analysis of Intl_Route/*.csv (DOT T-100 International Market).

Answers three questions before any Phase 6 code is written:

  1. Score bias — is the current US-fitted gravity model systematically wrong on
     pairs that involve a non-US airport? Measured as actual/predicted residuals,
     so it replaces guesswork about normalizing `score`.
  2. Distance decay — does a single beta hold across 50-7000nm, or do the bands
     have genuinely different slopes? Fitted per band by OLS on logs.
  3. Plausibility caps — per-band anchor percentiles, for capping modelled routes.

Writes nothing. Prints a report.

Usage:
    python3 Intl_Route/intl_analyze.py
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "US_Route"))

from bts_calibrate import (  # noqa: E402
    DECAY_LAMBDA,
    GRAVITY_MIN_WEEKLY,
    GROWTH_RATE,
    REF_YEAR,
    build_anchors,
    discover_year_files,
    duplicate_files_to_skip,
    haversine_nm,
    ingest_yearly_files,
    load_game_airports,
)

INTL_DIR = Path(__file__).resolve().parent
US_DIR = ROOT / "US_Route"
AIRPORTS_CSV = ROOT / "data" / "airports.csv"
CONSTANTS_CSV = ROOT / "data" / "financial_constants.csv"

BANDS = [("short", 0.0, 800.0), ("medium", 800.0, 3000.0), ("long", 3000.0, 1e9)]


def load_constants() -> dict[str, float]:
    out: dict[str, float] = {}
    with CONSTANTS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                out[row[0].strip()] = float(row[1])
            except ValueError:
                continue
    return out


def band_of(dist_nm: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= dist_nm < hi:
            return name
    return "long"


def gravity_current(origin: dict, dest: dict, dist_nm: float, c: dict) -> float:
    """Reproduces engine.route_demand.gravity_weekly without needing the DB."""
    k = c.get("bts_gravity_k", 0.00050176)
    alpha = c.get("bts_gravity_alpha", 0.80)
    beta = c.get("bts_gravity_beta", 1.20)
    damp = c.get("bts_small_small_damp", 0.7234)

    score_o = max(1.0, float(origin["score"]))
    score_d = max(1.0, float(dest["score"]))
    dist = max(50.0, float(dist_nm))
    weekly = k * ((score_o * score_d) ** alpha) / (dist**beta)
    if origin["category"] == "small_airport" and dest["category"] == "small_airport":
        weekly *= damp
    return max(0.0, weekly)


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting."""
    n = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / aug[col][col]
            for cc in range(col, n + 1):
                aug[r][cc] -= factor * aug[col][cc]
    return [aug[i][n] / aug[i][i] for i in range(n)]


def fit_log_ols(points: list[tuple[float, float, float]]) -> dict | None:
    """
    Fit log(weekly) = log(K) + alpha*log(score_o*score_d) - beta*log(dist).
    points: (score_product, dist_nm, weekly)
    """
    if len(points) < 50:
        return None
    xs = []
    for sp, dist, wk in points:
        xs.append((1.0, math.log(max(1.0, sp)), math.log(max(50.0, dist)), math.log(wk)))

    n_terms = 3
    xtx = [[0.0] * n_terms for _ in range(n_terms)]
    xty = [0.0] * n_terms
    for row in xs:
        feats = row[:3]
        y = row[3]
        for i in range(n_terms):
            for j in range(n_terms):
                xtx[i][j] += feats[i] * feats[j]
            xty[i] += feats[i] * y
    coefs = solve(xtx, xty)
    if coefs is None:
        return None
    log_k, alpha, neg_beta = coefs

    resid = []
    for row in xs:
        pred = log_k + alpha * row[1] + neg_beta * row[2]
        resid.append(row[3] - pred)
    ss_res = sum(r * r for r in resid)
    mean_y = statistics.mean(r[3] for r in xs)
    ss_tot = sum((r[3] - mean_y) ** 2 for r in xs)
    return {
        "k": math.exp(log_k),
        "alpha": alpha,
        "beta": -neg_beta,
        "n": len(xs),
        "r2": 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0,
    }


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def build_weekly_anchors(directory: Path, airports: dict) -> dict[tuple[str, str], float]:
    files = discover_year_files(directory)
    skip = duplicate_files_to_skip(files)
    per_od_year, stats = ingest_yearly_files(files, skip, airports, write_clean=None)
    rows = build_anchors(
        per_od_year, ref_year=REF_YEAR, growth=GROWTH_RATE, decay=DECAY_LAMBDA
    )
    print(
        f"  {directory.name}: {stats['files_read']} files, "
        f"{stats['rows_kept']:,} rows kept, {len(rows):,} pairs"
    )
    return {(r["origin_iata"], r["dest_iata"]): float(r["anchor_weekly"]) for r in rows}


def main() -> int:
    # Match the live gravity fit, which ignores ultra-thin pairs. Without this the
    # median pair is ~1 pax/week and the residuals measure noise, not model error.
    min_weekly = float(sys.argv[1]) if len(sys.argv) > 1 else GRAVITY_MIN_WEEKLY

    airports = load_game_airports(AIRPORTS_CSV)
    constants = load_constants()
    print(f"min_weekly filter: {min_weekly:g} pax/week (live fit uses {GRAVITY_MIN_WEEKLY:g})")

    print("=" * 78)
    print("INGEST")
    print("=" * 78)
    intl = build_weekly_anchors(INTL_DIR, airports)
    dom = build_weekly_anchors(US_DIR, airports)

    records = []
    for (o, d), weekly in intl.items():
        ao, ad = airports[o], airports[d]
        dist = haversine_nm(ao["lat"], ao["lon"], ad["lat"], ad["lon"])
        if dist < 50 or weekly < min_weekly:
            continue
        foreign = int(ao["country"] != "US") + int(ad["country"] != "US")
        records.append(
            {
                "o": o,
                "d": d,
                "weekly": weekly,
                "dist": dist,
                "band": band_of(dist),
                "sp": max(1.0, ao["score"]) * max(1.0, ad["score"]),
                "pred": gravity_current(ao, ad, dist, constants),
                "foreign_ends": foreign,
            }
        )

    dom_records = []
    for (o, d), weekly in dom.items():
        ao, ad = airports[o], airports[d]
        if ao["country"] != "US" or ad["country"] != "US":
            continue
        dist = haversine_nm(ao["lat"], ao["lon"], ad["lat"], ad["lon"])
        if dist < 50 or weekly < min_weekly:
            continue
        dom_records.append(
            {
                "weekly": weekly,
                "dist": dist,
                "band": band_of(dist),
                "sp": max(1.0, ao["score"]) * max(1.0, ad["score"]),
                "pred": gravity_current(ao, ad, dist, constants),
            }
        )

    print(f"\n  usable intl pairs: {len(records):,}   usable domestic pairs: {len(dom_records):,}")

    print()
    print("=" * 78)
    print("1. SCORE BIAS  — actual / predicted under the CURRENT US-fitted gravity")
    print("=" * 78)
    print("   ratio > 1 means the model UNDER-predicts real traffic.\n")
    print(f"   {'group':<34}{'n':>7}{'median':>10}{'p25':>10}{'p75':>10}")

    def ratio_row(label: str, rows: list[dict]) -> None:
        rs = [r["weekly"] / r["pred"] for r in rows if r["pred"] > 1e-9]
        if len(rs) < 20:
            print(f"   {label:<34}{len(rs):>7}   (too few)")
            return
        print(
            f"   {label:<34}{len(rs):>7}{statistics.median(rs):>10.2f}"
            f"{pct(rs, 0.25):>10.2f}{pct(rs, 0.75):>10.2f}"
        )

    ratio_row("US domestic (baseline)", dom_records)
    ratio_row("US <-> foreign (one foreign end)", [r for r in records if r["foreign_ends"] == 1])
    ratio_row("foreign <-> foreign", [r for r in records if r["foreign_ends"] == 2])
    print()
    for name, _, _ in BANDS:
        ratio_row(f"  US<->foreign, {name}", [r for r in records if r["foreign_ends"] == 1 and r["band"] == name])

    print()
    print("=" * 78)
    print("2. DISTANCE DECAY — beta fitted per band by OLS on logs")
    print("=" * 78)
    print(f"   current live params: k={constants.get('bts_gravity_k'):.8f} "
          f"alpha={constants.get('bts_gravity_alpha')} beta={constants.get('bts_gravity_beta')}\n")
    print(f"   {'band / source':<34}{'n':>7}{'alpha':>9}{'beta':>9}{'R^2':>8}")

    def fit_row(label: str, rows: list[dict]) -> None:
        fit = fit_log_ols([(r["sp"], r["dist"], r["weekly"]) for r in rows])
        if not fit:
            print(f"   {label:<34}{len(rows):>7}   (too few)")
            return
        print(
            f"   {label:<34}{fit['n']:>7}{fit['alpha']:>9.3f}"
            f"{fit['beta']:>9.3f}{fit['r2']:>8.3f}"
        )

    for name, _, _ in BANDS:
        fit_row(f"{name} / US domestic", [r for r in dom_records if r["band"] == name])
    print()
    for name, _, _ in BANDS:
        fit_row(f"{name} / intl (T-100)", [r for r in records if r["band"] == name])
    print()
    fit_row("ALL / US domestic", dom_records)
    fit_row("ALL / intl (T-100)", records)

    print()
    print("=" * 78)
    print("3. PLAUSIBILITY CAPS — real anchor distribution per band")
    print("=" * 78)
    print(f"   {'band':<16}{'n':>8}{'median':>11}{'p90':>11}{'p99':>11}{'max':>11}")
    combined = defaultdict(list)
    for r in dom_records:
        combined[r["band"]].append(r["weekly"])
    for r in records:
        combined[r["band"]].append(r["weekly"])
    for name, _, _ in BANDS:
        v = combined[name]
        if not v:
            continue
        print(
            f"   {name:<16}{len(v):>8,}{statistics.median(v):>11,.0f}"
            f"{pct(v, 0.90):>11,.0f}{pct(v, 0.99):>11,.0f}{max(v):>11,.0f}"
        )

    print()
    print("=" * 78)
    print("4. COVERAGE LOST TO MISSING AIRPORTS")
    print("=" * 78)
    missing: dict[str, float] = defaultdict(float)
    total_pax = kept_pax = 0.0
    for path in discover_year_files(INTL_DIR):
        if int(path.stem) not in (2018, 2019, 2023, 2024, 2025):
            continue
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                try:
                    pax = float(row.get("PASSENGERS") or 0)
                except (TypeError, ValueError):
                    continue
                if pax < 1:
                    continue
                total_pax += pax
                o = str(row.get("ORIGIN") or "").strip().upper()
                d = str(row.get("DEST") or "").strip().upper()
                if o in airports and d in airports:
                    kept_pax += pax
                else:
                    for x in (o, d):
                        if x not in airports:
                            missing[x] += pax
    print(f"   passengers covered: {kept_pax:,.0f} / {total_pax:,.0f} = {kept_pax/total_pax*100:.1f}%")
    ranked = sorted(missing.items(), key=lambda kv: -kv[1])
    running = kept_pax
    for target in (0.80, 0.90, 0.95):
        need = 0
        run = kept_pax
        for _, v in ranked:
            if run / total_pax >= target:
                break
            run += v
            need += 1
        print(f"   airports to add to reach {target*100:.0f}% coverage: {need}")
    print("\n   top 25 missing by lost passengers:")
    for code, v in ranked[:25]:
        print(f"     {code}  {v:>13,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
