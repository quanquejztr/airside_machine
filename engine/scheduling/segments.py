"""Week spawn and passenger accounting (generation layer)."""

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


def dedupe_duplicate_spawn_segments(game_week: int) -> int:
    """
    Remove duplicate live segments for the same tail/week/route/departure.

    Keeps the earliest segment_id per key. Returns rows deleted.
    """
    gw = int(game_week)
    rows = db.fetch_all(
        """
        SELECT segment_id, tail_number, route_id, scheduled_dep_game_hour
        FROM flight_segments
        WHERE game_week = ? AND status != 'CANCELLED'
        ORDER BY tail_number, route_id, scheduled_dep_game_hour, segment_id
        """,
        (gw,),
    )
    seen: set[tuple[str, str, float]] = set()
    delete_ids: list[str] = []
    for r in rows or []:
        key = (
            str(r["tail_number"]),
            str(r["route_id"]),
            round(float(r["scheduled_dep_game_hour"] or 0.0), 3),
        )
        if key in seen:
            delete_ids.append(str(r["segment_id"]))
        else:
            seen.add(key)
    for sid in delete_ids:
        db.execute("DELETE FROM flight_segments WHERE segment_id = ?", (sid,))
    return len(delete_ids)


def reset_operational_schedule_for_new_calendar_week(new_calendar_week: int) -> int:
    """
    At the start of a new game week: snap all unflown legs whose **published** plan
    starts in this week or later back to baseline (clears delay cascades and MTT
    slippage from the prior week). Does not change baseline_* or LANDED/IN_AIR history.
    """
    from engine.scheduling.time_helpers import _day_of_week_label, hhmm_from_absolute_game_hour, week_base_hours
    if new_calendar_week < 1:
        return 0
    wb = week_base_hours(int(new_calendar_week))
    rows = db.fetch_all(
        """
        SELECT segment_id, baseline_dep_game_hour, baseline_arr_game_hour
        FROM flight_segments
        WHERE status IN ('SCHEDULED', 'DELAYED', 'HOLDING')
          AND baseline_dep_game_hour IS NOT NULL
          AND baseline_arr_game_hour IS NOT NULL
          AND baseline_dep_game_hour >= ?
        """,
        (wb,),
    )
    n = 0
    for r in rows:
        sid = str(r["segment_id"])
        bdep = float(r["baseline_dep_game_hour"])
        barr = float(r["baseline_arr_game_hour"])
        gwd = int(bdep // 168.0) + 1
        db.execute(
            """
            UPDATE flight_segments
            SET scheduled_dep_game_hour = ?,
                scheduled_arr_game_hour = ?,
                scheduled_dep_time = ?,
                scheduled_arr_time = ?,
                delay_minutes = 0,
                status = 'SCHEDULED',
                game_week = ?,
                day_of_week = ?
            WHERE segment_id = ?
            """,
            (
                bdep,
                barr,
                hhmm_from_absolute_game_hour(bdep),
                hhmm_from_absolute_game_hour(barr),
                gwd,
                _day_of_week_label(gwd, bdep),
                sid,
            ),
        )
        n += 1
    # Hard gate enforcement for the new week: cancel any legacy over-capacity segments.
    try:
        from engine.gates import enforce_player_gate_capacity_for_week

        enforce_player_gate_capacity_for_week(int(new_calendar_week))
    except Exception:
        pass
    return n


def remove_superseded_scheduled_segments_for_tail(tail_number: str) -> None:
    """
    Drop not-yet-departed segment rows when the player replaces or clears the plan.
    IN_AIR / LANDED / DIVERTED legs are left for cancel_rotation to handle.
    """
    db.execute(
        """
        DELETE FROM flight_segments
        WHERE tail_number = ?
          AND status IN ('SCHEDULED', 'DELAYED', 'HOLDING')
        """,
        (tail_number,),
    )


def _spawn_simple_rotation_legs(tail_number, target_game_week, legs, callsign):
    """Spawn quick-rotation template (list of leg dicts). Returns (inserted_count, touched_tails)."""
    from engine.scheduling.shared import (
        _assert_incremental_spawn_airport_limits,
        get_financial_constant,
        segment_exists_for_spawn,
    )
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour, week_base_hours
    inserted = 0
    tails_touched = set()
    w_base = week_base_hours(target_game_week)

    # Concurrent-gates capacity check for all legs being spawned (auctioned airports only).
    planned = []
    for leg in legs:
        route_id = leg.get("route_id")
        if not route_id:
            continue
        rt = get_route(route_id)
        if not rt:
            continue
        try:
            dep_off = float(leg["dep_offset_hours"])
            fh = float(leg["flight_duration_hours"])
        except (KeyError, TypeError, ValueError):
            continue
        dep_abs = w_base + dep_off
        leg_turn = leg.get("turn_minutes")
        planned.append(
            {
                "tail_number": tail_number,
                "route_id": str(route_id),
                "origin_iata": str(rt["origin_iata"]).upper(),
                "dest_iata": str(rt["dest_iata"]).upper(),
                "dep_abs": float(dep_abs),
                "arr_abs": float(dep_abs) + float(fh),
                "turn_minutes": int(round(float(leg_turn))) if leg_turn is not None else None,
            }
        )
    if planned:
        _assert_incremental_spawn_airport_limits(int(target_game_week), planned)

    template_dirty = False
    for i, leg in enumerate(legs):
        route_id = leg.get("route_id")
        if not route_id or not get_route(route_id):
            continue
        try:
            dep_off = float(leg["dep_offset_hours"])
            fh = float(leg["flight_duration_hours"])
        except (KeyError, TypeError, ValueError):
            continue

        dep_abs = w_base + dep_off
        arr_abs = dep_abs + fh

        if segment_exists_for_spawn(tail_number, target_game_week, route_id, dep_abs):
            continue

        segment_id = f"{tail_number}-{route_id}-W{target_game_week}-{uuid.uuid4().hex[:12]}"

        if db.fetch_one("SELECT 1 FROM flight_segments WHERE segment_id = ?", (segment_id,)):
            continue

        from engine.scheduling.flight_numbers import allocate_flight_number, normalize_flight_number

        pref = normalize_flight_number(str(leg.get("flight_number") or ""))
        leg_turn = int(round(float(leg.get("turn_minutes") or get_financial_constant("mtt_minutes", 30))))
        try:
            flight_number = allocate_flight_number(
                route_id,
                [(dep_abs, arr_abs)],
                target_game_week,
                preferred=pref or None,
                callsign=callsign,
                remember=True,
            )
        except ValueError:
            # Prefer keeping template FN when spawn collides (e.g. concurrent same FN);
            # fall back to a fresh allocation without preferred.
            flight_number = allocate_flight_number(
                route_id,
                [(dep_abs, arr_abs)],
                target_game_week,
                preferred=None,
                callsign=callsign,
                remember=True,
            )
        if not pref or pref != flight_number:
            leg["flight_number"] = flight_number
            template_dirty = True
        else:
            leg["flight_number"] = flight_number

        dep_time_str = hhmm_from_absolute_game_hour(dep_abs)
        arr_time_str = hhmm_from_absolute_game_hour(arr_abs)
        day_of_week = "MON"
        rt = get_route(route_id)
        oi = rt["origin_iata"] if rt else None
        di = rt["dest_iata"] if rt else None

        db.execute(
            """
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
            """,
            (
                segment_id,
                target_game_week,
                day_of_week,
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
                leg_turn,
            ),
        )
        inserted += 1
        tails_touched.add(tail_number)

    if template_dirty:
        try:
            import json as _json

            row = db.fetch_one(
                "SELECT legs_json FROM weekly_rotations WHERE tail_number = ?",
                (tail_number,),
            )
            if row and row["legs_json"]:
                raw = _json.loads(row["legs_json"])
                if isinstance(raw, list):
                    db.execute(
                        """
                        UPDATE weekly_rotations SET legs_json = ? WHERE tail_number = ?
                        """,
                        (_json.dumps(legs), tail_number),
                    )
                elif isinstance(raw, dict) and raw.get("mode") == "quick":
                    raw["legs"] = legs
                    db.execute(
                        """
                        UPDATE weekly_rotations SET legs_json = ? WHERE tail_number = ?
                        """,
                        (_json.dumps(raw), tail_number),
                    )
        except Exception:
            pass

    return inserted, tails_touched


def _spawn_detailed_template_week(tail_number, target_game_week, items):
    """Spawn detailed template segments for a week. Returns (inserted_count, touched_tails)."""
    from engine.scheduling.shared import (
        _assert_incremental_spawn_airport_limits,
        get_financial_constant,
        segment_exists_for_spawn,
    )
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour, week_base_hours
    inserted = 0
    tails_touched = set()
    aircraft = get_fleet_aircraft(tail_number)
    if not aircraft:
        return 0, tails_touched

    aircraft_type = db.fetch_one(
        "SELECT * FROM aircraft_types WHERE type_id = ?",
        (aircraft["type_id"],),
    )
    if not aircraft_type:
        return 0, tails_touched

    w_base = week_base_hours(target_game_week)
    cruise_speed_kts = aircraft_type["cruise_speed_kts"]

    # Concurrent-gates capacity check for all segments in this template (auctioned airports only).
    planned_all = []
    for item in items:
        route_id = item.get("route_id")
        route = get_route(route_id) if route_id else None
        if not route:
            continue
        flight_hours = route["distance_nm"] / cruise_speed_kts
        departure_time = item.get("departure_time") or "08:00"
        try:
            dep_hours, dep_mins = map(int, departure_time.split(":"))
            hod = float(dep_hours) + float(dep_mins) / 60.0
        except (ValueError, TypeError):
            continue
        days_of_week = item.get("days_of_week")
        if days_of_week == "DAILY":
            operating_days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        else:
            try:
                operating_days = json.loads(days_of_week)
            except (json.JSONDecodeError, TypeError):
                operating_days = ["MON"]
        for day in operating_days:
            if day not in DAY_START_HOURS:
                continue
            dep_abs = w_base + DAY_START_HOURS[day] + hod
            item_turn = item.get("turn_minutes")
            planned_all.append(
                {
                    "tail_number": tail_number,
                    "route_id": str(route_id),
                    "origin_iata": str(route["origin_iata"]).upper(),
                    "dest_iata": str(route["dest_iata"]).upper(),
                    "dep_abs": float(dep_abs),
                    "arr_abs": float(dep_abs) + float(flight_hours),
                    "turn_minutes": int(round(float(item_turn))) if item_turn is not None else None,
                }
            )
    if planned_all:
        _assert_incremental_spawn_airport_limits(int(target_game_week), planned_all)

    for item in items:
        route_id = item.get("route_id")
        route = get_route(route_id) if route_id else None
        if not route:
            continue
        flight_hours = route["distance_nm"] / cruise_speed_kts
        departure_time = item.get("departure_time") or "08:00"
        try:
            dep_hours, dep_mins = map(int, departure_time.split(":"))
            hod = float(dep_hours) + float(dep_mins) / 60.0
        except (ValueError, TypeError):
            continue

        days_of_week = item.get("days_of_week")
        if days_of_week == "DAILY":
            operating_days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        else:
            try:
                operating_days = json.loads(days_of_week)
            except (json.JSONDecodeError, TypeError):
                operating_days = ["MON"]

        flight_number = item.get("flight_number") or "FL001"
        leg_turn = int(round(float(item.get("turn_minutes") or get_financial_constant("mtt_minutes", 30))))

        for day in operating_days:
            if day not in DAY_START_HOURS:
                continue
            hours_into_week = DAY_START_HOURS[day] + hod
            dep_abs = w_base + hours_into_week
            arr_abs = dep_abs + flight_hours

            dup = db.fetch_one(
                """
                SELECT 1 FROM flight_segments
                WHERE tail_number = ? AND game_week = ? AND route_id = ?
                AND ABS(scheduled_dep_game_hour - ?) < 0.001
                AND status != 'CANCELLED'
                """,
                (tail_number, target_game_week, route_id, dep_abs),
            )
            if dup:
                continue

            dep_time_str = hhmm_from_absolute_game_hour(dep_abs)
            arr_time_str = hhmm_from_absolute_game_hour(arr_abs)
            segment_id = f"{tail_number}-{route_id}-W{target_game_week}-{day}-{uuid.uuid4().hex[:8]}"
            oi = route["origin_iata"] if route else None
            di = route["dest_iata"] if route else None

            db.execute(
                """
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
                """,
                (
                    segment_id,
                    target_game_week,
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
                    leg_turn,
                ),
            )
            inserted += 1
            tails_touched.add(tail_number)

    return inserted, tails_touched


def _turn_by_route_from_legs(legs_conf: list, default_turn: int) -> dict[str, int]:
    turn_by_route: dict[str, int] = {}
    for leg in legs_conf or []:
        rid = leg.get("route_id")
        if not rid:
            continue
        tm = leg.get("turn_minutes")
        if tm is not None and str(tm).strip() != "":
            turn_by_route[str(rid)] = int(round(float(tm)))
    return turn_by_route


def _gate_segments_from_planned(
    tail_number: str,
    planned: list,
    turn_by_route: dict[str, int],
    default_turn: int,
) -> list[dict]:
    return [
        {
            "tail_number": tail_number,
            "origin_iata": p["origin_iata"],
            "dest_iata": p["dest_iata"],
            "dep_abs": p["dep_abs"],
            "arr_abs": p["arr_abs"],
            "route_id": str(p["route_id"]),
            "turn_minutes": turn_by_route.get(str(p["route_id"]), default_turn),
        }
        for p in (planned or [])
    ]


def _plan_detailed_chained_chain(tail_number, target_game_week, chain: dict):
    """Plan chained segments for gate checks / spawn. Returns (planned, turn_by_route) or None."""
    from engine.scheduling.rotation import _operating_days_list, _plan_chained_detailed_segments, normalize_turn_minutes
    from engine.scheduling.shared import get_financial_constant

    legs_conf = (chain or {}).get("legs") or []
    if not legs_conf:
        return None

    aircraft = get_fleet_aircraft(tail_number)
    if not aircraft:
        return None

    aircraft_type = db.fetch_one(
        "SELECT * FROM aircraft_types WHERE type_id = ?",
        (aircraft["type_id"],),
    )
    if not aircraft_type:
        return None

    routes_ordered = []
    flight_numbers = []
    for leg in legs_conf:
        rid = leg.get("route_id")
        fn = leg.get("flight_number") or "FL001"
        r = get_route(rid) if rid else None
        if not r:
            return None
        routes_ordered.append(r)
        flight_numbers.append(fn)

    leg_turns = [leg.get("turn_minutes") for leg in legs_conf]
    days_str = chain.get("days_of_week") or "DAILY"
    first_dep = chain.get("first_departure_time") or "08:00"
    mtt_hours = float(get_financial_constant("mtt_minutes", 30)) / 60.0
    cruise_speed_kts = aircraft_type["cruise_speed_kts"]
    default_turn = int(round(float(get_financial_constant("mtt_minutes", 30))))

    operating_days = _operating_days_list(days_str)
    try:
        planned, _, _ = _plan_chained_detailed_segments(
            target_game_week,
            operating_days,
            first_dep,
            routes_ordered,
            flight_numbers,
            cruise_speed_kts,
            mtt_hours,
            turn_hours=[m / 60.0 for m in normalize_turn_minutes(
                [l.get("route_id") for l in legs_conf],
                None if all(t is None for t in leg_turns) else leg_turns,
            )],
        )
    except ValueError:
        return None

    turn_by_route = _turn_by_route_from_legs(legs_conf, default_turn)
    return planned, turn_by_route


def _spawn_one_detailed_chained_chain(tail_number, target_game_week, chain: dict, *, skip_gate_assert: bool = False):
    """Insert segments for one chained block (used by weekly spawn)."""
    from engine.scheduling.shared import _assert_incremental_spawn_airport_limits, get_financial_constant
    from engine.scheduling.time_helpers import hhmm_from_absolute_game_hour
    inserted = 0
    tails_touched = set()
    legs_conf = (chain or {}).get("legs") or []
    if not legs_conf:
        return 0, tails_touched

    default_turn = int(round(float(get_financial_constant("mtt_minutes", 30))))
    planned_bundle = _plan_detailed_chained_chain(tail_number, target_game_week, chain)
    if not planned_bundle:
        return 0, tails_touched
    planned, turn_by_route = planned_bundle

    if not skip_gate_assert:
        gate_segs = _gate_segments_from_planned(tail_number, planned, turn_by_route, default_turn)
        _assert_incremental_spawn_airport_limits(int(target_game_week), gate_segs)

    for p in planned:
        # A chain anchored late in the week finishes in the next one; tag each leg with
        # the week its own departure falls in, not the week the chain was spawned for.
        leg_week = int(p.get("game_week") or target_game_week)
        dup = db.fetch_one(
            """
            SELECT 1 FROM flight_segments
            WHERE tail_number = ? AND game_week = ? AND route_id = ?
            AND ABS(scheduled_dep_game_hour - ?) < 0.001
            AND status != 'CANCELLED'
            """,
            (tail_number, leg_week, p["route_id"], p["dep_abs"]),
        )
        if dup:
            continue

        leg_turn = turn_by_route.get(str(p["route_id"]), default_turn)

        dep_time_str = hhmm_from_absolute_game_hour(p["dep_abs"])
        arr_time_str = hhmm_from_absolute_game_hour(p["arr_abs"])
        segment_id = f"{tail_number}-{p['route_id']}-W{leg_week}-{p['day']}-{uuid.uuid4().hex[:8]}"

        db.execute(
            """
            INSERT INTO flight_segments (
                segment_id, game_week, day_of_week, tail_number, route_id,
                origin_iata, dest_iata, flight_number,
                scheduled_dep_time, scheduled_dep_game_hour,
                scheduled_arr_time, scheduled_arr_game_hour,
                baseline_dep_game_hour, baseline_arr_game_hour,
                turn_minutes,
                status, pax_business, pax_leisure, revenue_gross,
                excise_tax, segment_fee, security_fee, pfc_fee,
                landing_fee, gate_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', 0, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (
                segment_id,
                leg_week,
                p["day"],
                tail_number,
                p["route_id"],
                p.get("origin_iata"),
                p.get("dest_iata"),
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
        inserted += 1
        tails_touched.add(tail_number)

    return inserted, tails_touched


def _spawn_detailed_chained_template_week(tail_number, target_game_week, raw_blob: dict):
    """Spawn segments for mode=detailed_chained: every stored chain, plus any standalone
    detailed lines stacked onto the same tail.

    The chained blob carries both `chains` and `items` so a player can add a single
    flight to an aircraft that already flies a chained rotation. Spawning only the
    chains here would let those stacked lines fly for the week they were created and
    then silently disappear at the next weekly respawn.
    """
    from engine.scheduling.rotation import _detailed_chained_chains_list_from_blob
    from engine.scheduling.shared import _assert_incremental_spawn_airport_limits, get_financial_constant

    chains = _detailed_chained_chains_list_from_blob(raw_blob or {})
    default_turn = int(round(float(get_financial_constant("mtt_minutes", 30))))
    planned_all: list[dict] = []
    for chain in chains:
        planned_bundle = _plan_detailed_chained_chain(tail_number, target_game_week, chain)
        if not planned_bundle:
            continue
        planned, turn_by_route = planned_bundle
        planned_all.extend(
            _gate_segments_from_planned(tail_number, planned, turn_by_route, default_turn)
        )
    if planned_all:
        _assert_incremental_spawn_airport_limits(int(target_game_week), planned_all)

    inserted = 0
    tails_touched = set()
    for chain in chains:
        n, ts = _spawn_one_detailed_chained_chain(
            tail_number, target_game_week, chain, skip_gate_assert=True
        )
        inserted += n
        tails_touched |= ts

    # Standalone detailed lines stacked onto this tail alongside its chains.
    items = list((raw_blob or {}).get("items") or [])
    if items:
        n, ts = _spawn_detailed_template_week(tail_number, target_game_week, items)
        inserted += n
        tails_touched |= ts

    return inserted, tails_touched


def spawn_rotation_segments_for_week(target_game_week: int) -> dict:
    """
    Create flight_segments for target_game_week from weekly_rotations templates.
    Supports quick rotations (JSON array) and detailed schedules (mode=detailed).
    Idempotent: skips existing segments.
    """
    if target_game_week < 1:
        return {"inserted": 0, "tails": [], "deduped": 0}

    deduped = dedupe_duplicate_spawn_segments(int(target_game_week))

    rows = db.fetch_all("SELECT tail_number, legs_json FROM weekly_rotations")
    if not rows:
        return {"inserted": 0, "tails": []}

    airline = db.fetch_one("SELECT callsign FROM airline WHERE id = 1")
    callsign = airline["callsign"] if airline else "FL"

    inserted_total = 0
    all_tails = set()

    def _notify_spawn_skipped(tail: str, why: str) -> None:
        try:
            db.execute(
                """
                INSERT INTO player_notifications (notification_id, game_week, type, route_pair_id, body, read)
                VALUES (?, ?, 'GATE_CAPACITY', NULL, ?, 0)
                """,
                (str(uuid.uuid4()), int(target_game_week), f"Schedule spawn skipped for {tail}: {why}"),
            )
        except Exception:
            pass

    for row in rows:
        tail_number = row["tail_number"]
        try:
            raw = json.loads(row["legs_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        if not db.fetch_one("SELECT 1 FROM fleet WHERE tail_number = ?", (tail_number,)):
            continue

        try:
            if isinstance(raw, dict) and raw.get("mode") == "detailed_chained":
                n, tset = _spawn_detailed_chained_template_week(tail_number, target_game_week, raw)
            elif isinstance(raw, dict) and raw.get("mode") == "detailed":
                n, tset = _spawn_detailed_template_week(tail_number, target_game_week, raw.get("items") or [])
            elif isinstance(raw, dict) and raw.get("mode") == "quick":
                n, tset = _spawn_simple_rotation_legs(
                    tail_number, target_game_week, raw.get("legs") or [], callsign
                )
            elif isinstance(raw, list):
                n, tset = _spawn_simple_rotation_legs(tail_number, target_game_week, raw, callsign)
            else:
                continue
        except Exception as e:
            _notify_spawn_skipped(tail_number, str(e))
            continue

        inserted_total += n
        all_tails |= tset

    for tail in all_tails:
        db.execute(
            """
            UPDATE fleet
            SET status = 'SCHEDULED'
            WHERE tail_number = ?
            """,
            (tail,),
        )

    return {"inserted": inserted_total, "tails": sorted(all_tails), "deduped": deduped}


def _per_leg_demand_from_weekly_pool(
    route_id: str,
    game_week: int,
    segment_id: str,
    leisure_weekly: int,
    business_weekly: int,
) -> tuple[int, int]:
    """
    Remaining weekly demand after legs that already boarded, assigned to this
    departure (compute_revenue seat-caps). Matches sequential remaining-pool fill.
    """
    carried = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(pax_leisure), 0) AS lei,
            COALESCE(SUM(pax_business), 0) AS bus
        FROM flight_segments
        WHERE route_id = ?
          AND game_week = ?
          AND status IN ('IN_AIR', 'LANDED', 'DIVERTED')
        """,
        (route_id, game_week),
    )
    lei_left = max(0, int(leisure_weekly) - int(carried["lei"] or 0) if carried else int(leisure_weekly))
    bus_left = max(0, int(business_weekly) - int(carried["bus"] or 0) if carried else int(business_weekly))
    return lei_left, bus_left


def route_weekly_passenger_accounting(
    route_id: str, game_week: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Weekly modeled demand vs legs on this route: pool split per `on_departure`,
    seat-capped absorption on not-yet-departed legs, actual pax on airborne/landed legs,
    and remaining market.
    """
    route = get_route(route_id)
    if not route:
        return None
    gs = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
    if not gs:
        return None
    gw = int(game_week) if game_week is not None else int(gs["game_week"])
    month = int(gs["current_month"] or 1)
    rid = str(route["route_id"]).upper().strip()
    demand = compute_demand(rid, gw, month)
    weekly_business = int(demand.get("business_pax") or 0)
    weekly_leisure = int(demand.get("leisure_pax") or 0)

    # Cabin demand split (market willingness), consistent with engine.demand.compute_revenue() split.
    try:
        row = db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'demand_share_premium_economy'"
        )
        pe_share = float(row["value"]) if row else 0.30
    except Exception:
        pe_share = 0.30
    try:
        row = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'demand_share_first'")
        first_share = float(row["value"]) if row else 0.25
    except Exception:
        first_share = 0.25
    pe_share = max(0.0, min(0.9, float(pe_share)))
    first_share = max(0.0, min(0.9, float(first_share)))

    weekly_w = int(round(float(weekly_leisure) * pe_share))
    weekly_y = int(max(0, weekly_leisure - weekly_w))
    weekly_f = int(round(float(weekly_business) * first_share))
    weekly_j = int(max(0, weekly_business - weekly_f))
    rows = db.fetch_all(
        """
        SELECT segment_id, tail_number, status, pax_business, pax_leisure,
               pax_economy, pax_premium_economy, pax_business_cabin, pax_first
        FROM flight_segments
        WHERE route_id = ? AND game_week = ? AND status != 'CANCELLED'
        ORDER BY scheduled_dep_game_hour ASC, segment_id ASC
        """,
        (rid, gw),
    )
    segments_total = len(rows)
    segments_scheduled = 0
    segments_delayed = 0
    segments_in_air = 0
    segments_landed = 0
    carried_business = 0
    carried_leisure = 0
    carried_y = 0
    carried_w = 0
    carried_j = 0
    carried_f = 0

    # "Reserved scheduled" here is a planning assumption: scheduled legs will be full up to remaining demand.
    reserved_scheduled_business = 0
    reserved_scheduled_leisure = 0
    reserved_y = 0
    reserved_w = 0
    reserved_j = 0
    reserved_f = 0
    for r in rows:
        st = str(r["status"] or "")
        if st == "SCHEDULED":
            segments_scheduled += 1
        elif st == "DELAYED":
            segments_delayed += 1
        elif st in ("IN_AIR", "HOLDING", "DIVERTED"):
            segments_in_air += 1
        elif st == "LANDED":
            segments_landed += 1
        if st in ("IN_AIR", "LANDED", "DIVERTED", "HOLDING"):
            # Prefer cabin-specific pax if present; fall back to legacy B/L buckets.
            y = int(r["pax_economy"] or 0)
            w = int(r["pax_premium_economy"] or 0)
            j = int(r["pax_business_cabin"] or 0)
            f = int(r["pax_first"] or 0)
            if (y + w + j + f) <= 0:
                # Legacy: treat leisure as economy and business as business.
                y = int(r["pax_leisure"] or 0)
                j = int(r["pax_business"] or 0)
                w = 0
                f = 0
            carried_y += y
            carried_w += w
            carried_j += j
            carried_f += f
            carried_leisure += y + w
            carried_business += j + f

    # Remaining demand after what already flew (or is in air).
    rem_y = max(0, weekly_y - carried_y)
    rem_w = max(0, weekly_w - carried_w)
    rem_j = max(0, weekly_j - carried_j)
    rem_f = max(0, weekly_f - carried_f)

    # For not-yet-departed legs, assume they will be "full" up to remaining demand
    # (i.e., subtract cabin seat capacity rather than splitting demand evenly per leg).
    for r in rows:
        st = str(r["status"] or "")
        if st not in ("SCHEDULED", "DELAYED"):
            continue
        cabin = get_cabin_config(r["tail_number"]) or {
            "seats_economy": 0,
            "seats_premium_economy": 0,
            "seats_business": 0,
            "seats_first": 0,
        }
        cap_y = int(cabin.get("seats_economy") or 0)
        cap_w = int(cabin.get("seats_premium_economy") or 0)
        cap_j = int(cabin.get("seats_business") or 0)
        cap_f = int(cabin.get("seats_first") or 0)

        take_y = min(rem_y, cap_y)
        take_w = min(rem_w, cap_w)
        take_j = min(rem_j, cap_j)
        take_f = min(rem_f, cap_f)

        rem_y -= take_y
        rem_w -= take_w
        rem_j -= take_j
        rem_f -= take_f

        reserved_y += take_y
        reserved_w += take_w
        reserved_j += take_j
        reserved_f += take_f

    reserved_scheduled_leisure = int(reserved_y + reserved_w)
    reserved_scheduled_business = int(reserved_j + reserved_f)

    committed_business = int(carried_business + reserved_scheduled_business)
    committed_leisure = int(carried_leisure + reserved_scheduled_leisure)
    remaining_business = int(max(0, rem_j + rem_f))
    remaining_leisure = int(max(0, rem_y + rem_w))
    return {
        "game_week": gw,
        "weekly_business": weekly_business,
        "weekly_leisure": weekly_leisure,
        "carried_business": carried_business,
        "carried_leisure": carried_leisure,
        "reserved_scheduled_business": reserved_scheduled_business,
        "reserved_scheduled_leisure": reserved_scheduled_leisure,
        "committed_business": committed_business,
        "committed_leisure": committed_leisure,
        "remaining_business": remaining_business,
        "remaining_leisure": remaining_leisure,
        "weekly_cabin_demand": {"Y": weekly_y, "W": weekly_w, "J": weekly_j, "F": weekly_f},
        "remaining_cabin_demand": {"Y": rem_y, "W": rem_w, "J": rem_j, "F": rem_f},
        "segments_total": segments_total,
        "segments_scheduled": segments_scheduled,
        "segments_delayed": segments_delayed,
        "segments_in_air": segments_in_air,
        "segments_landed": segments_landed,
    }


