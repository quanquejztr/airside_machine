#!/usr/bin/env python3
"""
Phase 6d: merge US domestic and international anchors into the file the game loads.

    data/us_demand_anchors.csv    (DB1B, US domestic)
    data/intl_demand_anchors.csv  (T-100 International)
        -> data/bts_demand_anchors.csv

Each row keeps its own `method` tag, so the source of any anchor stays traceable
and db.load_bts_demand_anchors_if_empty() can detect a stale calibration.

Where a directional pair appears in both files the domestic row wins: DB1B is
ticket-based origin-destination data, while T-100 counts segment boardings, and
mixing the two on one pair would be inconsistent.

Usage:
    python3 Intl_Route/merge_anchors.py
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
US_ANCHORS = ROOT / "data" / "us_demand_anchors.csv"
INTL_ANCHORS = ROOT / "data" / "intl_demand_anchors.csv"
MERGED = ROOT / "data" / "bts_demand_anchors.csv"

FIELDS = [
    "origin_iata",
    "dest_iata",
    "anchor_annual",
    "anchor_weekly",
    "years_used",
    "first_year",
    "last_year",
    "method",
]


def read(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"missing {path} — run bts_calibrate.py first")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    us = read(US_ANCHORS)
    intl = read(INTL_ANCHORS)
    print(f"  us:   {len(us):,} rows")
    print(f"  intl: {len(intl):,} rows")

    merged: dict[tuple[str, str], dict] = {}
    for row in us:
        merged[(row["origin_iata"], row["dest_iata"])] = row
    overlaps = 0
    for row in intl:
        key = (row["origin_iata"], row["dest_iata"])
        if key in merged:
            overlaps += 1
            continue
        merged[key] = row

    rows = sorted(
        merged.values(),
        key=lambda r: (-float(r["anchor_weekly"]), r["origin_iata"], r["dest_iata"]),
    )
    print(f"  overlapping pairs (domestic kept): {overlaps:,}")
    print(f"  merged: {len(rows):,} rows")

    with MERGED.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in FIELDS})

    by_method: dict[str, int] = {}
    for row in rows:
        by_method[row["method"]] = by_method.get(row["method"], 0) + 1
    for method, n in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"    {method}: {n:,}")
    print(f"\nWrote {MERGED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
