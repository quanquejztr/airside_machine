#!/usr/bin/env python3
"""
Phase 6c: fit a banded-decay gravity model across BOTH anchor sets.

Replaces the single `beta` with three distance regimes. The decay term is built to
be continuous at the band edges, so a route at 799nm and one at 801nm can't jump:

    D(d) = d^b1                                             d <= d1
         = d1^(b1-b2) * d^b2                                d1 < d <= d2
         = d1^(b1-b2) * d2^(b2-b3) * d^b3                   d > d2

    weekly = k * (score_o * score_d)^alpha / D(d) * region_prior

Because log(D) is linear in (b1, b2, b3), the whole model is linear in logs and
solves exactly by least squares -- no grid search, no local minima:

    log(w) = log(k) + alpha*log(S) - b1*c1 - b2*c2 - b3*c3

Region priors are then measured as residual medians per region pair, replacing the
hand-tuned REGIONAL_PRIORS table.

Reads:  data/us_demand_anchors.csv, data/intl_demand_anchors.csv, data/airports.csv
        (the per-source files, NOT the merged data/bts_demand_anchors.csv, which
        would double-count the international rows)
Writes: data/gravity_bands.json

Usage:
    python3 Intl_Route/fit_gravity_bands.py
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "US_Route"))
sys.path.insert(0, str(ROOT))

from bts_calibrate import (  # noqa: E402
    GRAVITY_MIN_WEEKLY,
    haversine_nm,
    load_game_airports,
)

AIRPORTS_CSV = ROOT / "data" / "airports.csv"
US_ANCHORS = ROOT / "data" / "us_demand_anchors.csv"
INTL_ANCHORS = ROOT / "data" / "intl_demand_anchors.csv"
OUT_JSON = ROOT / "data" / "gravity_bands.json"

BAND1_NM = 800.0
BAND2_NM = 3000.0

# Minimum sample before a measured region prior is trusted over 1.0.
MIN_REGION_PAIRS = 30
# Number of quantile knots stored for the bias-correction curve.
QUANTILE_KNOTS = 101
# How far to pull a modelled route toward the real-world demand distribution.
# 0 = raw gravity, 1 = full quantile mapping. Blended geometrically at runtime.
QUANTILE_BLEND = 0.5
# Betas below this are implausible (demand rising with distance in-band).
BETA_FLOOR = 0.05
# The fit only sees pairs above GRAVITY_MIN_WEEKLY. That cutoff bites hardest on
# small x small pairs, whose predictions are lowest, so surviving ones are the
# unusually busy tail and the fitted coefficient comes out as a large *boost*.
# Applying that to unanchored small pairs would invent traffic, so the multiplier
# is capped at 1.0 for prediction. The measured value is kept in the JSON.
SMALL_SMALL_MAX = 1.0


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
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


def decay_coefficients(dist: float) -> tuple[float, float, float]:
    """Per-point coefficients (c1, c2, c3) such that log D = b1*c1 + b2*c2 + b3*c3."""
    ld, l1, l2 = math.log(dist), math.log(BAND1_NM), math.log(BAND2_NM)
    if dist <= BAND1_NM:
        return ld, 0.0, 0.0
    if dist <= BAND2_NM:
        return l1, ld - l1, 0.0
    return l1, l2 - l1, ld - l2


def decay(dist: float, b1: float, b2: float, b3: float) -> float:
    c1, c2, c3 = decay_coefficients(dist)
    return math.exp(b1 * c1 + b2 * c2 + b3 * c3)


def load_anchor_points(path: Path, airports: dict) -> list[dict]:
    points: list[dict] = []
    if not path.is_file():
        print(f"  WARN missing {path}")
        return points
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            o, d = row["origin_iata"], row["dest_iata"]
            ao, ad = airports.get(o), airports.get(d)
            if not ao or not ad:
                continue
            weekly = float(row["anchor_weekly"])
            if weekly < GRAVITY_MIN_WEEKLY:
                continue
            dist = max(50.0, haversine_nm(ao["lat"], ao["lon"], ad["lat"], ad["lon"]))
            points.append(
                {
                    "o": o,
                    "d": d,
                    "weekly": weekly,
                    "dist": dist,
                    "score": max(1.0, float(ao["score"]) * float(ad["score"])),
                    "cat_o": ao["category"],
                    "cat_d": ad["category"],
                    "country_o": ao["country"],
                    "country_d": ad["country"],
                }
            )
    return points


def fit(points: list[dict], country_region: dict) -> dict:
    """
    Single joint OLS in log space:

        log(w) = log(k) + alpha*log(S) - b1*c1 - b2*c2 - b3*c3
                 + log(damp)*[small x small] + sum_r log(prior_r)*[region pair r]

    Region priors and the small-small damp are dummy variables rather than
    post-hoc residual medians, so they can't be confounded with k. US-US is the
    held-out baseline, which pins its prior to exactly 1.0. Region pairs with
    fewer than MIN_REGION_PAIRS observations fold into that baseline.
    """
    for p in points:
        ro = region_of(p["country_o"], country_region)
        rd = region_of(p["country_d"], country_region)
        p["region_key"] = "|".join(sorted((ro, rd)))
        p["small_small"] = p["cat_o"] == "small_airport" and p["cat_d"] == "small_airport"

    counts: dict[str, int] = defaultdict(int)
    for p in points:
        counts[p["region_key"]] += 1
    baseline = "US|US"
    region_cols = sorted(
        key
        for key, n in counts.items()
        if n >= MIN_REGION_PAIRS and key != baseline
    )
    col_index = {key: 6 + i for i, key in enumerate(region_cols)}
    n_terms = 6 + len(region_cols)

    rows = []
    for p in points:
        c1, c2, c3 = decay_coefficients(p["dist"])
        feats = [0.0] * n_terms
        feats[0] = 1.0
        feats[1] = math.log(p["score"])
        feats[2] = -c1
        feats[3] = -c2
        feats[4] = -c3
        feats[5] = 1.0 if p["small_small"] else 0.0
        idx = col_index.get(p["region_key"])
        if idx is not None:
            feats[idx] = 1.0
        rows.append((feats, math.log(p["weekly"])))

    xtx = [[0.0] * n_terms for _ in range(n_terms)]
    xty = [0.0] * n_terms
    for feats, y in rows:
        for i in range(n_terms):
            fi = feats[i]
            if fi == 0.0:
                continue
            for j in range(n_terms):
                xtx[i][j] += fi * feats[j]
            xty[i] += fi * y
    coefs = solve(xtx, xty)
    if coefs is None:
        raise SystemExit("singular design matrix")

    log_k, alpha, b1, b2, b3, log_damp = coefs[:6]
    clamped = []
    for name, val in (("short", b1), ("medium", b2), ("long", b3)):
        if val < BETA_FLOOR:
            clamped.append(f"{name} {val:.3f}->{BETA_FLOOR}")
    b1, b2, b3 = (max(BETA_FLOOR, b) for b in (b1, b2, b3))

    priors = {baseline: 1.0}
    for key, idx in col_index.items():
        priors[key] = round(math.exp(coefs[idx]), 4)

    resid = [y - sum(f * c for f, c in zip(feats, coefs)) for feats, y in rows]
    ss_res = sum(r * r for r in resid)
    mean_y = statistics.mean(y for _, y in rows)
    ss_tot = sum((y - mean_y) ** 2 for _, y in rows)

    return {
        "k": math.exp(log_k),
        "alpha": alpha,
        "beta_short": b1,
        "beta_medium": b2,
        "beta_long": b3,
        "small_small_damp": round(math.exp(log_damp), 4),
        "region_priors": priors,
        "region_counts": {k: counts[k] for k in sorted(counts, key=lambda x: -counts[x])},
        "n": len(rows),
        "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        "clamped": clamped,
    }


def region_of(country: str, country_region: dict) -> str:
    return country_region.get(country.upper(), "OT")


def derive_missing_region_priors(fitted: dict[str, float], country_region: dict) -> dict:
    """
    Fill in foreign<->foreign region pairs the data cannot fit directly.

    T-100 only contains US-touching routes, so every prior the regression can
    estimate has US on one side. That left EU|AS, AS|LA and the rest folding to the
    US-US baseline of 1.0 -- LHR-SIN was being priced like a US domestic route of
    the same length, ~16x below reality, while the fit already knew EU-US runs
    1.95x and AS-US 1.48x.

    Treating each prior as a product of per-region airport effects with US as the
    reference gives e_US = 1 (since US|US = e_US^2 = 1), so e_R = prior(R|US)
    falls straight out. An unfitted pair is then e_R1 * e_R2.
    """
    effects: dict[str, float] = {"US": 1.0}
    for key, val in fitted.items():
        a, b = key.split("|")
        if a == b == "US":
            continue
        if a == "US" or b == "US":
            effects[b if a == "US" else a] = float(val)

    # Multiplying two single-sided effects compounds them: e_ME = 5.5 implies
    # ME|ME = 30x, which no measurement supports. Nothing observed lies outside the
    # fitted range, so extrapolation is clamped to it.
    observed = [float(v) for v in fitted.values()]
    lo, hi = (min(observed), max(observed)) if observed else (1.0, 1.0)

    regions = sorted(set(country_region.values()) | set(effects) | {"OT"})
    out = dict(fitted)
    derived: list[str] = []
    clamped: list[str] = []
    for i, r1 in enumerate(regions):
        for r2 in regions[i:]:
            key = "|".join(sorted((r1, r2)))
            if key in out:
                continue
            e1 = effects.get(r1)
            e2 = effects.get(r2)
            if e1 is None or e2 is None:
                continue
            raw = e1 * e2
            val = min(hi, max(lo, raw))
            if abs(val - raw) > 1e-9:
                clamped.append(f"{key} {raw:.1f}->{val:.2f}")
            out[key] = round(val, 4)
            derived.append(key)

    if derived:
        print(f"\nDerived {len(derived)} region priors from per-region effects:")
        print("  effects (relative to US=1.0): " + "  ".join(
            f"{r}={v:.2f}" for r, v in sorted(effects.items(), key=lambda kv: -kv[1])
        ))
        print(f"  clamped to observed range [{lo:.2f}, {hi:.2f}]")
        for key in sorted(derived, key=lambda k: -out[k])[:10]:
            print(f"  {key:<8} = {out[key]:.3f}  (derived)")
        if clamped:
            print(f"  hit the clamp: {', '.join(sorted(clamped))}")
        no_effect = [r for r in regions if r not in effects]
        if no_effect:
            print(f"  no fitted effect, still baseline 1.0: {', '.join(no_effect)}")
    return out


def main() -> int:
    airports = load_game_airports(AIRPORTS_CSV)
    from engine.route_demand import _COUNTRY_REGION  # noqa: PLC0415

    print("Loading anchors...")
    pts = load_anchor_points(US_ANCHORS, airports)
    print(f"  US domestic: {len(pts):,} pairs >= {GRAVITY_MIN_WEEKLY:g}/wk")
    intl = load_anchor_points(INTL_ANCHORS, airports)
    print(f"  international: {len(intl):,} pairs >= {GRAVITY_MIN_WEEKLY:g}/wk")
    pts += intl
    print(f"  combined: {len(pts):,}")

    print("\nFitting banded decay + region priors + damp (single joint OLS)...")
    f = fit(pts, _COUNTRY_REGION)
    print(f"  k            = {f['k']:.10f}")
    print(f"  alpha        = {f['alpha']:.4f}")
    print(f"  beta_short   = {f['beta_short']:.4f}   (< {BAND1_NM:.0f}nm)")
    print(f"  beta_medium  = {f['beta_medium']:.4f}   ({BAND1_NM:.0f}-{BAND2_NM:.0f}nm)")
    print(f"  beta_long    = {f['beta_long']:.4f}   (> {BAND2_NM:.0f}nm)")
    print(f"  R^2          = {f['r2']:.3f}   n = {f['n']:,}")
    if f["clamped"]:
        print(f"  clamped: {', '.join(f['clamped'])}")

    priors = derive_missing_region_priors(f["region_priors"], _COUNTRY_REGION)
    damp_measured = f["small_small_damp"]
    damp = min(SMALL_SMALL_MAX, damp_measured)
    print(f"\n  small_small_damp = {damp}  (measured {damp_measured}, capped at {SMALL_SMALL_MAX})")
    print("\nRegion priors (US-US is the held-out baseline = 1.0):")
    for key in sorted(priors, key=lambda k: -f["region_counts"].get(k, 0)):
        print(f"  {key:<8} n={f['region_counts'].get(key, 0):>6}  prior={priors[key]:.3f}")
    folded = [
        k for k, n in f["region_counts"].items()
        if n < MIN_REGION_PAIRS
    ]
    if folded:
        print(f"  folded into baseline (n < {MIN_REGION_PAIRS}): {', '.join(sorted(folded))}")

    def full_pred(p: dict) -> float:
        val = f["k"] * (p["score"] ** f["alpha"]) / decay(
            p["dist"], f["beta_short"], f["beta_medium"], f["beta_long"]
        )
        val *= priors.get(p["region_key"], 1.0)
        if p["small_small"]:
            val *= damp
        return val

    # Log-space OLS is unbiased in logs, which leaves the level-space median off by
    # a constant factor. Fold that factor into k so a typical route predicts its
    # actual market rather than a systematically low one.
    ratios_all = [p["weekly"] / full_pred(p) for p in pts if full_pred(p) > 0]
    level_correction = statistics.median(ratios_all)
    f["k"] *= level_correction
    print(f"\n  level correction applied to k: x{level_correction:.4f}")

    print("\nValidation — actual/predicted after the full model (target ~1.00):")
    print(f"  {'group':<26}{'n':>8}{'median':>9}{'p25':>9}{'p75':>9}")

    def check(label: str, subset: list[dict]) -> None:
        ratios = [p["weekly"] / full_pred(p) for p in subset if full_pred(p) > 0]
        if len(ratios) < 20:
            print(f"  {label:<26}{len(ratios):>8}   (too few)")
            return
        ratios.sort()
        q1 = ratios[len(ratios) // 4]
        q3 = ratios[3 * len(ratios) // 4]
        print(
            f"  {label:<26}{len(ratios):>8}{statistics.median(ratios):>9.2f}"
            f"{q1:>9.2f}{q3:>9.2f}"
        )

    check("all", pts)
    for band, lo, hi in (("short", 0.0, BAND1_NM), ("medium", BAND1_NM, BAND2_NM), ("long", BAND2_NM, 1e9)):
        check(f"  {band}", [p for p in pts if lo < p["dist"] <= hi])
    check("US domestic", [p for p in pts if p["country_o"] == "US" and p["country_d"] == "US"])
    check("US <-> foreign", [p for p in pts if (p["country_o"] == "US") != (p["country_d"] == "US")])

    # Quantile mapping. Gravity's spread is far too narrow -- across the anchored
    # pairs its p10->p99 range is ~32x where the real one is ~779x, so thin routes
    # come out too busy and trunk routes far too quiet. Pairing sorted predictions
    # with sorted actuals lets a modelled route inherit the real market sitting at
    # its own rank, which fixes the spread without reordering anything.
    print("\nQuantile mapping (model -> real demand distribution):")
    pred_sorted = sorted(full_pred(p) for p in pts if full_pred(p) > 0)
    act_sorted = sorted(p["weekly"] for p in pts)

    def knot(vals: list[float], q: float) -> float:
        return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]

    qs = [i / (QUANTILE_KNOTS - 1) for i in range(QUANTILE_KNOTS)]
    q_pred = [round(knot(pred_sorted, q), 4) for q in qs]
    q_act = [round(knot(act_sorted, q), 4) for q in qs]
    print(f"  {'quantile':<10}{'model':>12}{'real':>12}{'factor':>10}")
    for q in (0.05, 0.25, 0.50, 0.75, 0.90, 0.99, 1.00):
        mp, ma = knot(pred_sorted, q), knot(act_sorted, q)
        print(f"  {q:<10.2f}{mp:>12,.0f}{ma:>12,.0f}{(ma / mp if mp else 0):>10.2f}")
    print(f"  blend = {QUANTILE_BLEND} (geometric, 0=raw gravity 1=full mapping)")

    # Plausibility caps. p99 rather than p90: gravity already under-predicts trunk
    # routes badly (R^2 ~0.32), so a p90 cap would truncate legitimate large
    # modelled markets. p99 still blocks a fictional route from outranking all but
    # the very busiest measured ones.
    print("\nPlausibility caps per band:")
    per_band: dict[str, list[float]] = defaultdict(list)
    for p in pts:
        band = "short" if p["dist"] <= BAND1_NM else ("medium" if p["dist"] <= BAND2_NM else "long")
        per_band[band].append(p["weekly"])
    caps = {}
    for band in ("short", "medium", "long"):
        vals = sorted(per_band[band])
        if not vals:
            continue

        def at(q: float, v: list[float] = vals) -> float:
            return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]

        caps[band] = round(at(0.99), 1)
        print(
            f"  {band:<7} n={len(vals):>6}  p90={at(0.90):>10,.0f}  "
            f"p99={at(0.99):>10,.0f} <- cap   max={vals[-1]:>10,.0f}"
        )

    payload = {
        "method": "banded_ols_v1",
        "band1_nm": BAND1_NM,
        "band2_nm": BAND2_NM,
        "k": round(f["k"], 10),
        "alpha": round(f["alpha"], 4),
        "beta_short": round(f["beta_short"], 4),
        "beta_medium": round(f["beta_medium"], 4),
        "beta_long": round(f["beta_long"], 4),
        "small_small_damp": damp,
        "small_small_damp_measured": damp_measured,
        "level_correction": round(level_correction, 4),
        "region_priors": priors,
        "region_baseline": "US|US",
        "quantile_blend": QUANTILE_BLEND,
        "quantile_model": q_pred,
        "quantile_real": q_act,
        "caps_weekly": caps,
        "fit_points": f["n"],
        "r2": round(f["r2"], 4),
        "min_weekly_for_fit": GRAVITY_MIN_WEEKLY,
        "min_region_pairs": MIN_REGION_PAIRS,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
