"""Weekly rotation templates and assignment (planning layer)."""

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

from engine.scheduling.shared import DAY_START_HOURS

def route_ids_from_airport_chain(chain: str) -> list[str]:
    """
    Convert an airport chain like ``sfo-san-sfo-sea`` into route_ids:
    ``[SFO-SAN, SAN-SFO, SFO-SEA]``.

    Used by the CLI scheduler (menu 9) to support hyphen airport-chain input.
    """
    raw = (chain or "").strip()
    if not raw:
        raise ValueError("Airport chain is empty.")

    parts = [p.strip().upper() for p in raw.split("-") if p.strip()]
    if len(parts) < 2:
        raise ValueError("Airport chain must contain at least 2 airports (e.g., sfo-san).")

    route_ids: list[str] = []
    for a, b in zip(parts, parts[1:]):
        rid = f"{a}-{b}"
        if not get_route(rid):
            raise ValueError(f"Route '{rid}' not found. Open it first under Routes menu.")
        route_ids.append(rid)
    return route_ids


def format_rotation_airport_chain(route_ids: list[str]) -> str:
    """
    Display helper: convert a list of route_ids into an airport chain like ``MIA-TPA-MIA``.
    """
    if not route_ids:
        return ""

    airports: list[str] = []
    for i, rid in enumerate(route_ids):
        r = get_route(rid)
        if r:
            o = str(r["origin_iata"]).upper()
            d = str(r["dest_iata"]).upper()
        else:
            if isinstance(rid, str) and "-" in rid:
                o, d = (p.strip().upper() for p in rid.split("-", 1))
            else:
                o, d = "???", "???"

        if i == 0:
            airports.append(o)
        airports.append(d)

    out: list[str] = []
    for a in airports:
        if not out or out[-1] != a:
            out.append(a)
    return "-".join(out)


def normalize_tail_schedule_for_week(
    tail_number: str,
    game_week: int,
    horizon_game_hour: float | None = None,
) -> dict:
    """
    Hard guardrail: enforce one-tail chronological schedule with MTT for a week window.
    - Keeps IN_AIR legs as fixed anchors.
    - For SCHEDULED/DELAYED/HOLDING legs, pushes **operational** departures forward to satisfy
      previous-arrival + MTT when needed (does not change ``baseline_*`` — published plan).
    - Rewrites ``scheduled_*_time``, ``day_of_week``, and ``game_week`` from absolute time.
    - Optionally auto-cancels future legs pushed beyond ``horizon_game_hour``.
    """
    from engine.scheduling.shared import get_financial_constant
    from engine.scheduling.time_helpers import _day_of_week_label, hhmm_from_absolute_game_hour, week_base_hours
    tail_number = tail_number.strip().upper()
    w_base = week_base_hours(int(game_week))
    w_end = w_base + 168.0
    mtt_h = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    rows = db.fetch_all(
        """
        SELECT segment_id, status, scheduled_dep_game_hour, scheduled_arr_game_hour, delay_minutes,
               COALESCE(baseline_dep_game_hour, scheduled_dep_game_hour) AS base_dep,
               COALESCE(baseline_arr_game_hour, scheduled_arr_game_hour) AS base_arr
        FROM flight_segments
        WHERE tail_number = ?
          AND scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status IN ('IN_AIR', 'SCHEDULED', 'DELAYED', 'HOLDING')
        ORDER BY scheduled_dep_game_hour ASC, segment_id ASC
        """,
        (tail_number, w_base, w_end),
    )
    moved = 0
    cancelled = 0
    anchor_arr = None
    for r in rows:
        sid = str(r["segment_id"])
        st = str(r["status"])
        dep = float(r["scheduled_dep_game_hour"])
        arr = float(r["scheduled_arr_game_hour"])
        base_dep = float(r["base_dep"])
        block = max(0.0, arr - dep)
        if st == "IN_AIR":
            gwd = int(dep // 168.0) + 1
            db.execute(
                """
                UPDATE flight_segments
                SET game_week = ?, day_of_week = ?
                WHERE segment_id = ?
                """,
                (gwd, _day_of_week_label(gwd, dep), sid),
            )
            anchor_arr = max(anchor_arr, arr) if anchor_arr is not None else arr
            continue
        min_dep = w_base if anchor_arr is None else (anchor_arr + mtt_h)
        if dep < min_dep - 1e-9:
            dep = min_dep
            arr = dep + block
            moved += 1
            new_status = "DELAYED" if st == "SCHEDULED" else st
        else:
            new_status = st
        new_delay = max(0, int(round((dep - base_dep) * 60.0)))
        if horizon_game_hour is not None and dep > float(horizon_game_hour):
            db.execute("UPDATE flight_segments SET status = 'CANCELLED' WHERE segment_id = ?", (sid,))
            cancelled += 1
            continue
        gwd = int(dep // 168.0) + 1
        db.execute(
            """
            UPDATE flight_segments
            SET scheduled_dep_game_hour = ?,
                scheduled_arr_game_hour = ?,
                scheduled_dep_time = ?,
                scheduled_arr_time = ?,
                delay_minutes = ?,
                status = ?,
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
                new_status,
                gwd,
                _day_of_week_label(gwd, dep),
                sid,
            ),
        )
        anchor_arr = arr
    return {"moved": moved, "cancelled": cancelled}


def assert_tail_schedule_accepts_new_intervals(tail_number, game_week, new_intervals, mtt_hours):
    """
    Block times (dep, arr) must not overlap existing SCHEDULED/IN_AIR flights and must leave
    at least MTT between one flight's arrival and the next departure (same tail, same week).
    """
    rows = db.fetch_all(
        """
        SELECT scheduled_dep_game_hour, scheduled_arr_game_hour
        FROM flight_segments
        WHERE tail_number = ? AND game_week = ? AND status IN ('SCHEDULED', 'IN_AIR')
        """,
        (tail_number, game_week),
    )
    intervals = [
        (float(r["scheduled_dep_game_hour"]), float(r["scheduled_arr_game_hour"]))
        for r in rows
    ]
    intervals.extend(new_intervals)
    intervals.sort(key=lambda x: x[0])
    for i in range(len(intervals) - 1):
        d1, a1 = intervals[i]
        d2, a2 = intervals[i + 1]
        if d2 < a1 - 1e-6:
            raise ValueError(
                "Schedule conflict: two flights overlap (next departure is before the previous flight lands)."
            )
        gap_h = d2 - a1
        if gap_h < mtt_hours - 1e-9:
            raise ValueError(
                f"Turnaround rule: need at least {mtt_hours * 60:.0f} min between flights; "
                f"only {(gap_h * 60):.0f} min after arrival at {a1:.2f}h before next departure at {d2:.2f}h."
            )


def _flight_number_suffix_for_tail_week(tail_number, game_week):
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM flight_segments
        WHERE tail_number = ? AND game_week = ?
        """,
        (tail_number, game_week),
    )
    return int(row["c"]) + 1 if row else 1


def merge_quick_weekly_template(tail_number, new_leg_dicts, game_week):
    """Append quick-rotation legs to weekly template; errors if a detailed template exists."""
    row = db.fetch_one("SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail_number,))
    if row:
        try:
            raw = json.loads(row["legs_json"])
        except (json.JSONDecodeError, TypeError):
            raw = []
        if isinstance(raw, dict) and raw.get("mode") in ("detailed", "detailed_chained"):
            raise ValueError(
                "This aircraft already has a detailed weekly schedule. "
                "Cancel that schedule before adding quick rotations."
            )
        if isinstance(raw, list):
            merged = raw + new_leg_dicts
        elif isinstance(raw, dict) and raw.get("mode") == "quick":
            merged = (raw.get("legs") or []) + new_leg_dicts
        else:
            merged = new_leg_dicts
    else:
        merged = new_leg_dicts
    db.execute(
        """
        INSERT OR REPLACE INTO weekly_rotations (tail_number, legs_json, created_game_week)
        VALUES (?, ?, ?)
        """,
        (tail_number, json.dumps(merged), game_week),
    )


def _initial_airport_before_first_event(tail_number, game_week, first_dep_game_hour):
    """Where the aircraft is immediately before the chronologically first event at first_dep_game_hour."""
    row = db.fetch_one(
        """
        SELECT fs.route_id
        FROM flight_segments fs
        WHERE fs.tail_number = ? AND fs.game_week = ?
          AND fs.status IN ('SCHEDULED', 'IN_AIR', 'LANDED')
          AND fs.scheduled_dep_game_hour < ?
        ORDER BY fs.scheduled_dep_game_hour DESC
        LIMIT 1
        """,
        (tail_number, game_week, first_dep_game_hour),
    )
    if row:
        rt = get_route(row["route_id"])
        if rt:
            return str(rt["dest_iata"]).upper()
    ac = get_fleet_aircraft(tail_number)
    loc = ac.get("current_airport_iata") if ac else None
    if loc:
        return str(loc).upper()
    airline = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
    if airline and airline.get("home_hub_iata"):
        return str(airline["home_hub_iata"]).upper()
    return None


def validate_position_timeline_for_new_legs(tail_number, game_week, new_legs_od):
    """
    Ensure the aircraft is at each leg's origin when that leg departs.

    new_legs_od: list of (dep_abs, arr_abs, origin_iata, dest_iata) for proposed flights only.
    Merges with existing same-week segments, sorts by departure time, simulates position.
    """
    rows = db.fetch_all(
        """
        SELECT scheduled_dep_game_hour, scheduled_arr_game_hour, route_id
        FROM flight_segments
        WHERE tail_number = ? AND game_week = ?
          AND status IN ('SCHEDULED', 'IN_AIR', 'LANDED')
        """,
        (tail_number, game_week),
    )
    events = []
    for r in rows:
        rt = get_route(r["route_id"])
        if not rt:
            continue
        events.append(
            (
                float(r["scheduled_dep_game_hour"]),
                float(r["scheduled_arr_game_hour"]),
                str(rt["origin_iata"]).upper(),
                str(rt["dest_iata"]).upper(),
            )
        )
    for dep, arr, o, d in new_legs_od:
        events.append(
            (
                float(dep),
                float(arr),
                str(o).upper(),
                str(d).upper(),
            )
        )
    events.sort(key=lambda x: x[0])
    if not events:
        return

    pos = _initial_airport_before_first_event(tail_number, game_week, events[0][0])
    for dep, arr, o, d in events:
        if pos is not None and pos != o:
            hint = ""
            if len(events) > 1:
                hint = (
                    " For the same route on multiple days you need return legs in between "
                    "(e.g. alternate ATL→LAX with LAX→ATL) so the aircraft returns to the departure city."
                )
            raise ValueError(
                f"Positioning: at hour {dep:.2f} the aircraft is at {pos}, "
                f"but this leg departs {o}→{d}.{hint}"
            )
        pos = d


def merge_detailed_weekly_template(tail_number, new_items, created_game_week):
    """Append detailed schedule lines; errors if a quick-only template exists."""
    if not new_items:
        return
    row = db.fetch_one("SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail_number,))
    if row:
        try:
            raw = json.loads(row["legs_json"])
        except (json.JSONDecodeError, TypeError):
            raw = {}
        if isinstance(raw, list) or (isinstance(raw, dict) and raw.get("mode") == "quick"):
            raise ValueError(
                "This aircraft already has a quick weekly rotation. Clear it before mixing detailed schedules."
            )
        if isinstance(raw, dict) and raw.get("mode") == "detailed_chained":
            raise ValueError(
                "This aircraft already has a chained detailed rotation. "
                "Clear it (menu 9 → 3) before adding standalone detailed lines."
            )
        if isinstance(raw, dict) and raw.get("mode") == "detailed":
            items = (raw.get("items") or []) + list(new_items)
        else:
            items = list(new_items)
        payload = {"mode": "detailed", "items": items}
    else:
        payload = {"mode": "detailed", "items": list(new_items)}
    db.execute(
        """
        INSERT OR REPLACE INTO weekly_rotations (tail_number, legs_json, created_game_week)
        VALUES (?, ?, ?)
        """,
        (tail_number, json.dumps(payload), created_game_week),
    )


def normalize_turn_minutes(route_ids, turn_minutes=None) -> list:
    """
    Per-leg ground time (minutes) after each leg arrives, before the next departs.

    `turn_minutes` may be None (use the MTT default for every leg), a single number
    (same turn on every leg), or a list aligned to route_ids. Each entry is floored at
    `mtt_minutes` — MTT is a hard operational minimum, not a suggestion — and capped at
    24 h so a typo cannot push legs into next week.
    """
    from engine.scheduling.shared import get_financial_constant
    n = len(route_ids)
    floor_min = float(get_financial_constant("mtt_minutes", 30))
    if turn_minutes is None:
        vals = [floor_min] * n
    elif isinstance(turn_minutes, (int, float)):
        vals = [float(turn_minutes)] * n
    else:
        vals = list(turn_minutes) + [floor_min] * max(0, n - len(turn_minutes))
        vals = vals[:n]
    out = []
    for i, v in enumerate(vals):
        # A blank box means "use the default", not an error.
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(floor_min)
            continue
        try:
            m = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"Leg {i + 1}: turnaround must be a number of minutes.")
        if m < floor_min - 1e-9:
            raise ValueError(
                f"Leg {i + 1}: turnaround {m:.0f} min is below the {floor_min:.0f} min "
                f"minimum turnaround time."
            )
        if m > 1440.0:
            raise ValueError(f"Leg {i + 1}: turnaround {m:.0f} min exceeds the 24 h cap.")
        out.append(m)
    return out


def assign_rotation(tail_number, route_ids, departure_times=None, flight_numbers=None, turn_minutes=None):
    """
    Assign an aircraft to a rotation (ordered list of routes).
    
    Validates:
    - Aircraft exists and is available (IDLE or SCHEDULED)
    - Routes exist
    - Each leg is within aircraft range
    - Consecutive legs form a continuous route (next origin = previous destination)
    - Aircraft meets runway requirements
    - Positioning: aircraft must be at each leg's origin given prior flights this week and fleet location
    - Minimum Turnaround Time (MTT) is respected (including vs existing flights)
    - No time overlap with existing scheduled flights
    - Total airborne time does not exceed the weekly per-aircraft cap
    
    Adds flights to the current plan without removing other non-conflicting flights.
    
    Args:
        tail_number: Aircraft tail number (e.g., 'N123AA')
        route_ids: List of route IDs in order (e.g., ['TPA-JFK', 'JFK-BOS', 'BOS-TPA'])
        departure_times: Optional list of absolute departure times in game hours
    
    Returns:
        dict: {
            'rotation_id': str,
            'tail_number': str,
            'route_ids': list,
            'segments': list of segment_ids,
            'total_distance': float,
            'estimated_duration': int (seconds; for UI duration display)
        }
    
    Raises:
        ValueError: If validation fails
    """
    from engine.scheduling.ferry import max_weekly_airborne_hours_cap, sum_airborne_hours_for_tail_week
    from engine.scheduling.shared import _assert_new_segment_airport_limits, get_financial_constant
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour, week_base_hours
    # Get aircraft
    aircraft = get_fleet_aircraft(tail_number)
    if not aircraft:
        raise ValueError(f"Aircraft '{tail_number}' not found in fleet")
    
    # Aircraft can be IDLE or SCHEDULED (multiple rotations allowed)
    if aircraft['status'] not in ('IDLE', 'SCHEDULED'):
        raise ValueError(f"Aircraft is not available for scheduling (current status: {aircraft['status']})")
    
    # Get aircraft type details
    aircraft_type = db.fetch_one(
        "SELECT * FROM aircraft_types WHERE type_id = ?",
        (aircraft['type_id'],)
    )
    
    if not aircraft_type:
        raise ValueError(f"Aircraft type '{aircraft['type_id']}' not found")
    
    # Validate routes
    if not route_ids:
        raise ValueError("At least one route required")
    
    routes = []
    for route_id in route_ids:
        route = get_route(route_id)
        if not route:
            raise ValueError(f"Route '{route_id}' not found")
        routes.append(route)
    # Gate capacity is enforced later using concurrent-gates windows (needs final dep/arr times).
    
    total_distance = sum(r["distance_nm"] for r in routes)
    for route in routes:
        if route["distance_nm"] > aircraft_type["range_nm"]:
            raise ValueError(
                f"Leg {route['route_id']} ({route['distance_nm']:.0f} nm) exceeds aircraft max range "
                f"({aircraft_type['range_nm']} nm)."
            )
    for i in range(1, len(routes)):
        prev_d = routes[i - 1]["dest_iata"].upper()
        cur_o = routes[i]["origin_iata"].upper()
        if cur_o != prev_d:
            raise ValueError(
                f"Rotation must connect airport-to-airport: after {routes[i - 1]['route_id']} "
                f"the next leg must begin at {prev_d}, not {cur_o} ({routes[i]['route_id']})."
            )
    
    # Validate runway requirements
    for route in routes:
        origin = get_airport(route['origin_iata'])
        dest = get_airport(route['dest_iata'])
        
        for airport in [origin, dest]:
            if airport['runway_length_ft'] and airport['runway_length_ft'] < aircraft_type['runway_req_ft']:
                raise ValueError(
                    f"Airport {airport['iata']} runway ({airport['runway_length_ft']} ft) "
                    f"is too short for aircraft (requires {aircraft_type['runway_req_ft']} ft)"
                )
    
    # Flight durations and MTT in game hours (continuous clock: absolute game hours in DB)
    mtt_minutes = get_financial_constant('mtt_minutes', 30)
    mtt_hours = float(mtt_minutes) / 60.0

    # Per-leg ground time. turn_hours[i] is the gap after leg i lands.
    turn_hours = [m / 60.0 for m in normalize_turn_minutes(route_ids, turn_minutes)]

    cruise_speed_kts = aircraft_type['cruise_speed_kts']
    flight_duration_hours = [r['distance_nm'] / cruise_speed_kts for r in routes]

    game_state = db.fetch_one(
        "SELECT game_week, game_hours_elapsed FROM game_state WHERE id = 1"
    )
    game_week = int(game_state['game_week']) if game_state else 1
    ghe = float(game_state['game_hours_elapsed'] or 0.0) if game_state else 0.0
    w_base = week_base_hours(game_week)

    # Calculate schedule (departure_times = absolute game hours)
    if departure_times is None:
        existing = db.fetch_all(
            """
            SELECT scheduled_arr_game_hour
            FROM flight_segments
            WHERE tail_number = ? AND game_week = ? AND status IN ('SCHEDULED', 'IN_AIR')
            ORDER BY scheduled_arr_game_hour DESC
            LIMIT 1
            """,
            (tail_number, game_week),
        )

        if existing:
            current_time = float(existing[0]['scheduled_arr_game_hour']) + mtt_hours
        else:
            current_time = max(w_base, ghe)

        departure_times = []
        for i, fh in enumerate(flight_duration_hours):
            departure_times.append(current_time)
            current_time += fh + turn_hours[i]
    else:
        if len(departure_times) != len(route_ids):
            raise ValueError("Number of departure times must match number of routes")
        departure_times = [float(x) for x in departure_times]
        for i in range(len(flight_duration_hours) - 1):
            arrival_time = departure_times[i] + flight_duration_hours[i]
            turnaround = departure_times[i + 1] - arrival_time
            if turnaround < turn_hours[i] - 1e-9:
                raise ValueError(
                    f"Insufficient turnaround between flights {i} and {i + 1} "
                    f"({turnaround * 60.0:.0f} min < {turn_hours[i] * 60.0:.0f} min requested)"
                )
    
    new_legs_od = [
        (
            departure_times[i],
            departure_times[i] + flight_duration_hours[i],
            routes[i]["origin_iata"],
            routes[i]["dest_iata"],
        )
        for i in range(len(route_ids))
    ]
    validate_position_timeline_for_new_legs(tail_number, game_week, new_legs_od)

    new_intervals = [
        (departure_times[i], departure_times[i] + flight_duration_hours[i])
        for i in range(len(route_ids))
    ]
    assert_tail_schedule_accepts_new_intervals(tail_number, game_week, new_intervals, mtt_hours)

    existing_air = sum_airborne_hours_for_tail_week(tail_number, game_week)
    total_airborne = existing_air + sum(flight_duration_hours)
    cap_air = max_weekly_airborne_hours_cap()
    if total_airborne > cap_air + 1e-6:
        raise ValueError(
            f"Weekly airborne limit is {cap_air:.0f} h per aircraft. "
            f"After this add-on the tail would be at {total_airborne:.1f} h — reduce flying or use another aircraft."
        )
    
    # Concurrent-gates capacity check (auctioned airports only) using the finalized dep/arr times.
    try:
        segs = []
        for rid, dep_abs, fh in zip(route_ids, departure_times, flight_duration_hours):
            rt = get_route(rid)
            if not rt:
                continue
            segs.append(
                {
                    "tail_number": tail_number,
                    "origin_iata": str(rt["origin_iata"]).upper(),
                    "dest_iata": str(rt["dest_iata"]).upper(),
                    "dep_abs": float(dep_abs),
                    "arr_abs": float(dep_abs) + float(fh),
                }
            )
        _assert_new_segment_airport_limits(game_week, segs)
    except Exception as e:
        raise ValueError(str(e))

    rotation_id = str(uuid.uuid4())
    segment_ids = []
    from engine.scheduling.flight_numbers import allocate_flight_numbers_for_legs

    fn_legs = []
    for i, (route_id, dep_abs, fh) in enumerate(zip(route_ids, departure_times, flight_duration_hours)):
        pref = ""
        if flight_numbers:
            try:
                pref = str(flight_numbers[i] or "").strip()
            except (IndexError, TypeError):
                pref = ""
        fn_legs.append(
            {
                "route_id": route_id,
                "intervals": [(float(dep_abs), float(dep_abs) + float(fh))],
                "preferred": pref or None,
            }
        )
    resolved_fns = allocate_flight_numbers_for_legs(fn_legs, game_week)
    turn_mins = normalize_turn_minutes(route_ids, turn_minutes)

    for i, (route_id, dep_abs, fh) in enumerate(zip(route_ids, departure_times, flight_duration_hours)):
        segment_id = f"{tail_number}-{route_id}-W{game_week}-{uuid.uuid4().hex[:12]}"
        arr_abs = dep_abs + fh
        flight_number = resolved_fns[i]
        leg_turn = int(round(turn_mins[i]))

        dep_time_str = hhmm_from_absolute_game_hour(dep_abs)
        arr_time_str = hhmm_from_absolute_game_hour(arr_abs)
        day_of_week = 'MON'
        rt = get_route(route_id)
        oi = rt["origin_iata"] if rt else None
        di = rt["dest_iata"] if rt else None
        
        db.execute("""
            INSERT INTO flight_segments (
                segment_id, game_week, day_of_week, tail_number, route_id, origin_iata, dest_iata, flight_number,
                scheduled_dep_time, scheduled_dep_game_hour,
                scheduled_arr_time, scheduled_arr_game_hour,
                baseline_dep_game_hour, baseline_arr_game_hour,
                turn_minutes,
                status, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee,
                landing_fee, gate_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
        """, (
            segment_id, game_week, day_of_week, tail_number, route_id, oi, di, flight_number,
            dep_time_str, dep_abs,
            arr_time_str, arr_abs,
            dep_abs,
            arr_abs,
            leg_turn,
        ))
        
        segment_ids.append(segment_id)
    
    # Post-insert safety check: ensure gate concurrency is still within capacity.
    # (Covers legacy DB states where airports table was unseeded at schedule-time.)
    try:
        from engine.gates import assert_player_gate_capacity_for_week

        airports = []
        for rid in route_ids:
            rt2 = get_route(rid)
            if rt2:
                airports.append(str(rt2["origin_iata"]).upper())
                airports.append(str(rt2["dest_iata"]).upper())
        assert_player_gate_capacity_for_week(game_week, airports=airports)
    except Exception as e:
        try:
            if segment_ids:
                q = ",".join(["?"] * len(segment_ids))
                db.execute(f"DELETE FROM flight_segments WHERE segment_id IN ({q})", tuple(segment_ids))
        except Exception:
            pass
        raise ValueError(str(e))

    legs_payload = []
    for i, route_id in enumerate(route_ids):
        dep_off = departure_times[i] - w_base
        legs_payload.append(
            {
                "route_id": route_id,
                "dep_offset_hours": dep_off,
                "flight_duration_hours": flight_duration_hours[i],
                "flight_number": resolved_fns[i],
                "turn_minutes": int(round(turn_mins[i])),
            }
        )
    merge_quick_weekly_template(tail_number, legs_payload, game_week)
    
    db.execute("""
        UPDATE fleet
        SET status = 'SCHEDULED'
        WHERE tail_number = ?
    """, (tail_number,))

    # Concurrent-gates model: no gate counters to bump here.

    est_seconds = sum(h * 3600.0 for h in flight_duration_hours)
    if len(flight_duration_hours) > 1:
        est_seconds += sum(turn_hours[:-1]) * 3600.0
    
    return {
        'rotation_id': rotation_id,
        'tail_number': tail_number,
        'route_ids': route_ids,
        'segments': segment_ids,
        'total_distance': total_distance,
        'estimated_duration': int(est_seconds),
    }


def _operating_days_list(days_of_week: str) -> list:
    """Parse days_of_week from create_flight_schedule / template (DAILY or JSON array string)."""
    if days_of_week == "DAILY":
        return ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    try:
        parsed = json.loads(days_of_week)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return ["MON"]


def _plan_chained_detailed_segments(
    game_week: int,
    operating_days: list,
    first_departure_hhmm: str,
    routes_ordered: list,
    flight_numbers: list,
    cruise_speed_kts: float,
    mtt_hours: float,
    turn_hours: list | None = None,
):
    """
    For each operating day, chain routes in order: first leg at first_departure local time,
    then per-leg ground time between legs (defaults to MTT when turn_hours is None).
    """
    from engine.scheduling.time_helpers import _day_of_week_label, week_base_hours
    w_base = week_base_hours(game_week)
    try:
        dep_hours, dep_mins = map(int, first_departure_hhmm.split(":"))
        if not (0 <= dep_hours < 24 and 0 <= dep_mins < 60):
            raise ValueError("Invalid time")
        hod = float(dep_hours) + float(dep_mins) / 60.0
    except Exception:
        raise ValueError(f"Invalid departure time format: {first_departure_hhmm}. Use HH:MM format.")

    order = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    days_sorted = sorted(
        [d for d in operating_days if d in DAY_START_HOURS],
        key=lambda x: order.index(x) if x in order else 99,
    )
    if not days_sorted:
        raise ValueError("No valid operating days for chained rotation.")

    fh_list = [r["distance_nm"] / cruise_speed_kts for r in routes_ordered]
    turns = list(turn_hours) if turn_hours else [float(mtt_hours)] * len(routes_ordered)
    if len(turns) < len(routes_ordered):
        turns = turns + [float(mtt_hours)] * (len(routes_ordered) - len(turns))
    planned = []
    for anchor_day in days_sorted:
        t = w_base + DAY_START_HOURS[anchor_day] + hod
        for i, route in enumerate(routes_ordered):
            dep_abs = t
            fhh = fh_list[i]
            arr_abs = dep_abs + fhh
            planned.append(
                {
                    "day": _day_of_week_label(game_week, dep_abs),
                    "dep_abs": dep_abs,
                    "arr_abs": arr_abs,
                    "route_id": route["route_id"],
                    "flight_number": flight_numbers[i],
                    "origin_iata": str(route["origin_iata"]).upper(),
                    "dest_iata": str(route["dest_iata"]).upper(),
                }
            )
            t = arr_abs + turns[i]
    return planned, fh_list, days_sorted


def _detailed_chained_chains_list_from_blob(stored: dict) -> list:
    """Normalize weekly_rotations detailed_chained JSON to a list of chain dicts (legacy or new format)."""
    if not stored or stored.get("mode") != "detailed_chained":
        return []
    if stored.get("chains"):
        return list(stored["chains"])
    if stored.get("legs"):
        return [
            {
                "days_of_week": stored.get("days_of_week", "DAILY"),
                "first_departure_time": stored.get("first_departure_time", "08:00"),
                "legs": list(stored["legs"]),
            }
        ]
    return []


def _reject_if_incompatible_weekly_template_for_chained(tail_number: str):
    """
    Chained detailed can stack with other chained blocks on the same tail, but not with quick
    or standalone detailed line templates (those use different storage shapes).
    """
    row = db.fetch_one("SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail_number,))
    if not row or not row["legs_json"]:
        return
    try:
        raw = json.loads(row["legs_json"])
    except (json.JSONDecodeError, TypeError):
        return
    if isinstance(raw, list):
        raise ValueError(
            "This aircraft already has a quick weekly rotation. Clear it (menu 9 → 3) before adding "
            "a chained detailed rotation."
        )
    if not isinstance(raw, dict):
        return
    mode = raw.get("mode")
    if mode == "quick":
        raise ValueError(
            "This aircraft already has a quick weekly rotation. Clear it (menu 9 → 3) before adding "
            "a chained detailed rotation."
        )
    if mode == "detailed":
        raise ValueError(
            "This aircraft already has standalone detailed schedule lines. Clear them (menu 9 → 3) "
            "before adding a chained detailed rotation."
        )


def create_chained_detailed_rotation(
    tail_number: str,
    route_ids: list,
    flight_numbers: list,
    days_of_week: str,
    first_departure_time: str,
    turn_minutes=None,
):
    """
    Multi-route detailed schedule: same operating days for every leg; each calendar day runs the
    full rotation in order, with the first leg of the day at first_departure_time and subsequent
    legs chained with MTT. Adds to this tail's plan (does not wipe other chained blocks or segments).

    Requires the route list to connect airport-to-airport and return to the first departure airport
    on the last leg (closed loop each day).
    """
    from engine.scheduling.ferry import max_weekly_airborne_hours_cap, sum_airborne_hours_for_tail_week
    from engine.scheduling.shared import _assert_new_segment_airport_limits, _assert_player_routes_schedulable, get_financial_constant
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour
    if len(route_ids) < 2:
        raise ValueError("Chained detailed rotation needs at least two routes.")
    if len(flight_numbers) != len(route_ids):
        raise ValueError("Flight numbers count must match routes count.")

    _reject_if_incompatible_weekly_template_for_chained(tail_number)

    aircraft = get_fleet_aircraft(tail_number)
    if not aircraft:
        raise ValueError(f"Aircraft '{tail_number}' not found in fleet")
    if aircraft["status"] not in ("IDLE", "SCHEDULED"):
        raise ValueError(f"Aircraft is not available (status: {aircraft['status']})")

    aircraft_type = db.fetch_one(
        "SELECT * FROM aircraft_types WHERE type_id = ?",
        (aircraft["type_id"],),
    )
    if not aircraft_type:
        raise ValueError(f"Aircraft type '{aircraft['type_id']}' not found")

    routes_ordered = []
    for route_id in route_ids:
        route = get_route(route_id)
        if not route:
            raise ValueError(f"Route '{route_id}' not found")
        routes_ordered.append(route)
    _assert_player_routes_schedulable(list(route_ids))

    for route in routes_ordered:
        if route["distance_nm"] > aircraft_type["range_nm"]:
            raise ValueError(
                f"Leg {route['route_id']} ({route['distance_nm']:.0f} nm) exceeds aircraft max range "
                f"({aircraft_type['range_nm']} nm)."
            )

    for i in range(1, len(routes_ordered)):
        prev_d = str(routes_ordered[i - 1]["dest_iata"]).upper()
        cur_o = str(routes_ordered[i]["origin_iata"]).upper()
        if cur_o != prev_d:
            raise ValueError(
                f"Rotation must connect: after {routes_ordered[i - 1]['route_id']} "
                f"the next leg must begin at {prev_d}, not {cur_o} ({routes_ordered[i]['route_id']})."
            )

    first_o = str(routes_ordered[0]["origin_iata"]).upper()
    last_d = str(routes_ordered[-1]["dest_iata"]).upper()
    if first_o != last_d:
        raise ValueError(
            "Chained detailed rotation must return to the starting airport each day so the next day "
            f"can begin there. First leg departs {first_o} but last leg lands at {last_d}."
        )

    for route in routes_ordered:
        origin = get_airport(route["origin_iata"])
        dest = get_airport(route["dest_iata"])
        for airport in [origin, dest]:
            if airport and airport.get("runway_length_ft") and airport["runway_length_ft"] < aircraft_type["runway_req_ft"]:
                raise ValueError(
                    f"Airport {airport['iata']} runway ({airport['runway_length_ft']} ft) "
                    f"is too short for aircraft (requires {aircraft_type['runway_req_ft']} ft)"
                )

    mtt_hours = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    cruise_speed_kts = aircraft_type["cruise_speed_kts"]

    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    game_week = int(gs["game_week"]) if gs else 1

    operating_days = _operating_days_list(days_of_week)
    turn_mins = normalize_turn_minutes(route_ids, turn_minutes)
    turn_hours = [m / 60.0 for m in turn_mins]
    prefs = [str(x or "").strip().upper() for x in flight_numbers]
    placeholder_fns = [p if p else f"__TMP{i}" for i, p in enumerate(prefs)]
    planned, fh_list, _ = _plan_chained_detailed_segments(
        game_week,
        operating_days,
        first_departure_time,
        routes_ordered,
        placeholder_fns,
        cruise_speed_kts,
        mtt_hours,
        turn_hours=turn_hours,
    )

    from engine.scheduling.flight_numbers import allocate_flight_numbers_for_legs

    n_routes = len(routes_ordered)
    fn_legs = []
    for i in range(n_routes):
        intervals = [
            (float(p["dep_abs"]), float(p["arr_abs"]))
            for idx, p in enumerate(planned)
            if idx % n_routes == i
        ]
        fn_legs.append(
            {
                "route_id": routes_ordered[i]["route_id"],
                "intervals": intervals,
                "preferred": prefs[i] or None,
            }
        )
    resolved_fns = allocate_flight_numbers_for_legs(fn_legs, game_week)
    for idx, p in enumerate(planned):
        p["flight_number"] = resolved_fns[idx % n_routes]
    flight_numbers = list(resolved_fns)

    # Concurrent-gates capacity check (auctioned airports only) using finalized dep/arr times.
    # Must run BEFORE any INSERTs so the schedule cannot "succeed" and then be fixed later.
    try:
        _assert_new_segment_airport_limits(
            int(game_week),
            [
                {
                    "tail_number": tail_number,
                    "origin_iata": str(p["origin_iata"]).upper(),
                    "dest_iata": str(p["dest_iata"]).upper(),
                    "dep_abs": float(p["dep_abs"]),
                    "arr_abs": float(p["arr_abs"]),
                }
                for p in planned
            ],
        )
    except Exception as e:
        raise ValueError(str(e))

    new_legs_od = [
        (p["dep_abs"], p["arr_abs"], p["origin_iata"], p["dest_iata"]) for p in planned
    ]
    validate_position_timeline_for_new_legs(tail_number, game_week, new_legs_od)

    planned_intervals = [(p["dep_abs"], p["arr_abs"]) for p in planned]
    assert_tail_schedule_accepts_new_intervals(tail_number, game_week, planned_intervals, mtt_hours)

    # Count how many times the chain starts per week (operating days), not how many
    # calendar weekday labels one rotation spans — same rule as quick schedule / standalone detailed.
    n_rotations = len(operating_days) or 1
    additional_air = sum(fh_list) * n_rotations
    cap_air = max_weekly_airborne_hours_cap()
    existing_air = sum_airborne_hours_for_tail_week(tail_number, game_week)
    if existing_air + additional_air > cap_air + 1e-6:
        raise ValueError(
            f"Weekly airborne limit is {cap_air:.0f} h per aircraft. "
            f"This tail already has about {existing_air:.1f} h planned; "
            f"adding this chain (~{additional_air:.1f} h) would exceed the limit."
        )

    schedule_ids = []
    for i, route in enumerate(routes_ordered):
        sid = str(uuid.uuid4())
        schedule_ids.append(sid)
        dep_display = first_departure_time if i == 0 else "__CHAIN__"
        db.execute(
            """
            INSERT INTO flight_schedules (
                schedule_id, tail_number, route_id, flight_number,
                days_of_week, departure_time, active, created_week
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                sid,
                tail_number,
                route["route_id"],
                flight_numbers[i],
                days_of_week,
                dep_display,
                game_week,
            ),
        )

    for idx, p in enumerate(planned):
        route_idx = idx % len(routes_ordered)
        schedule_id = schedule_ids[route_idx]
        rt = get_route(p["route_id"])
        oi = rt["origin_iata"] if rt else None
        di = rt["dest_iata"] if rt else None
        dep_time_str = hhmm_from_absolute_game_hour(p["dep_abs"])
        arr_time_str = hhmm_from_absolute_game_hour(p["arr_abs"])
        segment_id = f"{tail_number}-{p['route_id']}-W{game_week}-{p['day']}-{uuid.uuid4().hex[:8]}"

        leg_turn = int(round(turn_mins[route_idx]))

        db.execute(
            """
            INSERT INTO flight_segments (
                segment_id, schedule_id, game_week, day_of_week,
                tail_number, route_id, origin_iata, dest_iata, flight_number,
                scheduled_dep_time, scheduled_dep_game_hour,
                scheduled_arr_time, scheduled_arr_game_hour,
                baseline_dep_game_hour, baseline_arr_game_hour,
                turn_minutes,
                status, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee,
                landing_fee, gate_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (
                segment_id,
                schedule_id,
                game_week,
                p["day"],
                tail_number,
                p["route_id"],
                oi,
                di,
                p["flight_number"],
                dep_time_str,
                p["dep_abs"],
                arr_time_str,
                p["arr_abs"],
                p["dep_abs"],
                p["arr_abs"],
                leg_turn,
            ),
        )

    rot_row = db.fetch_one("SELECT legs_json FROM weekly_rotations WHERE tail_number = ?", (tail_number,))
    existing_chains = []
    if rot_row and rot_row["legs_json"]:
        try:
            ex = json.loads(rot_row["legs_json"])
            if isinstance(ex, dict):
                existing_chains = _detailed_chained_chains_list_from_blob(ex)
        except (json.JSONDecodeError, TypeError):
            existing_chains = []

    new_chain = {
        "days_of_week": days_of_week,
        "first_departure_time": first_departure_time,
        "legs": [
            {
                "route_id": r["route_id"],
                "flight_number": flight_numbers[i],
                "turn_minutes": turn_mins[i],
            }
            for i, r in enumerate(routes_ordered)
        ],
    }
    payload = {
        "mode": "detailed_chained",
        "chains": existing_chains + [new_chain],
    }
    db.execute(
        """
        INSERT OR REPLACE INTO weekly_rotations (tail_number, legs_json, created_game_week)
        VALUES (?, ?, ?)
        """,
        (tail_number, json.dumps(payload), game_week),
    )

    db.execute(
        """
        UPDATE fleet
        SET status = 'SCHEDULED'
        WHERE tail_number = ?
        """,
        (tail_number,),
    )

    # Gate scheduling counters are handled at scheduling-time inserts (assign_rotation / create_flight_schedule).

    return {
        "tail_number": tail_number,
        "route_ids": list(route_ids),
        "segments_planned": len(planned),
        "template": payload,
    }


def create_flight_schedule(tail_number, route_id, flight_number, days_of_week, departure_time):
    """
    Create a detailed flight schedule with custom flight number, days, and departure time.
    
    This creates entries in both flight_schedules (recurring schedule template) 
    and flight_segments (actual flights for this week).
    
    Args:
        tail_number: Aircraft tail number
        route_id: Route ID (e.g., 'ATL-LAX')
        flight_number: Custom flight number (e.g., 'DAL101')
        days_of_week: Either "DAILY" or JSON array like '["MON","WED","FRI"]'
        departure_time: Departure time in HH:MM format (e.g., '08:00')
    
    Returns:
        dict: schedule_id, template_item (for weekly respawn)
    
    Raises:
        ValueError: If validation fails
    """
    from engine.scheduling.ferry import max_weekly_airborne_hours_cap, sum_airborne_hours_for_tail_week
    from engine.scheduling.shared import _assert_new_segment_airport_limits, get_financial_constant
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour, week_base_hours
    # Validate aircraft
    aircraft = get_fleet_aircraft(tail_number)
    if not aircraft:
        raise ValueError(f"Aircraft '{tail_number}' not found in fleet")
    
    if aircraft['status'] not in ('IDLE', 'SCHEDULED'):
        raise ValueError(f"Aircraft is not available (status: {aircraft['status']})")
    
    # Validate route
    route = get_route(route_id)
    if not route:
        raise ValueError(f"Route '{route_id}' not found")
    # Gate capacity is enforced after we compute concrete dep/arr times (concurrent-gates model).

    # Validate aircraft range and runway
    aircraft_type = db.fetch_one(
        "SELECT * FROM aircraft_types WHERE type_id = ?",
        (aircraft['type_id'],)
    )
    
    if route['distance_nm'] > aircraft_type['range_nm']:
        raise ValueError(
            f"Route distance ({route['distance_nm']} nm) exceeds aircraft range ({aircraft_type['range_nm']} nm)"
        )
    
    try:
        dep_hours, dep_mins = map(int, departure_time.split(':'))
        if not (0 <= dep_hours < 24 and 0 <= dep_mins < 60):
            raise ValueError("Invalid time")
        hod = float(dep_hours) + float(dep_mins) / 60.0
    except Exception:
        raise ValueError(f"Invalid departure time format: {departure_time}. Use HH:MM format.")
    
    cruise_speed_kts = aircraft_type['cruise_speed_kts']
    flight_hours = route['distance_nm'] / cruise_speed_kts
    
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    game_week = int(gs['game_week']) if gs else 1
    w_base = week_base_hours(game_week)
    mtt_hours = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    
    if days_of_week == "DAILY":
        operating_days = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
    else:
        try:
            operating_days = json.loads(days_of_week)
        except Exception:
            operating_days = ['MON']
    
    planned = []
    for day in operating_days:
        if day not in DAY_START_HOURS:
            continue
        hours_into_week = DAY_START_HOURS[day] + hod
        dep_abs = w_base + hours_into_week
        arr_abs = dep_abs + flight_hours
        planned.append((day, dep_abs, arr_abs))
    
    n_segments_this_line = len(planned)
    if n_segments_this_line == 0:
        raise ValueError("No valid operating days for this schedule line.")
    new_legs_od = [
        (dep_abs, arr_abs, route["origin_iata"], route["dest_iata"])
        for day, dep_abs, arr_abs in planned
    ]
    if len(new_legs_od) > 1:
        origins = {str(o).upper() for _, _, o, _ in new_legs_od}
        dests = {str(d).upper() for _, _, _, d in new_legs_od}
        if len(origins) == 1 and len(dests) == 1:
            o0 = next(iter(origins))
            d0 = next(iter(dests))
            if o0 != d0:
                raise ValueError(
                    "This line repeats the same one-way flight on multiple days (e.g. daily ATL→LAX). "
                    "After the first day the aircraft is not at the departure city. "
                    "Fix: use Quick Schedule with a full rotation including the return "
                    "(e.g. ATL-LAX,LAX-ATL), or use Detailed with only one operating day per week for this "
                    "direction until you add return legs on other days."
                )
    validate_position_timeline_for_new_legs(tail_number, game_week, new_legs_od)

    additional_airborne = flight_hours * n_segments_this_line
    cap_air = max_weekly_airborne_hours_cap()
    existing_air = sum_airborne_hours_for_tail_week(tail_number, game_week)
    if existing_air + additional_airborne > cap_air + 1e-6:
        raise ValueError(
            f"Weekly airborne limit is {cap_air:.0f} h per aircraft. "
            f"This tail already has {existing_air:.1f} h this week; "
            f"adding {additional_airborne:.1f} h would exceed the limit."
        )
    
    planned_intervals = [(p[1], p[2]) for p in planned]
    assert_tail_schedule_accepts_new_intervals(tail_number, game_week, planned_intervals, mtt_hours)

    from engine.scheduling.flight_numbers import allocate_flight_number, normalize_flight_number

    intervals = [(float(d), float(a)) for _day, d, a in planned]
    flight_number = allocate_flight_number(
        route_id,
        intervals,
        game_week,
        preferred=normalize_flight_number(flight_number) or None,
    )

    # Concurrent-gates capacity check (auctioned airports only).
    try:
        _assert_new_segment_airport_limits(
            game_week,
            [
                {
                    "origin_iata": str(route["origin_iata"]).upper(),
                    "dest_iata": str(route["dest_iata"]).upper(),
                    "dep_abs": dep_abs,
                    "arr_abs": arr_abs,
                }
                for _day, dep_abs, arr_abs in planned
            ],
        )
    except Exception as e:
        raise ValueError(str(e))
    
    schedule_id = str(uuid.uuid4())
    inserted_segment_ids: list[str] = []

    db.execute(
        """
        INSERT INTO flight_schedules (
            schedule_id, tail_number, route_id, flight_number,
            days_of_week, departure_time, active, created_week
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            schedule_id,
            tail_number,
            route_id,
            flight_number,
            days_of_week,
            departure_time,
            game_week,
        ),
    )

    mtt_min = int(round(float(get_financial_constant("mtt_minutes", 30))))
    oi = str(route["origin_iata"]).upper()
    di = str(route["dest_iata"]).upper()

    for day, dep_abs, arr_abs in planned:
        dep_time_str = hhmm_from_absolute_game_hour(dep_abs)
        arr_time_str = hhmm_from_absolute_game_hour(arr_abs)
        segment_id = f"{tail_number}-{route_id}-W{game_week}-{day}-{uuid.uuid4().hex[:8]}"
        
        db.execute(
            """
            INSERT INTO flight_segments (
                segment_id, schedule_id, game_week, day_of_week,
                tail_number, route_id, origin_iata, dest_iata, flight_number,
                scheduled_dep_time, scheduled_dep_game_hour,
                scheduled_arr_time, scheduled_arr_game_hour,
                baseline_dep_game_hour, baseline_arr_game_hour,
                turn_minutes,
                status, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee,
                landing_fee, gate_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (
                segment_id,
                schedule_id,
                game_week,
                day,
                tail_number,
                route_id,
                oi,
                di,
                flight_number,
                dep_time_str,
                dep_abs,
                arr_time_str,
                arr_abs,
                dep_abs,
                arr_abs,
                mtt_min,
            ),
        )
        inserted_segment_ids.append(segment_id)

    # Post-insert safety check: ensure we didn't overbook concurrent gates due to
    # multiple schedules being created back-to-back.
    try:
        from engine.gates import assert_player_gate_capacity_for_week

        assert_player_gate_capacity_for_week(
            game_week,
            airports=[str(route["origin_iata"]).upper(), str(route["dest_iata"]).upper()],
        )
    except Exception as e:
        # Roll back this schedule line so scheduling never "succeeds" when gates aren't available.
        try:
            if inserted_segment_ids:
                q = ",".join(["?"] * len(inserted_segment_ids))
                db.execute(f"DELETE FROM flight_segments WHERE segment_id IN ({q})", tuple(inserted_segment_ids))
        except Exception:
            pass
        try:
            db.execute("DELETE FROM flight_schedules WHERE schedule_id = ?", (schedule_id,))
        except Exception:
            pass
        raise ValueError(str(e))
    
    # Update aircraft status to SCHEDULED
    db.execute("""
        UPDATE fleet
        SET status = 'SCHEDULED'
        WHERE tail_number = ?
    """, (tail_number,))

    # Concurrent-gates model: no gate counters to bump here.

    template_item = {
        "route_id": route_id,
        "flight_number": flight_number,
        "days_of_week": days_of_week,
        "departure_time": departure_time,
    }
    return {"schedule_id": schedule_id, "template_item": template_item}


