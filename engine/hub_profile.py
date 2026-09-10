"""
Hub selection metrics — what a player is choosing when they pick a home base.

Two numbers, deliberately separate components of the same total so they are not
redundant with each other:

  travel demand  = od_share x reachable market   -> locals and tourists, a 5-icon bar
  transit power  = 1 / od_share                  -> how much a connecting bank adds, "N.Nx"

The demand anchors are *segment* traffic and so already include connecting passengers.
Showing that raw total against a separate "transit" figure would count the same people
twice and make every large airport look strong on both. Splitting the total by od_share
is what makes Las Vegas and Charlotte — near-identical traffic, opposite character —
read differently.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db

# Local demand per week -> filled icons. Calibrated so the scale actually spreads the
# realistic candidates: the obvious round-number thresholds put half of them at 5/5,
# which is useless precisely where the choice is made.
DEMAND_ICON_THRESHOLDS = (450_000.0, 300_000.0, 180_000.0, 80_000.0)
MAX_DEMAND_ICONS = 5

# Aircraft range used for "what can I reach from here" before any fleet exists.
DEFAULT_REACH_NM = 3000.0
MIN_LEG_NM = 100.0
CANDIDATE_MIN_SCORE = 300_000


def _fc(key: str, default: float) -> float:
    try:
        v = db.get_financial_constant(key)
        return float(default if v is None else v)
    except Exception:
        return float(default)


def od_share_for(iata: str) -> float:
    row = db.fetch_one(
        "SELECT od_share FROM airports WHERE iata = ?", (str(iata).strip().upper(),)
    )
    default = _fc("od_share_default", 0.85)
    if not row or row["od_share"] is None:
        return default
    try:
        share = float(row["od_share"])
    except (TypeError, ValueError):
        return default
    return share if 0.0 < share <= 1.0 else default


def demand_icons(local_weekly: float) -> int:
    """Filled icons out of MAX_DEMAND_ICONS for a local weekly demand figure."""
    for i, threshold in enumerate(DEMAND_ICON_THRESHOLDS):
        if float(local_weekly) >= threshold:
            return MAX_DEMAND_ICONS - i
    return 1


def transit_power(iata: str) -> float:
    """How much the addressable market multiplies once a connecting bank exists.

    This is 1 / od_share, so it says exactly what it looks like. It tops out near 5.0x
    because no real airport runs below roughly 20% local traffic — a 10x rating would
    require an airport that does not exist.
    """
    return round(1.0 / max(0.01, od_share_for(iata)), 1)


def connecting_unlock_fraction(routes_at_hub: int) -> float:
    """Share of connecting demand earned by a network of this size.

    Threshold then ramp: a couple of routes feed nobody, so connections stay at zero
    until a real bank exists and then scale up. A purely linear curve would hand
    connecting passengers to a single route, which is not how a hub works.
    """
    lo = int(_fc("hub_connect_min_routes", 4))
    hi = int(_fc("hub_connect_full_routes", 14))
    n = int(routes_at_hub or 0)
    if n < lo:
        return 0.0
    if n >= hi or hi <= lo:
        return 1.0
    return round((n - lo) / float(hi - lo), 4)


def _reachable_markets(hub: str, reach_nm: float) -> List[Dict[str, Any]]:
    """Weekly market size to every worthwhile airport within range, one direction.

    Uses base demand rather than the full preview: seasonality swings a route by more
    than 60% across the year and the per-route noise term moves it +/-10% per week, so a
    live preview would make a hub's rating drift for reasons that carry no signal.
    """
    from engine.demand import preview_weekly_demand_before_open
    from engine.routes import haversine_distance

    origin = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (hub,))
    if not origin:
        raise ValueError(f"Airport '{hub}' not found.")
    o = dict(origin)

    rows = db.fetch_all(
        "SELECT * FROM airports WHERE lat IS NOT NULL AND lon IS NOT NULL"
        " AND iata != ? AND COALESCE(score, 0) >= ?",
        (hub, CANDIDATE_MIN_SCORE),
    )
    out: List[Dict[str, Any]] = []
    for raw in rows or []:
        a = dict(raw)
        try:
            dist = haversine_distance(
                float(o["lat"]), float(o["lon"]), float(a["lat"]), float(a["lon"])
            )
        except (TypeError, ValueError):
            continue
        if dist < MIN_LEG_NM or dist > float(reach_nm):
            continue
        try:
            dem = preview_weekly_demand_before_open(o, a, dist)
        except Exception:
            continue
        base = float(dem.get("base_demand_business") or 0) + float(
            dem.get("base_demand_leisure") or 0
        )
        scale = float(dem.get("demand_scale") or 1.0)
        bus_scale = float(dem.get("business_segment_scale") or 1.0)
        lei_scale = float(dem.get("leisure_segment_scale") or 1.0)
        market = (
            float(dem.get("base_demand_business") or 0) * bus_scale
            + float(dem.get("base_demand_leisure") or 0) * lei_scale
        ) * scale
        if market <= 0:
            continue
        out.append(
            {
                "iata": str(a["iata"]),
                "city": a.get("city"),
                "distance_nm": round(dist),
                "weekly_market": market,
                "business_base": float(dem.get("base_demand_business") or 0) * bus_scale * scale,
                "base_total": base,
            }
        )
    out.sort(key=lambda x: x["weekly_market"], reverse=True)
    return out


def hub_profile(iata: str, *, reach_nm: float = DEFAULT_REACH_NM, top_n: int = 8) -> Dict[str, Any]:
    """Everything shown for one candidate hub."""
    hub = str(iata or "").strip().upper()
    ap = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (hub,))
    if not ap:
        raise ValueError(f"Airport '{hub}' not found.")

    markets = _reachable_markets(hub, reach_nm)
    total = sum(m["weekly_market"] for m in markets)
    business = sum(m["business_base"] for m in markets)
    share = od_share_for(hub)
    local = total * share
    connecting = total - local

    from engine.gates import is_auctioned_airport, total_gate_units

    competitors = db.fetch_all(
        "SELECT competitor_id, name, fleet_size FROM competitors WHERE home_hub_iata = ?"
        " ORDER BY fleet_size DESC",
        (hub,),
    )

    return {
        "iata": hub,
        "name": ap["name"],
        "city": ap["city"],
        "country": ap["country"],
        "reach_nm": reach_nm,
        "reachable_destinations": len(markets),
        "weekly_market_total": round(total),
        "od_share": round(share, 3),
        "travel_demand_weekly": round(local),
        "travel_demand_icons": demand_icons(local),
        "travel_demand_icons_max": MAX_DEMAND_ICONS,
        "connecting_weekly": round(connecting),
        "transit_power": transit_power(hub),
        "business_share": round(business / total, 3) if total else 0.0,
        "gate_units": total_gate_units(hub),
        "gates_by_auction": bool(is_auctioned_airport(hub)),
        "competitors": [
            {
                "competitor_id": c["competitor_id"],
                "name": c["name"],
                "fleet_size": int(c["fleet_size"] or 0),
            }
            for c in (competitors or [])
        ],
        "top_destinations": [
            {
                "iata": m["iata"],
                "city": m["city"],
                "distance_nm": m["distance_nm"],
                "weekly_market": round(m["weekly_market"]),
            }
            for m in markets[: max(0, int(top_n))]
        ],
    }


def hub_candidates(
    *, limit: int = 30, min_score: int = 900_000, reach_nm: float = DEFAULT_REACH_NM
) -> List[Dict[str, Any]]:
    """Ranked shortlist for the hub picker, largest local market first."""
    rows = db.fetch_all(
        "SELECT iata FROM airports WHERE COALESCE(score, 0) >= ?"
        " AND lat IS NOT NULL ORDER BY score DESC LIMIT ?",
        (int(min_score), int(limit)),
    )
    out = []
    for r in rows or []:
        try:
            out.append(hub_profile(str(r["iata"]), reach_nm=reach_nm, top_n=3))
        except Exception:
            continue
    out.sort(key=lambda p: p["travel_demand_weekly"], reverse=True)
    return out
