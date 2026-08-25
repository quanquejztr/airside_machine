"""Shared scheduling helpers (constants, airport limit asserts)."""

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

# Hours from Monday 00:00 to the start of each weekday inside a 168h game week.
DAY_START_HOURS = {
    "MON": 0.0,
    "TUE": 24.0,
    "WED": 48.0,
    "THU": 72.0,
    "FRI": 96.0,
    "SAT": 120.0,
    "SUN": 144.0,
}

def _assert_player_routes_schedulable(route_ids: list) -> None:
    # Phase 11 revised: routes are openable, but airport gate capacity must exist
    # when scheduling flights touching auctioned airports.
    return


def get_financial_constant(key, default=0):
    """Get a financial constant from the database."""
    result = db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = ?",
        (key,)
    )
    return result['value'] if result else default


def _flight_fuel_gallons(route, aircraft_type) -> float:
    speed = float(aircraft_type["cruise_speed_kts"] or 0) or 1.0
    burn = float(aircraft_type["fuel_burn_gph"] or 0)
    dist = float(route["distance_nm"] or 0)
    return (dist / speed) * burn


def _assert_new_segment_airport_limits(game_week: int, segs: list) -> None:
    """Gate concurrency and slot hourly caps. Raises ValueError on failure."""
    from engine.gates import assert_player_gate_capacity_for_new_segments as _assert_gates
    from engine.slots import assert_player_slots_for_new_segments

    _assert_gates(int(game_week), segs)
    assert_player_slots_for_new_segments(int(game_week), segs)


