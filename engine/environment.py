"""
Airport environment — weather closures with until-game-hour semantics (Phase 9).

Closures are in-memory only; cleared on process restart.
"""

from __future__ import annotations

import math
import threading
from typing import Dict, Optional, Set

from db import db


_closed_lock = threading.Lock()
# iata -> absolute game hour when airport reopens
_closure_until: Dict[str, float] = {}


def register_weather_closure(iata: str) -> None:
    """Legacy: mark closed with no reopen time (use trigger_weather for timed closure)."""
    with _closed_lock:
        _closure_until[iata.strip().upper()] = float("inf")


def clear_weather_closure(iata: str) -> None:
    with _closed_lock:
        _closure_until.pop(iata.strip().upper(), None)


def set_closure_until(iata: str, close_until_game_hours: float) -> None:
    with _closed_lock:
        _closure_until[iata.strip().upper()] = float(close_until_game_hours)


def closure_until(iata: str) -> Optional[float]:
    with _closed_lock:
        return _closure_until.get(iata.strip().upper())


def active_weather_closures() -> Set[str]:
    """Airports currently closed (may include permanently closed legacy entries)."""
    with _closed_lock:
        return set(_closure_until.keys())


def clear_all_closures() -> None:
    with _closed_lock:
        _closure_until.clear()


def is_airport_closed_at(iata: str, game_hours_elapsed: float) -> bool:
    """True if airport is closed at this game time."""
    code = iata.strip().upper()
    with _closed_lock:
        until = _closure_until.get(code)
    if until is None:
        return False
    if math.isinf(until):
        return True
    return game_hours_elapsed < until


def expire_closures_before(game_hours_elapsed: float) -> None:
    """Drop closures whose window has ended."""
    with _closed_lock:
        dead = [k for k, u in _closure_until.items() if not math.isinf(u) and u <= game_hours_elapsed]
        for k in dead:
            del _closure_until[k]


def weather_closure_hits_active_flights() -> Optional[str]:
    """
    If any closed airport has a SCHEDULED / IN_AIR / HOLDING segment this week, return reason.
    """
    gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    ghe = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0

    with _closed_lock:
        closed = [k for k, u in _closure_until.items() if is_airport_closed_at(k, ghe)]

    if not closed:
        return None

    ph = ",".join("?" * len(closed))
    params = tuple(closed) + tuple(closed)
    row = db.fetch_one(
        f"""
        SELECT fs.segment_id, r.origin_iata, r.dest_iata
        FROM flight_segments fs
        JOIN routes r ON fs.route_id = r.route_id
        WHERE fs.status IN ('SCHEDULED', 'IN_AIR', 'HOLDING')
          AND (r.origin_iata IN ({ph}) OR r.dest_iata IN ({ph}))
        LIMIT 1
        """,
        params,
    )
    if not row:
        return None
    o, d = row["origin_iata"], row["dest_iata"]
    hit = o if o in closed else d
    return f"Weather closure at {hit} affects active flight {row['segment_id']}"


def flight_board_hint_for_closed_airport(iata: str) -> str:
    """
    One line for the CLI after a closure: how many weekly segments touch this airport,
    and what to look for on the flight board (status column / route brackets).
    """
    from engine.scheduling import calendar_game_week_from_state

    code = iata.strip().upper()
    gw = calendar_game_week_from_state()
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ? AND fs.status != 'CANCELLED'
          AND (r.origin_iata = ? OR r.dest_iata = ?)
        """,
        (gw, code, code),
    )
    n = int(row["c"] or 0) if row else 0
    if n == 0:
        return (
            f"No flights this calendar week are scheduled via {code}. "
            "Closure still applies if you add routes there later before it ends."
        )
    return (
        f"[dim]{n} flight row(s) this week include {code}. "
        f"Main menu → 10: Status may show “Dest closed — diverting”, DIVERTED, "
        f"diverted dest in brackets on the route, or HOLDING (weather). "
        f"Footer “News” shows recent DEP events.[/dim]"
    )
