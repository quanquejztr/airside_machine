"""
Base route demand from BTS anchors (US) with gravity / legacy fallbacks.

Lookup order when a route is created:
  1. BTS   — directional weekly market from bts_demand_anchors
  2. GRAVITY — score × distance estimate (only if calibrated ≥ LEGACY)
  3. LEGACY — old category × distance buckets (intl / thin / gravity miss)

Calibration (Claude Mode B): base_total = anchor_weekly × (target_share / eff_mult)
so at default multipliers the pre-logit pool ≈ bts_target_market_share of BTS market.

Playability: after Mode B, BTS/GRAVITY pools are lifted to bts_min_weekly_pool
(base × eff_mult) so ultra-thin OD pairs stay flyable without inventing trunk markets.
Do not raise bts_base_demand_floor for this — that fights Mode B scaling.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from db import db

TOURISM_AIRPORTS = frozenset(
    {
        "FLL",
        "MCO",
        "LAS",
        "HNL",
        "SJU",
        "ANC",
        "PSP",
        "MYR",
        "EYW",
        "RSW",
        "SFB",
        "PIE",
        "PGD",
        "CUN",
        "MBJ",
        "PUJ",
        "GUM",
        "SPN",
    }
)

# Coarse region buckets for international gravity dampening / boost.
_COUNTRY_REGION = {
    "US": "US",
    # US territories behave like domestic markets for demand purposes.
    "PR": "US",
    "VI": "US",
    "GU": "US",
    "MP": "US",
    "CA": "NA",
    "MX": "LA",
    # Caribbean / Central America. Previously unmapped, so they fell into the
    # generic "OT" bucket and shared a prior with unrelated regions.
    "DO": "LA",
    "BS": "LA",
    "JM": "LA",
    "CR": "LA",
    "HN": "LA",
    "CU": "LA",
    "PA": "LA",
    "GT": "LA",
    "SV": "LA",
    "AW": "LA",
    "KY": "LA",
    "BZ": "LA",
    "TC": "LA",
    "HT": "LA",
    "BQ": "LA",
    "NI": "LA",
    "TT": "LA",
    "BB": "LA",
    "EC": "LA",
    "UY": "LA",
    "PY": "LA",
    "BO": "LA",
    "VE": "LA",
    "GB": "EU",
    "IE": "EU",
    "FR": "EU",
    "DE": "EU",
    "ES": "EU",
    "IT": "EU",
    "NL": "EU",
    "PT": "EU",
    "BE": "EU",
    "CH": "EU",
    "AT": "EU",
    "SE": "EU",
    "NO": "EU",
    "DK": "EU",
    "FI": "EU",
    "PL": "EU",
    "IS": "EU",
    "GR": "EU",
    "CZ": "EU",
    "HU": "EU",
    "RO": "EU",
    "CN": "AS",
    "JP": "AS",
    "KR": "AS",
    "TW": "AS",
    "HK": "AS",
    "SG": "AS",
    "TH": "AS",
    "VN": "AS",
    "PH": "AS",
    "ID": "AS",
    "MY": "AS",
    "IN": "AS",
    "AU": "OC",
    "NZ": "OC",
    "FJ": "OC",
    "PG": "OC",
    "BD": "AS",
    "PK": "AS",
    "LK": "AS",
    "NP": "AS",
    "KH": "AS",
    "MM": "AS",
    "BN": "AS",
    "BR": "LA",
    "AR": "LA",
    "CL": "LA",
    "CO": "LA",
    "PE": "LA",
    "AE": "ME",
    "QA": "ME",
    "SA": "ME",
    "IL": "ME",
    "TR": "ME",
    "JO": "ME",
    "KW": "ME",
    "OM": "ME",
    "BH": "ME",
    # Default for Russia; _region() overrides it by longitude when known.
    "RU": "EU",
    # Reached via the Korean route data.
    "MO": "AS",
    "UZ": "AS",
    "KZ": "AS",
    "KG": "AS",
    "TM": "AS",
    "TJ": "AS",
    "MV": "AS",
    "MN": "AS",
    "HR": "EU",
    "PW": "OC",
    "AF": "AF",
    "EG": "AF",
    "ZA": "AF",
    "NG": "AF",
    "KE": "AF",
    "ET": "AF",
    "MA": "AF",
    "TN": "AF",
    "DZ": "AF",
    "GH": "AF",
    "SN": "AF",
    "TZ": "AF",
    "UG": "AF",
    "MU": "AF",
    "SC": "AF",
}

REGIONAL_PRIORS = {
    ("US", "EU"): 0.85,
    ("US", "AS"): 0.70,
    ("US", "LA"): 1.10,
    ("US", "ME"): 0.55,
    ("US", "OC"): 0.80,
    ("US", "NA"): 1.00,
}


def _fc(key: str, default: float) -> float:
    try:
        v = db.get_financial_constant(key)
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def effective_demand_multiplier() -> float:
    """Same split used when mapping BTS weekly market → game base_demand_*."""
    pdm = _fc("passenger_demand_multiplier", 9.0)
    seg_b = _fc("demand_business_segment_multiplier", 2.0)
    seg_l = _fc("demand_leisure_segment_multiplier", 3.5)
    return 0.30 * pdm * seg_b + 0.70 * pdm * seg_l


def calibration_k() -> float:
    share = _fc("bts_target_market_share", 0.30)
    eff = effective_demand_multiplier()
    if eff <= 0:
        return 0.0
    return share / eff


def biz_lei_split(origin_ap: dict, dest_ap: dict, distance_nm: float) -> tuple[float, float]:
    o_cat = str(origin_ap.get("category") or "")
    d_cat = str(dest_ap.get("category") or "")
    o_iata = str(origin_ap.get("iata") or "").upper()
    d_iata = str(dest_ap.get("iata") or "").upper()

    both_large = o_cat == "large_airport" and d_cat == "large_airport"
    either_small = o_cat == "small_airport" or d_cat == "small_airport"
    is_tourism = o_iata in TOURISM_AIRPORTS or d_iata in TOURISM_AIRPORTS

    if both_large and float(distance_nm) < 500:
        biz = 0.45
    elif both_large:
        biz = 0.35
    elif either_small:
        biz = 0.25
    else:
        biz = 0.30

    if is_tourism:
        biz = max(0.15, biz - 0.10)

    return float(biz), float(1.0 - biz)


# Russia straddles the split. Moscow and St Petersburg trade with Europe, while
# Vladivostok and Irkutsk are intra-Asian markets flying to Seoul and Beijing --
# calling them all European would price ICN-VVO like a long-haul Europe run.
_URALS_LON = 60.0


def _region(country: Optional[str], lon: Optional[float] = None) -> str:
    c = str(country or "").strip().upper()
    if c == "RU" and lon is not None:
        try:
            return "AS" if float(lon) >= _URALS_LON else "EU"
        except (TypeError, ValueError):
            pass
    return _COUNTRY_REGION.get(c, "OT")


_BAND_PARAMS: dict[str, Any] | None = None


def gravity_band_params() -> dict[str, Any]:
    """
    Fitted banded-gravity parameters from data/gravity_bands.json.

    Region priors are a matrix, so unlike the scalar knobs they can't live in
    financial_constants; the JSON is the source of truth and individual scalars
    can still be overridden by a matching financial_constants row.
    """
    global _BAND_PARAMS
    if _BAND_PARAMS is not None:
        return _BAND_PARAMS
    path = Path(__file__).resolve().parent.parent / "data" / "gravity_bands.json"
    try:
        _BAND_PARAMS = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        _BAND_PARAMS = {}
    return _BAND_PARAMS


def reset_gravity_band_cache() -> None:
    """Drop the cached JSON so tests can swap calibrations mid-process."""
    global _BAND_PARAMS
    _BAND_PARAMS = None


def regional_prior(origin_ap: dict, dest_ap: dict) -> float:
    """
    Region-pair multiplier, measured rather than hand-tuned.

    Priors are fitted jointly with the gravity coefficients (US-US is the held-out
    baseline, pinned to 1.0). REGIONAL_PRIORS below is the pre-Phase-6 fallback for
    when the fitted JSON is unavailable.
    """
    ro = _region(origin_ap.get("country"), origin_ap.get("lon"))
    rd = _region(dest_ap.get("country"), dest_ap.get("lon"))

    fitted = gravity_band_params().get("region_priors") or {}
    if fitted:
        key = "|".join(sorted((ro, rd)))
        val = fitted.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
        # Region pairs too sparse to fit fold into the baseline.
        return 1.0

    if ro == rd:
        return 1.0
    # Symmetric key order: put US first when present.
    if ro == "US" or rd == "US":
        key_t = ("US", rd if ro == "US" else ro)
        return float(REGIONAL_PRIORS.get(key_t, 0.75))
    return 0.85


def legacy_base_demand(distance_nm: float, origin_airport: dict, dest_airport: dict) -> tuple[int, int]:
    """Original category × distance estimate (LEGACY fallback)."""
    category_factors = {
        "small_airport": 50,
        "medium_airport": 150,
        "large_airport": 300,
    }
    origin_factor = category_factors.get(origin_airport.get("category"), 100)
    dest_factor = category_factors.get(dest_airport.get("category"), 100)
    base_factor = (origin_factor + dest_factor) / 2
    if distance_nm < 500:
        distance_mod = 1.3
    elif distance_nm < 1500:
        distance_mod = 1.0
    elif distance_nm < 3000:
        distance_mod = 0.8
    else:
        distance_mod = 0.6
    total_demand = base_factor * distance_mod
    return int(total_demand * 0.3), int(total_demand * 0.7)


def _band_decay(dist: float, params: dict) -> float:
    """
    Piecewise-power distance decay, continuous at the band edges.

    A single exponent fitted on US domestic distances badly mis-extrapolates to
    intercontinental range — it under-predicted real long-haul markets by ~6x.
    Three regimes fix that; building D() to be continuous means a route at 799nm
    and one at 801nm can't produce a visible jump in demand.
    """
    d1 = _fc("bts_gravity_band1_nm", float(params.get("band1_nm", 800.0)))
    d2 = _fc("bts_gravity_band2_nm", float(params.get("band2_nm", 3000.0)))
    b1 = _fc("bts_gravity_beta_short", float(params.get("beta_short", 0.5632)))
    b2 = _fc("bts_gravity_beta_medium", float(params.get("beta_medium", 0.7314)))
    b3 = _fc("bts_gravity_beta_long", float(params.get("beta_long", 0.8936)))

    if dist <= d1:
        return dist**b1
    if dist <= d2:
        return (d1 ** (b1 - b2)) * (dist**b2)
    return (d1 ** (b1 - b2)) * (d2 ** (b2 - b3)) * (dist**b3)


def gravity_weekly(origin_ap: dict, dest_ap: dict, distance_nm: float) -> float:
    params = gravity_band_params()
    k = _fc("bts_gravity_k", float(params.get("k", 0.0000079)))
    alpha = _fc("bts_gravity_alpha", float(params.get("alpha", 0.84)))
    damp = _fc("bts_small_small_damp", float(params.get("small_small_damp", 1.0)))

    score_o = max(1.0, float(origin_ap.get("score") or 1))
    score_d = max(1.0, float(dest_ap.get("score") or 1))
    dist = max(50.0, float(distance_nm))

    weekly = k * ((score_o * score_d) ** alpha) / _band_decay(dist, params)

    if (
        str(origin_ap.get("category") or "") == "small_airport"
        and str(dest_ap.get("category") or "") == "small_airport"
    ):
        weekly *= damp

    weekly *= regional_prior(origin_ap, dest_ap)

    # Correct the model's too-narrow spread before capping.
    weekly = apply_quantile_mapping(weekly, params)

    # Never let a modelled market outrank the real ones. Without this, two
    # high-scoring foreign airports at short range can invent a market larger
    # than the measured JFK-LHR, which is immediately obvious to a player.
    cap = gravity_cap_weekly(dist, params, min_score=min(score_o, score_d))
    if cap > 0:
        weekly = min(weekly, cap)

    return max(0.0, float(weekly))


def apply_quantile_mapping(weekly: float, params: dict | None = None) -> float:
    """
    Pull a modelled market toward the real-world demand distribution.

    Raw gravity is far too compressed: across the anchored pairs its p10->p99 span
    is ~32x where the measured span is ~779x, so it over-states thin routes and
    badly under-states trunks (HND-FUK came out ~150x low). The calibration stores
    paired quantiles of model output and real anchors; a prediction is looked up by
    rank and translated to the real market at the same rank.

    The mapping is monotonic, so route ordering is preserved -- it corrects spread,
    not ranking. Results are blended geometrically with the raw value so the
    correction can be dialled back via ``bts_gravity_quantile_blend``.
    """
    params = params if params is not None else gravity_band_params()
    if weekly <= 0:
        return 0.0

    model_q = params.get("quantile_model") or []
    real_q = params.get("quantile_real") or []
    if len(model_q) < 2 or len(model_q) != len(real_q):
        return float(weekly)

    blend = _fc("bts_gravity_quantile_blend", float(params.get("quantile_blend", 0.5)))
    blend = min(1.0, max(0.0, blend))
    if blend <= 0:
        return float(weekly)

    mapped = _interp_quantile(float(weekly), model_q, real_q)
    if mapped <= 0:
        return float(weekly)

    # Geometric blend: equivalent to averaging in log space, which keeps the
    # result positive and treats the two estimates as multiplicative.
    return float(weekly) ** (1.0 - blend) * mapped**blend


def _interp_quantile(value: float, model_q: list, real_q: list) -> float:
    """Log-linear interpolation of value through the model->real quantile curve."""
    lo, hi = float(model_q[0]), float(model_q[-1])
    # Outside the fitted range, hold the edge ratio so the curve stays monotonic.
    if value <= lo:
        return value * (float(real_q[0]) / lo) if lo > 0 else value
    if value >= hi:
        return value * (float(real_q[-1]) / hi) if hi > 0 else value

    for i in range(1, len(model_q)):
        left, right = float(model_q[i - 1]), float(model_q[i])
        if value > right:
            continue
        if right <= left:
            return float(real_q[i])
        t = (math.log(value) - math.log(max(1e-9, left))) / (
            math.log(max(1e-9, right)) - math.log(max(1e-9, left))
        )
        a, b = float(real_q[i - 1]), float(real_q[i])
        if a <= 0 or b <= 0:
            return a + t * (b - a)
        return math.exp(math.log(a) + t * (math.log(b) - math.log(a)))
    return float(real_q[-1])


def gravity_cap_weekly(
    distance_nm: float,
    params: dict | None = None,
    min_score: float | None = None,
) -> float:
    """
    p99 of real anchors in the matching distance band; 0 disables capping.

    Stratified by the smaller of the two airport scores when the calibration
    provides tiered caps. A single per-distance p99 is dictated by whichever
    routes are most numerous, which is thin regional ones, so it held hub pairs
    far below comparable measured hub routes. Falls back to the flat layout so
    an older gravity_bands.json keeps working.
    """
    params = params if params is not None else gravity_band_params()
    caps = params.get("caps_weekly") or {}
    dist = max(50.0, float(distance_nm))
    if dist <= float(params.get("band1_nm", 800.0)):
        band = "short"
    elif dist <= float(params.get("band2_nm", 3000.0)):
        band = "medium"
    else:
        band = "long"

    tiered = isinstance(caps.get("hub"), dict)
    if not tiered:
        try:
            return float(caps.get(band) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ms = float(min_score) if min_score is not None else 0.0
    if ms > float(params.get("score_tier_hub", 600_000.0)):
        order = ("hub", "med", "thin")
    elif ms >= float(params.get("score_tier_med", 100_000.0)):
        order = ("med", "thin", "hub")
    else:
        order = ("thin", "med", "hub")
    # A tier/band cell can be too sparse to have produced a cap; fall through to
    # the next most similar tier rather than uncapping the route entirely.
    for tier in order:
        try:
            val = float((caps.get(tier) or {}).get(band) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            return val
    return 0.0


def min_weekly_pool(origin_ap: dict, dest_ap: dict) -> tuple[float, bool]:
    """
    Minimum playable weekly market for a pair, scaled by airport size.

    A flat floor made 86.8% of anchored routes display an identical number, so a
    dying regional route looked exactly like a busy one. Real demand at the tenth
    percentile scales as min_score**0.435 (fitted across score bands), so the
    floor follows the same curve.

    Only ever scales *downward* from the configured pool. Scaling up would inflate
    dead pairs between two large airports -- BWI-IAD is a 39nm drive with almost
    no traffic, and both endpoints are hub-scored. Returns (floor, scaled).
    """
    base = _fc("bts_min_weekly_pool", 500.0)
    if base <= 0:
        return 0.0, False
    ref = _fc("bts_floor_score_ref", 1_200_000.0)
    exp = _fc("bts_floor_score_exponent", 0.435)
    hard = _fc("bts_floor_min_weekly", 250.0)
    try:
        ms = min(float(origin_ap.get("score") or 0), float(dest_ap.get("score") or 0))
    except (TypeError, ValueError):
        return base, False
    if ms <= 0 or ref <= 0:
        return base, False
    scaled = base * (ms / ref) ** exp
    return max(hard, min(base, scaled)), scaled < base


def compute_base_demand(
    distance_nm: float,
    origin_airport: dict,
    dest_airport: dict,
) -> dict[str, Any]:
    """
    Returns base_demand_business / leisure plus demand_source for route creation.

    Phase 6: anchors now cover US domestic (DB1B) and US-international (T-100), and
    gravity is fitted with banded distance decay plus measured region priors, so it
    no longer collapses on long-haul. LEGACY is therefore a last-resort path for
    pairs with no usable airport score, not a routine fallback.
    """
    o = str(origin_airport.get("iata") or "").strip().upper()
    d = str(dest_airport.get("iata") or "").strip().upper()
    k = calibration_k()
    floor = _fc("bts_base_demand_floor", 5.0)

    anchor = None
    try:
        anchor = db.lookup_bts_anchor_weekly(o, d)
    except Exception:
        anchor = None

    # BTS and T-100 list every city pair a carrier ever touched, so a one-off
    # charter leaves an anchor of a few pax a week. Those beat gravity, collapse
    # under Mode B and land on the floor -- HND-PUS showed 750/wk off an anchor of
    # 3.35 where the model gives 3,838. Below the level the fit itself trusts
    # (GRAVITY_MIN_WEEKLY) an anchor carries no signal: gravity disagrees with that
    # band by ~209x, against 1.0x for anchors over 200/wk, which are informative
    # and must be kept.
    #
    # The distance guard matters more than the threshold. On short pairs a
    # near-zero anchor is *correct* -- ORD-MDW is 13nm apart and nobody flies it,
    # but gravity knows nothing about driving and wants 8,120/wk. Only discard an
    # anchor where a dead market cannot be explained by surface transport.
    if anchor is not None:
        min_credible = _fc("bts_min_credible_anchor", 10.0)
        guard_nm = _fc("bts_anchor_discard_min_nm", 250.0)
        if float(anchor) < min_credible and float(distance_nm) > guard_nm:
            anchor = None

    legacy_b, legacy_l = legacy_base_demand(distance_nm, origin_airport, dest_airport)
    legacy_total = float(max(0, int(legacy_b)) + max(0, int(legacy_l)))

    source = "LEGACY"
    base_total = 0.0
    if anchor is not None and float(anchor) > 0:
        base_total = float(anchor) * k
        source = "BTS"
    else:
        try:
            scores_ok = (
                origin_airport.get("score") is not None
                and dest_airport.get("score") is not None
            )
            g = gravity_weekly(origin_airport, dest_airport, distance_nm)
            if g > 0 and scores_ok:
                base_total = float(g) * k
                source = "GRAVITY"
        except Exception:
            source = "LEGACY"

    if source == "LEGACY" or base_total <= 0:
        return {
            "base_demand_business": max(0, int(legacy_b)),
            "base_demand_leisure": max(0, int(legacy_l)),
            "demand_source": "LEGACY",
            "anchor_weekly": float(anchor) if anchor is not None else None,
            "base_total": legacy_total,
            "market_floor_applied": False,
        }

    base_total = max(floor, base_total)
    # Soft weekly pool floor (pax/week after eff_mult, before seasonality/logit),
    # scaled down for pairs of small airports so thin routes stay distinguishable.
    min_pool, _scaled = min_weekly_pool(origin_airport, dest_airport)
    eff = effective_demand_multiplier()
    market_floor_applied = False
    if min_pool > 0 and eff > 0:
        lifted = min_pool / eff
        if base_total + 1e-9 < lifted:
            market_floor_applied = True
            base_total = lifted

    biz_pct, lei_pct = biz_lei_split(origin_airport, dest_airport, distance_nm)
    business = int(round(base_total * biz_pct))
    leisure = int(round(base_total * lei_pct))
    # Keep at least 1 pax in the larger pool if rounding wiped both.
    if business + leisure <= 0 and base_total > 0:
        leisure = max(1, int(round(floor)))

    return {
        "base_demand_business": max(0, business),
        "base_demand_leisure": max(0, leisure),
        "demand_source": source,
        "anchor_weekly": float(anchor) if anchor is not None else None,
        "base_total": float(base_total),
        "market_floor_applied": bool(market_floor_applied),
    }
