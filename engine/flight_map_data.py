"""
JSON payload for the optional local flight map (fleet positions + active leg polylines).

Segments are included only from the boarding window (same ≤5 min rule as the flight board)
through in-flight statuses; landed and cancelled legs are omitted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

from db import db

# Same threshold as `ui.flight_board.render_flight_status`: "BOARDING" when ≤5 min to departure.
_BOARDING_LEAD_GAME_HOURS = 300.0 / 3600.0


def _segment_visible_on_flight_map(
    status: str,
    scheduled_dep_game_hour: float,
    current_game_hour: float,
    *,
    player: bool,
) -> bool:
    """
    Boarding window through wheels-down. Future week legs stay off the map so a tail
    is not drawn on every remaining sector at once (e.g. a ferry SFO→ATL plus later ATL→SFO).
    """
    if status in ("LANDED", "CANCELLED"):
        return False
    if status in ("IN_AIR", "HOLDING", "DIVERTED"):
        return True
    if status in ("SCHEDULED", "DELAYED"):
        lead = _BOARDING_LEAD_GAME_HOURS
        if player:
            # ~1 game hour so the player can see the next departure taxiing out.
            lead = max(lead, 1.0)
        return float(scheduled_dep_game_hour) <= current_game_hour + lead
    return False


def _one_segment_per_tail(rows: List[Any]) -> List[Any]:
    """Keep the airborne leg, else the next departure, so one aircraft is one dot."""
    by_tail: Dict[str, List[Any]] = {}
    for row in rows:
        by_tail.setdefault(str(row["tail_number"] or ""), []).append(row)
    out: List[Any] = []
    for _tail, segs in by_tail.items():
        flying = [
            s
            for s in segs
            if str(s["status"]) in ("IN_AIR", "HOLDING", "DIVERTED")
        ]
        if flying:
            flying.sort(key=lambda s: float(s["scheduled_dep_game_hour"] or 0.0))
            out.append(flying[-1])
            continue
        upcoming = sorted(segs, key=lambda s: float(s["scheduled_dep_game_hour"] or 0.0))
        if upcoming:
            out.append(upcoming[0])
    return out


_airport_geo: Dict[str, Dict[str, Any]] = {}


def _airports_for_iatas(iatas: Set[str]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    for iata in iatas:
        code = str(iata or "").strip().upper()
        if not code:
            continue
        cached = _airport_geo.get(code)
        if cached:
            found[code] = cached
        else:
            missing.append(code)
    for i in range(0, len(missing), 400):
        chunk = missing[i : i + 400]
        ph = ",".join("?" * len(chunk))
        for row in db.fetch_all(
            f"SELECT iata, name, city, country, lat, lon FROM airports WHERE iata IN ({ph})",
            tuple(chunk),
        ) or []:
            rec = {
                "iata": row["iata"],
                "name": row["name"],
                "city": row["city"],
                "country": row["country"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
            }
            _airport_geo[str(row["iata"])] = rec
            found[str(row["iata"])] = rec
    return found


def get_flight_map_payload() -> Dict[str, Any]:
    """
    Build a snapshot for the current game week: fleet at current airports and
    route polylines for legs that are in the boarding window, in flight, or diverted/holding;
    landed legs are omitted.
    """
    gs = db.fetch_one("SELECT game_week, game_hours_elapsed, speed_multiplier FROM game_state WHERE id = 1")
    game_week = int(gs["game_week"]) if gs else 1
    try:
        from engine.clock import get_display_game_hours

        current_game_hour = float(get_display_game_hours())
    except Exception:
        current_game_hour = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0

    fleet_rows = db.fetch_all(
        "SELECT tail_number, type_id, status, current_airport_iata FROM fleet ORDER BY tail_number"
    )
    segment_rows = db.fetch_all(
        """
        SELECT fs.segment_id, fs.tail_number, fs.route_id, fs.status,
               fs.flight_number, fs.scheduled_dep_time, fs.day_of_week,
               fs.scheduled_dep_game_hour, fs.scheduled_arr_game_hour,
               COALESCE(fs.is_ferry, 0) AS is_ferry,
               COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
               COALESCE(fs.dest_iata, r.dest_iata) AS dest_iata
        FROM flight_segments fs
        JOIN routes r ON fs.route_id = r.route_id
        WHERE fs.game_week = ? AND fs.status NOT IN ('LANDED', 'CANCELLED')
        ORDER BY fs.scheduled_dep_game_hour
        """,
        (game_week,),
    )

    visible_segment_rows: List[Any] = []
    for row in segment_rows:
        if _segment_visible_on_flight_map(
            str(row["status"]),
            float(row["scheduled_dep_game_hour"]),
            current_game_hour,
            player=True,
        ):
            visible_segment_rows.append(row)
    visible_segment_rows = _one_segment_per_tail(visible_segment_rows)

    visible_ai_rows: List[Any] = []
    try:
        ai_until = current_game_hour + _BOARDING_LEAD_GAME_HOURS
        visible_ai_rows = list(
            db.fetch_all(
                """
                SELECT segment_id, competitor_id, origin_iata, dest_iata, status,
                       scheduled_dep_game_hour, scheduled_arr_game_hour, flight_number
                FROM ai_flight_segments
                WHERE game_week = ?
                  AND status NOT IN ('LANDED', 'CANCELLED')
                  AND (
                    status IN ('IN_AIR', 'HOLDING', 'DIVERTED')
                    OR (
                      status IN ('SCHEDULED', 'DELAYED')
                      AND scheduled_dep_game_hour <= ?
                    )
                  )
                ORDER BY scheduled_dep_game_hour
                """,
                (game_week, ai_until),
            )
            or []
        )
    except Exception:
        visible_ai_rows = []

    iatas: Set[str] = set()
    for row in fleet_rows or []:
        ca = row["current_airport_iata"]
        if ca:
            iatas.add(str(ca))
    for row in visible_segment_rows:
        iatas.add(str(row["origin_iata"]))
        iatas.add(str(row["dest_iata"]))
    for row in visible_ai_rows:
        iatas.add(str(row["origin_iata"]))
        iatas.add(str(row["dest_iata"]))

    airports = _airports_for_iatas(iatas)

    fleet: List[Dict[str, Any]] = []
    type_by_tail: Dict[str, str] = {}
    for row in fleet_rows or []:
        iata = row["current_airport_iata"]
        ap = airports.get(str(iata)) if iata else None
        tid = str(row["type_id"] or "").strip()
        tail = str(row["tail_number"] or "")
        if tail and tid:
            type_by_tail[tail] = tid
        fleet.append(
            {
                "tail_number": row["tail_number"],
                "type_id": row["type_id"],
                "status": row["status"],
                "current_airport_iata": iata,
                "lat": ap["lat"] if ap else None,
                "lon": ap["lon"] if ap else None,
            }
        )

    ai_type_by_key: Dict[tuple, str] = {}
    try:
        for cr in db.fetch_all(
            """
            SELECT competitor_id, outbound_route_id, inbound_route_id, aircraft_type_id
            FROM competitor_routes
            """
        ) or []:
            tid = str(cr["aircraft_type_id"] or "").strip()
            if not tid:
                continue
            cid = str(cr["competitor_id"] or "")
            if cid and cr["outbound_route_id"]:
                ai_type_by_key[(cid, str(cr["outbound_route_id"]))] = tid
            if cid and cr["inbound_route_id"]:
                ai_type_by_key[(cid, str(cr["inbound_route_id"]))] = tid
    except Exception:
        pass

    def _progress(dep: float, arr: float) -> float:
        d = float(dep or 0.0)
        a = float(arr or (d + 1.0))
        if a <= d + 1e-6:
            a = d + 1.0
        p = (current_game_hour - d) / (a - d)
        return max(0.0, min(1.0, float(p)))

    segments: List[Dict[str, Any]] = []
    for row in visible_segment_rows:
        o, d = row["origin_iata"], row["dest_iata"]
        ao, ad = airports.get(o), airports.get(d)
        segments.append(
            {
                "segment_id": row["segment_id"],
                "operator": "PLAYER",
                "tail_number": row["tail_number"],
                "flight_number": row["flight_number"],
                "type_id": type_by_tail.get(str(row["tail_number"] or "")) or None,
                "route_id": row["route_id"],
                "status": row["status"],
                "is_ferry": bool(int(row["is_ferry"] or 0)),
                "scheduled_dep_time": row["scheduled_dep_time"],
                "day_of_week": row["day_of_week"],
                "origin_iata": o,
                "dest_iata": d,
                "origin_lat": ao["lat"] if ao else None,
                "origin_lon": ao["lon"] if ao else None,
                "dest_lat": ad["lat"] if ad else None,
                "dest_lon": ad["lon"] if ad else None,
                "scheduled_dep_game_hour": float(row["scheduled_dep_game_hour"] or 0.0),
                "scheduled_arr_game_hour": float(row["scheduled_arr_game_hour"] or 0.0),
                "progress": _progress(
                    float(row["scheduled_dep_game_hour"] or 0.0),
                    float(row["scheduled_arr_game_hour"] or 0.0),
                ),
            }
        )
    for row in visible_ai_rows:
        o, d = str(row["origin_iata"]), str(row["dest_iata"])
        ao, ad = airports.get(o), airports.get(d)
        segments.append(
            {
                "segment_id": row["segment_id"],
                "operator": str(row["competitor_id"]),
                "tail_number": str(row["flight_number"] or row["competitor_id"]),
                "flight_number": str(row["flight_number"] or row["competitor_id"]),
                "type_id": ai_type_by_key.get((str(row["competitor_id"]), f"{o}-{d}")) or None,
                "route_id": f"{o}-{d}",
                "status": row["status"],
                "scheduled_dep_time": None,
                "day_of_week": None,
                "origin_iata": o,
                "dest_iata": d,
                "origin_lat": ao["lat"] if ao else None,
                "origin_lon": ao["lon"] if ao else None,
                "dest_lat": ad["lat"] if ad else None,
                "dest_lon": ad["lon"] if ad else None,
                "scheduled_dep_game_hour": float(row["scheduled_dep_game_hour"] or 0.0),
                "scheduled_arr_game_hour": float(row["scheduled_arr_game_hour"] or 0.0),
                "progress": _progress(
                    float(row["scheduled_dep_game_hour"] or 0.0),
                    float(row["scheduled_arr_game_hour"] or 0.0),
                ),
            }
        )

    al = db.fetch_one("SELECT name, callsign, home_hub_iata FROM airline WHERE id = 1")
    airline = None
    if al:
        airline = {
            "name": al["name"],
            "callsign": al["callsign"],
            "home_hub_iata": al["home_hub_iata"],
        }

    speed = 0
    real_s = 30
    try:
        from engine.clock import get_global_clock

        clk = get_global_clock()
        if clk is not None:
            speed = int(clk.speed_multiplier or 0)
            real_s = int(getattr(clk, "real_seconds_per_game_hour", 30) or 30)
        elif gs:
            speed = int(gs["speed_multiplier"] or 0)
    except Exception:
        pass

    return {
        "ok": True,
        "game_week": game_week,
        "current_game_hour": current_game_hour,
        "speed_multiplier": speed,
        "real_seconds_per_game_hour": real_s,
        "airline": airline,
        "airports": airports,
        "fleet": fleet,
        "segments": segments,
    }

