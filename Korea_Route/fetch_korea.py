#!/usr/bin/env python3
"""
Download Korean route-level passenger statistics from airportal.go.kr.

Korea publishes scheduled passengers per airport pair for both domestic and
international routes, which is the only free source found that supplies genuine
intra-Asian (AS|AS) markets — every anchor we had before this touched the US.

Writes Korea_Route/YYYY.csv with PASSENGERS,ORIGIN,DEST so the existing
US_Route/bts_calibrate.py can consume it unchanged:

  python3 Korea_Route/fetch_korea.py
  python3 US_Route/bts_calibrate.py --us-dir Korea_Route \
      --out-anchors data/korea_demand_anchors.csv \
      --out-gravity data/korea_gravity_params.json \
      --method-tag kr_trend_wm_max_recent_v2

Notes on the source's own conventions (stated on the site):
  - International figures are reported against the Korean airport, so an
    arrival row means foreign -> Korea and is emitted reversed.
  - pass_gubun=4 is 유임+환승 (paid + transfer), the site's headline measure.
  - sn_gubun=0 restricts to scheduled service; charter is noisy and seasonal.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
ENDPOINT = "https://www.airportal.go.kr/stats/transport/getDetailedAirTransportStats3.do"
REFERER = "https://www.airportal.go.kr/stats/transport/chartDetail3.do"

# The portal rejects any window longer than 12 months, so a year is one request.
IATA_RE = re.compile(r"\(([A-Z]{3})\)\s*$")

# Korea still reports Astana and Bishkek under their pre-rename codes; folding
# them stops one physical airport being counted as two thinner markets.
ALIASES = {"TSE": "NQZ", "FRU": "BSZ"}


def parse_iata(label: str | None) -> str:
    """'간사이(KIX)' -> 'KIX'. Returns '' when the row is a subtotal."""
    if not label:
        return ""
    m = IATA_RE.search(label.strip())
    if not m:
        return ""
    return ALIASES.get(m.group(1), m.group(1))


def query(year: int, di_gubun: str, arvl_type: str, timeout: float) -> list[dict]:
    payload = {
        "last_yearmonth": f"{year}01",
        "this_yearmonth": f"{year}12",
        "sn_gubun": "0",
        "airport_gubun": "total",
        "pass_gubun": "4",
        "carge_gubun": "total",
        "forreign_airport_gubun": "total",
        "di_gubun": di_gubun,
        "arvl_type": arvl_type,
        "pyn_gubun": "total",
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": REFERER,
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("content") or []


def collect_year(year: int, timeout: float, pause: float) -> dict[tuple[str, str], float]:
    """Directional pax per OD pair for one calendar year."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    # (di_gubun, arvl_type, reverse): international rows are keyed on the Korean
    # airport regardless of direction, so arrivals describe foreign -> Korea.
    passes = [("D", "D", False), ("I", "D", False), ("I", "A", True)]
    for di, arvl, reverse in passes:
        for attempt in range(3):
            try:
                rows = query(year, di, arvl, timeout)
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt == 2:
                    print(f"  {year} {di}/{arvl}: giving up ({e})", file=sys.stderr)
                    rows = []
                else:
                    time.sleep(2 * (attempt + 1))
        kept = 0
        for r in rows:
            o = parse_iata(r.get("code_name"))
            d = parse_iata(r.get("airport_name"))
            if not o or not d or o == d:
                continue
            try:
                pax = float(r.get("pass") or 0)
            except (TypeError, ValueError):
                continue
            if pax <= 0:
                continue
            totals[(d, o) if reverse else (o, d)] += pax
            kept += 1
        print(f"  {year}  di={di} arvl={arvl}{' (reversed)' if reverse else '':<11} {kept:>5} routes")
        time.sleep(pause)
    return totals


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", type=int, default=2015)
    p.add_argument("--end", type=int, default=2025)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--pause", type=float, default=1.0, help="Seconds between requests")
    args = p.parse_args()

    grand = 0
    for year in range(args.start, args.end + 1):
        totals = collect_year(year, args.timeout, args.pause)
        if not totals:
            print(f"  {year}: no data, skipping\n")
            continue
        out = OUT_DIR / f"{year}.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["PASSENGERS", "ORIGIN", "DEST"])
            for (o, d), pax in sorted(totals.items()):
                wr.writerow([f"{pax:.0f}", o, d])
        grand += len(totals)
        print(f"  -> {out.name}: {len(totals):,} OD pairs, {sum(totals.values()):,.0f} pax\n")
    print(f"done: {grand:,} rows across {args.end - args.start + 1} years")
    return 0


if __name__ == "__main__":
    sys.exit(main())
