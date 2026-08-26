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
    "CA": "NA",
    "MX": "LA",
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


def _region(country: Optional[str]) -> str:
    c = str(country or "").strip().upper()
    return _COUNTRY_REGION.get(c, "OT")


def regional_prior(origin_ap: dict, dest_ap: dict) -> float:
    ro = _region(origin_ap.get("country"))
    rd = _region(dest_ap.get("country"))
    if ro == rd:
        return 1.0
    # Symmetric key order: put US first when present.
    if ro == "US" or rd == "US":
        key = ("US", rd if ro == "US" else ro)
        return float(REGIONAL_PRIORS.get(key, 0.75))
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


def gravity_weekly(origin_ap: dict, dest_ap: dict, distance_nm: float) -> float:
    k = _fc("bts_gravity_k", 0.00050176)
    alpha = _fc("bts_gravity_alpha", 0.80)
    beta = _fc("bts_gravity_beta", 1.20)
    damp = _fc("bts_small_small_damp", 0.7234)

    score_o = max(1.0, float(origin_ap.get("score") or 1))
    score_d = max(1.0, float(dest_ap.get("score") or 1))
    dist = max(50.0, float(distance_nm))

    weekly = k * ((score_o * score_d) ** alpha) / (dist ** beta)

    if (
        str(origin_ap.get("category") or "") == "small_airport"
        and str(dest_ap.get("category") or "") == "small_airport"
    ):
        weekly *= damp

    weekly *= regional_prior(origin_ap, dest_ap)
    return max(0.0, float(weekly))


def compute_base_demand(
    distance_nm: float,
    origin_airport: dict,
    dest_airport: dict,
) -> dict[str, Any]:
    """
    Returns base_demand_business / leisure plus demand_source for route creation.

    Gravity is fitted to BTS weekly markets, then scaled by Mode B ``calibration_k``.
    On many non-BTS pairs (especially long-haul international) that product sits at
    the floor while LEGACY is far larger — prefer LEGACY whenever gravity loses so
    new routes stay playable until better non-US anchors exist.
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

    legacy_b, legacy_l = legacy_base_demand(distance_nm, origin_airport, dest_airport)
    legacy_total = float(max(0, int(legacy_b)) + max(0, int(legacy_l)))

    source = "LEGACY"
    base_total = 0.0
    if anchor is not None and float(anchor) > 0:
        base_total = float(anchor) * k
        source = "BTS"
    else:
        try:
            g = gravity_weekly(origin_airport, dest_airport, distance_nm)
            scores_ok = (
                origin_airport.get("score") is not None
                and dest_airport.get("score") is not None
            )
            if g > 0 and scores_ok:
                calibrated = float(g) * k
                # Only keep gravity when it is meaningfully above the floor AND
                # at least as large as the old category formula.
                if calibrated > floor and calibrated >= legacy_total:
                    base_total = calibrated
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
    # Soft weekly pool floor (pax/week after eff_mult, before seasonality/logit).
    min_pool = _fc("bts_min_weekly_pool", 500.0)
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
