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


def segment_exists_for_spawn(
    tail_number: str,
    game_week: int,
    route_id: str,
    dep_abs: float,
) -> bool:
    """True when this tail/week/route/departure already has a live segment row."""
    return bool(
        db.fetch_one(
            """
            SELECT 1 FROM flight_segments
            WHERE tail_number = ? AND game_week = ? AND route_id = ?
              AND ABS(scheduled_dep_game_hour - ?) < 0.001
              AND status != 'CANCELLED'
            """,
            (str(tail_number), int(game_week), str(route_id), float(dep_abs)),
        )
    )


def _filter_new_spawn_segments(game_week: int, segs: list) -> list:
    """Return only segments that are not already present for this week."""
    out = []
    for s in segs or []:
        tail = str(s.get("tail_number") or "")
        rid = str(s.get("route_id") or "")
        dep = s.get("dep_abs")
        if not tail or not rid or dep is None:
            out.append(s)
            continue
        if segment_exists_for_spawn(tail, game_week, rid, float(dep)):
            continue
        out.append(s)
    return out


def _assert_new_segment_airport_limits(
    game_week: int,
    segs: list,
    *,
    replace_tails: bool = True,
    ferry: bool = False,
    gate_shortfall_mode: bool = False,
) -> None:
    """Gate concurrency and slot hourly caps. Raises ValueError on failure.

    ferry=True: repositioning legs skip gate-allocation and weekly slot-quota
    checks (aircraft already sits at the spoke). Hourly runway capacity still applies.

    gate_shortfall_mode=True: a gate shortage opens a payable shortfall event rather than
    refusing the segments. Runway slots are deliberately excluded from this — an hourly
    movement cap is the airport authority's limit, and no amount of money creates runway
    capacity, so slots keep hard-blocking.
    """
    from engine.gates import assert_player_gate_capacity_for_new_segments as _assert_gates
    from engine.slots import assert_player_slots_for_new_segments

    if not ferry:
        _assert_gates(
            int(game_week),
            segs,
            replace_tails=replace_tails,
            shortfall_mode=gate_shortfall_mode,
        )
    assert_player_slots_for_new_segments(int(game_week), segs, ferry=ferry)


def _assert_incremental_spawn_airport_limits(game_week: int, segs: list) -> None:
    """Gate/slot check for weekly spawn adds (DB rows kept; no tail exclusion).

    The published schedule is already committed and the week is already running, so a
    gate shortage here bills rather than blocks.
    """
    new_segs = _filter_new_spawn_segments(int(game_week), segs)
    if not new_segs:
        return
    _assert_new_segment_airport_limits(
        int(game_week), new_segs, replace_tails=False, gate_shortfall_mode=True
    )


