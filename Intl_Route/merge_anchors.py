#!/usr/bin/env python3
"""
Phase 6d: merge US domestic and international anchors into the file the game loads.

    data/us_demand_anchors.csv     (DB1B, US domestic)
    data/intl_demand_anchors.csv   (T-100 International)
    data/korea_demand_anchors.csv  (airportal.go.kr, intra-Asian)
    data/japan_demand_anchors.csv  (e-Stat/MLIT, Japanese domestic)
    data/europe_demand_anchors.csv (Eurostat avia_par_*, Europe and its links)
    data/australia_demand_anchors.csv (BITRE via data.gov.au)
        -> data/bts_demand_anchors.csv

Each row keeps its own `method` tag, so the source of any anchor stays traceable
and db.load_bts_demand_anchors_if_empty() can detect a stale calibration.

Where a directional pair appears in more than one file, the first source listed
in SOURCES wins. Domestic outranks the rest because DB1B is ticket-based
origin-destination data while the others count segment boardings, and mixing
bases on a single pair would be inconsistent. Korea is last so it only fills
genuine gaps -- its Seoul-US routes duplicate T-100 coverage we already trust.

Usage:
    python3 Intl_Route/merge_anchors.py
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MERGED = ROOT / "data" / "bts_demand_anchors.csv"

# Highest precedence first.
SOURCES = [
    ("us", ROOT / "data" / "us_demand_anchors.csv", True),
    ("intl", ROOT / "data" / "intl_demand_anchors.csv", True),
    ("korea", ROOT / "data" / "korea_demand_anchors.csv", False),
    ("japan", ROOT / "data" / "japan_demand_anchors.csv", False),
    ("europe", ROOT / "data" / "europe_demand_anchors.csv", False),
    ("australia", ROOT / "data" / "australia_demand_anchors.csv", False),
]

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


def read(path: Path, required: bool) -> list[dict]:
    if not path.is_file():
        if required:
            raise SystemExit(f"missing {path} — run bts_calibrate.py first")
        print(f"  {path.name}: absent, skipping")
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    merged: dict[tuple[str, str], dict] = {}
    for name, path, required in SOURCES:
        rows = read(path, required)
        if not rows:
            continue
        kept = dropped = 0
        for row in rows:
            key = (row["origin_iata"], row["dest_iata"])
            if key in merged:
                dropped += 1
                continue
            merged[key] = row
            kept += 1
        note = f", {dropped:,} already covered" if dropped else ""
        print(f"  {name}: {len(rows):,} rows -> {kept:,} added{note}")

    rows = sorted(
        merged.values(),
        key=lambda r: (-float(r["anchor_weekly"]), r["origin_iata"], r["dest_iata"]),
    )
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
