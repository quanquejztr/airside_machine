"""
Random events, AOG, weather, strikes — Phase 9 (integrates with GameClock auto-pause).
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from db import db

_events_lock = threading.Lock()
_events_queue: List[Tuple[float, str, Dict[str, Any]]] = []
# wall-clock time when tech outage ends (real seconds)
_tech_outage_until_wall: float = 0.0


def _game_time_hours() -> float:
    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    return float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0


def insert_event_log(
    game_week: int,
    game_time_hours: float,
    event_type: str,
    description: str,
    *,
    affected_iata: Optional[str] = None,
    affected_tail: Optional[str] = None,
    financial_impact: float = 0.0,
    resolved: int = 0,
) -> str:
    # Support both the Phase 9 schema and older saves that still have (message, game_hours_elapsed).
    cols: set[str] = set()
    try:
        info = db.fetch_all("PRAGMA table_info(event_log)")
        cols = {str(r["name"]) for r in (info or []) if r and r.get("name")}
    except Exception:
        cols = set()

    eid = str(uuid.uuid4())
    gw = int(game_week)
    gth = float(game_time_hours)
    et = str(event_type)
    desc = str(description)
    ai = affected_iata
    at = affected_tail
    fi = float(financial_impact)
    rs = int(resolved)

    if "description" in cols and "game_time_hours" in cols and "event_id" in cols:
        db.execute(
            """
            INSERT INTO event_log (
                event_id, game_week, game_time_hours, event_type,
                affected_iata, affected_tail, description, financial_impact, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, gw, gth, et, ai, at, desc, fi, rs),
        )
        # best-effort backfill old columns if present
        if "message" in cols or "game_hours_elapsed" in cols:
            try:
                db.execute(
                    """
                    UPDATE event_log
                    SET message = COALESCE(message, ?),
                        game_hours_elapsed = COALESCE(game_hours_elapsed, ?)
                    WHERE event_id = ?
                    """,
                    (desc, gth, eid),
                )
            except Exception:
                pass
        return eid

    # Older schema fallback (INTEGER PK autoincrement)
    db.execute(
        """
        INSERT INTO event_log (game_week, event_type, message, game_hours_elapsed)
        VALUES (?, ?, ?, ?)
        """,
        (gw, et, desc, gth),
    )
    return eid


def check_aog_probability(tail_number: str) -> float:
    """
    Maintenance overdue drives AOG risk: 0 until interval, then linear 0→0.30 from interval to 2× interval.
    """
    tail_number = tail_number.strip().upper()
    row = db.fetch_one(
        """
        SELECT f.weeks_since_maintenance, t.maintenance_interval_weeks, t.type_id
        FROM fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE f.tail_number = ?
        """,
        (tail_number,),
    )
    if not row:
        return 0.0
    wsm = int(row["weeks_since_maintenance"] or 0)
    interval = row["maintenance_interval_weeks"]
    if interval is None or int(interval) <= 0:
        return 0.0
    interval = int(interval)
    if wsm <= interval:
        return 0.0
    span = float(interval)
    overdue = float(wsm - interval)
    if overdue >= span:
        return 0.30
    return 0.30 * (overdue / span)


def trigger_aog(tail_number: str, game_time_hours: Optional[float] = None, reason: Optional[str] = None) -> None:
    """
    Aircraft on ground: cancel remaining SCHEDULED legs this week for this tail (after current time),
    log event, news ticker, auto-pause clock.
    """
    from engine.news_feed import push_news
    from engine.clock import get_global_clock

    tail_number = tail_number.strip().upper()
    row = db.fetch_one("SELECT tail_number FROM fleet WHERE tail_number = ?", (tail_number,))
    if not row:
        raise ValueError(f"No aircraft with tail {tail_number}")

    gth = float(game_time_hours) if game_time_hours is not None else _game_time_hours()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1

    desc = reason or "Mechanical — aircraft on ground"
    db.execute(
        """
        UPDATE fleet
        SET status = 'AOG', aog_reason = ?
        WHERE tail_number = ?
        """,
        (desc, tail_number),
    )

    db.execute(
        """
        UPDATE flight_segments
        SET status = 'CANCELLED'
        WHERE tail_number = ?
          AND game_week = ?
          AND status IN ('SCHEDULED', 'DELAYED', 'HOLDING')
          AND scheduled_dep_game_hour >= ?
        """,
        (tail_number, gw, gth),
    )

    insert_event_log(
        gw,
        gth,
        "AOG",
        desc,
        affected_tail=tail_number,
        financial_impact=0.0,
    )
    push_news(f"AOG: {tail_number} — {desc}")

    clk = get_global_clock()
    if clk is not None and clk.is_alive():
        clk.request_auto_pause(f"Aircraft {tail_number} is AOG ({desc})")


def resolve_aog(tail_number: str) -> float:
    """
    Charge maintenance via event_log.financial_impact (settlement P&L).
    Player still chooses when to resume the clock (explicit unpause).
    Returns maintenance_fee charged.
    """
    from engine.news_feed import push_news

    tail_number = tail_number.strip().upper()
    row = db.fetch_one(
        """
        SELECT f.status, f.type_id, t.mtow_lbs
        FROM fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE f.tail_number = ?
        """,
        (tail_number,),
    )
    if not row or str(row["status"]) != "AOG":
        raise ValueError(f"Aircraft {tail_number} is not AOG")

    mtow = float(row["mtow_lbs"] or 0)
    maintenance_fee = mtow * 0.15

    db.execute(
        """
        UPDATE fleet
        SET status = 'IDLE', weeks_since_maintenance = 0, aog_reason = NULL
        WHERE tail_number = ?
        """,
        (tail_number,),
    )

    gth = _game_time_hours()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1

    insert_event_log(
        gw,
        gth,
        "AOG",
        f"Resolved maintenance for {tail_number}",
        affected_tail=tail_number,
        financial_impact=-maintenance_fee,
        resolved=1,
    )
    push_news(f"AOG cleared: {tail_number} — maintenance ${maintenance_fee:,.0f}")

    return maintenance_fee


def swap_aircraft(cancelled_segment_id: str, replacement_tail: str) -> str:
    """
    Replace a cancelled leg with another IDLE aircraft at the same origin.
    Does not fix AOG on the original tail.
    """
    from engine.scheduling import (
        get_financial_constant,
        hhmm_from_absolute_game_hour,
    )
    from engine.routes import get_route
    from engine.aircraft import get_fleet_aircraft
    from engine.airports import get_airport

    replacement_tail = replacement_tail.strip().upper()
    seg = db.fetch_one("SELECT * FROM flight_segments WHERE segment_id = ?", (cancelled_segment_id,))
    if not seg:
        raise ValueError("Segment not found")
    if str(seg["status"]) != "CANCELLED":
        raise ValueError("swap_aircraft expects a CANCELLED segment row")

    route = get_route(seg["route_id"])
    if not route:
        raise ValueError("Route missing")

    rep = get_fleet_aircraft(replacement_tail)
    if not rep or str(rep["status"]) != "IDLE":
        raise ValueError("Replacement tail must be IDLE")

    origin = str(route["origin_iata"]).upper()
    if str(rep.get("current_airport_iata") or "").upper() != origin:
        raise ValueError(f"Replacement aircraft must be at {origin}")

    ac_type = db.fetch_one("SELECT * FROM aircraft_types WHERE type_id = ?", (rep["type_id"],))
    if not ac_type:
        raise ValueError("Aircraft type not found")

    if float(route["distance_nm"]) > float(ac_type["range_nm"]):
        raise ValueError("Replacement aircraft lacks range for this leg")

    for ap_code in (route["origin_iata"], route["dest_iata"]):
        ap = get_airport(ap_code)
        if ap and ap.get("runway_length_ft") and int(ap["runway_length_ft"]) < int(ac_type["runway_req_ft"]):
            raise ValueError(f"Runway at {ap_code} too short for replacement type")

    mtt_hours = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    cruise = float(ac_type["cruise_speed_kts"])
    fh = float(route["distance_nm"]) / cruise
    gth = _game_time_hours()
    dep_abs = gth + mtt_hours
    arr_abs = dep_abs + fh

    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else int(seg["game_week"])

    new_id = f"{replacement_tail}-{seg['route_id']}-W{gw}-swap-{uuid.uuid4().hex[:10]}"
    db.execute(
        """
        INSERT INTO flight_segments (
            segment_id, game_week, day_of_week, tail_number, route_id, flight_number,
            scheduled_dep_time, scheduled_dep_game_hour,
            scheduled_arr_time, scheduled_arr_game_hour,
            baseline_dep_game_hour, baseline_arr_game_hour,
            status, pax_business, pax_leisure, revenue_gross,
            excise_tax, segment_fee, security_fee, pfc_fee,
            landing_fee, gate_fee
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
        """,
        (
            new_id,
            gw,
            str(seg["day_of_week"]),
            replacement_tail,
            seg["route_id"],
            seg["flight_number"],
            hhmm_from_absolute_game_hour(dep_abs),
            dep_abs,
            hhmm_from_absolute_game_hour(arr_abs),
            arr_abs,
            dep_abs,
            arr_abs,
        ),
    )

    db.execute(
        """
        UPDATE fleet SET status = 'SCHEDULED' WHERE tail_number = ?
        """,
        (replacement_tail,),
    )

    insert_event_log(
        gw,
        gth,
        "SWAP",
        f"Swap: {replacement_tail} covers route {seg['route_id']} (was {seg['tail_number']})",
        affected_tail=replacement_tail,
        financial_impact=0.0,
    )
    return new_id


def trigger_weather(
    airport_iata: str,
    duration_game_hours: float,
    *,
    request_pause_if_affecting: bool = True,
) -> str:
    """
    Close airport until current game time + duration.

    Returns a short human-readable summary line for the CLI.

    If request_pause_if_affecting is True (default), auto-pauses when an active flight
    touches the closed airport (can feel like the menu "did nothing" until you resume).
    """
    from engine.environment import set_closure_until
    from engine.clock import get_global_clock

    iata = airport_iata.strip().upper()
    gth = _game_time_hours()
    until = gth + float(duration_game_hours)
    set_closure_until(iata, until)

    from engine.scheduling import (
        apply_weather_closure_to_inbound_in_air,
        format_absolute_game_hour_as_week_clock,
    )

    try:
        apply_weather_closure_to_inbound_in_air(iata)
    except Exception:
        pass

    until_disp = format_absolute_game_hour_as_week_clock(until)

    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1
    insert_event_log(
        gw,
        gth,
        "WEATHER",
        f"Weather closure at {iata} until {until_disp}",
        affected_iata=iata,
        financial_impact=0.0,
    )

    pause_note = ""
    clk = get_global_clock()
    if request_pause_if_affecting and clk is not None and clk.is_alive():
        try:
            from engine.environment import weather_closure_hits_active_flights

            wr = weather_closure_hits_active_flights()
            if wr:
                clk.request_auto_pause(wr)
                pause_note = " Game auto-paused (active flight affected). Use Game Clock → 1 to resume."
        except Exception:
            pass

    return (
        f"{iata} closed until {until_disp} (~{float(duration_game_hours):g} h from now).{pause_note}"
    )


def trigger_strike(scope: str, airport_iata: Optional[str] = None) -> None:
    """
    GROUND_CREW: delay departures at one airport (20–40 min game-time equivalent → use delay minutes).
    ATC: all airports in same country — delay 30–90 min; 20% flights cancelled vs delayed.
    """
    from engine.scheduling import propagate_delay
    from engine.clock import get_global_clock

    gth = _game_time_hours()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1

    scope_u = scope.strip().upper()
    if scope_u == "GROUND_CREW":
        if not airport_iata:
            raise ValueError("airport_iata required for GROUND_CREW strike")
        ap = airport_iata.strip().upper()
        delay_min = random.randint(20, 40)
        rows = db.fetch_all(
            """
            SELECT DISTINCT fs.tail_number
            FROM flight_segments fs
            JOIN routes r ON fs.route_id = r.route_id
            WHERE fs.game_week = ?
              AND fs.status = 'SCHEDULED'
              AND r.origin_iata = ?
            """,
            (gw, ap),
        )
        for r in rows:
            propagate_delay(str(r["tail_number"]), float(delay_min))
        insert_event_log(
            gw,
            gth,
            "STRIKE",
            f"Ground crew strike at {ap} — ~{delay_min} min delays",
            affected_iata=ap,
            financial_impact=0.0,
        )
        clk = get_global_clock()
        if clk is not None and clk.is_alive():
            clk.request_auto_pause(f"Strike: ground crew at {ap}")
        return

    if scope_u == "ATC":
        if not airport_iata:
            raise ValueError("airport_iata required to resolve country for ATC strike")
        hub = airport_iata.strip().upper()
        country_row = db.fetch_one("SELECT country FROM airports WHERE iata = ?", (hub,))
        if not country_row:
            raise ValueError("Unknown airport")
        country = str(country_row["country"])
        airports = db.fetch_all(
            "SELECT iata FROM airports WHERE country = ?",
            (country,),
        )
        codes = [str(r["iata"]) for r in airports]
        if not codes:
            return
        ph = ",".join("?" * len(codes))
        segs = db.fetch_all(
            f"""
            SELECT fs.segment_id, fs.tail_number, r.origin_iata
            FROM flight_segments fs
            JOIN routes r ON fs.route_id = r.route_id
            WHERE fs.game_week = ?
              AND fs.status = 'SCHEDULED'
              AND r.origin_iata IN ({ph})
            """,
            (gw, *codes),
        )
        for s in segs:
            if random.random() < 0.20:
                db.execute(
                    "UPDATE flight_segments SET status = 'CANCELLED' WHERE segment_id = ?",
                    (s["segment_id"],),
                )
            else:
                dm = random.randint(30, 90)
                propagate_delay(str(s["tail_number"]), float(dm))
        insert_event_log(
            gw,
            gth,
            "STRIKE",
            f"ATC slowdown — {country}: delays / some cancellations",
            affected_iata=hub,
            financial_impact=0.0,
        )
        clk = get_global_clock()
        if clk is not None and clk.is_alive():
            clk.request_auto_pause(f"ATC strike affecting {country}")
        return

    raise ValueError("scope must be GROUND_CREW or ATC")


def trigger_tech_outage() -> None:
    """Blank flight board for ~30 real seconds; flights continue."""
    global _tech_outage_until_wall
    db.execute("UPDATE game_state SET ui_blackout = 1 WHERE id = 1")
    _tech_outage_until_wall = time.time() + 30.0

    gth = _game_time_hours()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1
    insert_event_log(
        gw,
        gth,
        "TECH_OUTAGE",
        "Operations IT outage — displays degraded",
        financial_impact=0.0,
    )


def maybe_clear_tech_outage() -> None:
    global _tech_outage_until_wall
    if _tech_outage_until_wall <= 0:
        return
    if time.time() >= _tech_outage_until_wall:
        _tech_outage_until_wall = 0.0
        db.execute("UPDATE game_state SET ui_blackout = 0 WHERE id = 1")


def clear_player_disruptions() -> None:
    """
    Skip / cancel in-memory disruption effects: all weather closures, tech UI blackout,
    and dismiss the clock auto-pause banner (does not change pause state — use Resume).
    """
    global _tech_outage_until_wall
    from engine.environment import clear_all_closures

    clear_all_closures()
    _tech_outage_until_wall = 0.0
    db.execute("UPDATE game_state SET ui_blackout = 0 WHERE id = 1")
    try:
        from engine.clock import get_global_clock

        clk = get_global_clock()
        if clk is not None and clk.is_alive():
            clk.clear_auto_pause_alert()
    except Exception:
        pass


def is_ui_blackout() -> bool:
    maybe_clear_tech_outage()
    row = db.fetch_one("SELECT ui_blackout FROM game_state WHERE id = 1")
    return bool(row and int(row["ui_blackout"] or 0))


_scheduled_events_week: Optional[int] = None


def schedule_weekly_events(game_week: int) -> None:
    """
    Roll weekly random events into the in-memory queue (game-time triggers).
    """
    global _scheduled_events_week
    from engine.scheduling import week_base_hours

    gw = int(game_week)
    if _scheduled_events_week == gw:
        return

    base = week_base_hours(gw)
    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    now = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0
    t_min = max(base, now)
    t_end = base + 168.0
    if t_min >= t_end:
        _scheduled_events_week = gw
        return

    net = _player_network_airports()
    random.shuffle(net)

    def _at() -> float:
        return t_min + random.random() * (t_end - t_min)

    with _events_lock:
        _events_queue.clear()

        # Weather: 8% per airport in network (cap to avoid huge queues)
        for ap in net[:40]:
            if random.random() < 0.08:
                t = _at()
                dur = random.uniform(0.5, 4.0)
                _events_queue.append((t, "weather", {"iata": ap, "dur": dur}))

        if random.random() < 0.03:
            t = _at()
            _events_queue.append((t, "strike_gc", {}))

        if random.random() < 0.05:
            t = _at()
            _events_queue.append((t, "tech", {}))

        if random.random() < 0.02:
            t = _at()
            _events_queue.append((t, "fuel_shock", {}))

        _events_queue.sort(key=lambda x: x[0])

    _scheduled_events_week = gw


def _player_network_airports() -> List[str]:
    rows = db.fetch_all(
        """
        SELECT DISTINCT r.origin_iata AS iata
        FROM routes r
        JOIN player_routes pr ON pr.route_id = r.route_id
        UNION
        SELECT DISTINCT r.dest_iata
        FROM routes r
        JOIN player_routes pr ON pr.route_id = r.route_id
        """
    )
    out = [str(r["iata"]) for r in rows if r["iata"]]
    if out:
        return out
    al = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
    hub = str(al["home_hub_iata"]) if al and al["home_hub_iata"] else "TPA"
    return [hub]


def process_events_queue(t_prev: float, t_now: float) -> None:
    """Fire queued events whose trigger time falls in (t_prev, t_now] (and overdue backlog)."""
    from engine.environment import expire_closures_before

    expire_closures_before(t_now)

    if t_now <= t_prev:
        return

    pending: List[Tuple[float, str, Dict[str, Any]]] = []
    with _events_lock:
        remain: List[Tuple[float, str, Dict[str, Any]]] = []
        for item in _events_queue:
            trig, kind, payload = item
            if trig > t_now:
                remain.append(item)
                continue
            pending.append(item)
        _events_queue[:] = remain

    hub = None
    try:
        r = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
        hub = str(r["home_hub_iata"]) if r and r.get("home_hub_iata") else None
    except Exception:
        pass

    for trig, kind, payload in pending:
        try:
            if kind == "weather":
                trigger_weather(str(payload.get("iata", "TPA")), float(payload.get("dur", 2.0)))
            elif kind == "strike_gc" and hub:
                trigger_strike("GROUND_CREW", hub)
            elif kind == "tech":
                trigger_tech_outage()
            elif kind == "fuel_shock":
                from engine.fuel import debug_force_shock

                debug_force_shock()
        except Exception:
            continue


def queue_len() -> int:
    with _events_lock:
        return len(_events_queue)
