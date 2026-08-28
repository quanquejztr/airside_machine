#!/usr/bin/env python3
"""
Download Japanese domestic route passengers from e-Stat (MLIT 航空輸送統計調査).

Table 9, 国内定期航空空港間旅客流動表（暦年）, is a directional airport-to-airport
passenger matrix for all scheduled domestic service. It closes the largest gap
left after the Korean import: routes inside Japan such as HND-FUK and HND-CTS
are among the busiest in the world, and nothing in our other sources sees them.

Downloads need no API key -- e-Stat's file-download endpoint is open, and the
xlsx is parsed with the standard library so the script has no dependencies.

    python3 Japan_Route/fetch_japan.py
    python3 US_Route/bts_calibrate.py --us-dir Japan_Route \
        --out-anchors data/japan_demand_anchors.csv \
        --out-gravity /tmp/japan_gravity.json \
        --method-tag jp_trend_wm_max_recent_v2 --exclude-years 2020,2021,2022

Only domestic service is covered. Japan's international tables are published by
region (方面別) rather than by route, so they cannot be turned into anchors.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

OUT_DIR = Path(__file__).resolve().parent
BASE = "https://www.e-stat.go.jp"
LIST_URL = (
    BASE + "/stat-search/files?page=1&layout=datalist&cycle=7&kikan=00600"
    "&toukei=00600360&tstat=000001018894&result_page=1&tclass1val=0&second2=1"
    "&year={year}0&month=0&result_back=1"
)
DOWNLOAD_URL = BASE + "/stat-search/file-download?statInfId={sid}&fileKind=0"

# Calendar-year edition, to line up with the other sources. The fiscal-year
# table (年度) covers April-March and would shift every anchor by a quarter.
TABLE_TITLE = "国内定期航空空港間旅客流動表"
TABLE_CALENDAR = "暦年"

# The published matrix labels airports in Japanese only.
NAME_TO_IATA = {
    "稚内": "WKJ", "釧路": "KUH", "函館": "HKD", "旭川": "AKJ", "利尻": "RIS",
    "帯広": "OBO", "中標津": "SHB", "紋別": "MBE", "女満別": "MMB", "奥尻": "OIR",
    "新千歳": "CTS", "丘珠": "OKD", "青森": "AOJ", "三沢": "MSJ", "花巻": "HNA",
    "仙台": "SDJ", "秋田": "AXT", "大館": "ONJ", "山形": "GAJ", "庄内": "SYO",
    "福島": "FKS", "百里": "IBR", "成田": "NRT", "羽田": "HND", "大島": "OIM",
    "三宅島": "MYE", "八丈島": "HAC", "新島": "NII", "新潟": "KIJ", "富山": "TOY",
    "小松": "KMQ", "能登": "NTQ", "松本": "MMJ", "静岡": "FSZ",
    # 名古屋 is Komaki (the old city airport); Centrair is listed separately.
    "名古屋": "NKM", "中部": "NGO", "大阪": "ITM", "関西": "KIX", "神戸": "UKB",
    "但馬": "TJH", "南紀白浜": "SHM", "鳥取": "TTJ", "美保": "YGJ", "隠岐": "OKI",
    "出雲": "IZO", "石見": "IWJ", "岡山": "OKJ", "広島": "HIJ", "山口宇部": "UBJ",
    "岩国": "IWK", "徳島": "TKS", "高松": "TAK", "松山": "MYJ", "高知": "KCZ",
    "福岡": "FUK", "北九州": "KKJ", "佐賀": "HSG", "長崎": "NGS", "福江": "FUJ",
    "壱岐": "IKI", "対馬": "TSJ", "熊本": "KMJ", "天草": "AXJ", "大分": "OIT",
    "宮崎": "KMI", "鹿児島": "KOJ", "種子島": "TNE", "屋久島": "KUM", "奄美": "ASJ",
    "喜界島": "KKX", "沖永良部": "OKE", "与論": "RNJ", "徳之島": "TKN", "那覇": "OKA",
    "南大東島": "MMD", "久米島": "UEO", "宮古島": "MMY", "石垣": "ISG",
    "与那国": "OGN", "多良間": "TRA", "北大東島": "KTD", "下地島": "SHI",
}
# Heliports and general-aviation fields with no scheduled jet service; they
# appear in the matrix but carry no IATA code we model.
IGNORE_NAMES = {
    "調布", "神津島", "青ヶ島", "御蔵島", "利島", "合計", "",
    # Commuter strips and heliports that appear in some years only, all far
    # below any traffic level the game models.
    "上五島", "伊江", "佐渡", "八尾", "小値賀", "広島西", "慶良間", "波照間",
    "礼文", "福井", "粟国",
}

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_first_sheet(blob: bytes, tmp: Path) -> list[list[str]]:
    """Minimal xlsx reader -- avoids a pandas/openpyxl dependency."""
    tmp.write_bytes(blob)
    with zipfile.ZipFile(tmp) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
        names = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(names[0])).iter(NS + "row"):
            cells: dict[int, str] = {}
            for c in row.iter(NS + "c"):
                v = c.find(NS + "v")
                if v is None or v.text is None:
                    continue
                cells[col_index(c.get("r"))] = (
                    shared[int(v.text)] if c.get("t") == "s" else v.text
                )
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def clean(text: object) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def find_table_id(year: int, timeout: float) -> str | None:
    html = get(LIST_URL.format(year=year), timeout).decode("utf-8", "replace")
    for article in html.split('<article class="stat-dataset_list-item">')[1:]:
        m = re.search(r'class="stat-link_text[^"]*"[^>]*>\s*([^<]+)', article)
        if not m:
            continue
        title = clean(m.group(1))
        if TABLE_TITLE not in title or TABLE_CALENDAR not in title:
            continue
        sid = re.search(r"statInfId=(\d+)&fileKind=0", article)
        if sid:
            return sid.group(1)
    return None


def parse_matrix(rows: list[list[str]]) -> tuple[dict[tuple[str, str], float], set[str]]:
    """Row label is the origin (発), column header the destination (着)."""
    header_row = next(
        (r for r in rows if sum(1 for c in r if clean(c) in NAME_TO_IATA) > 5), None
    )
    if header_row is None:
        return {}, set()
    # The sheet is printed in page-width blocks, so spacer columns repeat the
    # corner label; keying by name skips them along with the 合計 total column.
    cols = {i: NAME_TO_IATA[clean(c)] for i, c in enumerate(header_row) if clean(c) in NAME_TO_IATA}

    totals: dict[tuple[str, str], float] = {}
    unknown: set[str] = set()
    start = rows.index(header_row) + 1
    for row in rows[start:]:
        if not row:
            continue
        label = clean(row[0])
        if label in IGNORE_NAMES:
            continue
        origin = NAME_TO_IATA.get(label)
        if not origin:
            unknown.add(label)
            continue
        for i, dest in cols.items():
            if i >= len(row) or origin == dest:
                continue
            raw = str(row[i]).replace(",", "").strip()
            if not raw:
                continue
            try:
                pax = float(raw)
            except ValueError:
                continue
            if pax > 0:
                totals[(origin, dest)] = totals.get((origin, dest), 0.0) + pax
    return totals, unknown


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", type=int, default=2015)
    p.add_argument("--end", type=int, default=2025)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--pause", type=float, default=1.0)
    args = p.parse_args()

    tmp = OUT_DIR / ".download.xlsx"
    written = 0
    try:
        for year in range(args.start, args.end + 1):
            try:
                sid = find_table_id(year, args.timeout)
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"  {year}: listing failed ({e})", file=sys.stderr)
                continue
            if not sid:
                print(f"  {year}: table not published")
                continue
            try:
                blob = get(DOWNLOAD_URL.format(sid=sid), args.timeout)
                rows = read_first_sheet(blob, tmp)
            except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile) as e:
                print(f"  {year}: download failed ({e})", file=sys.stderr)
                continue

            totals, unknown = parse_matrix(rows)
            if not totals:
                print(f"  {year}: no rows parsed")
                continue
            if unknown:
                print(f"  {year}: unmapped labels {sorted(unknown)}", file=sys.stderr)

            out = OUT_DIR / f"{year}.csv"
            with out.open("w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["PASSENGERS", "ORIGIN", "DEST"])
                for (o, d), pax in sorted(totals.items()):
                    wr.writerow([f"{pax:.0f}", o, d])
            written += 1
            print(f"  {year}: {len(totals):,} OD pairs, {sum(totals.values()):,.0f} pax")
            time.sleep(args.pause)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"done: {written} years written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
