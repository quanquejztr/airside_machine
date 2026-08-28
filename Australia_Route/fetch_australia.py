#!/usr/bin/env python3
"""
Download Australian route passengers from data.gov.au (BITRE series).

Two datasets, both open CSV downloads with no key:

  Domestic Airlines - Top Routes            city-pair passenger trips
  International Airlines - Traffic by city  Australian port <-> foreign port

This anchors SYD-MEL and the rest of the Australian domestic network, and the
international file reaches a long list of Asian cities -- including 25+ Chinese
airports -- that no other source we have connects to at route level.

    python3 Australia_Route/fetch_australia.py
    python3 US_Route/bts_calibrate.py --us-dir Australia_Route \
        --out-anchors data/australia_demand_anchors.csv \
        --out-gravity /tmp/au_gravity.json \
        --method-tag au_trend_wm_max_recent_v2 --exclude-years 2020,2021,2022

bitre.gov.au itself times out from some networks, so the mirrored copies on
data.gov.au are used instead; they are the same BITRE series.
"""

from __future__ import annotations

import argparse
import csv
import io
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
BASE = "https://data.gov.au/data/dataset"
DOMESTIC_URL = (
    f"{BASE}/c5029f2a-39b3-4aef-8ae1-73e7962f6170/resource/"
    "677d307f-6a1f-4de4-9b85-5e1aa7074423/download/dom_citypairs_web.csv"
)
INTERNATIONAL_URL = (
    f"{BASE}/d9fbffaa-836f-4f52-80e8-324249ff269f/resource/"
    "ebcafd83-9514-4f72-a995-fe7ee90cb9da/download/city_pairs.csv"
)

# BITRE reports city names, not airport codes. Multi-airport cities are mapped
# to the airport that actually carries the Australian traffic.
CITY_TO_IATA = {
    # --- Australia ---
    "SYDNEY": "SYD", "MELBOURNE": "MEL", "BRISBANE": "BNE", "PERTH": "PER",
    "ADELAIDE": "ADL", "DARWIN": "DRW", "CAIRNS": "CNS", "GOLD COAST": "OOL",
    "HOBART": "HBA", "CANBERRA": "CBR", "NEWCASTLE": "NTL", "TOWNSVILLE": "TSV",
    "BROOME": "BME", "PORT HEDLAND": "PHE", "SUNSHINE COAST": "MCY",
    "NORFOLK ISLAND": "NLK", "TOOWOOMBA WELLCAMP": "WTB", "ALBURY": "ABX",
    "ALICE SPRINGS": "ASP", "ARMIDALE": "ARM", "AYERS ROCK": "AYQ",
    "BALLINA": "BNK", "BUNDABERG": "BDB", "COFFS HARBOUR": "CFS",
    "DEVONPORT": "DPO", "DUBBO": "DBO", "EMERALD": "EMD", "GERALDTON": "GET",
    "GLADSTONE": "GLT", "HAMILTON ISLAND": "HTI", "KALGOORLIE": "KGI",
    "KARRATHA": "KTA", "LAUNCESTON": "LST", "MACKAY": "MKY", "MILDURA": "MQL",
    "MORANBAH": "MOV", "MOUNT ISA": "ISA", "NEWMAN": "ZNE",
    "PORT LINCOLN": "PLO", "PORT MACQUARIE": "PQQ", "PROSERPINE": "PPP",
    "ROCKHAMPTON": "ROK", "TAMWORTH": "TMW", "WAGGA WAGGA": "WGA",
    # --- New Zealand and Pacific ---
    "AUCKLAND": "AKL", "CHRISTCHURCH": "CHC", "WELLINGTON": "WLG",
    "QUEENSTOWN": "ZQN", "DUNEDIN": "DUD", "ROTORUA": "ROT", "HAMILTON": "HLZ",
    "NADI": "NAN", "SUVA": "SUV", "PORT VILA": "VLI", "NOUMEA": "NOU",
    "PORT MORESBY": "POM", "RABAUL": "RAB", "HONIARA": "HIR", "MUNDA": "MUA",
    "ESPIRITU SANTO": "SON", "APIA": "APW", "TONGATAPU": "TBU",
    "RAROTONGA": "RAR", "NAURU": "INU", "TARAWA": "TRW", "PALAU": "ROR",
    "GUAM": "GUM", "HONOLULU": "HNL",
    # --- Asia ---
    "SINGAPORE": "SIN", "HONG KONG": "HKG", "MACAU": "MFM", "TAIPEI": "TPE",
    "BANGKOK": "BKK", "PHUKET": "HKT", "KUALA LUMPUR": "KUL", "KUCHING": "KCH",
    "KOTA KINABALU": "BKI", "JAKARTA": "CGK", "DENPASAR": "DPS", "MEDAN": "KNO",
    "SURABAYA": "SUB", "LOMBOK": "LOP", "BATAM": "BTH", "MANILA": "MNL",
    "CEBU": "CEB", "GENERAL SANTOS": "GES", "LUZON ISLAND": "CRK",
    "BANDAR SERI BEGAWAN": "BWN", "PHNOM PENH": "PNH", "VIENTIANE": "VTE",
    "HANOI": "HAN", "HO CHI MINH CITY": "SGN", "DA NANG": "DAD",
    "CAN THO": "VCA", "VAN DON": "VDO", "DILI": "DIL", "DHAKA": "DAC",
    "COLOMBO": "CMB", "BOMBAY": "BOM", "NEW DELHI": "DEL", "BANGALORE": "BLR",
    "ALMATY": "ALA", "BAKU": "GYD",
    # Japan and Korea. BITRE's "Tokyo" and "Osaka" are the international
    # gateways Australian carriers use.
    "TOKYO": "NRT", "OSAKA": "KIX", "NAGOYA": "NGO", "SAPPORO": "CTS",
    "KITA KYUSHU": "KKJ", "SEOUL": "ICN", "GIMHAE": "PUS",
    # --- China ---
    "BEIJING": "PEK", "SHANGHAI": "PVG", "GUANGZHOU": "CAN", "SHENZHEN": "SZX",
    "CHENGDU": "CTU", "CHONGQING": "CKG", "XIAMEN": "XMN", "QINGDAO": "TAO",
    "HANGZHOU": "HGH", "NANJING": "NKG", "WUHAN": "WUH", "XI'AN": "XIY",
    "TIANJIN": "TSN", "CHANGSHA": "CSX", "KUNMING": "KMG", "ZHENGZHOU": "CGO",
    "NINGBO": "NGB", "FUZHOU": "FOC", "HAIKOU": "HAK", "SHENYANG": "SHE",
    "JINAN": "TNA", "TAIYUAN": "TYN", "GUIYANG": "KWE", "LANZHOU": "LHW",
    "HOHHOT": "HET", "CHANGCHUN": "CGQ", "NANNING": "NNG",
    # --- Middle East, Africa, Americas, Europe ---
    "DUBAI": "DXB", "ABU DHABI": "AUH", "DOHA": "DOH", "BAHRAIN": "BAH",
    "AL-FUJAIRAH": "FJR", "ISTANBUL": "IST", "MAURITIUS": "MRU",
    "JOHANNESBURG": "JNB", "GABORONE": "GBE", "LONDON": "LHR", "PARIS": "CDG",
    "ROME": "FCO", "COLOGNE": "CGN", "DRESDEN": "DRS", "LOS ANGELES": "LAX",
    "SAN FRANCISCO": "SFO", "OAKLAND": "OAK", "NEW YORK": "JFK",
    "CHICAGO": "ORD", "DALLAS": "DFW", "HOUSTON": "IAH", "MIAMI": "MIA",
    "LOUISVILLE": "SDF", "CINCINNATI": "CVG", "ANCHORAGE": "ANC",
    "VANCOUVER": "YVR", "TORONTO": "YYZ", "SANTIAGO": "SCL", "LIMA": "LIM",
    "BUENOS AIRES": "EZE", "RIO DE JANEIRO": "GIG",
}


def get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ssl.SSLError) as e:
        reason = getattr(e, "reason", e)
        if not isinstance(reason, ssl.SSLError):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("  warning: TLS verification failed, continuing unverified", file=sys.stderr)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", "replace")


def code(name: str) -> str | None:
    return CITY_TO_IATA.get((name or "").strip().upper())


def num(raw: object) -> float:
    try:
        return float(str(raw or "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", type=int, default=2015)
    p.add_argument("--end", type=int, default=2025)
    p.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args()

    by_year: dict[int, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
    unknown: set[str] = set()

    print("Downloading domestic city pairs...")
    dom = get(DOMESTIC_URL, args.timeout)
    for r in csv.DictReader(io.StringIO(dom)):
        try:
            year = int(r["Year"])
        except (KeyError, TypeError, ValueError):
            continue
        if not args.start <= year <= args.end:
            continue
        a, b = code(r.get("City1")), code(r.get("City2"))
        if not a or not b:
            for nm in (r.get("City1"), r.get("City2")):
                if nm and not code(nm):
                    unknown.add(nm.strip().upper())
            continue
        # Passenger_Trips counts both directions on the pair; BITRE does not
        # break it down, and real directional splits sit very close to even.
        half = num(r.get("Passenger_Trips")) / 2.0
        if half > 0:
            by_year[year][(a, b)] += half
            by_year[year][(b, a)] += half

    print("Downloading international city pairs...")
    intl = get(INTERNATIONAL_URL, args.timeout)
    for r in csv.DictReader(io.StringIO(intl)):
        try:
            year = int(r["Year"])
        except (KeyError, TypeError, ValueError):
            continue
        if not args.start <= year <= args.end:
            continue
        au, fo = code(r.get("AustralianPort")), code(r.get("ForeignPort"))
        if not au or not fo:
            for nm in (r.get("AustralianPort"), r.get("ForeignPort")):
                if nm and not code(nm):
                    unknown.add(nm.strip().upper())
            continue
        out_pax = num(r.get("Passengers_Out"))
        in_pax = num(r.get("Passengers_In"))
        if out_pax > 0:
            by_year[year][(au, fo)] += out_pax
        if in_pax > 0:
            by_year[year][(fo, au)] += in_pax

    if unknown:
        print(f"  unmapped city names ({len(unknown)}): {sorted(unknown)}", file=sys.stderr)
    if not by_year:
        print("No data decoded; nothing written.", file=sys.stderr)
        return 1

    total = 0
    for year, totals in sorted(by_year.items()):
        out = OUT_DIR / f"{year}.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["PASSENGERS", "ORIGIN", "DEST"])
            for (o, d), pax in sorted(totals.items()):
                wr.writerow([f"{pax:.0f}", o, d])
        total += len(totals)
        print(f"  {year}: {len(totals):,} OD pairs, {sum(totals.values()):,.0f} pax")
    print(f"done: {total:,} rows across {len(by_year)} years")
    return 0


if __name__ == "__main__":
    sys.exit(main())
