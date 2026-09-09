"""Calendar / clock helpers for weekly scheduling."""

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

def week_base_hours(game_week: int) -> float:
    """Absolute game-hour offset where this game_week begins (week 1 → 0)."""
    return float(game_week - 1) * 168.0


def calendar_game_week_from_state() -> int:
    """
    Return the current calendar game week based on live game hours.
    Week 1 starts at hour 0, week 2 at hour 168, etc.
    """
    try:
        from engine.clock import get_display_game_hours

        ghe = float(get_display_game_hours())
    except Exception:
        gs = db.fetch_one(
            "SELECT game_hours_elapsed FROM game_state WHERE id = 1"
        )
        if not gs or gs["game_hours_elapsed"] is None:
            return 1
        ghe = float(gs["game_hours_elapsed"])
    return int(ghe // 168.0) + 1


def hhmm_from_absolute_game_hour(absolute_game_hour: float) -> str:
    """Format absolute game hour as HH:MM within the 24h clock (for display)."""
    sod = absolute_game_hour % 24.0
    h = int(sod)
    m = int(round((sod - h) * 60.0))
    if m >= 60:
        h += m // 60
        m = m % 60
    h = h % 24
    return f"{h:02d}:{m:02d}"


def game_week_from_abs_hour(absolute_game_hour: float) -> int:
    """1-based game week containing this absolute hour (week 1 starts at hour 0).

    Legs of a long chain can land in a later week than the one the chain was anchored
    in, so every segment must take its week from its own departure rather than from the
    chain's anchor.
    """
    return int(float(absolute_game_hour) // 168.0) + 1


def _day_of_week_label(game_week: int, dep_abs: float) -> str:
    """Map an absolute hour to MON..SUN (for the segment day_of_week column).

    Derived from the hour itself, so a leg that runs past the end of its anchor week
    gets its real weekday. This previously subtracted the passed week's base and clamped
    the index to 0..6, which silently labelled every overflowing leg "SUN".

    `game_week` is kept for call-site compatibility and is no longer consulted.
    """
    hours_into_week = float(dep_abs) % 168.0
    idx = int(hours_into_week // 24.0)
    order = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return order[max(0, min(6, idx))]


