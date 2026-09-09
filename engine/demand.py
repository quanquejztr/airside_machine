"""
Demand Model & Pricing Engine for Phase 2.
Calculates passenger demand and revenue estimates based on pricing and market factors.
"""

import sys
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db
from engine.routes import get_route
from engine.setup import get_airline
from engine.rng import seeded_rng


def _passenger_demand_multiplier():
    """Global scale from financial_constants (synced from data/financial_constants.csv on startup)."""
    row = db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = ?",
        ("passenger_demand_multiplier",),
    )
    try:
        v = float(row["value"]) if row else 1.0
    except (TypeError, ValueError):
        v = 1.0
    return max(0.01, min(10.0, v))


def _cabin_market_split_from_pools(business_pax: int, leisure_pax: int) -> dict[str, int]:
    """
    Model-level cabin intent from business/leisure pool sizes (not seat-capped).
    Same split as route-opening preview. Used for demand outputs and what-if.
    """
    try:
        row = db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'demand_share_premium_economy'"
        )
        pe_share = float(row["value"]) if row else 0.30
    except Exception:
        pe_share = 0.15
    try:
        row = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'demand_share_first'")
        first_share = float(row["value"]) if row else 0.25
    except Exception:
        first_share = 0.10
    pe_share = max(0.0, min(0.9, pe_share))
    first_share = max(0.0, min(0.9, first_share))
    b = int(max(0, int(business_pax)))
    l = int(max(0, int(leisure_pax)))
    premium_economy_pax = int(round(float(l) * pe_share))
    economy_pax = int(max(0, l - premium_economy_pax))
    first_pax = int(round(float(b) * first_share))
    business_cabin_pax = int(max(0, b - first_pax))
    return {
        "economy_pax": max(0, economy_pax),
        "premium_economy_pax": max(0, premium_economy_pax),
        "business_cabin_pax": max(0, business_cabin_pax),
        "first_pax": max(0, first_pax),
    }


def _weekly_demand_noise_seed():
    """Rotates each in-game week at settlement; combined with route id for stable weekly draws."""
    row = db.fetch_one("SELECT demand_noise_seed FROM game_state WHERE id = 1")
    try:
        return int(row["demand_noise_seed"]) if row and row["demand_noise_seed"] is not None else 1
    except (TypeError, ValueError):
        return 1


def _distance_reference_fares(distance_nm: float) -> tuple[float, float]:
    """
    Market reference fares for elasticity (same formula as routes.open_route defaults).
    Demand responds when your listed fares move vs these references, not vs your own last save.
    """
    d = float(distance_nm)
    return round(d * 0.20, 2), round(d * 0.10, 2)


def _demand_segment_multiplier(segment):
    """Extra scale for business vs leisure weekly demand (financial_constants)."""
    key = (
        "demand_business_segment_multiplier"
        if segment == "business"
        else "demand_leisure_segment_multiplier"
    )
    default = 2.0 if segment == "business" else 3.5
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    try:
        v = float(row["value"]) if row else default
    except (TypeError, ValueError):
        v = default
    return max(0.01, min(20.0, v))


def get_seasonality_multiplier(month, segment):
    """
    Get seasonality multiplier for given month and segment.
    
    Args:
        month: 1-12 (January-December)
        segment: 'business' or 'leisure'
    
    Returns:
        float: Seasonality multiplier
    """
    result = db.fetch_one(
        "SELECT business_multiplier, leisure_multiplier FROM seasonality WHERE month = ?",
        (month,)
    )
    
    if not result:
        return 1.0  # Default to no adjustment if not found
    
    if segment == 'business':
        return result['business_multiplier']
    elif segment == 'leisure':
        return result['leisure_multiplier']
    else:
        return 1.0


def price_factor(fare, segment, base_fare):
    """
    Calculate price elasticity factor for demand.
    
    Business segment: Low elasticity (price changes have small impact)
    Leisure segment: High elasticity (price changes have large impact)
    
    Formula:
    - Business: elasticity = -0.3 (inelastic)
    - Leisure: elasticity = -1.2 (elastic)
    
    Price Factor = (base_fare / actual_fare) ^ elasticity
    
    Args:
        fare: Current fare being charged
        base_fare: Reference/base fare (from route defaults)
        segment: 'business' or 'leisure'
    
    Returns:
        float: Demand multiplier based on price. The elasticity term is clamped to
        0.05..2.0, but fares above 4× reference then decay without a floor, so
        revenue cannot grow without bound (see below).
    """
    if fare <= 0 or base_fare <= 0:
        return 1.0

    price_ratio = base_fare / fare

    if segment == 'business':
        elasticity = 0.3
    elif segment == 'leisure':
        elasticity = 1.2
    else:
        elasticity = 0.5

    factor = max(0.05, min(2.0, price_ratio ** elasticity))
    # Collapse demand above 4× reference so monopoly fares do not grow revenue forever.
    # This must be applied AFTER the clamp: folding it in before meant the 0.05 floor
    # swallowed it, demand stopped responding to price above ~8× reference, and gross
    # revenue (fare × a fixed passenger count) then scaled linearly with fare forever.
    if fare > 4.0 * base_fare:
        factor *= (4.0 * base_fare / fare) ** 2.0
    return factor


def preview_weekly_demand_before_open(origin_airport, dest_airport, distance_nm, game_week=None, current_month=None):
    """
    Weekly market demand for a route not yet in the DB (route-opening preview).

    Uses the same pipeline as compute_demand: BTS/gravity/legacy base demand, default fares
    (match open_route: $0.20/nm business, $0.10/nm leisure), seasonality, brand,
    noise, passenger_demand_multiplier, and business/leisure segment multipliers.
    """
    from engine.route_demand import compute_base_demand

    demand_info = compute_base_demand(distance_nm, origin_airport, dest_airport)
    base_business = int(demand_info["base_demand_business"])
    base_leisure = int(demand_info["base_demand_leisure"])
    price_business = round(float(distance_nm) * 0.20, 2)
    price_leisure = round(float(distance_nm) * 0.10, 2)

    airline = get_airline()
    brand_multiplier = airline["brand_power"] if airline else 1.0

    gs = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
    if game_week is None:
        game_week = int(gs["game_week"]) if gs else 1
    if current_month is None:
        current_month = int(gs["current_month"]) if gs else 1

    seasonality_business = get_seasonality_multiplier(current_month, "business")
    seasonality_leisure = get_seasonality_multiplier(current_month, "leisure")

    rb, rl = _distance_reference_fares(float(distance_nm))
    price_factor_business = price_factor(price_business, "business", rb)
    price_factor_leisure = price_factor(price_leisure, "leisure", rl)

    route_key = f"{origin_airport['iata']}-{dest_airport['iata']}"
    rng = seeded_rng(_weekly_demand_noise_seed(), game_week, route_key)
    noise_factor = rng.uniform(0.9, 1.1)
    demand_scale = _passenger_demand_multiplier()
    bus_seg = _demand_segment_multiplier("business")
    lei_seg = _demand_segment_multiplier("leisure")

    demand_business = int(
        base_business
        * seasonality_business
        * price_factor_business
        * brand_multiplier
        * noise_factor
        * demand_scale
        * bus_seg
    )
    demand_leisure = int(
        base_leisure
        * seasonality_leisure
        * price_factor_leisure
        * brand_multiplier
        * noise_factor
        * demand_scale
        * lei_seg
    )

    # Cabin split (for UI previews): same model as _cabin_market_split_from_pools
    csplit = _cabin_market_split_from_pools(demand_business, demand_leisure)
    market_total = max(0, demand_business + demand_leisure)

    return {
        "business_pax": max(0, demand_business),
        "leisure_pax": max(0, demand_leisure),
        "total_pax": market_total,
        "weekly_market_total": market_total,
        **csplit,
        "base_demand_business": base_business,
        "base_demand_leisure": base_leisure,
        "demand_source": demand_info.get("demand_source"),
        "market_floor_applied": bool(demand_info.get("market_floor_applied")),
        "price_business_default": price_business,
        "price_leisure_default": price_leisure,
        "game_week": game_week,
        "current_month": current_month,
        "demand_scale": demand_scale,
        "business_segment_scale": bus_seg,
        "leisure_segment_scale": lei_seg,
        "noise_factor": noise_factor,
        "brand_multiplier": brand_multiplier,
    }


def _elasticity_for_logit(segment: str) -> float:
    return 0.3 if segment == "business" else 1.2


def _logit_utility(fare: float, reputation: float, segment: str) -> float:
    f = max(1.0, float(fare))
    e = _elasticity_for_logit(segment)
    return (-e) * math.log(f) + 0.02 * float(reputation)


def _fetch_competitor_rows_for_route(route_id: str) -> List[Dict[str, Any]]:
    return list(
        db.fetch_all(
            """
            SELECT cr.competitor_id, cr.fare_business, cr.fare_leisure, c.reputation
            FROM competitor_routes cr
            JOIN competitors c ON c.competitor_id = cr.competitor_id
            WHERE (cr.outbound_route_id = ? OR cr.inbound_route_id = ?)
              AND cr.status IN ('ACTIVE','SUSPENDED')
            """,
            (route_id, route_id),
        )
        or []
    )


def compute_logit_shares_for_route(
    route_id: str,
    *,
    player_fare_business: float,
    player_fare_leisure: float,
    player_reputation: float,
    competitor_rows: Optional[List[Any]] = None,
    pre_business: Optional[float] = None,
    pre_leisure: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Multinomial logit over player + each AI, per segment (business / leisure).
    player_score = -(elasticity) * log(fare) + 0.02 * reputation (same for competitors).
    """
    rows = competitor_rows if competitor_rows is not None else _fetch_competitor_rows_for_route(route_id)
    if not rows:
        return {
            "player_share_business": 1.0,
            "player_share_leisure": 1.0,
            "competitor_factor": 0.0,
            "competitors": [],
        }

    u_pb = _logit_utility(player_fare_business, player_reputation, "business")
    u_pl = _logit_utility(player_fare_leisure, player_reputation, "leisure")
    exp_b = [math.exp(u_pb)]
    exp_l = [math.exp(u_pl)]
    comp_ids: List[str] = []
    for r in rows:
        rep = float(r["reputation"] or 50.0)
        comp_ids.append(str(r["competitor_id"]))
        exp_b.append(math.exp(_logit_utility(float(r["fare_business"]), rep, "business")))
        exp_l.append(math.exp(_logit_utility(float(r["fare_leisure"]), rep, "leisure")))

    den_b = sum(exp_b) or 1.0
    den_l = sum(exp_l) or 1.0
    ps_b = exp_b[0] / den_b
    ps_l = exp_l[0] / den_l
    comps_out: List[Dict[str, Any]] = []
    for i, cid in enumerate(comp_ids):
        comps_out.append(
            {
                "competitor_id": cid,
                "share_business": exp_b[i + 1] / den_b,
                "share_leisure": exp_l[i + 1] / den_l,
            }
        )
    pb = float(pre_business) if pre_business is not None else None
    pl = float(pre_leisure) if pre_leisure is not None else None
    if pb is not None and pl is not None and (pb + pl) > 1e-6:
        cf = (pb * (1.0 - float(ps_b)) + pl * (1.0 - float(ps_l))) / (pb + pl)
    else:
        cf = 1.0 - 0.5 * (float(ps_b) + float(ps_l))
    return {
        "player_share_business": float(ps_b),
        "player_share_leisure": float(ps_l),
        "competitor_factor": float(max(0.0, min(1.0, cf))),
        "competitors": comps_out,
    }


def _pre_share_route_demands(
    route: Dict[str, Any],
    airline: Optional[Any],
    game_week: int,
    current_month: int,
    *,
    fare_base_business: Optional[float] = None,
    fare_base_leisure: Optional[float] = None,
) -> Dict[str, Any]:
    """Same demand pipeline as compute_demand, before competitor logit dilution."""
    brand_multiplier = airline["brand_power"] if airline else 1.0
    base_business = route["base_demand_business"]
    base_leisure = route["base_demand_leisure"]
    seasonality_business = get_seasonality_multiplier(current_month, "business")
    seasonality_leisure = get_seasonality_multiplier(current_month, "leisure")
    ref_b, ref_l = _distance_reference_fares(float(route.get("distance_nm") or 0.0))
    bb = float(fare_base_business) if fare_base_business is not None else ref_b
    bl = float(fare_base_leisure) if fare_base_leisure is not None else ref_l
    price_factor_business = price_factor(
        route["price_business"],
        "business",
        bb,
    )
    price_factor_leisure = price_factor(
        route["price_leisure"],
        "leisure",
        bl,
    )
    route_id = str(route["route_id"])
    rng = seeded_rng(_weekly_demand_noise_seed(), int(game_week), route_id)
    noise_factor = rng.uniform(0.9, 1.1)
    demand_scale = _passenger_demand_multiplier()
    bus_seg = _demand_segment_multiplier("business")
    lei_seg = _demand_segment_multiplier("leisure")
    demand_business = int(
        base_business
        * seasonality_business
        * price_factor_business
        * brand_multiplier
        * noise_factor
        * demand_scale
        * bus_seg
    )
    demand_leisure = int(
        base_leisure
        * seasonality_leisure
        * price_factor_leisure
        * brand_multiplier
        * noise_factor
        * demand_scale
        * lei_seg
    )
    return {
        "pre_business": max(0, demand_business),
        "pre_leisure": max(0, demand_leisure),
        "seasonality_business": seasonality_business,
        "seasonality_leisure": seasonality_leisure,
        "price_factor_business": price_factor_business,
        "price_factor_leisure": price_factor_leisure,
        "brand_multiplier": brand_multiplier,
        "noise_factor": noise_factor,
        "demand_scale": demand_scale,
        "business_segment_scale": bus_seg,
        "leisure_segment_scale": lei_seg,
    }


def compute_route_contested_intel(route_id: str, game_week: int = 1, current_month: int = 1) -> Optional[Dict[str, Any]]:
    """Player vs AI fares and logit shares for UI (no DB writes)."""
    route = get_route(route_id)
    if not route:
        return None
    airline = get_airline()
    player_rep = float(airline["reputation_score"] or 50.0) if airline else 50.0
    comp_rows = _fetch_competitor_rows_for_route(route_id)
    if not comp_rows:
        return None
    pre = _pre_share_route_demands(route, airline, game_week, current_month)
    shares = compute_logit_shares_for_route(
        route_id,
        player_fare_business=float(route["price_business"]),
        player_fare_leisure=float(route["price_leisure"]),
        player_reputation=player_rep,
        competitor_rows=comp_rows,
        pre_business=float(pre["pre_business"]),
        pre_leisure=float(pre["pre_leisure"]),
    )
    by_id = {str(r["competitor_id"]): r for r in comp_rows}
    comp_lines = []
    for c in shares["competitors"]:
        rr = by_id.get(str(c["competitor_id"]))
        if not rr:
            continue
        comp_lines.append(
            {
                "competitor_id": c["competitor_id"],
                "fare_business": float(rr["fare_business"]),
                "fare_leisure": float(rr["fare_leisure"]),
                "share_business": c["share_business"],
                "share_leisure": c["share_leisure"],
            }
        )
    return {
        "route_id": route_id,
        "player_fare_business": float(route["price_business"]),
        "player_fare_leisure": float(route["price_leisure"]),
        "player_share_business": shares["player_share_business"],
        "player_share_leisure": shares["player_share_leisure"],
        "competitor_factor": shares["competitor_factor"],
        "competitors": comp_lines,
        "pre_business": pre["pre_business"],
        "pre_leisure": pre["pre_leisure"],
    }


def estimate_competitor_route_load_factor(
    competitor_id: str,
    route_id: str,
    *,
    game_week: int,
    current_month: int,
    assumed_seats_per_flight: int = 160,
) -> Optional[float]:
    """Proxy weekly load factor for one AI on a route (for fare rules). None if unknown."""
    cid = str(competitor_id)
    rid = str(route_id)
    route = get_route(rid)
    if not route:
        return None
    cr = db.fetch_one(
        """
        SELECT frequency_per_week, aircraft_type_id FROM competitor_routes
        WHERE competitor_id = ?
          AND (outbound_route_id = ? OR inbound_route_id = ? OR route_pair_id = ?)
        """,
        (cid, rid, rid, rid),
    )
    if not cr:
        return None
    freq = max(1, int(cr["frequency_per_week"] or 1))
    airline = get_airline()
    player_rep = float(airline["reputation_score"] or 50.0) if airline else 50.0
    comp_rows = _fetch_competitor_rows_for_route(rid)
    if not comp_rows:
        return None
    pre = _pre_share_route_demands(route, airline, int(game_week), int(current_month))
    pb = float(pre["pre_business"])
    pl = float(pre["pre_leisure"])
    shares = compute_logit_shares_for_route(
        rid,
        player_fare_business=float(route["price_business"]),
        player_fare_leisure=float(route["price_leisure"]),
        player_reputation=player_rep,
        competitor_rows=comp_rows,
        pre_business=pb,
        pre_leisure=pl,
    )
    mine = next((x for x in shares["competitors"] if x["competitor_id"] == cid), None)
    if not mine:
        return None
    pax = pb * float(mine["share_business"]) + pl * float(mine["share_leisure"])
    from engine.ai_economics import cabin_seats

    seats_y, seats_j = cabin_seats(str(cr["aircraft_type_id"] or "A320"))
    seats = max(1, seats_y + seats_j)
    cap = float(freq * seats)
    if cap <= 0:
        return None
    return float(max(0.0, min(1.15, pax / cap)))


def compute_demand(route_id, game_week=1, current_month=1, competitor_factor=None, persist_share=False):
    """
    Calculate passenger demand for a route using the full demand formula.
    
    Formula:
    Demand = Base * Seasonality * Price Factor * Brand * Noise * passenger_demand_multiplier
             * segment multiplier (2.0 business / 3.5 leisure defaults from financial_constants)
    
    Args:
        route_id: Route identifier (e.g., 'TPA-JFK')
        game_week: Current game week (default 1 for Phase 2)
        current_month: Current month 1-12 (default 1 for Phase 2)
        competitor_factor: Optional override for aggregate non-player share in [0,1];
            when None, logit shares vs competitors are computed automatically.
    
    Returns:
        dict: {
            'business_pax': int,
            'leisure_pax': int,
            'total_pax': int,
            'business_base': int,
            'leisure_base': int,
            'business_seasonality': float,
            'leisure_seasonality': float,
            'business_price_factor': float,
            'leisure_price_factor': float,
            'brand_multiplier': float,
            'noise_factor': float,
            'competitor_factor': float,
        }
    """
    route = get_route(route_id)
    if not route:
        raise ValueError(f"Route '{route_id}' not found.")

    airline = get_airline()
    player_rep = float(airline["reputation_score"] or 50.0) if airline else 50.0

    base_business = route["base_demand_business"]
    base_leisure = route["base_demand_leisure"]

    pre = _pre_share_route_demands(route, airline, int(game_week), int(current_month))
    demand_business = int(pre["pre_business"])
    demand_leisure = int(pre["pre_leisure"])
    seasonality_business = pre["seasonality_business"]
    seasonality_leisure = pre["seasonality_leisure"]
    price_factor_business = pre["price_factor_business"]
    price_factor_leisure = pre["price_factor_leisure"]
    brand_multiplier = pre["brand_multiplier"]
    noise_factor = pre["noise_factor"]
    demand_scale = pre["demand_scale"]
    bus_seg = pre["business_segment_scale"]
    lei_seg = pre["leisure_segment_scale"]

    share_b = 1.0
    share_l = 1.0
    competitor_factor_val = 0.0
    try:
        comps = _fetch_competitor_rows_for_route(str(route_id))
        if comps:
            if competitor_factor is not None:
                cf = float(competitor_factor)
                cf = max(0.0, min(1.0, cf))
                competitor_factor_val = cf
                share_b = share_l = max(0.05, min(1.0, 1.0 - cf))
                if persist_share:
                    db.execute(
                        "UPDATE routes SET competitor_share_this_week = ? WHERE route_id = ?",
                        (competitor_factor_val, str(route_id)),
                    )
            else:
                lg = compute_logit_shares_for_route(
                    str(route_id),
                    player_fare_business=float(route["price_business"]),
                    player_fare_leisure=float(route["price_leisure"]),
                    player_reputation=player_rep,
                    competitor_rows=comps,
                    pre_business=float(demand_business),
                    pre_leisure=float(demand_leisure),
                )
                share_b = max(0.05, min(1.0, float(lg["player_share_business"])))
                share_l = max(0.05, min(1.0, float(lg["player_share_leisure"])))
                competitor_factor_val = float(lg["competitor_factor"])
                if persist_share:
                    db.execute(
                        "UPDATE routes SET competitor_share_this_week = ? WHERE route_id = ?",
                        (competitor_factor_val, str(route_id)),
                    )
        else:
            if persist_share:
                db.execute(
                    "UPDATE routes SET competitor_share_this_week = 0.0 WHERE route_id = ?",
                    (str(route_id),),
                )
    except Exception:
        competitor_factor_val = 0.0

    demand_business = int(float(demand_business) * float(share_b))
    demand_leisure = int(float(demand_leisure) * float(share_l))
    csplit = _cabin_market_split_from_pools(demand_business, demand_leisure)
    market_total = max(0, demand_business + demand_leisure)

    return {
        "business_pax": max(0, demand_business),
        "leisure_pax": max(0, demand_leisure),
        "total_pax": market_total,
        "weekly_market_total": market_total,
        **csplit,
        "business_base": base_business,
        "leisure_base": base_leisure,
        "business_seasonality": seasonality_business,
        "leisure_seasonality": seasonality_leisure,
        "business_price_factor": price_factor_business,
        "leisure_price_factor": price_factor_leisure,
        "brand_multiplier": brand_multiplier,
        "noise_factor": noise_factor,
        "demand_scale": demand_scale,
        "business_segment_scale": bus_seg,
        "leisure_segment_scale": lei_seg,
        "player_share_business": share_b,
        "player_share_leisure": share_l,
        "player_share": 0.5 * (float(share_b) + float(share_l)),
        "competitor_factor": competitor_factor_val,
    }


def compute_demand_with_price(route_id, price_business, price_leisure, game_week=1, current_month=1):
    """
    Calculate demand with hypothetical leisure/business *base* fares (for "what if" analysis).

    Uses the same elasticity baseline as compute_demand (distance × $0.20 / $0.10 per nm),
    so results align with Route details and weekly accounting. Premium/first ticket prices do
    not affect the B/L pools (they only affect revenue via compute_revenue elsewhere).

    Args:
        route_id: Route identifier
        price_business: Hypothetical business base (J bracket)
        price_leisure: Hypothetical leisure base (Y bracket)
        game_week: Current game week
        current_month: Current month

    Returns:
        dict: Same as compute_demand()
    """
    # Get route for base demand
    route = get_route(route_id)
    if not route:
        raise ValueError(f"Route '{route_id}' not found.")
    
    # Store original prices
    original_price_business = route['price_business']
    original_price_leisure = route['price_leisure']
    
    # Temporarily update prices for calculation
    route['price_business'] = price_business
    route['price_leisure'] = price_leisure
    
    airline = get_airline()
    player_rep = float(airline["reputation_score"] or 50.0) if airline else 50.0

    base_business = route["base_demand_business"]
    base_leisure = route["base_demand_leisure"]

    # Elasticity baseline = distance reference fares (same as compute_demand), so what-if matches live forecast.
    pre = _pre_share_route_demands(route, airline, int(game_week), int(current_month))
    demand_business = int(pre["pre_business"])
    demand_leisure = int(pre["pre_leisure"])
    seasonality_business = pre["seasonality_business"]
    seasonality_leisure = pre["seasonality_leisure"]
    price_factor_business = pre["price_factor_business"]
    price_factor_leisure = pre["price_factor_leisure"]
    brand_multiplier = pre["brand_multiplier"]
    noise_factor = pre["noise_factor"]
    demand_scale = pre["demand_scale"]
    bus_seg = pre["business_segment_scale"]
    lei_seg = pre["leisure_segment_scale"]

    share_b = 1.0
    share_l = 1.0
    competitor_factor_val = 0.0
    try:
        comps = _fetch_competitor_rows_for_route(str(route_id))
        if comps:
            lg = compute_logit_shares_for_route(
                str(route_id),
                player_fare_business=float(price_business),
                player_fare_leisure=float(price_leisure),
                player_reputation=player_rep,
                competitor_rows=comps,
                pre_business=float(demand_business),
                pre_leisure=float(demand_leisure),
            )
            share_b = max(0.05, min(1.0, float(lg["player_share_business"])))
            share_l = max(0.05, min(1.0, float(lg["player_share_leisure"])))
            competitor_factor_val = float(lg["competitor_factor"])
    except Exception:
        competitor_factor_val = 0.0

    demand_business = int(float(demand_business) * float(share_b))
    demand_leisure = int(float(demand_leisure) * float(share_l))
    csplit = _cabin_market_split_from_pools(demand_business, demand_leisure)

    route["price_business"] = original_price_business
    route["price_leisure"] = original_price_leisure

    return {
        "business_pax": max(0, demand_business),
        "leisure_pax": max(0, demand_leisure),
        "total_pax": max(0, demand_business + demand_leisure),
        **csplit,
        "business_base": base_business,
        "leisure_base": base_leisure,
        "business_seasonality": seasonality_business,
        "leisure_seasonality": seasonality_leisure,
        "business_price_factor": price_factor_business,
        "leisure_price_factor": price_factor_leisure,
        "brand_multiplier": brand_multiplier,
        "noise_factor": noise_factor,
        "demand_scale": demand_scale,
        "business_segment_scale": bus_seg,
        "leisure_segment_scale": lei_seg,
        "player_share_business": share_b,
        "player_share_leisure": share_l,
        "player_share": 0.5 * (float(share_b) + float(share_l)),
        "competitor_factor": competitor_factor_val,
    }


def compute_revenue(route_id, cabin_config, leisure_demand, business_demand, route_row=None):
    """
    Calculate gross revenue with cabin-aware seat fill logic.
    
    Implements Part C.2 seat fill logic from addendum:
    1. Fill leisure seats: economy first, then premium economy with overflow
    2. Fill business seats: business class first, then first class with overflow
    3. Calculate revenue per class using yield multipliers
    
    Args:
        route_id: Route identifier
        cabin_config: dict with seats_economy, seats_premium_economy, seats_business, seats_first
        leisure_demand: Number of leisure passengers wanting to fly
        business_demand: Number of business passengers wanting to fly
        route_row: Optional route dict (e.g. hypothetical fares without a DB write)
    
    Returns:
        dict: {
            'pax_economy': int,
            'pax_premium_economy': int,
            'pax_business': int,
            'pax_first': int,
            'total_pax': int,
            'revenue_economy': float,
            'revenue_premium_economy': float,
            'revenue_business': float,
            'revenue_first': float,
            'gross_revenue': float,
            'avg_fare': float,
            'load_factor': float
        }
    """
    from engine.cabin import fare_for_class
    
    route = route_row if route_row is not None else get_route(route_id)
    if not route:
        raise ValueError(f"Route '{route_id}' not found.")
    
    # Initialize passenger counts
    pax_economy = 0
    pax_premium_economy = 0
    pax_business = 0
    pax_first = 0
    
    # Cabin split model (works even when economy/business aren't full):
    # - A fraction of leisure prefers Premium Economy (W) over Economy (Y)
    # - A fraction of business prefers First (F) over Business (J)
    #
    # Demand is then seat-constrained per cabin.
    try:
        row = db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'demand_share_premium_economy'"
        )
        pe_share = float(row["value"]) if row else 0.30
    except Exception:
        pe_share = 0.15
    try:
        row = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'demand_share_first'")
        first_share = float(row["value"]) if row else 0.25
    except Exception:
        first_share = 0.10
    pe_share = max(0.0, min(0.9, pe_share))
    first_share = max(0.0, min(0.9, first_share))

    pref_w = int(round(float(leisure_demand) * pe_share))
    pref_y = int(max(0, int(leisure_demand) - pref_w))
    pref_f = int(round(float(business_demand) * first_share))
    pref_j = int(max(0, int(business_demand) - pref_f))

    pax_premium_economy = min(pref_w, int(cabin_config["seats_premium_economy"]))
    pax_economy = min(pref_y, int(cabin_config["seats_economy"]))
    leftover_leisure = (pref_w - pax_premium_economy) + (pref_y - pax_economy)
    w_room = int(cabin_config["seats_premium_economy"]) - pax_premium_economy
    extra_w = min(leftover_leisure, w_room)
    pax_premium_economy += extra_w
    leftover_leisure -= extra_w
    y_room = int(cabin_config["seats_economy"]) - pax_economy
    extra_y = min(leftover_leisure, y_room)
    pax_economy += extra_y

    pax_first = min(pref_f, int(cabin_config["seats_first"]))
    pax_business = min(pref_j, int(cabin_config["seats_business"]))
    leftover_business = (pref_f - pax_first) + (pref_j - pax_business)
    f_room = int(cabin_config["seats_first"]) - pax_first
    extra_f = min(leftover_business, f_room)
    pax_first += extra_f
    leftover_business -= extra_f
    j_room = int(cabin_config["seats_business"]) - pax_business
    extra_j = min(leftover_business, j_room)
    pax_business += extra_j
    
    # REVENUE CALCULATION
    # Get fares for each class using yield multipliers
    rdict = dict(route) if not isinstance(route, dict) else route
    try:
        fare_economy = fare_for_class("economy", route_id, route_row=rdict)
        fare_premium_economy = fare_for_class("premium_economy", route_id, route_row=rdict)
        fare_business = fare_for_class("business", route_id, route_row=rdict)
        fare_first = fare_for_class("first", route_id, route_row=rdict)
    except Exception:
        pl, pb = float(rdict.get("price_leisure") or 0), float(rdict.get("price_business") or 0)
        fare_economy = pl
        fare_premium_economy = pl * 1.5
        fare_business = pb * 2.0
        fare_first = pb * 4.0
    
    # Calculate revenue per class
    revenue_economy = pax_economy * fare_economy
    revenue_premium_economy = pax_premium_economy * fare_premium_economy
    revenue_business = pax_business * fare_business
    revenue_first = pax_first * fare_first
    
    # Total revenue
    gross_revenue = (
        revenue_economy +
        revenue_premium_economy +
        revenue_business +
        revenue_first
    )
    
    # Totals
    total_pax = pax_economy + pax_premium_economy + pax_business + pax_first
    total_seats = (
        cabin_config['seats_economy'] +
        cabin_config['seats_premium_economy'] +
        cabin_config['seats_business'] +
        cabin_config['seats_first']
    )
    
    avg_fare = gross_revenue / total_pax if total_pax > 0 else 0
    load_factor = (total_pax / total_seats) if total_seats > 0 else 0
    
    return {
        'pax_economy': pax_economy,
        'pax_premium_economy': pax_premium_economy,
        'pax_business': pax_business,
        'pax_first': pax_first,
        'total_pax': total_pax,
        'revenue_economy': revenue_economy,
        'revenue_premium_economy': revenue_premium_economy,
        'revenue_business': revenue_business,
        'revenue_first': revenue_first,
        'gross_revenue': gross_revenue,
        'avg_fare': avg_fare,
        'load_factor': load_factor,
        'leisure_demand': leisure_demand,
        'business_demand': business_demand,
        'leisure_spilled': max(0, leisure_demand - pax_economy - pax_premium_economy),
        'business_spilled': max(0, business_demand - pax_business - pax_first)
    }


def estimate_route_performance(route_id, cabin_config=None, game_week=None, current_month=None):
    """
    Complete route performance estimate combining demand and revenue.

    If game_week/current_month are omitted, reads ``game_state`` so forecasts match the live clock
    (same week as route list / weekly ops). Pass explicit integers to override (e.g. scenario tools).
    
    Args:
        route_id: Route identifier
        cabin_config: Optional cabin configuration dict. If None, uses default all-economy config.
        game_week: Current game week (default: from ``game_state``)
        current_month: Current month (default: from ``game_state``)
    
    Returns:
        dict: Combined demand and revenue data
    """
    gs = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
    gw_def = int(gs["game_week"] or 1) if gs else 1
    cm_def = int(gs["current_month"] or 1) if gs else 1
    gw = gw_def if game_week is None else int(game_week)
    cm = cm_def if current_month is None else int(current_month)

    demand = compute_demand(route_id, gw, cm)
    weekly_market_total = int(
        demand.get("weekly_market_total")
        or (int(demand.get("business_pax") or 0) + int(demand.get("leisure_pax") or 0))
    )

    # If no cabin config provided, create default all-economy config
    if cabin_config is None:
        # Get route to estimate seats (simplified for now)
        route = get_route(route_id)
        if route:
            # Default: assume 150-seat narrow-body with 12 business, rest economy
            cabin_config = {
                'seats_economy': 138,
                'seats_premium_economy': 0,
                'seats_business': 12,
                'seats_first': 0
            }
        else:
            raise ValueError(f"Route '{route_id}' not found.")

    # Calculate cabin-aware revenue
    revenue = compute_revenue(
        route_id,
        cabin_config,
        demand['leisure_pax'],
        demand['business_pax']
    )

    # revenue.total_pax is one-aircraft seat fill — do not overwrite weekly market.
    return {
        **demand,
        **revenue,
        "weekly_market_total": weekly_market_total,
        "aircraft_fill_pax": int(revenue.get("total_pax") or 0),
    }


if __name__ == "__main__":
    # Test the module
    print("Testing demand engine...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        # Test with a route if one exists
        routes = db.fetch_all("SELECT route_id FROM routes LIMIT 1")
        if routes:
            route_id = routes[0]['route_id']
            print(f"\nTesting cabin-aware demand calculation for route: {route_id}")
            
            # Test with default cabin config
            performance = estimate_route_performance(route_id)
            print(f"\n  Demand:")
            print(f"    Business: {performance['business_pax']} pax")
            print(f"    Leisure: {performance['leisure_pax']} pax")
            print(f"\n  Seat Fill:")
            print(f"    Economy: {performance['pax_economy']} pax")
            print(f"    Premium Economy: {performance['pax_premium_economy']} pax")
            print(f"    Business: {performance['pax_business']} pax")
            print(f"    First: {performance['pax_first']} pax")
            print(f"    Total: {performance['total_pax']} pax")
            print(f"    Load Factor: {performance['load_factor']:.1%}")
            print(f"\n  Revenue:")
            print(f"    Economy: ${performance['revenue_economy']:,.2f}")
            print(f"    Premium Economy: ${performance['revenue_premium_economy']:,.2f}")
            print(f"    Business: ${performance['revenue_business']:,.2f}")
            print(f"    First: ${performance['revenue_first']:,.2f}")
            print(f"    Total: ${performance['gross_revenue']:,.2f}")
            print(f"    Average Fare: ${performance['avg_fare']:.2f}")
        else:
            print("\n⚠ No routes found. Create a route first to test demand.")
