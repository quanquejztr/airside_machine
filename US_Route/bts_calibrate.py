#!/usr/bin/env python3
"""
Build US BTS route demand anchors from yearly US_Route/*.csv files.

Reads PASSENGERS, ORIGIN, DEST per year (year from filename), applies cleaning
and Claude Stage-2 anchor math (trend-adjust to REF_YEAR + weighted median),
then writes:

  data/bts_demand_anchors.csv
  data/bts_gravity_params.json

Run from repo root:
  python3 US_Route/bts_calibrate.py
  python3 US_Route/bts_calibrate.py --write-clean   # optional audit CSV
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
US_ROUTE_DIR = Path(__file__).resolve().parent
AIRPORTS_CSV = ROOT / "data" / "airports.csv"
OUT_ANCHORS = ROOT / "data" / "bts_demand_anchors.csv"
OUT_GRAVITY = ROOT / "data" / "bts_gravity_params.json"
OUT_CLEAN = ROOT / "data" / "bts_clean.csv"

# Stage-2 defaults (overridable via financial_constants later at runtime).
REF_YEAR = 2019
GROWTH_RATE = 0.02
DECAY_LAMBDA = 0.85
EXCLUDE_YEARS = frozenset({2020, 2021})
# Recent-market blend: median of trend-adjusted years in this window, then
# annual = max(weighted_median, recent_median). Lifts growth OD pairs (e.g. TPA-SAN)
# without flattening long-run structure on stable trunks.
RECENT_YEAR_MIN = 2019
METHOD_TAG = "bts_trend_wm_max_recent_v2"

# Gravity grid search bounds (weekly pax units; fit on anchor_weekly >= MIN_WK_GRAVITY).
GRAVITY_MIN_WEEKLY = 10.0
GRAVITY_ALPHA_RANGE = [x / 100.0 for x in range(40, 81, 5)]
GRAVITY_BETA_RANGE = [x / 100.0 for x in range(80, 141, 5)]


def load_game_airports(path: Path) -> dict[str, dict]:
    airports: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iata = str(row.get("iata") or "").strip().upper()
            if len(iata) != 3:
                continue
            airports[iata] = {
                "iata": iata,
                "country": str(row.get("country") or "").strip().upper(),
                "category": str(row.get("category") or "").strip(),
                "score": float(row.get("score") or 0),
                "lat": float(row.get("lat") or 0),
                "lon": float(row.get("lon") or 0),
            }
    return airports


def discover_year_files(us_dir: Path) -> list[Path]:
    return sorted(us_dir.glob("[0-9][0-9][0-9][0-9].csv"), key=lambda p: int(p.stem))


def duplicate_files_to_skip(files: list[Path]) -> set[str]:
    """If two year files are byte-identical, keep the earlier year only."""
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for p in files:
        digest = hashlib.md5(p.read_bytes()).hexdigest()
        by_hash[digest].append(p)
    skip: set[str] = set()
    for group in by_hash.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: int(p.stem))
        for dup in ordered[1:]:
            skip.add(dup.name)
    return skip


def trend_adjust(pax: float, year: int, *, ref_year: int, growth: float) -> float:
    if year <= ref_year:
        return pax * ((1.0 + growth) ** (ref_year - year))
    return pax / ((1.0 + growth) ** (year - ref_year))


def year_weight(year: int, *, ref_year: int, decay: float) -> float:
    if year in EXCLUDE_YEARS:
        return 0.0
    return decay ** abs(ref_year - year)


def weighted_median(values: list[float], weights: list[float]) -> float | None:
    pairs = sorted(
        [(float(v), float(w)) for v, w in zip(values, weights) if w > 0],
        key=lambda x: x[0],
    )
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    half = total / 2.0
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return value
    return pairs[-1][0]


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))
    return 3440.065 * c


def ingest_yearly_files(
    files: list[Path],
    skip_names: set[str],
    airports: dict[str, dict],
    *,
    write_clean: Path | None,
) -> tuple[dict[tuple[str, str], dict[int, float]], dict[str, int]]:
    per_od_year: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    stats: dict[str, int] = defaultdict(int)

    clean_writer = None
    clean_file = None
    if write_clean is not None:
        write_clean.parent.mkdir(parents=True, exist_ok=True)
        clean_file = write_clean.open("w", newline="", encoding="utf-8")
        clean_writer = csv.writer(clean_file)
        clean_writer.writerow(["origin_iata", "dest_iata", "year", "passengers"])

    try:
        for path in files:
            if path.name in skip_names:
                stats["files_skipped_duplicate"] += 1
                continue
            year = int(path.stem)
            if year in EXCLUDE_YEARS:
                stats["files_skipped_covid"] += 1
                continue

            stats["files_read"] += 1
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or "PASSENGERS" not in reader.fieldnames:
                    raise ValueError(f"{path.name}: expected PASSENGERS,ORIGIN,DEST columns")
                for row in reader:
                    stats["rows_read"] += 1
                    try:
                        pax = float(row.get("PASSENGERS") or 0)
                    except (TypeError, ValueError):
                        stats["rows_bad_pax"] += 1
                        continue
                    if pax < 1.0:
                        stats["rows_zero_pax"] += 1
                        continue

                    origin = str(row.get("ORIGIN") or "").strip().upper()
                    dest = str(row.get("DEST") or "").strip().upper()
                    if origin not in airports:
                        stats["rows_origin_not_in_game"] += 1
                        continue
                    if dest not in airports:
                        stats["rows_dest_not_in_game"] += 1
                        continue
                    if origin == dest:
                        stats["rows_same_airport"] += 1
                        continue

                    per_od_year[(origin, dest)][year] += pax
                    stats["rows_kept"] += 1
                    if clean_writer is not None:
                        clean_writer.writerow([origin, dest, year, round(pax, 2)])
    finally:
        if clean_file is not None:
            clean_file.close()

    return per_od_year, stats


def build_anchors(
    per_od_year: dict[tuple[str, str], dict[int, float]],
    *,
    ref_year: int,
    growth: float,
    decay: float,
) -> list[dict]:
    rows: list[dict] = []
    for (origin, dest), year_map in per_od_year.items():
        adjusted: list[float] = []
        weights: list[float] = []
        years_present: list[int] = []
        recent_adjusted: list[float] = []
        for year, pax in sorted(year_map.items()):
            weight = year_weight(year, ref_year=ref_year, decay=decay)
            if weight <= 0:
                continue
            adj = trend_adjust(pax, year, ref_year=ref_year, growth=growth)
            adjusted.append(adj)
            weights.append(weight)
            years_present.append(year)
            if year >= RECENT_YEAR_MIN:
                recent_adjusted.append(adj)
        if not adjusted:
            continue
        annual = weighted_median(adjusted, weights)
        if annual is None or annual <= 0:
            continue
        if recent_adjusted:
            recent_med = weighted_median(
                recent_adjusted, [1.0] * len(recent_adjusted)
            )
            if recent_med is not None and recent_med > annual:
                annual = recent_med
        rows.append(
            {
                "origin_iata": origin,
                "dest_iata": dest,
                "anchor_annual": round(annual, 2),
                "anchor_weekly": round(annual / 52.0, 4),
                "years_used": len(years_present),
                "first_year": min(years_present),
                "last_year": max(years_present),
                "method": METHOD_TAG,
            }
        )
    rows.sort(key=lambda r: (-float(r["anchor_weekly"]), r["origin_iata"], r["dest_iata"]))
    return rows


def fit_gravity(anchors: list[dict], airports: dict[str, dict]) -> dict:
    """Grid-search fit: weekly = k * (score_o*score_d)^alpha / distance^beta."""
    points: list[tuple[float, float, float]] = []
    for row in anchors:
        weekly = float(row["anchor_weekly"])
        if weekly < GRAVITY_MIN_WEEKLY:
            continue
        o = airports[row["origin_iata"]]
        d = airports[row["dest_iata"]]
        dist = max(50.0, haversine_nm(o["lat"], o["lon"], d["lat"], d["lon"]))
        score = max(1.0, float(o["score"]) * float(d["score"]))
        points.append((weekly, score, dist))

    if len(points) < 100:
        return {
            "k": 0.0005,
            "alpha": 0.60,
            "beta": 1.20,
            "small_small_damp": 0.73,
            "fit_points": len(points),
            "method": "fallback_defaults",
        }

    best_sse = None
    best_k = best_alpha = best_beta = 0.0
    for alpha in GRAVITY_ALPHA_RANGE:
        for beta in GRAVITY_BETA_RANGE:
            num = den = 0.0
            for weekly, score, dist in points:
                pred_shape = (score ** alpha) / (dist ** beta)
                num += weekly * pred_shape
                den += pred_shape * pred_shape
            if den <= 0:
                continue
            k = num / den
            sse = 0.0
            for weekly, score, dist in points:
                pred = k * (score ** alpha) / (dist ** beta)
                sse += (math.log(max(1e-9, weekly)) - math.log(max(1e-9, pred))) ** 2
            if best_sse is None or sse < best_sse:
                best_sse, best_k, best_alpha, best_beta = sse, k, alpha, beta

    ss_actual = 0.0
    ss_pred = 0.0
    ss_n = 0
    for row in anchors:
        o_ap = airports[row["origin_iata"]]
        d_ap = airports[row["dest_iata"]]
        if o_ap["category"] != "small_airport" or d_ap["category"] != "small_airport":
            continue
        weekly = float(row["anchor_weekly"])
        if weekly < 1.0:
            continue
        dist = max(50.0, haversine_nm(o_ap["lat"], o_ap["lon"], d_ap["lat"], d_ap["lon"]))
        score = max(1.0, float(o_ap["score"]) * float(d_ap["score"]))
        pred = best_k * (score ** best_alpha) / (dist ** best_beta)
        if pred <= 0:
            continue
        ss_actual += weekly
        ss_pred += pred
        ss_n += 1

    damp = (ss_actual / ss_pred) if ss_pred > 0 else 0.73

    return {
        "k": round(best_k, 8),
        "alpha": round(best_alpha, 4),
        "beta": round(best_beta, 4),
        "small_small_damp": round(damp, 4),
        "fit_points": len(points),
        "small_small_pairs": ss_n,
        "log_sse": round(best_sse or 0.0, 2),
        "method": "grid_search_log_mse",
        "min_weekly_for_fit": GRAVITY_MIN_WEEKLY,
    }


def write_anchors(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "origin_iata",
        "dest_iata",
        "anchor_annual",
        "anchor_weekly",
        "years_used",
        "first_year",
        "last_year",
        "method",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fields})


def print_report(
    rows: list[dict],
    ingest_stats: dict[str, int],
    skip_names: set[str],
    gravity: dict,
) -> None:
    weeklies = sorted(float(r["anchor_weekly"]) for r in rows)
    n = len(weeklies)

    def pct(p: float) -> float:
        if n == 0:
            return 0.0
        idx = min(n - 1, int(n * p))
        return weeklies[idx]

    print("=" * 72)
    print("BTS calibration complete")
    print("=" * 72)
    print(f"Files skipped (duplicate): {sorted(skip_names)}")
    print(f"Files read: {ingest_stats.get('files_read', 0)}")
    print(f"Rows read: {ingest_stats.get('rows_read', 0):,}")
    print(f"Rows kept: {ingest_stats.get('rows_kept', 0):,}")
    print(f"Unique anchors: {n:,}")
    print(
        "Anchor weekly percentiles: "
        f"p50={pct(0.50):,.0f}  p90={pct(0.90):,.0f}  p99={pct(0.99):,.0f}  max={pct(1.0):,.0f}"
    )
    print(f"Weekly < 1: {sum(1 for w in weeklies if w < 1):,}  weekly < 10: {sum(1 for w in weeklies if w < 10):,}")

    print("\nTop 10 markets:")
    for row in rows[:10]:
        print(
            f"  {row['origin_iata']}->{row['dest_iata']}: "
            f"{float(row['anchor_weekly']):,.0f}/wk  ({float(row['anchor_annual']):,.0f}/yr, "
            f"{row['years_used']} yrs)"
        )

    checks = [("ATL", "LAX"), ("LAX", "SFO"), ("ATL", "MCO")]
    print("\nSanity routes:")
    by_od = {(r["origin_iata"], r["dest_iata"]): r for r in rows}
    for od in checks:
        hit = by_od.get(od)
        if hit:
            print(
                f"  {od[0]}->{od[1]}: weekly={float(hit['anchor_weekly']):,.0f}  "
                f"years={hit['years_used']}"
            )
        else:
            print(f"  {od[0]}->{od[1]}: MISSING")

    print("\nGravity fit:")
    for key in ("k", "alpha", "beta", "small_small_damp", "fit_points", "log_sse"):
        print(f"  {key}: {gravity.get(key)}")

    # Mode B calibration preview (target_share from CSV default 0.90, pdm=9, seg 2/3.5)
    eff = 0.30 * 9.0 * 2.0 + 0.70 * 9.0 * 3.5
    cal_k = 0.90 / eff
    print(f"\nCalibration preview (base_total = anchor_weekly × {cal_k:.4f}, share=0.90):")
    for od in checks:
        hit = by_od.get(od)
        if hit:
            base = float(hit["anchor_weekly"]) * cal_k
            print(f"  {od[0]}->{od[1]}: base_total≈{base:.0f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build BTS demand anchors from US_Route/*.csv")
    parser.add_argument(
        "--write-clean",
        action="store_true",
        help="Also write data/bts_clean.csv (large audit file)",
    )
    parser.add_argument(
        "--us-dir",
        type=Path,
        default=US_ROUTE_DIR,
        help="Directory containing YYYY.csv files",
    )
    args = parser.parse_args(argv)

    if not AIRPORTS_CSV.is_file():
        print(f"Missing {AIRPORTS_CSV}", file=sys.stderr)
        return 1

    airports = load_game_airports(AIRPORTS_CSV)
    year_files = discover_year_files(args.us_dir)
    if not year_files:
        print(f"No year CSV files in {args.us_dir}", file=sys.stderr)
        return 1

    skip_names = duplicate_files_to_skip(year_files)
    clean_path = OUT_CLEAN if args.write_clean else None

    per_od_year, ingest_stats = ingest_yearly_files(
        year_files,
        skip_names,
        airports,
        write_clean=clean_path,
    )
    anchors = build_anchors(
        per_od_year,
        ref_year=REF_YEAR,
        growth=GROWTH_RATE,
        decay=DECAY_LAMBDA,
    )
    gravity = fit_gravity(anchors, airports)

    write_anchors(OUT_ANCHORS, anchors)
    OUT_GRAVITY.parent.mkdir(parents=True, exist_ok=True)
    OUT_GRAVITY.write_text(
        json.dumps(
            {
                "ref_year": REF_YEAR,
                "growth_rate": GROWTH_RATE,
                "decay_lambda": DECAY_LAMBDA,
                "exclude_years": sorted(EXCLUDE_YEARS),
                "skipped_duplicate_files": sorted(skip_names),
                "method": METHOD_TAG,
                **gravity,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print_report(anchors, ingest_stats, skip_names, gravity)
    print(f"\nWrote {OUT_ANCHORS} ({len(anchors):,} rows)")
    print(f"Wrote {OUT_GRAVITY}")
    if clean_path is not None:
        print(f"Wrote {clean_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
