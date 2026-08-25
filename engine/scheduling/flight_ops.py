"""Live flight clock callbacks and delay propagation."""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from db import db
from engine.aircraft import get_fleet_aircraft
from engine.airports import get_airport
from engine.cabin import get_cabin_config
from engine.demand import compute_demand, compute_revenue
from engine.routes import get_route, haversine_distance

def propagate_delay(tail_number: str, delay_minutes: int) -> dict:
    """
    Shift this tail's future legs forward by delay_minutes and re-normalize MTT ordering.

    - Only affects future legs (scheduled_dep_game_hour > current game time).
    - Shifts SCHEDULED/DELAYED/HOLDING legs; leaves IN_AIR anchors untouched.
    - Legs pushed beyond (now + 168h) are CANCELLED and logged.
    """
    from engine.scheduling.rotation import normalize_tail_schedule_for_week
    from engine.scheduling.time_helpers import _day_of_week_label, hhmm_from_absolute_game_hour
    tail = tail_number.strip().upper()
    dm = int(delay_minutes or 0)
    if dm <= 0:
        return {"tail_number": tail, "delay_minutes": dm, "shifted": 0, "cancelled": 0}

    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    now = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0
    horizon = now + 168.0
    delta_h = float(dm) / 60.0

    rows = db.fetch_all(
        """
        SELECT segment_id, status, scheduled_dep_game_hour, scheduled_arr_game_hour, delay_minutes,
               COALESCE(baseline_dep_game_hour, scheduled_dep_game_hour) AS base_dep
        FROM flight_segments
        WHERE tail_number = ?
          AND status IN ('SCHEDULED', 'DELAYED', 'HOLDING')
          AND scheduled_dep_game_hour > ?
        ORDER BY scheduled_dep_game_hour ASC, segment_id ASC
        """,
        (tail, now),
    )

    shifted = 0
    cancelled = 0
    for r in rows:
        sid = str(r["segment_id"])
        dep = float(r["scheduled_dep_game_hour"] or 0.0) + delta_h
        arr = float(r["scheduled_arr_game_hour"] or 0.0) + delta_h
        base_dep = float(r["base_dep"] or 0.0)
        new_delay = max(0, int(round((dep - base_dep) * 60.0)))

        if dep > horizon:
            db.execute("UPDATE flight_segments SET status = 'CANCELLED' WHERE segment_id = ?", (sid,))
            cancelled += 1
            try:
                from engine.events import insert_event_log

                gww = int(dep // 168.0) + 1
                insert_event_log(
                    game_week=gww,
                    game_time_hours=now,
                    event_type="CANCELLED",
                    description=f"{tail} leg auto-cancelled: pushed beyond 168h horizon by delay cascade",
                    affected_tail=tail,
                    financial_impact=0.0,
                )
            except Exception:
                pass
            continue

        db.execute(
            """
            UPDATE flight_segments
            SET scheduled_dep_game_hour = ?,
                scheduled_arr_game_hour = ?,
                scheduled_dep_time = ?,
                scheduled_arr_time = ?,
                delay_minutes = ?,
                status = CASE WHEN status = 'SCHEDULED' THEN 'DELAYED' ELSE status END,
                game_week = ?,
                day_of_week = ?
            WHERE segment_id = ?
            """,
            (
                dep,
                arr,
                hhmm_from_absolute_game_hour(dep),
                hhmm_from_absolute_game_hour(arr),
                new_delay,
                int(dep // 168.0) + 1,
                _day_of_week_label(int(dep // 168.0) + 1, dep),
                sid,
            ),
        )
        shifted += 1

    cal_week = int(now // 168.0) + 1
    try:
        normalize_tail_schedule_for_week(tail, cal_week, horizon_game_hour=horizon)
    except Exception:
        pass
    try:
        check_mtt_violation(tail)
    except Exception:
        pass

    return {
        "tail_number": tail,
        "delay_minutes": dm,
        "shifted": shifted,
        "cancelled": cancelled,
        "horizon_game_hour": horizon,
    }


def check_mtt_violation(tail_number: str) -> list[dict]:
    """
    Scan consecutive legs for this tail in the current calendar week and log MTT_VIOLATION warnings.
    Returns a list of violations found.
    """
    from engine.scheduling.shared import get_financial_constant
    from engine.scheduling.time_helpers import week_base_hours
    tail = tail_number.strip().upper()
    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    now = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0
    cal_week = int(now // 168.0) + 1
    w0 = week_base_hours(cal_week)
    w1 = w0 + 168.0
    mtt_h = float(get_financial_constant("mtt_minutes", 30)) / 60.0

    segs = db.fetch_all(
        """
        SELECT segment_id, status, scheduled_dep_game_hour, scheduled_arr_game_hour
        FROM flight_segments
        WHERE tail_number = ?
          AND scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status IN ('SCHEDULED', 'DELAYED', 'HOLDING', 'IN_AIR', 'LANDED', 'DIVERTED')
        ORDER BY scheduled_dep_game_hour ASC, segment_id ASC
        """,
        (tail, w0, w1),
    )
    out: list[dict] = []
    prev = None
    for s in segs:
        if prev is None:
            prev = s
            continue
        a_arr = float(prev["scheduled_arr_game_hour"] or 0.0)
        b_dep = float(s["scheduled_dep_game_hour"] or 0.0)
        gap = b_dep - a_arr
        if gap < mtt_h - 1e-9:
            out.append(
                {
                    "tail_number": tail,
                    "prev_segment_id": str(prev["segment_id"]),
                    "next_segment_id": str(s["segment_id"]),
                    "gap_hours": gap,
                    "mtt_hours": mtt_h,
                }
            )
        prev = s

    if not out:
        return []

    # Log one warning per tail/week (avoid spam)
    desc = f"MTT warning: {tail} schedule has {len(out)} gap(s) below {mtt_h:.2f}h"
    try:
        exists = db.fetch_one(
            """
            SELECT 1 AS ok FROM event_log
            WHERE game_week = ? AND event_type = 'MTT_VIOLATION'
              AND (affected_tail = ? OR affected_tail IS NULL)
              AND (description = ? OR message = ?)
            LIMIT 1
            """,
            (cal_week, tail, desc, desc),
        )
        if not exists:
            from engine.events import insert_event_log

            insert_event_log(
                game_week=cal_week,
                game_time_hours=now,
                event_type="MTT_VIOLATION",
                description=desc,
                affected_tail=tail,
                financial_impact=0.0,
            )
    except Exception:
        pass

    return out


def on_departure(segment_id):
    """
    Handle flight departure event.
    
    Called by SessionClock when a flight departs.
    
    Actions:
    - Set status to IN_AIR
    - Calculate demand and fill seats
    - Calculate revenue
    - Calculate fees and taxes
    - Write all data to flight_segments
    Args:
        segment_id: Flight segment identifier
    """
    from engine.scheduling.segments import _per_leg_demand_from_weekly_pool
    from engine.scheduling.shared import _flight_fuel_gallons, get_financial_constant
    # Get segment
    segment = db.fetch_one(
        "SELECT * FROM flight_segments WHERE segment_id = ?",
        (segment_id,)
    )
    
    if not segment:
        try:
            from engine.news_feed import push_news

            push_news(f"⚠️ Segment {segment_id} not found")
        except Exception:
            pass
        return

    segment = dict(segment)
    st = str(segment.get("status") or "")
    if st in ("IN_AIR", "LANDED", "DIVERTED", "CANCELLED"):
        return

    from engine.environment import expire_closures_before, is_airport_closed_at

    dep_h = float(segment["scheduled_dep_game_hour"] or 0)
    expire_closures_before(dep_h)
    origin_code = str(segment.get("origin_iata") or "")
    dest_code = str(segment.get("dest_iata") or "")
    if not origin_code or not dest_code:
        route_early = get_route(segment["route_id"])
        if route_early:
            origin_code = origin_code or str(route_early["origin_iata"])
            dest_code = dest_code or str(route_early["dest_iata"])
    if origin_code and is_airport_closed_at(origin_code, dep_h):
        db.execute(
            "UPDATE flight_segments SET status = 'HOLDING' WHERE segment_id = ?",
            (segment_id,),
        )
        return
    if dest_code and is_airport_closed_at(dest_code, dep_h):
        db.execute(
            "UPDATE flight_segments SET status = 'HOLDING' WHERE segment_id = ?",
            (segment_id,),
        )
        return

    try:
        from engine.events import check_aog_probability, trigger_aog

        p_aog = check_aog_probability(str(segment["tail_number"]))
        if p_aog > 0 and random.random() < p_aog:
            trigger_aog(
                str(segment["tail_number"]),
                game_time_hours=dep_h,
                reason="Mechanical — AOG at departure",
            )
            return
    except Exception:
        pass

    # Phase 11 revised: gate capacity is enforced at scheduling time (schedule save must reject invalid banks).
    
    # Get route and cabin config
    route = get_route(segment['route_id'])
    cabin_config = get_cabin_config(segment['tail_number'])
    
    if not cabin_config:
        # Use default cabin config
        cabin_config = {
            'seats_economy': 138,
            'seats_premium_economy': 0,
            'seats_business': 12,
            'seats_first': 0
        }
    
    # Get current game state
    game_state = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
    game_week = game_state['game_week'] if game_state else 1
    current_month = game_state['current_month'] if game_state else 1
    
    # Weekly route demand, then an equal split across all flights on this route/week.
    # A ferry carries no passengers, so it never touches the demand pool for that market.
    try:
        is_ferry = bool(int(segment["is_ferry"] or 0)) if "is_ferry" in segment.keys() else False
    except (KeyError, IndexError, TypeError, ValueError):
        is_ferry = False
    if is_ferry:
        demand = {"leisure_pax": 0, "business_pax": 0}
        leisure_leg, business_leg = 0, 0
    else:
        demand = compute_demand(segment['route_id'], game_week, current_month, persist_share=True)
        leisure_leg, business_leg = _per_leg_demand_from_weekly_pool(
            segment['route_id'],
            game_week,
            segment_id,
            demand['leisure_pax'],
            demand['business_pax'],
        )

    # Cabin-aware seat fill: per-leg demand is capped by fleet_cabin_config
    revenue = compute_revenue(
        segment['route_id'],
        cabin_config,
        leisure_leg,
        business_leg,
    )
    
    # Calculate fees and taxes
    # Get airports for fee calculation
    origin = get_airport(route['origin_iata'])
    dest = get_airport(route['dest_iata'])
    
    # Get aircraft for MTOW
    aircraft = db.fetch_one(
        "SELECT type_id FROM fleet WHERE tail_number = ?",
        (segment['tail_number'],)
    )
    aircraft_type = db.fetch_one(
        "SELECT mtow_lbs, fuel_burn_gph, cruise_speed_kts FROM aircraft_types WHERE type_id = ?",
        (aircraft['type_id'],)
    )
    
    mtow_lbs = aircraft_type['mtow_lbs']
    
    # Landing fees (origin + dest)
    landing_fee_origin = (mtow_lbs / 1000.0) * origin.get('landing_fee_per_1000', 0)
    landing_fee_dest = (mtow_lbs / 1000.0) * dest.get('landing_fee_per_1000', 0)
    total_landing_fee = landing_fee_origin + landing_fee_dest
    
    # Gate fees (origin + dest)
    gate_fee_origin = origin.get('gate_fee', 0)
    gate_fee_dest = dest.get('gate_fee', 0)
    total_gate_fee = gate_fee_origin + gate_fee_dest
    
    # Per-passenger fees
    total_pax = revenue['total_pax']
    
    excise_tax_rate = get_financial_constant('excise_tax_rate', 0.075)
    excise_tax = revenue['gross_revenue'] * excise_tax_rate
    
    segment_fee_per_pax = get_financial_constant('segment_fee', 5.30)
    segment_fee = total_pax * segment_fee_per_pax
    
    security_fee_per_pax = get_financial_constant('security_fee', 5.60)
    security_fee = total_pax * security_fee_per_pax
    
    pfc_fee_per_pax = get_financial_constant('pfc_fee', 4.50)
    pfc_fee = total_pax * pfc_fee_per_pax
    
    game_state_fuel = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    fuel_price_per_barrel = game_state_fuel["fuel_price_current"] if game_state_fuel else 195.0
    fuel_price_per_gallon = float(fuel_price_per_barrel) / 42.0
    fuel_tax_per_gallon = get_financial_constant("fuel_tax_per_gallon", 0.043)
    fuel_spot_per_gallon = fuel_price_per_gallon + float(fuel_tax_per_gallon)

    fuel_gallons = _flight_fuel_gallons(route, aircraft_type)
    fuel_cost_est = fuel_gallons * float(fuel_spot_per_gallon)
    try:
        from engine.fuel import resolve_fuel_cost

        fuel_cost_est, _ = resolve_fuel_cost(fuel_gallons, float(fuel_spot_per_gallon))
    except Exception:
        pass

    # Stored pax columns: leisure vs business traveler counts actually carried (by cabin funnel)
    pax_leisure_carried = int(revenue['pax_economy'] + revenue['pax_premium_economy'])
    pax_business_carried = int(revenue['pax_business'] + revenue['pax_first'])

    # Update segment with all data
    db.execute("""
        UPDATE flight_segments
        SET status = 'IN_AIR',
            actual_dep_game_hour = ?,
            pax_business = ?,
            pax_leisure = ?,
            pax_economy = ?,
            pax_premium_economy = ?,
            pax_business_cabin = ?,
            pax_first = ?,
            revenue_gross = ?,
            revenue_economy = ?,
            revenue_premium_economy = ?,
            revenue_business_cabin = ?,
            revenue_first = ?,
            fuel_spot_price_per_gallon = ?,
            excise_tax = ?,
            segment_fee = ?,
            security_fee = ?,
            pfc_fee = ?,
            landing_fee = ?,
            gate_fee = ?,
            fuel_burned_gallons = ?,
            fuel_cost = ?
        WHERE segment_id = ?
    """, (
        segment['scheduled_dep_game_hour'],
        pax_business_carried,
        pax_leisure_carried,
        int(revenue.get("pax_economy", 0) or 0),
        int(revenue.get("pax_premium_economy", 0) or 0),
        int(revenue.get("pax_business", 0) or 0),
        int(revenue.get("pax_first", 0) or 0),
        revenue['gross_revenue'],
        revenue['revenue_economy'],
        revenue['revenue_premium_economy'],
        revenue['revenue_business'],
        revenue['revenue_first'],
        fuel_spot_per_gallon,
        excise_tax,
        segment_fee,
        security_fee,
        pfc_fee,
        total_landing_fee,
        total_gate_fee,
        fuel_gallons,
        fuel_cost_est,
        segment_id
    ))

    try:
        from engine.news_feed import push_news

        push_news(
            f"✈️  {segment['flight_number']} departed {route['origin_iata']} → {route['dest_iata']} "
            f"({total_pax} pax, ${revenue['gross_revenue']:,.0f})"
        )
    except Exception:
        pass

    db.execute(
        """
        UPDATE fleet
        SET status = 'IN_AIR'
        WHERE tail_number = ?
        """,
        (segment["tail_number"],),
    )


def on_arrival(segment_id):
    """
    Handle flight arrival event.
    
    Called by SessionClock when a flight arrives.
    
    Actions:
    - Set status to LANDED
    - Update actual arrival time
    - Update aircraft location
    - Calculate fuel burned and cost
    - Calculate net contribution
    
    Args:
        segment_id: Flight segment identifier
    """
    from engine.scheduling.shared import _flight_fuel_gallons, get_financial_constant
    # Get segment
    segment = db.fetch_one(
        "SELECT * FROM flight_segments WHERE segment_id = ?",
        (segment_id,)
    )
    
    if not segment:
        try:
            from engine.news_feed import push_news

            push_news(f"⚠️ Segment {segment_id} not found")
        except Exception:
            pass
        return
    
    # Get route
    route = get_route(segment['route_id'])
    
    # Get aircraft type for fuel burn
    aircraft = db.fetch_one(
        "SELECT type_id FROM fleet WHERE tail_number = ?",
        (segment['tail_number'],)
    )
    if not aircraft:
        return
    aircraft_type = db.fetch_one(
        "SELECT fuel_burn_gph, cruise_speed_kts FROM aircraft_types WHERE type_id = ?",
        (aircraft['type_id'],)
    )
    if not aircraft_type:
        return
    
    # Calculate fuel burned
    fuel_burned_gallons = _flight_fuel_gallons(route, aircraft_type)

    already = segment["fuel_cost"]
    if already is not None:
        fuel_cost = float(already)
    else:
        spot = segment["fuel_spot_price_per_gallon"]
        if spot is None:
            game_state = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
            fuel_price_per_barrel = game_state["fuel_price_current"] if game_state else 195.0
            fuel_price_per_gallon = float(fuel_price_per_barrel) / 42.0
            fuel_tax_per_gallon = get_financial_constant("fuel_tax_per_gallon", 0.043)
            spot = fuel_price_per_gallon + float(fuel_tax_per_gallon)
        try:
            from engine.fuel import resolve_fuel_cost

            fuel_cost, _ = resolve_fuel_cost(fuel_burned_gallons, float(spot))
        except Exception:
            fuel_cost = fuel_burned_gallons * float(spot)
    
    # Calculate net contribution
    net_contribution = (
        segment['revenue_gross'] -
        segment['excise_tax'] -
        segment['segment_fee'] -
        segment['security_fee'] -
        segment['pfc_fee'] -
        segment['landing_fee'] -
        segment['gate_fee'] -
        fuel_cost
    )
    
    # Update segment
    db.execute("""
        UPDATE flight_segments
        SET status = 'LANDED',
            actual_arr_game_hour = ?,
            fuel_burned_gallons = ?,
            fuel_cost = ?,
            net_contribution = ?
        WHERE segment_id = ?
    """, (
        segment['scheduled_arr_game_hour'],
        fuel_burned_gallons,
        fuel_cost,
        net_contribution,
        segment_id
    ))
    
    # Update aircraft location
    db.execute("""
        UPDATE fleet
        SET current_airport_iata = ?, status = 'LANDED'
        WHERE tail_number = ?
    """, (route['dest_iata'], segment['tail_number']))
    
    try:
        from engine.news_feed import push_news

        push_news(
            f"🛬 {segment['flight_number']} arrived {route['dest_iata']} "
            f"(${net_contribution:,.0f} net)"
        )
    except Exception:
        pass


def get_flight_events_between(t_prev: float, t_now: float):
    """
    Flights with departure or arrival in (t_prev, t_now], plus overdue catch-up.

    DELAYED legs depart; HOLDING waits until the closed airport reopens.
    """
    from engine.environment import expire_closures_before, is_airport_closed_at

    expire_closures_before(t_now)

    holding = db.fetch_all(
        """
        SELECT fs.segment_id, COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
               COALESCE(fs.dest_iata, r.dest_iata) AS dest_iata
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.status = 'HOLDING'
        """
    )
    for h in holding or []:
        orig = str(h["origin_iata"] or "")
        dest = str(h["dest_iata"] or "")
        if orig and is_airport_closed_at(orig, t_now):
            continue
        if dest and is_airport_closed_at(dest, t_now):
            continue
        db.execute(
            "UPDATE flight_segments SET status = 'DELAYED' WHERE segment_id = ?",
            (h["segment_id"],),
        )

    departures = db.fetch_all(
        """
        SELECT segment_id
        FROM flight_segments
        WHERE status IN ('SCHEDULED', 'DELAYED')
          AND scheduled_dep_game_hour <= ?
        """,
        (t_now,),
    )
    arrivals = db.fetch_all(
        """
        SELECT segment_id
        FROM flight_segments
        WHERE status = 'IN_AIR'
          AND scheduled_arr_game_hour <= ?
        """,
        (t_now,),
    )
    ai_deps = []
    ai_arrs = []
    try:
        ai_deps = db.fetch_all(
            """
            SELECT segment_id
            FROM ai_flight_segments
            WHERE status IN ('SCHEDULED', 'DELAYED')
              AND scheduled_dep_game_hour <= ?
            """,
            (t_now,),
        )
        ai_arrs = db.fetch_all(
            """
            SELECT segment_id
            FROM ai_flight_segments
            WHERE status = 'IN_AIR'
              AND scheduled_arr_game_hour <= ?
            """,
            (t_now,),
        )
    except Exception:
        ai_deps = []
        ai_arrs = []
    return {
        "departures": [d["segment_id"] for d in departures],
        "arrivals": [a["segment_id"] for a in arrivals],
        "ai_departures": [d["segment_id"] for d in ai_deps],
        "ai_arrivals": [a["segment_id"] for a in ai_arrs],
    }


def get_all_flights():
    """
    Get all flights for current session.
    
    Returns:
        list: All flight segments with route info
    """
    flights = db.fetch_all(
        """
        SELECT
            fs.*,
            COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
            COALESCE(fs.dest_iata, r.dest_iata)     AS dest_iata,
            r.origin_iata AS route_origin_iata,
            r.dest_iata   AS route_dest_iata
        FROM flight_segments fs
        JOIN routes r ON fs.route_id = r.route_id
        WHERE fs.game_week = (SELECT game_week FROM game_state WHERE id = 1)
          AND fs.status != 'CANCELLED'
        ORDER BY fs.scheduled_dep_game_hour
        """
    )
    # sqlite3.Row has no .get(); UI code expects dict-like flights
    return [dict(row) for row in flights]


def get_tail_flight_segments_for_week(tail_number, game_week=None):
    """
    Scheduled / active segments for one aircraft in one game week (for weekly grid views).
    """
    if game_week is None:
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        game_week = int(gs["game_week"]) if gs else 1
    flights = db.fetch_all(
        """
        SELECT
            fs.*,
            COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
            COALESCE(fs.dest_iata, r.dest_iata)     AS dest_iata,
            r.origin_iata AS route_origin_iata,
            r.dest_iata   AS route_dest_iata
        FROM flight_segments fs
        JOIN routes r ON fs.route_id = r.route_id
        WHERE fs.tail_number = ?
          AND fs.game_week = ?
          AND fs.status != 'CANCELLED'
        ORDER BY fs.scheduled_dep_game_hour
        """,
        (tail_number, game_week),
    )
    return [dict(row) for row in flights]


