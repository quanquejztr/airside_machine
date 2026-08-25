"""
Phase 11+ — Live-clock AI flight operations.

AI flights are stored in ai_flight_segments and depart/arrive through GameClock callbacks,
similar to player flight_segments.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from db import db
from engine.scheduling import week_base_hours
from engine.routes import get_route


def _mtt_hours() -> float:
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'mtt_minutes'")
    try:
        mins = float(row["value"]) if row else 30.0
    except (TypeError, ValueError):
        mins = 30.0
    return mins / 60.0


def _stable_pair_jitter_hours(route_pair_id: str) -> float:
    n = 0
    for ch in str(route_pair_id).upper():
        n = (n * 33 + ord(ch)) & 0xFFFFFFFF
    return float(n % 16) * 0.25


def bank_dep_hours(
    freq: int, strategy: str, w0: float, *, pair_id: str | None = None
) -> list[float]:
    """Return `freq` outbound departure hours (absolute game-hours) for the week."""
    n = max(1, int(freq))
    strat = str(strategy or "HUBSPOKE").upper()
    base = float(w0)
    jitter = _stable_pair_jitter_hours(pair_id) if pair_id else 0.0
    if strat == "HUBSPOKE":
        slots: list[float] = []
        morning = base + 7.0 + jitter
        afternoon = base + 15.0 + jitter
        for i in range(n):
            bank = morning if i % 2 == 0 else afternoon
            slots.append(bank + (i // 2) * 24.0)
        return slots
    if strat == "PREMIUM":
        return [base + 9.0 + jitter + float(i) * (168.0 / float(n)) for i in range(n)]
    return [base + jitter + (float(i) / float(n)) * 168.0 for i in range(n)]


def _resolve_aircraft_type_id(competitor_id: str, route_id: str) -> Optional[str]:
    # Prefer the competitor_routes assigned type if present (set by AI engine later).
    r = db.fetch_one(
        """
        SELECT aircraft_type_id
        FROM competitor_routes
        WHERE competitor_id = ?
          AND (outbound_route_id = ? OR inbound_route_id = ?)
        LIMIT 1
        """,
        (competitor_id, route_id, route_id),
    )
    if r and r["aircraft_type_id"]:
        return str(r["aircraft_type_id"])
    # Fallback: any NARROW with enough range for this route.
    rt = get_route(route_id)
    if not rt:
        return None
    dist = float(rt["distance_nm"] or 0.0)
    row = db.fetch_one(
        """
        SELECT type_id
        FROM aircraft_types
        WHERE category IN ('NARROW','WIDE','REGIONAL_JET','TURBOPROP')
          AND range_nm >= ?
        ORDER BY CASE category
            WHEN 'NARROW' THEN 1
            WHEN 'WIDE' THEN 2
            WHEN 'REGIONAL_JET' THEN 3
            WHEN 'TURBOPROP' THEN 4
            ELSE 9
        END
        LIMIT 1
        """,
        (dist,),
    )
    return str(row["type_id"]) if row else None


def _cruise_speed_kts(type_id: str) -> float:
    row = db.fetch_one("SELECT cruise_speed_kts FROM aircraft_types WHERE type_id = ?", (type_id,))
    try:
        return float(row["cruise_speed_kts"]) if row else 450.0
    except (TypeError, ValueError):
        return 450.0


def _flight_duration_hours(route_id: str, type_id: str) -> float:
    rt = get_route(route_id)
    if not rt:
        return 2.0
    dist = float(rt["distance_nm"] or 0.0)
    spd = max(120.0, _cruise_speed_kts(type_id))
    return float(dist / spd)


def spawn_ai_segments_for_week(target_game_week: int) -> Dict[str, Any]:
    """
    Create ai_flight_segments for all ACTIVE competitor_routes for the target week.
    Live-clock model: writes one row per actual flight leg (outbound + inbound cycles).

    This is run at week rollover (after settlement) so the next week is populated.
    """
    gw = int(target_game_week)
    if gw < 1:
        return {"inserted": 0}

    from engine.slots import (
        find_available_cycle,
        grandfather_historic_slot_holdings,
        holder_can_add_movements,
        is_slot_controlled,
        rebuild_slot_usages_for_week,
        seed_slot_controlled_airports,
    )
    from engine.ai_gates import cycle_fits_ai_gates
    from engine.ai_log import append_ai_narrative, push_ai_news

    seed_slot_controlled_airports()
    grandfather_historic_slot_holdings(gw)

    # Replace this week’s planned AI ops (idempotent, deterministic enough).
    db.execute("DELETE FROM ai_flight_segments WHERE game_week = ?", (gw,))

    w0 = week_base_hours(gw)
    mtt = _mtt_hours()
    inserted = 0

    rows = db.fetch_all(
        """
        SELECT cr.*, c.callsign, c.strategy
        FROM competitor_routes cr
        JOIN competitors c ON c.competitor_id = cr.competitor_id
        WHERE COALESCE(cr.status, 'ACTIVE') = 'ACTIVE'
        ORDER BY cr.competitor_id, cr.route_pair_id
        """
    )
    for r in rows:
        cid = str(r["competitor_id"])
        callsign = str(r["callsign"] or "AI")
        out_id = str(r["outbound_route_id"])
        in_id = str(r["inbound_route_id"])
        freq = max(1, int(r["frequency_per_week"] or 1))
        type_id = str(r["aircraft_type_id"] or _resolve_aircraft_type_id(cid, out_id) or "A320")

        out_rt = get_route(out_id)
        in_rt = get_route(in_id)
        if not out_rt or not in_rt:
            continue
        out_dur = _flight_duration_hours(out_id, type_id)
        in_dur = _flight_duration_hours(in_id, type_id)
        strat = str(r["strategy"] or "HUBSPOKE")
        slots = bank_dep_hours(freq, strat, w0, pair_id=str(r["route_pair_id"]))

        for j, dep_pref in enumerate(slots):
            picked = find_available_cycle(
                origin_iata=str(out_rt["origin_iata"]),
                dest_iata=str(out_rt["dest_iata"]),
                dep_out=float(dep_pref),
                out_dur=out_dur,
                mtt=mtt,
                in_dur=in_dur,
                game_week=gw,
            )
            if not picked:
                ch = int(float(dep_pref) % 168.0)
                tok = f"SLOT_MISS:{out_rt['dest_iata']}:{ch}"
                append_ai_narrative(cid, tok)
                push_ai_news(f"⏱ {callsign} skipped a {out_id} cycle — no runway slot")
                continue
            dep_out, arr_out, dep_in, arr_in = picked
            oi = str(out_rt["origin_iata"])
            di = str(out_rt["dest_iata"])
            extra_o = (2 if is_slot_controlled(oi) else 0)
            extra_d = (2 if is_slot_controlled(di) else 0)
            if extra_o and not holder_can_add_movements(oi, cid, gw, extra_o):
                append_ai_narrative(cid, f"SLOT_QUOTA:{oi}")
                continue
            if extra_d and not holder_can_add_movements(di, cid, gw, extra_d):
                append_ai_narrative(cid, f"SLOT_QUOTA:{di}")
                continue
            if not cycle_fits_ai_gates(cid, oi, di, dep_out, arr_out, dep_in, arr_in, gw, mtt):
                append_ai_narrative(cid, f"GATE_PEAK:{oi}/{di}")
                continue

            fn_base = f"{callsign}{(j + 1):03d}"
            # Outbound
            db.execute(
                """
                INSERT INTO ai_flight_segments (
                    segment_id, competitor_id, route_id, game_week, flight_number,
                    origin_iata, dest_iata,
                    scheduled_dep_game_hour, scheduled_arr_game_hour,
                    actual_dep_game_hour, actual_arr_game_hour,
                    frequency, status,
                    simulated_pax_leisure, simulated_pax_business, simulated_revenue, simulated_load_factor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'SCHEDULED', 0, 0, 0.0, 0.0)
                """,
                (
                    str(uuid.uuid4()),
                    cid,
                    out_id,
                    gw,
                    fn_base,
                    str(out_rt["origin_iata"]),
                    str(out_rt["dest_iata"]),
                    dep_out,
                    arr_out,
                    freq,
                ),
            )
            inserted += 1

            # Inbound (return)
            db.execute(
                """
                INSERT INTO ai_flight_segments (
                    segment_id, competitor_id, route_id, game_week, flight_number,
                    origin_iata, dest_iata,
                    scheduled_dep_game_hour, scheduled_arr_game_hour,
                    actual_dep_game_hour, actual_arr_game_hour,
                    frequency, status,
                    simulated_pax_leisure, simulated_pax_business, simulated_revenue, simulated_load_factor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'SCHEDULED', 0, 0, 0.0, 0.0)
                """,
                (
                    str(uuid.uuid4()),
                    cid,
                    in_id,
                    gw,
                    fn_base + "R",
                    str(in_rt["origin_iata"]),
                    str(in_rt["dest_iata"]),
                    dep_in,
                    arr_in,
                    freq,
                ),
            )
            inserted += 1

    rebuild_slot_usages_for_week(gw)
    return {"inserted": inserted}


def ai_on_departure(segment_id: str) -> None:
    seg = db.fetch_one("SELECT * FROM ai_flight_segments WHERE segment_id = ?", (segment_id,))
    if not seg:
        return
    if str(seg["status"]) != "SCHEDULED":
        return
    db.execute(
        "UPDATE ai_flight_segments SET status = 'IN_AIR', actual_dep_game_hour = ? WHERE segment_id = ?",
        (float(seg["scheduled_dep_game_hour"]), segment_id),
    )
    try:
        from engine.news_feed import push_news

        push_news(
            f"✈️  {seg['flight_number']} ({seg['competitor_id']}) departed {seg['origin_iata']} → {seg['dest_iata']}"
        )
    except Exception:
        pass


def ai_on_arrival(segment_id: str) -> None:
    seg = db.fetch_one("SELECT * FROM ai_flight_segments WHERE segment_id = ?", (segment_id,))
    if not seg:
        return
    if str(seg["status"]) != "IN_AIR":
        return
    cid = str(seg["competitor_id"])
    route_id = str(seg["route_id"])
    pax_l, pax_b, revenue, lf, net = _simulate_ai_leg_pnl(cid, route_id, seg)
    db.execute(
        """
        UPDATE ai_flight_segments
        SET status = 'COMPLETED',
            actual_arr_game_hour = ?,
            simulated_pax_leisure = ?,
            simulated_pax_business = ?,
            simulated_revenue = ?,
            simulated_load_factor = ?,
            simulated_net = ?
        WHERE segment_id = ?
        """,
        (
            float(seg["scheduled_arr_game_hour"]),
            int(pax_l),
            int(pax_b),
            float(revenue),
            float(lf),
            float(net),
            segment_id,
        ),
    )
    if net:
        db.execute("UPDATE competitors SET cash = cash + ? WHERE competitor_id = ?", (float(net), cid))
    try:
        from engine.news_feed import push_news

        push_news(
            f"🛬 {seg['flight_number']} ({cid}) arrived {seg['dest_iata']} (${net:,.0f} net)"
        )
    except Exception:
        pass


def _simulate_ai_leg_pnl(competitor_id: str, route_id: str, seg) -> tuple[int, int, float, float, float]:
    """Variable-cost arrival P&L from the shared book (lease is charged weekly, not per landing)."""
    from engine.ai_economics import one_leg_pnl

    cid = str(competitor_id)
    rid = str(route_id)
    cr = db.fetch_one(
        """
        SELECT fare_leisure, fare_business, aircraft_type_id, frequency_per_week
        FROM competitor_routes
        WHERE competitor_id = ?
          AND (outbound_route_id = ? OR inbound_route_id = ?)
        LIMIT 1
        """,
        (cid, rid, rid),
    )
    if not cr:
        return (0, 0, 0.0, 0.0, 0.0)
    book = one_leg_pnl(
        cid,
        rid,
        float(cr["fare_leisure"] or 0),
        float(cr["fare_business"] or 0),
        max(1, int(cr["frequency_per_week"] or 1)),
        str(cr["aircraft_type_id"] or "A320"),
    )
    return (
        int(book["pax_leisure"]),
        int(book["pax_business"]),
        float(book["revenue"]),
        float(book["lf"]),
        float(book["net"]),
    )

