"""
Single AI P&L book: capacity-capped revenue, player-symmetric fees, lease share.

Paper estimates and flown legs both call weekly_pair_pnl / one_leg_pnl.
Do not invent crew or maintenance — the player does not pay those.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from db import db
from engine.demand import (
    _distance_reference_fares,
    _logit_utility,
    get_seasonality_multiplier,
)
from engine.routes import get_route


TAIL_HOURS_PER_WEEK = 60.0
SEAT_LOAD_CAP = 0.94


def _fc(key: str, default: float) -> float:
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    if not row:
        return float(default)
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return float(default)


def tail_hours_per_week() -> float:
    return float(_fc("ai_tail_block_hours_per_week", TAIL_HOURS_PER_WEEK))


def cabin_seats(type_id: str) -> tuple[int, int]:
    cab = db.fetch_one(
        "SELECT seats_economy, seats_premium_economy, seats_business, seats_first FROM aircraft_default_config WHERE type_id = ?",
        (str(type_id),),
    )
    if not cab:
        return 150, 16
    y = int(cab["seats_economy"] or 0) + int(cab["seats_premium_economy"] or 0)
    j = int(cab["seats_business"] or 0) + int(cab["seats_first"] or 0)
    return max(1, y), max(0, j)


def block_hours(distance_nm: float, type_id: str) -> float:
    ac = db.fetch_one(
        "SELECT cruise_speed_kts FROM aircraft_types WHERE type_id = ?",
        (str(type_id),),
    )
    kts = max(120.0, float((ac["cruise_speed_kts"] if ac else None) or 450.0))
    return max(0.4, float(distance_nm) / kts)


def weekly_lease_share(frequency: int, hours: float, weekly_lease: float) -> float:
    used = 2.0 * max(1, int(frequency)) * float(hours)
    frac = min(1.0, used / max(1.0, tail_hours_per_week()))
    return float(weekly_lease) * frac


def player_operates_route(route_id: str) -> bool:
    rid = str(route_id)
    if db.fetch_one("SELECT 1 FROM flight_segments WHERE route_id = ? LIMIT 1", (rid,)):
        return True
    if db.fetch_one(
        "SELECT 1 FROM flight_schedules WHERE route_id = ? AND COALESCE(active, 1) = 1 LIMIT 1",
        (rid,),
    ):
        return True
    return False


def player_operates_pair(outbound_id: str, inbound_id: str) -> bool:
    return player_operates_route(outbound_id) or player_operates_route(inbound_id)


def market_anchor_fares(route_id: str) -> tuple[float, float]:
    """Player's listed fares if they fly it; else distance reference (not placeholder)."""
    rt = get_route(route_id)
    if not rt:
        return 180.0, 90.0
    if player_operates_route(str(rt["route_id"])):
        return float(rt["price_business"] or 0), float(rt["price_leisure"] or 0)
    rb, rl = _distance_reference_fares(float(rt["distance_nm"] or 0))
    return float(rb), float(rl)


def _landing_or_gate(iata: str, mtow_lbs: float, kind: str) -> float:
    ap = db.fetch_one(
        "SELECT landing_fee_override, gate_fee_override, category FROM airports WHERE iata = ?",
        (str(iata).upper(),),
    )
    if not ap:
        return 0.0
    cat = db.fetch_one(
        "SELECT landing_fee_per_1000, gate_fee FROM airport_categories WHERE category = ?",
        (str(ap["category"] or "medium_airport"),),
    )
    if kind == "landing":
        if ap["landing_fee_override"] is not None:
            rate = float(ap["landing_fee_override"])
        else:
            rate = float(cat["landing_fee_per_1000"] or 0.0) if cat else 0.0
        return (float(mtow_lbs) / 1000.0) * rate
    if ap["gate_fee_override"] is not None:
        return float(ap["gate_fee_override"])
    return float(cat["gate_fee"] or 0.0) if cat else 0.0


def _market_demand(
    route: Dict[str, Any],
    fare_business: float,
    fare_leisure: float,
    month: int,
) -> tuple[float, float]:
    """Directional weekly market demand using AI fares vs distance reference."""
    from engine.demand import price_factor, _demand_segment_multiplier, _passenger_demand_multiplier

    ref_b, ref_l = _distance_reference_fares(float(route.get("distance_nm") or 0.0))
    sb = float(get_seasonality_multiplier(month, "business"))
    sl = float(get_seasonality_multiplier(month, "leisure"))
    pb = price_factor(float(fare_business), "business", ref_b)
    pl = price_factor(float(fare_leisure), "leisure", ref_l)
    scale = _passenger_demand_multiplier()
    bus_seg = _demand_segment_multiplier("business")
    lei_seg = _demand_segment_multiplier("leisure")
    demand_b = (
        float(route["base_demand_business"] or 0)
        * sb * pb * scale * bus_seg
    )
    demand_l = (
        float(route["base_demand_leisure"] or 0)
        * sl * pl * scale * lei_seg
    )
    return max(0.0, demand_b), max(0.0, demand_l)


def segment_shares(
    competitor_id: str,
    route_id: str,
    fare_leisure: float,
    fare_business: float,
) -> tuple[float, float]:
    """
    Leisure and business shares for this AI using demand._logit_utility.
    Player is in the choice set only if they actually operate the pair.
    """
    cid = str(competitor_id)
    rid = str(route_id)
    me = db.fetch_one(
        "SELECT reputation FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    my_rep = float(me["reputation"] or 50.0) if me else 50.0
    others = db.fetch_all(
        """
        SELECT cr.competitor_id, cr.fare_business, cr.fare_leisure, c.reputation
        FROM competitor_routes cr
        JOIN competitors c ON c.competitor_id = cr.competitor_id
        WHERE (cr.outbound_route_id = ? OR cr.inbound_route_id = ?)
          AND cr.status IN ('ACTIVE','SUSPENDED')
          AND cr.competitor_id != ?
        """,
        (rid, rid, cid),
    )
    pair = canonical_ids_for_route(rid)
    include_player = player_operates_pair(pair[0], pair[1]) if pair else False
    exp_b = [math.exp(_logit_utility(float(fare_business), my_rep, "business"))]
    exp_l = [math.exp(_logit_utility(float(fare_leisure), my_rep, "leisure"))]
    if include_player:
        rt = get_route(rid)
        al = db.fetch_one("SELECT reputation_score FROM airline WHERE id = 1")
        rep_p = float(al["reputation_score"] or 50.0) if al else 50.0
        if rt:
            exp_b.append(math.exp(_logit_utility(float(rt["price_business"] or fare_business), rep_p, "business")))
            exp_l.append(math.exp(_logit_utility(float(rt["price_leisure"] or fare_leisure), rep_p, "leisure")))
    for r in others or []:
        rep = float(r["reputation"] or 50.0)
        exp_b.append(math.exp(_logit_utility(float(r["fare_business"]), rep, "business")))
        exp_l.append(math.exp(_logit_utility(float(r["fare_leisure"]), rep, "leisure")))
    den_b = sum(exp_b) or 1.0
    den_l = sum(exp_l) or 1.0
    return float(exp_l[0] / den_l), float(exp_b[0] / den_b)


def canonical_ids_for_route(route_id: str) -> Optional[tuple[str, str]]:
    rid = str(route_id or "")
    if "-" not in rid:
        return None
    a, b = rid.split("-", 1)
    return f"{a}-{b}", f"{b}-{a}"


def weekly_pair_pnl(
    competitor_id: str,
    route_pair_id: str,
    fare_leisure: float,
    fare_business: float,
    frequency: int,
    type_id: str,
    *,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    from engine.ai import _ensure_route_rows_exist_for_pair

    cid = str(competitor_id)
    pair = str(route_pair_id).upper().strip()
    freq = max(1, int(frequency))
    tid = str(type_id)
    out_id, in_id = _ensure_route_rows_exist_for_pair(pair)
    rt = get_route(out_id)
    if not rt:
        return {
            "profit": -1e9,
            "share": 0.0,
            "revenue": 0.0,
            "cost": 0.0,
            "lf": 0.0,
            "pax": 0.0,
            "unit_cost": 999.0,
        }
    if month is None:
        gs = db.fetch_one("SELECT current_month FROM game_state WHERE id = 1")
        month = int(gs["current_month"] or 1) if gs else 1
    dist = float(rt["distance_nm"] or 0.0)
    hours = block_hours(dist, tid)
    seats_y, seats_j = cabin_seats(tid)
    seats = seats_y + seats_j
    cap_y = seats_y * freq * SEAT_LOAD_CAP
    cap_j = max(1, seats_j) * freq * SEAT_LOAD_CAP if seats_j else 0.0
    fl = float(fare_leisure)
    fb = float(fare_business)
    share_l, share_b = segment_shares(cid, out_id, fl, fb)
    share = 0.5 * (share_l + share_b)
    dem_b, dem_l = _market_demand(dict(rt), fb, fl, int(month))
    pax_l_one = min(dem_l * share_l, cap_y)
    pax_b_one = min(dem_b * share_b, cap_j) if seats_j else 0.0
    # Symmetric round-trip
    pax_l = pax_l_one * 2.0
    pax_b = pax_b_one * 2.0
    revenue = pax_l * fl + pax_b * fb
    pax = pax_l + pax_b
    flights = 2.0 * freq
    seats_week = seats * flights * SEAT_LOAD_CAP
    lf = min(1.15, pax / max(1.0, seats * flights))

    ac = db.fetch_one(
        "SELECT fuel_burn_gph, mtow_lbs, weekly_lease_cost FROM aircraft_types WHERE type_id = ?",
        (tid,),
    )
    gph = float((ac["fuel_burn_gph"] if ac else None) or 500.0)
    mtow = float((ac["mtow_lbs"] if ac else None) or 150000.0)
    lease_wk = float((ac["weekly_lease_cost"] if ac else None) or 0.0)
    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    bbl = float((gs["fuel_price_current"] if gs else None) or 195.0)
    gal_price = bbl / 42.0 + float(_fc("fuel_tax_per_gallon", 0.043))
    fuel = hours * gph * gal_price * flights

    origin = str(rt["origin_iata"])
    dest = str(rt["dest_iata"])
    landing = (_landing_or_gate(origin, mtow, "landing") + _landing_or_gate(dest, mtow, "landing")) * flights
    gate = (_landing_or_gate(origin, mtow, "gate") + _landing_or_gate(dest, mtow, "gate")) * flights
    excise = revenue * float(_fc("excise_tax_rate", 0.075))
    pax_fees = pax * (
        float(_fc("segment_fee", 5.30))
        + float(_fc("security_fee", 5.60))
        + float(_fc("pfc_fee", 4.50))
    )
    lease = weekly_lease_share(freq, hours, lease_wk)
    cost = fuel + landing + gate + excise + pax_fees + lease
    unit = cost / max(1.0, pax)
    return {
        "profit": float(revenue - cost),
        "share": float(share),
        "share_leisure": float(share_l),
        "share_business": float(share_b),
        "revenue": float(revenue),
        "cost": float(cost),
        "lf": float(lf),
        "pax": float(pax),
        "unit_cost": float(unit),
        "lease": float(lease),
        "hours": float(hours),
        "seats": int(seats),
    }


def one_leg_pnl(
    competitor_id: str,
    route_id: str,
    fare_leisure: float,
    fare_business: float,
    frequency: int,
    type_id: str,
) -> Dict[str, Any]:
    """One directional arrival: weekly pair book split across 2*freq legs."""
    from engine.ai import canonical_pair_for_competitor

    cid = str(competitor_id)
    pair, _o, _i = canonical_pair_for_competitor(cid, route_id)
    book = weekly_pair_pnl(cid, pair, fare_leisure, fare_business, frequency, type_id)
    denom = max(1.0, 2.0 * max(1, int(frequency)))
    seats_y, seats_j = cabin_seats(type_id)
    sl = float(book.get("share_leisure") or 0.5)
    sb = float(book.get("share_business") or 0.5)
    mix = sl + sb if (sl + sb) > 1e-6 else 1.0
    pax_leg = book["pax"] / denom
    pax_l = int(min(seats_y, max(0, round(pax_leg * (sl / mix)))))
    pax_b = int(min(seats_j, max(0, round(pax_leg - pax_l))))
    revenue = book["revenue"] / denom
    cost_var = (book["cost"] - book["lease"]) / denom
    lf = float(pax_l + pax_b) / float(max(1, seats_y + seats_j))
    return {
        "pax_leisure": pax_l,
        "pax_business": pax_b,
        "revenue": float(revenue),
        "net": float(revenue - cost_var),
        "lf": float(lf),
    }


def charge_weekly_fixed_costs(competitor_id: str) -> float:
    """Debit weekly lease for every ACTIVE ai_fleet tail. Called once per settlement week."""
    cid = str(competitor_id)
    rows = db.fetch_all(
        """
        SELECT t.weekly_lease_cost
        FROM ai_fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE f.competitor_id = ? AND COALESCE(f.status, 'ACTIVE') = 'ACTIVE'
        """,
        (cid,),
    )
    total = 0.0
    for r in rows or []:
        total += float(r["weekly_lease_cost"] or 0.0)
    if total <= 0:
        return 0.0
    db.execute("UPDATE competitors SET cash = cash - ? WHERE competitor_id = ?", (total, cid))
    return total


def weekly_fixed_cost(competitor_id: str) -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(t.weekly_lease_cost), 0) AS s
        FROM ai_fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE f.competitor_id = ? AND COALESCE(f.status, 'ACTIVE') = 'ACTIVE'
        """,
        (str(competitor_id),),
    )
    return float(row["s"] or 0.0) if row else 0.0


def fleet_block_hours_used(competitor_id: str) -> float:
    rows = db.fetch_all(
        """
        SELECT cr.frequency_per_week, cr.aircraft_type_id, cr.outbound_route_id
        FROM competitor_routes cr
        WHERE cr.competitor_id = ? AND COALESCE(cr.status, 'ACTIVE') = 'ACTIVE'
        """,
        (str(competitor_id),),
    )
    used = 0.0
    for r in rows or []:
        rt = get_route(str(r["outbound_route_id"]))
        dist = float((rt["distance_nm"] if rt else 0) or 800.0)
        tid = str(r["aircraft_type_id"] or "A320")
        used += 2.0 * max(1, int(r["frequency_per_week"] or 1)) * block_hours(dist, tid)
    return used


def fleet_block_hours_cap(competitor_id: str) -> float:
    row = db.fetch_one(
        "SELECT fleet_size FROM competitors WHERE competitor_id = ?",
        (str(competitor_id),),
    )
    n = int(row["fleet_size"] or 0) if row else 0
    return float(n) * tail_hours_per_week()
