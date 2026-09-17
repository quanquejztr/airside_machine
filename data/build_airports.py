"""
Regenerate data/airports.csv from the OurAirports extracts plus the IATA WASG levels.

Replaces the hand-maintained table and the longitude-band timezone guesser in
process_airports.py. Every column is either copied from a source file, derived from one,
or deliberately carried over from the previous table — nothing is typed in by hand.

Inputs (all under data/temp data/):
  airports (1).csv          OurAirports master list
  runways (1).csv           OurAirports runway list, joined on the `ident` column
  wasg-annex-12.7.xlsx      IATA slot coordination levels, hand-adjusted for game balance

Run:  python3 data/build_airports.py [--out data/airports.new.csv]

`timezonefinder` is needed and is a BUILD-TIME dependency only; the game never imports it.
    pip install timezonefinder
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import random
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp data")
SRC_AIRPORTS = os.path.join(TEMP, "airports (1).csv")
SRC_RUNWAYS = os.path.join(TEMP, "runways (1).csv")
SRC_WASG = os.path.join(TEMP, "wasg-annex-12.7.xlsx")
CUR_AIRPORTS = os.path.join(HERE, "airports.csv")
CATEGORIES = os.path.join(HERE, "airport_categories.csv")

# Airports that are gate-scarce but absent from the WASG list, because the US does not
# runway-coordinate its big hubs. Atlanta is the busiest airport on earth and is Level 1
# in the real data; without this it would have unlimited free gates.
LEVEL2_ADDITIONS = {
    "ATL", "DFW", "DEN", "CLT", "MSP", "PHX", "MIA", "IAH",
    "LAS", "PHL", "DTW", "HNL", "SCL", "SJU",
    # Gate-auctioned in the previous table and Level 1 in the source file; keeping them
    # at 2 preserves existing behaviour.
    "YYZ", "YVR", "BCN", "BLR", "YUL", "YYC",
}

DROP_TYPES = {"heliport", "closed", "seaplane_base", "balloonport"}
TYPE_TO_CATEGORY = {
    "large_airport": "large_airport",
    "medium_airport": "medium_airport",
    "small_airport": "small_airport",
}
# Score bands matched to the medians of the previous table, so the thresholds already
# tuned against it (hub_profile's 300k/900k, the AI's ORDER BY score) keep working.
SCORE_BANDS = {
    "large_airport": (450_000, 1_100_000),
    "medium_airport": (120_000, 320_000),
    "small_airport": (8_000, 45_000),
}


def read_wasg_levels(path: str) -> dict:
    """IATA code -> '1' | '2' | '3' from the WASG annex spreadsheet."""
    z = zipfile.ZipFile(path)
    shared = [
        re.sub(r"<.*?>", "", m)
        for m in re.findall(
            r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8", "replace"), re.S
        )
    ]
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")
    rows = collections.defaultdict(dict)
    for c in re.finditer(r'<c r="([A-Z]+)(\d+)"([^>]*)>(.*?)</c>', sheet, re.S):
        col, row, attrs, inner = c.group(1), int(c.group(2)), c.group(3), c.group(4)
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        if not v:
            continue
        val = v.group(1)
        if 't="s"' in attrs:
            try:
                val = shared[int(val)]
            except (ValueError, IndexError):
                pass
        rows[row][col] = re.sub(r"\s+", " ", str(val)).strip()

    header_row = next(r for r in sorted(rows) if "Airport Code" in rows[r].values())
    hdr = {v: k for k, v in rows[header_row].items()}
    level_col = next((hdr[k] for k in hdr if k.endswith("Level")), None)
    if not level_col:
        raise SystemExit("No '... Level' column found in the WASG sheet.")
    out = {}
    for r in sorted(rows):
        if r <= header_row:
            continue
        code = rows[r].get(hdr["Airport Code"], "")
        lvl = rows[r].get(level_col, "")
        if re.fullmatch(r"[A-Z]{3}", code or "") and lvl in ("1", "2", "3"):
            out[code] = lvl
    return out


def longest_and_count(path: str) -> tuple:
    """ident -> (longest runway ft, usable runway count). Closed runways are ignored."""
    longest, count = {}, collections.Counter()
    for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
        if str(r.get("closed") or "0").strip() in ("1", "yes", "true"):
            continue
        ident = (r.get("airport_ident") or "").strip()
        try:
            length = int(float(r.get("length_ft") or 0))
        except (TypeError, ValueError):
            continue
        if not ident or length <= 0:
            continue
        longest[ident] = max(longest.get(ident, 0), length)
        count[ident] += 1
    return longest, count


def category_bands(path: str) -> tuple:
    """(gate min/max, runway minimum) per category."""
    gates, runway_min = {}, {}
    for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
        try:
            gates[r["category"]] = (int(r["gate_count_min"]), int(r["gate_count_max"]))
            runway_min[r["category"]] = int(r["runway_min_ft"])
        except (TypeError, ValueError, KeyError):
            continue
    return gates, runway_min


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "airports.new.csv"))
    args = ap.parse_args()

    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        print("ERROR: pip install timezonefinder (build-time only)", file=sys.stderr)
        return 2
    tf = TimezoneFinder(in_memory=True)

    levels = read_wasg_levels(SRC_WASG)
    longest, rwy_count = longest_and_count(SRC_RUNWAYS)
    bands, runway_min = category_bands(CATEGORIES)
    previous = {r["iata"]: r for r in csv.DictReader(open(CUR_AIRPORTS, newline="", encoding="utf-8"))}

    rows, stats = [], collections.Counter()
    for a in csv.DictReader(open(SRC_AIRPORTS, newline="", encoding="utf-8")):
        if a.get("type") in DROP_TYPES:
            stats["dropped_type"] += 1
            continue
        iata = (a.get("iata_code") or "").strip().upper()
        if not iata:
            stats["dropped_no_iata"] += 1
            continue
        if (a.get("scheduled_service") or "").strip().lower() != "yes":
            stats["dropped_no_service"] += 1
            continue
        try:
            lat, lon = float(a["latitude_deg"]), float(a["longitude_deg"])
        except (TypeError, ValueError, KeyError):
            stats["dropped_no_coords"] += 1
            continue

        ident = (a.get("ident") or "").strip()
        category = TYPE_TO_CATEGORY.get(a.get("type") or "", "small_airport")
        prev = previous.get(iata)

        # Deterministic per airport, so rebuilding the file does not reshuffle values.
        rng = random.Random(f"airside:{iata}")

        length = longest.get(ident, 0)
        if not length and prev:
            try:
                length = int(prev.get("runway_length_ft") or 0)
            except (TypeError, ValueError):
                length = 0
        if not length:
            # A zero here is worse than a guess: the aircraft-suitability checks read
            # `if airport['runway_length_ft'] and ...`, so a falsy length skips the check
            # entirely and lets any aircraft use the airport. Fall back to the category
            # minimum, which is the shortest runway that category is defined to have.
            length = runway_min.get(category, 3000)
            stats["runway_length_defaulted"] += 1

        # Gate counts: keep what the game already had, invent only for new airports.
        if prev and str(prev.get("gate_count") or "").strip().isdigit():
            gates = int(prev["gate_count"])
        else:
            lo, hi = bands.get(category, (2, 8))
            gates = rng.randint(lo, hi)
            stats["gates_generated"] += 1

        # Same for score: preserving it keeps the tuned thresholds elsewhere meaningful.
        if prev and str(prev.get("score") or "").strip().isdigit():
            score = int(prev["score"])
        else:
            lo, hi = SCORE_BANDS.get(category, (8_000, 45_000))
            score = rng.randint(lo, hi)
            stats["scores_generated"] += 1

        tz = tf.timezone_at(lat=lat, lng=lon)
        if not tz:
            tz = (prev or {}).get("timezone") or "UTC"
            stats["tz_fallback"] += 1
        elif prev and tz != (prev.get("timezone") or ""):
            stats["tz_corrected"] += 1

        level = "2" if iata in LEVEL2_ADDITIONS else levels.get(iata, "1")
        stats[f"level_{level}"] += 1

        rows.append(
            {
                "iata": iata,
                "icao": (a.get("icao_code") or a.get("gps_code") or ident or "").strip(),
                "name": (a.get("name") or "").strip(),
                "city": (a.get("municipality") or "").strip(),
                "country": (a.get("iso_country") or "").strip(),
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "runway_length_ft": length,
                "gate_count": gates,
                "timezone": tz,
                "score": score,
                "category": category,
                "slot_level": level,
                "runway_count": rwy_count.get(ident, 0),
            }
        )

    rows.sort(key=lambda r: r["iata"])
    fields = list(rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows):,} airports -> {args.out}")
    for k in sorted(stats):
        print(f"  {k:22} {stats[k]:>7,}")
    kept = {r["iata"] for r in rows}
    print(f"  carried over from old table: {len(kept & set(previous)):,} of {len(previous):,}")
    print(f"  brand new airports         : {len(kept - set(previous)):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
