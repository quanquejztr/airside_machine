#!/usr/bin/env python3
"""
Download European route-level passengers from Eurostat (dataset family avia_par_*).

Eurostat publishes passengers carried between each reporting country's main
airports and every partner airport worldwide, which fills the largest remaining
anchor gap after Korea and Japan: intra-European markets, plus Europe's links to
Asia, the Middle East and Africa. Region priors for EU|EU, EU|AS and EU|ME were
previously extrapolated rather than measured.

No API key. Uses the JSON-stat dissemination API and the standard library only.

    python3 Europe_Route/fetch_eurostat.py
    python3 US_Route/bts_calibrate.py --us-dir Europe_Route \
        --out-anchors data/europe_demand_anchors.csv \
        --out-gravity /tmp/europe_gravity.json \
        --method-tag eu_trend_wm_max_recent_v2

Airport pair codes are ICAO, resolved against data/airports.csv, so a pair is
only kept when both ends are airports the game actually models.
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent
AIRPORTS_CSV = ROOT / "data" / "airports.csv"
API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/avia_par_{cc}"

# Reporting countries in the avia_par_* family. 'el' is Greece and 'uk' is
# retained by Eurostat for historic years.
COUNTRIES = [
    "at", "be", "bg", "ch", "cy", "cz", "de", "dk", "ee", "el", "es", "fi",
    "fr", "hr", "hu", "ie", "is", "it", "lt", "lu", "lv", "me", "mk", "mt",
    "nl", "no", "pl", "pt", "ro", "rs", "se", "si", "sk", "tr", "uk",
]

# The pair is (main airport, partner airport); departures therefore run
# main -> partner and arrivals partner -> main.
MEASURES = [("PAS_CRD_DEP", False), ("PAS_CRD_ARR", True)]


_CONTEXT: ssl.SSLContext | None = None
_INSECURE = False


def cert_failure(exc: BaseException) -> bool:
    """urllib wraps SSL problems in URLError, so the real cause is .reason."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError)


def describe(exc: BaseException) -> str:
    if cert_failure(exc):
        return (
            f"{getattr(exc, 'reason', exc)} -- certificate verification failed. "
            "Re-run with --insecure, or install a CA bundle (pip install certifi, "
            "or on macOS run 'Install Certificates.command' from your Python folder)."
        )
    return str(getattr(exc, "reason", exc))


def urlopen(url: str, timeout: float) -> bytes:
    """
    Fetch a URL, preferring verified TLS.

    On a certificate failure we retry once without verification and stay in that
    mode for the rest of the run. These are public statistics on a government
    host, and a missing local CA bundle is a common setup problem rather than a
    reason to abandon the download -- but it is announced rather than silent.
    """
    global _CONTEXT, _INSECURE
    if _CONTEXT is None:
        try:
            import certifi  # noqa: PLC0415

            _CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _CONTEXT = ssl.create_default_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_CONTEXT) as resp:
            return resp.read()
    except (urllib.error.URLError, ssl.SSLError) as e:
        if _INSECURE or not cert_failure(e):
            raise
        print(f"\n  warning: {describe(e)}\n  continuing without verification\n",
              file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _CONTEXT, _INSECURE = ctx, True
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return resp.read()


def load_icao_to_iata() -> dict[str, str]:
    out: dict[str, str] = {}
    with AIRPORTS_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            icao = (r.get("icao") or "").strip().upper()
            iata = (r.get("iata") or "").strip().upper()
            if len(icao) == 4 and len(iata) == 3:
                out[icao] = iata
    return out


def fetch(cc: str, measure: str, since: int, timeout: float) -> dict | None:
    # lastTimePeriod is accepted but returns an empty value set for this family;
    # sinceTimePeriod is the one that actually filters.
    q = urllib.parse.urlencode(
        {
            "format": "JSON",
            "freq": "A",
            "unit": "PAS",
            "tra_meas": measure,
            "sinceTimePeriod": since,
        }
    )
    url = f"{API.format(cc=cc)}?{q}"
    for attempt in range(3):
        try:
            return json.loads(urlopen(url, timeout).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not every country publishes every measure
                return None
            if attempt == 2:
                print(f"  {cc}/{measure}: HTTP {e.code}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == 2:
                print(f"  {cc}/{measure}: {describe(e)}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def decode(doc: dict, reverse: bool, icao2iata: dict[str, str],
           out: dict[int, dict[tuple[str, str], float]]) -> int:
    """JSON-stat values are keyed by a flat index over the dimension product."""
    dim = doc["dimension"]
    order = doc["id"]
    sizes = doc["size"]
    pairs = dim["airp_pr"]["category"]["index"]
    times = dim["time"]["category"]["index"]
    pair_by_idx = {v: k for k, v in pairs.items()}
    time_by_idx = {v: k for k, v in times.items()}

    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    pi, ti = order.index("airp_pr"), order.index("time")

    kept = 0
    for flat, val in doc["value"].items():
        if val is None:
            continue
        n = int(flat)
        code = pair_by_idx.get((n // strides[pi]) % sizes[pi])
        year_s = time_by_idx.get((n // strides[ti]) % sizes[ti])
        if not code or not year_s:
            continue
        parts = code.split("_")
        if len(parts) != 4:
            continue
        a, b = icao2iata.get(parts[1]), icao2iata.get(parts[3])
        if not a or not b or a == b:
            continue
        o, d = (b, a) if reverse else (a, b)
        try:
            pax, year = float(val), int(year_s)
        except (TypeError, ValueError):
            continue
        if pax > 0:
            # An intra-EU flow is reported by both endpoints' countries, so keep
            # the larger figure rather than summing the same passengers twice.
            prev = out[year].get((o, d), 0.0)
            if pax > prev:
                out[year][(o, d)] = pax
            kept += 1
    return kept


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", type=int, default=2015, help="Earliest year to fetch")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--pause", type=float, default=0.5)
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (use when your CA bundle is missing/outdated)",
    )
    args = p.parse_args()

    global _CONTEXT, _INSECURE
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _CONTEXT, _INSECURE = ctx, True

    # Fail on one obvious request rather than after several minutes of retries.
    print("Checking connectivity to Eurostat...")
    try:
        urlopen(API.format(cc="fr") + "?format=JSON&freq=A&unit=PAS"
                "&tra_meas=PAS_CRD_DEP&time=2019", args.timeout)
    except Exception as e:
        print(f"\nCannot reach Eurostat: {describe(e)}", file=sys.stderr)
        return 2
    print("  reachable\n")

    icao2iata = load_icao_to_iata()
    print(f"ICAO->IATA map: {len(icao2iata):,} airports")

    by_year: dict[int, dict[tuple[str, str], float]] = defaultdict(dict)
    for cc in COUNTRIES:
        got = 0
        for measure, reverse in MEASURES:
            doc = fetch(cc, measure, args.since, args.timeout)
            if doc and doc.get("value"):
                got += decode(doc, reverse, icao2iata, by_year)
            time.sleep(args.pause)
        print(f"  {cc}: {got:,} observations")

    if not by_year:
        print(
            "\nNo data decoded from any country. Nothing was written, so do not "
            "run bts_calibrate.py yet -- it would read stale or missing files.",
            file=sys.stderr,
        )
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
