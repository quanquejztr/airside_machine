#!/usr/bin/env python3
"""
Phase 6a: add the international airports that T-100 traffic flows through but
data/airports.csv is missing.

These 24 airports carry 21.7% of all T-100 international passengers and were
silently dropped during calibration, which forced routes like JFK-FRA onto the
LEGACY fallback. Adding them lifts airport coverage from 68.3% to ~90%.

Objective fields (icao, lat, lon, name, city, country, runway_length_ft) come from
OurAirports. Game fields (score, category, gate_count) are assigned by matching each
airport to real-world peers already present in airports.csv -- e.g. FRA is sized
against CDG/LHR, which sit at 1.45-1.50M. Scores are NOT fitted from T-100 volume:
US-facing traffic is a poor proxy for airport size (regression R^2 was 0.23, since
CUN is US-heavy while FRA is not).

Idempotent: skips any IATA already present.

Usage:
    python3 Intl_Route/backfill_airports.py --oa-dir /tmp   [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_CSV = ROOT / "data" / "airports.csv"

# score / category / gate_count / timezone, assigned by peer comparison.
# Peer reference points already in airports.csv:
#   LHR 1,500,000  CDG 1,450,000  AMS 1,450,000  YYZ 1,500,000  MEX 1,500,000
#   GDL 1,200,000  PUJ 1,100,000 | large >=300k, medium 35k-480k
CURATED: dict[str, dict] = {
    # --- European mega hubs -------------------------------------------------
    "FRA": {"score": 1_450_000, "category": "large_airport", "gates": 140, "tz": "Europe/Berlin"},
    "IST": {"score": 1_500_000, "category": "large_airport", "gates": 140, "tz": "Europe/Istanbul"},
    "MAD": {"score": 1_400_000, "category": "large_airport", "gates": 104, "tz": "Europe/Madrid"},
    "MUC": {"score": 1_300_000, "category": "large_airport", "gates": 90, "tz": "Europe/Berlin"},
    "BCN": {"score": 1_300_000, "category": "large_airport", "gates": 80, "tz": "Europe/Madrid"},
    "FCO": {"score": 1_250_000, "category": "large_airport", "gates": 85, "tz": "Europe/Rome"},
    "ZRH": {"score": 1_200_000, "category": "large_airport", "gates": 70, "tz": "Europe/Zurich"},
    "LIS": {"score": 1_200_000, "category": "large_airport", "gates": 65, "tz": "Europe/Lisbon"},
    "DUB": {"score": 1_200_000, "category": "large_airport", "gates": 70, "tz": "Europe/Dublin"},
    "CPH": {"score": 1_150_000, "category": "large_airport", "gates": 60, "tz": "Europe/Copenhagen"},
    "MXP": {"score": 1_100_000, "category": "large_airport", "gates": 60, "tz": "Europe/Rome"},
    # --- Middle East / Asia -------------------------------------------------
    "DEL": {"score": 1_450_000, "category": "large_airport", "gates": 110, "tz": "Asia/Kolkata"},
    "DOH": {"score": 1_350_000, "category": "large_airport", "gates": 90, "tz": "Asia/Qatar"},
    "TLV": {"score": 1_100_000, "category": "large_airport", "gates": 55, "tz": "Asia/Jerusalem"},
    # --- Latin America ------------------------------------------------------
    "GRU": {"score": 1_300_000, "category": "large_airport", "gates": 95, "tz": "America/Sao_Paulo"},
    "BOG": {"score": 1_200_000, "category": "large_airport", "gates": 70, "tz": "America/Bogota"},
    "SCL": {"score": 1_100_000, "category": "large_airport", "gates": 65, "tz": "America/Santiago"},
    "LIM": {"score": 1_100_000, "category": "large_airport", "gates": 60, "tz": "America/Lima"},
    "EZE": {"score": 900_000, "category": "large_airport", "gates": 40, "tz": "America/Argentina/Buenos_Aires"},
    # --- North Atlantic / Caribbean / Pacific leisure -----------------------
    "KEF": {"score": 700_000, "category": "large_airport", "gates": 25, "tz": "Atlantic/Reykjavik"},
    "GUM": {"score": 480_000, "category": "medium_airport", "gates": 20, "tz": "Pacific/Guam"},
    "AUA": {"score": 460_000, "category": "medium_airport", "gates": 14, "tz": "America/Aruba"},
    "PLS": {"score": 420_000, "category": "medium_airport", "gates": 12, "tz": "America/Grand_Turk"},
    "GCM": {"score": 400_000, "category": "medium_airport", "gates": 12, "tz": "America/Cayman"},
    # --- Wave 2 ------------------------------------------------------------
    # Ranking wave 1 by US-facing traffic missed hubs whose volume is mostly
    # regional (Mumbai, Bangalore, Johannesburg...). These are selected on global
    # importance instead, and without them whole regions are unreachable.
    "BOM": {"score": 1_350_000, "category": "large_airport", "gates": 95, "tz": "Asia/Kolkata"},
    "BLR": {"score": 1_270_000, "category": "large_airport", "gates": 75, "tz": "Asia/Kolkata"},
    "HYD": {"score": 1_080_000, "category": "large_airport", "gates": 50, "tz": "Asia/Kolkata"},
    "MAA": {"score": 1_050_000, "category": "large_airport", "gates": 50, "tz": "Asia/Kolkata"},
    "CCU": {"score": 1_020_000, "category": "large_airport", "gates": 45, "tz": "Asia/Kolkata"},
    # Middle East
    "JED": {"score": 1_250_000, "category": "large_airport", "gates": 70, "tz": "Asia/Riyadh"},
    "RUH": {"score": 1_150_000, "category": "large_airport", "gates": 60, "tz": "Asia/Riyadh"},
    "AUH": {"score": 1_050_000, "category": "large_airport", "gates": 55, "tz": "Asia/Dubai"},
    # Africa
    "CAI": {"score": 1_100_000, "category": "large_airport", "gates": 55, "tz": "Africa/Cairo"},
    "JNB": {"score": 1_050_000, "category": "large_airport", "gates": 60, "tz": "Africa/Johannesburg"},
    "ADD": {"score": 920_000, "category": "large_airport", "gates": 40, "tz": "Africa/Addis_Ababa"},
    "CPT": {"score": 880_000, "category": "large_airport", "gates": 35, "tz": "Africa/Johannesburg"},
    "CMN": {"score": 880_000, "category": "large_airport", "gates": 35, "tz": "Africa/Casablanca"},
    "LOS": {"score": 700_000, "category": "large_airport", "gates": 25, "tz": "Africa/Lagos"},
    "NBO": {"score": 700_000, "category": "large_airport", "gates": 25, "tz": "Africa/Nairobi"},
    # Europe (second tier by traffic, still major hubs)
    "SVO": {"score": 1_150_000, "category": "large_airport", "gates": 60, "tz": "Europe/Moscow"},
    "VIE": {"score": 1_150_000, "category": "large_airport", "gates": 60, "tz": "Europe/Vienna"},
    "ATH": {"score": 1_120_000, "category": "large_airport", "gates": 55, "tz": "Europe/Athens"},
    "OSL": {"score": 1_120_000, "category": "large_airport", "gates": 55, "tz": "Europe/Oslo"},
    "ARN": {"score": 1_080_000, "category": "large_airport", "gates": 50, "tz": "Europe/Stockholm"},
    "LED": {"score": 1_000_000, "category": "large_airport", "gates": 40, "tz": "Europe/Moscow"},
    "WAW": {"score": 1_000_000, "category": "large_airport", "gates": 40, "tz": "Europe/Warsaw"},
    "HEL": {"score": 960_000, "category": "large_airport", "gates": 40, "tz": "Europe/Helsinki"},
    "PRG": {"score": 960_000, "category": "large_airport", "gates": 40, "tz": "Europe/Prague"},
    "BUD": {"score": 960_000, "category": "large_airport", "gates": 40, "tz": "Europe/Budapest"},
    # Latin America
    "MDE": {"score": 940_000, "category": "large_airport", "gates": 35, "tz": "America/Bogota"},
    "UIO": {"score": 620_000, "category": "large_airport", "gates": 20, "tz": "America/Guayaquil"},
    # --- Wave 3: airports the Korean route data flies to ---------------------
    # airportal.go.kr references these; without them those rows are dropped and
    # we lose intra-Asian markets that exist nowhere else in our sources.
    "TSN": {"score": 1_080_000, "category": "large_airport", "gates": 50, "tz": "Asia/Shanghai"},
    "VCE": {"score": 880_000, "category": "large_airport", "gates": 35, "tz": "Europe/Rome"},
    "CMB": {"score": 830_000, "category": "large_airport", "gates": 30, "tz": "Asia/Colombo"},
    "MFM": {"score": 800_000, "category": "large_airport", "gates": 30, "tz": "Asia/Macau"},
    "ALA": {"score": 680_000, "category": "large_airport", "gates": 25, "tz": "Asia/Almaty"},
    "OVB": {"score": 660_000, "category": "large_airport", "gates": 25, "tz": "Asia/Novosibirsk"},
    "NQZ": {"score": 600_000, "category": "large_airport", "gates": 20, "tz": "Asia/Almaty"},
    "MLE": {"score": 540_000, "category": "large_airport", "gates": 20, "tz": "Indian/Maldives"},
    "REP": {"score": 540_000, "category": "large_airport", "gates": 20, "tz": "Asia/Phnom_Penh"},
    "KTI": {"score": 540_000, "category": "large_airport", "gates": 20, "tz": "Asia/Phnom_Penh"},
    "TAS": {"score": 500_000, "category": "large_airport", "gates": 20, "tz": "Asia/Tashkent"},
    "WRO": {"score": 500_000, "category": "large_airport", "gates": 18, "tz": "Europe/Warsaw"},
    "ZAG": {"score": 490_000, "category": "large_airport", "gates": 18, "tz": "Europe/Zagreb"},
    "VVO": {"score": 470_000, "category": "large_airport", "gates": 15, "tz": "Asia/Vladivostok"},
    "IKT": {"score": 445_000, "category": "large_airport", "gates": 14, "tz": "Asia/Irkutsk"},
    "KHV": {"score": 440_000, "category": "medium_airport", "gates": 12, "tz": "Asia/Vladivostok"},
    "KWJ": {"score": 440_000, "category": "medium_airport", "gates": 10, "tz": "Asia/Seoul"},
    "YTY": {"score": 440_000, "category": "medium_airport", "gates": 10, "tz": "Asia/Shanghai"},
    "ULN": {"score": 430_000, "category": "large_airport", "gates": 14, "tz": "Asia/Ulaanbaatar"},
    "SPN": {"score": 420_000, "category": "medium_airport", "gates": 10, "tz": "Pacific/Saipan"},
    "BSZ": {"score": 420_000, "category": "large_airport", "gates": 14, "tz": "Asia/Bishkek"},
    "KCZ": {"score": 415_000, "category": "large_airport", "gates": 10, "tz": "Asia/Tokyo"},
    "UUS": {"score": 405_000, "category": "large_airport", "gates": 10, "tz": "Asia/Sakhalin"},
    "CIT": {"score": 395_000, "category": "large_airport", "gates": 10, "tz": "Asia/Almaty"},
    "ASB": {"score": 395_000, "category": "large_airport", "gates": 12, "tz": "Asia/Ashgabat"},
    "YKS": {"score": 380_000, "category": "large_airport", "gates": 10, "tz": "Asia/Yakutsk"},
    "RSU": {"score": 370_000, "category": "medium_airport", "gates": 6, "tz": "Asia/Seoul"},
    "OBO": {"score": 370_000, "category": "medium_airport", "gates": 6, "tz": "Asia/Tokyo"},
    "UUD": {"score": 350_000, "category": "large_airport", "gates": 8, "tz": "Asia/Irkutsk"},
    "ROR": {"score": 150_000, "category": "large_airport", "gates": 5, "tz": "Pacific/Palau"},
    "SHI": {"score": 120_000, "category": "medium_airport", "gates": 4, "tz": "Asia/Tokyo"},
    # --- Wave 4: Japanese domestic points from the e-Stat flow matrix --------
    "GAJ": {"score": 370_000, "category": "medium_airport", "gates": 5, "tz": "Asia/Tokyo"},
    "OKD": {"score": 350_000, "category": "medium_airport", "gates": 5, "tz": "Asia/Tokyo"},
    "HAC": {"score": 200_000, "category": "medium_airport", "gates": 3, "tz": "Asia/Tokyo"},
    "OGN": {"score": 110_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "KKX": {"score": 90_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "AXJ": {"score": 90_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "RIS": {"score": 70_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "OIM": {"score": 50_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "MYE": {"score": 45_000, "category": "medium_airport", "gates": 2, "tz": "Asia/Tokyo"},
    "OIR": {"score": 35_000, "category": "medium_airport", "gates": 1, "tz": "Asia/Tokyo"},
}

# Codes the Korean statistics still use for airports OurAirports lists under a
# newer IATA code. Folded together so one physical airport isn't split in two.
IATA_ALIASES = {"TSE": "NQZ", "FRU": "BSZ"}


def load_ourairports(oa_dir: Path) -> tuple[dict[str, dict], dict[str, float]]:
    airports: dict[str, dict] = {}
    with (oa_dir / "oa_airports.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iata = (row.get("iata_code") or "").strip().upper()
            if iata in CURATED:
                airports[iata] = row

    runways: dict[str, float] = {}
    idents = {r["ident"] for r in airports.values()}
    with (oa_dir / "oa_runways.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ref = (row.get("airport_ident") or "").strip()
            if ref not in idents:
                continue
            try:
                length = float(row.get("length_ft") or 0)
            except ValueError:
                continue
            if length > runways.get(ref, 0.0):
                runways[ref] = length
    return airports, runways


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill international airports")
    parser.add_argument("--oa-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with AIRPORTS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        existing = list(reader)
    have = {r["iata"] for r in existing}

    oa, runways = load_ourairports(args.oa_dir)

    new_rows = []
    for iata, meta in CURATED.items():
        if iata in have:
            print(f"  skip {iata}: already present")
            continue
        src = oa.get(iata)
        if src is None:
            print(f"  WARN {iata}: not found in OurAirports")
            continue
        ident = src["ident"]
        runway = runways.get(ident, 0.0)
        if runway <= 0:
            print(f"  WARN {iata}: no runway length")
        new_rows.append(
            {
                "iata": iata,
                "icao": (src.get("icao_code") or ident or "").strip().upper(),
                "name": (src.get("name") or "").strip(),
                "city": (src.get("municipality") or "").strip(),
                "country": (src.get("iso_country") or "").strip().upper(),
                "lat": f"{float(src['latitude_deg']):.6f}",
                "lon": f"{float(src['longitude_deg']):.6f}",
                "runway_length_ft": str(int(round(runway))),
                "gate_count": str(meta["gates"]),
                "timezone": meta["tz"],
                "score": str(meta["score"]),
                "category": meta["category"],
            }
        )

    print(f"\n{len(new_rows)} airports to add:")
    for r in new_rows:
        print(
            f"  {r['iata']} {r['icao']:<5} {r['country']:<3} "
            f"{r['lat']:>11},{r['lon']:>11}  rwy={r['runway_length_ft']:>6}  "
            f"score={int(r['score']):>9,}  {r['name'][:38]}"
        )

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    merged = existing + new_rows
    merged.sort(key=lambda r: r["iata"])
    with AIRPORTS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged)
    print(f"\nwrote {AIRPORTS_CSV} ({len(merged)} airports)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
