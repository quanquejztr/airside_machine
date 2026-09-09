"""Ferry reposition, cancel rotation, and utilization helpers."""

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

def max_weekly_airborne_hours_cap() -> float:
    """Max scheduled airborne hours per aircraft per game week (crew / ops limit)."""
    from engine.scheduling.shared import get_financial_constant
    return float(get_financial_constant("max_weekly_airborne_hours_per_tail", 168.0))


def sum_airborne_hours_for_tail_week(tail_number: str, game_week: int) -> float:
    """Total airborne (block) hours already on the books for this tail and week."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(scheduled_arr_game_hour - scheduled_dep_game_hour), 0) AS h
        FROM flight_segments
        WHERE tail_number = ?
          AND game_week = ?
          AND status IN ('SCHEDULED', 'IN_AIR', 'LANDED')
        """,
        (tail_number, game_week),
    )
    return float(row["h"]) if row else 0.0


def get_tail_weekly_utilization(tail_number, game_week=None):
    """
    Planned / completed airborne hours vs weekly cap for fleet UI.
    """
    if game_week is None:
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        game_week = int(gs["game_week"]) if gs else 1
    cap = max_weekly_airborne_hours_cap()
    used = sum_airborne_hours_for_tail_week(tail_number, game_week)
    pct = (used / cap * 100.0) if cap > 0 else 0.0
    at_limit = used >= cap
    return {
        "tail_number": tail_number,
        "game_week": game_week,
        "airborne_hours": used,
        "cap_hours": cap,
        "utilization_pct": min(pct, 999.0),
        "at_or_over_limit": at_limit,
    }


def route_ops_summaries_for_week(game_week: int | None = None) -> dict[str, dict]:
    """
    Per-route ops snapshot for the current (or given) game week.

    Returns route_id → {
      flights: [{flight_number, type_id, tail_number}],
      frequency: int (non-cancelled segments),
      flight_hours: float | None (avg block time from scheduled segments),
      schedule: str (human summary),
    }
    """
    if game_week is None:
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        game_week = int(gs["game_week"]) if gs else 1
    gw = int(game_week)
    rows = db.fetch_all(
        """
        SELECT fs.route_id, fs.flight_number, fs.tail_number,
               fs.scheduled_dep_game_hour, fs.scheduled_arr_game_hour,
               f.type_id
        FROM flight_segments fs
        JOIN fleet f ON f.tail_number = fs.tail_number
        WHERE fs.game_week = ? AND fs.status != 'CANCELLED'
        ORDER BY fs.route_id, fs.scheduled_dep_game_hour ASC, fs.flight_number
        """,
        (gw,),
    )
    out: dict[str, dict] = {}
    for r in rows or []:
        rid = str(r["route_id"] or "").upper()
        if not rid:
            continue
        bucket = out.setdefault(
            rid,
            {"flights": [], "frequency": 0, "flight_hours": None, "_hours": [], "_seen": set()},
        )
        bucket["frequency"] += 1
        dep = float(r["scheduled_dep_game_hour"] or 0.0)
        arr = float(r["scheduled_arr_game_hour"] or 0.0)
        if arr > dep:
            bucket["_hours"].append(arr - dep)
        key = (str(r["flight_number"]), str(r["type_id"]))
        if key not in bucket["_seen"]:
            bucket["_seen"].add(key)
            bucket["flights"].append(
                {
                    "flight_number": str(r["flight_number"] or ""),
                    "type_id": str(r["type_id"] or ""),
                    "tail_number": str(r["tail_number"] or ""),
                }
            )
    for bucket in out.values():
        hours = bucket.pop("_hours", [])
        bucket.pop("_seen", None)
        bucket["flight_hours"] = (sum(hours) / len(hours)) if hours else None
        parts = [f"{f['flight_number']} on {f['type_id']}" for f in bucket["flights"] if f["flight_number"]]
        bucket["schedule"] = "; ".join(parts) if parts else None
    return out


def route_current_schedule_summary(route_id: str) -> Optional[str]:
    """
    Distinct flight_number + aircraft type_id for this route in the current game week
    (non-cancelled segments), for route list / detail panels.
    """
    rid = route_id.upper().strip()
    info = route_ops_summaries_for_week().get(rid)
    return info.get("schedule") if info else None


def _ensure_catalog_route(origin_iata: str, dest_iata: str):
    """
    Make sure a `routes` catalog row exists for a directional pair, without claiming it as a
    player route. Used for positioning legs: a ferry may fly a pair the player never opened.
    """
    o = str(origin_iata).upper().strip()
    d = str(dest_iata).upper().strip()
    if not o or not d or o == d:
        return None
    rid = f"{o}-{d}"
    existing = get_route(rid)
    if existing:
        return existing
    ao = get_airport(o)
    ad = get_airport(d)
    if not ao or not ad:
        return None
    from engine.cabin import default_premium_first_ticket_fares
    from engine.route_demand import compute_base_demand

    dist = haversine_distance(float(ao["lat"]), float(ao["lon"]), float(ad["lat"]), float(ad["lon"]))
    demand_info = compute_base_demand(dist, dict(ao), dict(ad))
    bd_b = int(demand_info["base_demand_business"])
    bd_l = int(demand_info["base_demand_leisure"])
    demand_source = str(demand_info.get("demand_source") or "LEGACY")
    pb = round(dist * 0.20, 2)
    pl = round(dist * 0.10, 2)
    pp, pf = default_premium_first_ticket_fares(float(pl), float(pb))
    db.execute(
        """
        INSERT OR IGNORE INTO routes (
            route_id, origin_iata, dest_iata, distance_nm,
            base_demand_business, base_demand_leisure,
            price_business, price_leisure, price_premium_economy, price_first,
            competitor_share_this_week, is_active, demand_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 1, ?)
        """,
        (rid, o, d, dist, bd_b, bd_l, float(pb), float(pl), float(pp), float(pf), demand_source),
    )
    return get_route(rid)


def tail_position_and_free_hour(tail_number: str) -> tuple:
    """
    Where a tail ends up and the earliest hour it can depart again.

    Looks at the last leg that is still going to happen or already happened (IN_AIR / LANDED /
    DIVERTED), so a cleared plan still respects a flight currently in the air.
    Returns (iata_or_None, earliest_dep_hour).
    """
    from engine.scheduling.shared import get_financial_constant
    tail = str(tail_number).strip().upper()
    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    now = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0
    mtt_h = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    row = db.fetch_one(
        """
        SELECT COALESCE(fs.divert_airport_iata, fs.dest_iata, r.dest_iata) AS pos,
               fs.scheduled_arr_game_hour AS arr
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.tail_number = ?
          AND fs.status IN ('IN_AIR', 'LANDED', 'DIVERTED')
        ORDER BY fs.scheduled_arr_game_hour DESC
        LIMIT 1
        """,
        (tail,),
    )
    if row and row["pos"]:
        return (str(row["pos"]).upper(), max(now, float(row["arr"] or 0.0)) + mtt_h)
    ac = get_fleet_aircraft(tail)
    loc = (ac.get("current_airport_iata") if ac else None) or None
    return (str(loc).upper() if loc else None, now + mtt_h)


def schedule_ferry_reposition(
    tail_number: str,
    dest_iata: str,
    day_of_week: str = None,
    departure_time: str = None,
    *,
    dep_game_hour: float = None,
    game_week: int = None,
) -> Dict[str, Any]:
    """
    Schedule a one-off ferry leg (is_ferry=1) to reposition an aircraft.

    Either pass day_of_week (MON..SUN) + departure_time (HH:MM) for this game week,
    or dep_game_hour for automatic timing (e.g. immediately after the tail is free).
    """
    from engine.scheduling.rotation import assert_tail_schedule_accepts_new_intervals, validate_position_timeline_for_new_legs
    from engine.scheduling.segments import remove_superseded_scheduled_segments_for_tail
    from engine.scheduling.shared import _assert_new_segment_airport_limits, get_financial_constant
    from engine.scheduling.time_helpers import _day_of_week_label, hhmm_from_absolute_game_hour, week_base_hours
    tail = str(tail_number).strip().upper()
    dest = str(dest_iata or "").upper().strip()
    if not dest:
        raise ValueError("Pick a destination airport.")

    ac = get_fleet_aircraft(tail)
    if not ac:
        raise ValueError(f"Aircraft '{tail}' not found in fleet.")
    if str(ac.get("status") or "") == "AOG":
        raise ValueError(f"{tail} is AOG and cannot be repositioned until repaired.")

    pos, free_hour = tail_position_and_free_hour(tail)
    if not pos:
        raise ValueError(f"Cannot determine {tail}'s current location.")
    if pos == dest:
        raise ValueError(f"{tail} is already at {dest}.")

    gs = db.fetch_one("SELECT game_week, game_hours_elapsed FROM game_state WHERE id = 1")
    if game_week is None:
        game_week = int(gs["game_week"] or 1) if gs else 1
    else:
        game_week = int(game_week)
    try:
        from engine.clock import get_display_game_hours

        now = float(get_display_game_hours())
    except Exception:
        now = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0

    if dep_game_hour is not None:
        dep_abs = float(dep_game_hour)
    else:
        day = str(day_of_week or "").upper().strip()
        if day not in DAY_START_HOURS:
            raise ValueError("Day must be MON, TUE, WED, THU, FRI, SAT, or SUN.")
        dep_txt = str(departure_time or "").strip()
        if dep_txt.count(":") == 2:
            dep_txt = dep_txt.rsplit(":", 1)[0]
        try:
            dep_hours, dep_mins = map(int, dep_txt.split(":"))
            if not (0 <= dep_hours < 24 and 0 <= dep_mins < 60):
                raise ValueError("Invalid time")
            hod = float(dep_hours) + float(dep_mins) / 60.0
        except Exception:
            raise ValueError(f"Invalid departure time: {departure_time}. Use HH:MM format.")
        w_base = week_base_hours(game_week)
        dep_abs = w_base + DAY_START_HOURS[day] + hod

    if dep_abs + 1e-6 < now:
        raise ValueError(
            f"Departure is in the past. Earliest available: "
            f"{_day_of_week_label(game_week, free_hour)} {hhmm_from_absolute_game_hour(free_hour)}."
        )
    if dep_abs + 1e-6 < free_hour:
        raise ValueError(
            f"{tail} cannot depart before "
            f"{_day_of_week_label(game_week, free_hour)} {hhmm_from_absolute_game_hour(free_hour)} "
            f"(still flying or within minimum turnaround)."
        )

    rt = _ensure_catalog_route(pos, dest)
    if not rt:
        raise ValueError(f"No route possible from {pos} to {dest}.")

    atype = db.fetch_one("SELECT * FROM aircraft_types WHERE type_id = ?", (ac["type_id"],))
    if not atype:
        raise ValueError(f"Aircraft type '{ac['type_id']}' not found.")
    if float(rt["distance_nm"] or 0.0) > float(atype["range_nm"] or 0.0):
        raise ValueError(
            f"Distance {float(rt['distance_nm']):.0f} nm exceeds {tail}'s range "
            f"({float(atype['range_nm']):.0f} nm)."
        )
    for ap_code in (pos, dest):
        ap = get_airport(ap_code)
        if ap and ap.get("runway_length_ft") and int(ap["runway_length_ft"]) < int(atype["runway_req_ft"]):
            raise ValueError(
                f"Runway at {ap_code} is too short for {ac['type_id']} "
                f"(needs {int(atype['runway_req_ft'])} ft)."
            )

    fh = float(rt["distance_nm"]) / float(atype["cruise_speed_kts"] or 1.0)
    arr_abs = dep_abs + fh
    seg_gw = int(dep_abs // 168.0) + 1
    mtt_hours = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    new_legs_od = [(dep_abs, arr_abs, pos, dest)]
    validate_position_timeline_for_new_legs(tail, seg_gw, new_legs_od)
    assert_tail_schedule_accepts_new_intervals(tail, seg_gw, [(dep_abs, arr_abs)], mtt_hours)

    cap_air = max_weekly_airborne_hours_cap()
    existing_air = sum_airborne_hours_for_tail_week(tail, seg_gw)
    if existing_air + fh > cap_air + 1e-6:
        raise ValueError(
            f"Weekly airborne limit is {cap_air:.0f} h per aircraft. "
            f"This tail already has {existing_air:.1f} h this week; "
            f"adding {fh:.1f} h would exceed the limit."
        )

    # Ferry replaces the operating plan: drop not-yet-departed passenger legs and templates
    # before gate checks so leftover passenger banks are not double-counted with the ferry.
    remove_superseded_scheduled_segments_for_tail(tail)
    db.execute("DELETE FROM weekly_rotations WHERE tail_number = ?", (tail,))
    db.execute("DELETE FROM flight_schedules WHERE tail_number = ?", (tail,))

    try:
        _assert_new_segment_airport_limits(
            seg_gw,
            [{"origin_iata": pos, "dest_iata": dest, "dep_abs": dep_abs, "arr_abs": arr_abs, "tail_number": tail}],
            ferry=True,
        )
    except Exception as e:
        raise ValueError(str(e)) from e

    al = db.fetch_one("SELECT callsign FROM airline WHERE id = 1")
    callsign = str(al["callsign"]) if al and al["callsign"] else "FL"
    segment_id = f"{tail}-FERRY-{rt['route_id']}-{uuid.uuid4().hex[:10]}"
    day_label = _day_of_week_label(seg_gw, dep_abs)

    db.execute(
        """
        INSERT INTO flight_segments (
            segment_id, game_week, day_of_week, tail_number, route_id, origin_iata, dest_iata,
            flight_number, scheduled_dep_time, scheduled_dep_game_hour,
            scheduled_arr_time, scheduled_arr_game_hour,
            baseline_dep_game_hour, baseline_arr_game_hour,
            status, pax_business, pax_leisure, revenue_gross,
            excise_tax, segment_fee, security_fee, pfc_fee,
            landing_fee, gate_fee, is_ferry
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0, 1)
        """,
        (
            segment_id,
            seg_gw,
            day_label,
            tail,
            rt["route_id"],
            pos,
            dest,
            f"{callsign}9FR",
            hhmm_from_absolute_game_hour(dep_abs),
            dep_abs,
            hhmm_from_absolute_game_hour(arr_abs),
            arr_abs,
            dep_abs,
            arr_abs,
        ),
    )
    if str(ac.get("status") or "") != "IN_AIR":
        db.execute("UPDATE fleet SET status = 'SCHEDULED' WHERE tail_number = ?", (tail,))

    return {
        "tail_number": tail,
        "segment_id": segment_id,
        "from": pos,
        "to": dest,
        "route_id": rt["route_id"],
        "day": day_label,
        "dep_game_hour": dep_abs,
        "arr_game_hour": arr_abs,
        "dep_label": hhmm_from_absolute_game_hour(dep_abs),
        "arr_label": hhmm_from_absolute_game_hour(arr_abs),
        "game_week": seg_gw,
        "flight_hours": fh,
        "distance_nm": float(rt["distance_nm"] or 0.0),
    }


def schedule_ferry_to_hub(tail_number: str, hub_iata: str = None) -> Optional[Dict[str, Any]]:
    """
    Position an idle tail back to the hub with a real (flown) leg carrying no passengers.

    Called after a plan is cleared so an aircraft is not stranded down-route. The leg is
    marked `is_ferry = 1`: on_departure books zero pax and zero revenue for it, but fuel and
    airport fees are still paid — repositioning costs money, as it should.
    Returns None when the tail is already at the hub, is AOG, or no route is possible.
    """
    tail = str(tail_number).strip().upper()
    ac = get_fleet_aircraft(tail)
    if not ac:
        return None
    if str(ac.get("status") or "") == "AOG":
        return None
    if hub_iata:
        hub = str(hub_iata).upper().strip()
    else:
        al = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
        hub = str(al["home_hub_iata"]).upper().strip() if al and al["home_hub_iata"] else ""
    if not hub:
        return None

    pos, free_hour = tail_position_and_free_hour(tail)
    if not pos or pos == hub:
        return None

    gw = int(free_hour // 168.0) + 1
    try:
        return schedule_ferry_reposition(
            tail,
            hub,
            dep_game_hour=float(free_hour),
            game_week=gw,
        )
    except ValueError:
        return None


def cancel_rotation(tail_number, *, wipe_completed_this_week: bool = False):
    """
    Remove this tail's rotation templates and not-yet-departed legs.

    wipe_completed_this_week:
        If True, also deletes LANDED/DIVERTED legs for the current game week so the
        weekly grid is empty (cash already collected is not reversed).
        A currently airborne leg that has not reached ETA is kept; stuck IN_AIR
        legs past ETA are removed.
    """
    from engine.scheduling.segments import remove_superseded_scheduled_segments_for_tail
    tail_number = str(tail_number or "").strip().upper()
    gs = db.fetch_one("SELECT game_week, game_hours_elapsed FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    try:
        from engine.clock import get_display_game_hours

        ghe = float(get_display_game_hours())
    except Exception:
        ghe = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0

    remove_superseded_scheduled_segments_for_tail(tail_number)

    db.execute(
        """
        DELETE FROM flight_segments
        WHERE tail_number = ?
          AND status = 'IN_AIR'
          AND scheduled_arr_game_hour <= ?
        """,
        (tail_number, ghe),
    )

    if wipe_completed_this_week:
        db.execute(
            """
            DELETE FROM flight_segments
            WHERE tail_number = ?
              AND game_week = ?
              AND status IN ('LANDED', 'DIVERTED', 'CANCELLED')
            """,
            (tail_number, gw),
        )

    db.execute("DELETE FROM weekly_rotations WHERE tail_number = ?", (tail_number,))
    db.execute("DELETE FROM flight_schedules WHERE tail_number = ?", (tail_number,))

    still_air = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM flight_segments
        WHERE tail_number = ? AND status = 'IN_AIR'
        """,
        (tail_number,),
    )
    db.execute(
        """
        UPDATE fleet
        SET status = ?
        WHERE tail_number = ?
        """,
        ("IN_AIR" if still_air and int(still_air["c"] or 0) > 0 else "IDLE", tail_number),
    )

    # Do not strand the aircraft down-route: fly it home once the current trip finishes.
    ferry = None
    try:
        ferry = schedule_ferry_to_hub(tail_number)
    except Exception:
        ferry = None
    if ferry:
        try:
            from engine.news_feed import push_news

            push_news(
                f"↩ {tail_number} positioning {ferry['from']}→{ferry['to']} "
                f"at {ferry['dep_label']} (no passengers)"
            )
        except Exception:
            pass

    return ferry if ferry else True


