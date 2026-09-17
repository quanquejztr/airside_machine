"""
Phase 11 (revised): airport gate-use auctions.

- Airports with score >= gate_auction_score_threshold are auctioned.
- Gate units are concurrent stands: peak simultaneous occupancy must fit allocated units.
  One tail visit (arrive → turn → depart) uses one stand for [arr, dep + MTT).
- Ascending auction model: bids specify (units_requested, price_per_unit). The auction's current
  price is the max bid rounded up to gate_price_step.
- Resolution at settlement: allocate units to highest price bidders until supply, deduct cash, update allocations.
- Utilization = gate-hours / (gate_units * 168). Player gate-hours use per-leg turn_minutes
  (the turnaround you set when scheduling) merged into [arr, dep + turn) windows. AI still
  uses the global MTT default. Realistic util values are small at default MTT (hub ~0.06).
"""

from __future__ import annotations

import heapq
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

from db import db


def _notify_player(game_week: int, type_: str, body: str) -> None:
    try:
        db.execute(
            """
            INSERT INTO player_notifications (notification_id, game_week, type, route_pair_id, body, read)
            VALUES (?, ?, ?, NULL, ?, 0)
            """,
            (str(uuid.uuid4()), int(game_week), str(type_), str(body)),
        )
    except Exception:
        pass


def _fc(key: str, default: float) -> float:
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    if not row:
        return float(default)
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return float(default)


def gate_score_threshold() -> int:
    return int(_fc("gate_auction_score_threshold", 1080000))


def gate_utilization_threshold() -> float:
    return float(_fc("gate_utilization_threshold", 0.80))

def gate_utilization_threshold_hub() -> float:
    return float(_fc("gate_utilization_threshold_hub", 0.06))


def gate_utilization_threshold_nonhub() -> float:
    return float(_fc("gate_utilization_threshold_nonhub", 0.02))


def gate_utilization_grace_weeks() -> int:
    return int(_fc("gate_utilization_grace_weeks", 3))


def gate_min_price_per_unit() -> float:
    return float(_fc("gate_min_price_per_unit", 5000.0))


def gate_price_step() -> float:
    return float(_fc("gate_price_step", 1000.0))


def current_game_week() -> int:
    from engine.scheduling.time_helpers import calendar_game_week_from_state

    return calendar_game_week_from_state()


def is_auctioned_airport(iata: str) -> bool:
    """Gates are sold at auction wherever the airport is slot-coordinated or facilitated.

    Driven by `slot_level` (IATA levels 2 and 3), not by `score`. Score is a size number
    and was being asked to answer a different question — whether capacity is scarce —
    which it did badly: Boston at 87M annual passengers fell below the threshold while
    far quieter airports sat above it. `score` still ranks airports for the AI and hub
    picker; it no longer decides auctions.

    Falls back to the old score rule only when slot_level is missing, so a save that has
    not been reseeded yet behaves exactly as before.
    """
    ap = db.fetch_one(
        "SELECT score, slot_level FROM airports WHERE iata = ?", (iata.strip().upper(),)
    )
    if not ap:
        return False
    try:
        level = int(ap["slot_level"] or 0)
    except (TypeError, ValueError, IndexError, KeyError):
        level = 0
    if level > 0:
        return level >= 2
    return float(ap["score"] or 0) >= float(gate_score_threshold())


def total_gate_units(iata: str) -> int:
    ap = db.fetch_one("SELECT gate_count FROM airports WHERE iata = ?", (iata.strip().upper(),))
    if not ap:
        return 0
    return max(0, int(ap["gate_count"] or 0))


def _round_up_step(x: float, step: float) -> float:
    if step <= 0:
        return float(x)
    return float(math.ceil(float(x) / step) * step)


def _default_turn_minutes() -> float:
    """Fallback turnaround when a segment has no stored turn_minutes."""
    try:
        v = db.get_financial_constant("mtt_minutes")
        return float(v or 30)
    except Exception:
        return 30.0


def _mtt_hours() -> float:
    """Default gate-occupancy extension in hours (legacy / AI fallback)."""
    return _default_turn_minutes() / 60.0


def _turn_hours(minutes: Optional[float]) -> float:
    m = _default_turn_minutes() if minutes is None else float(minutes)
    return max(0.0, m) / 60.0


def _allocated_gates(iata: str, holder_id: str) -> int:
    """Stands this holder controls at an airport.

    SUM, not a single row: duplicate ACTIVE rows for one holder used to be possible, and a
    `fetch_one` then reported the first of them, crediting a two-stand holder with one.
    A unique index now prevents duplicates and a migration merged the existing ones, but
    summing is the honest read and costs nothing.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(gate_units), 0) AS n
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = ? AND status='ACTIVE'
        """,
        (iata.upper().strip(), str(holder_id)),
    )
    return int(row["n"] or 0) if row else 0


def _visit_intervals_for_tail_events(
    events: list[tuple[float, str, float]],
) -> list[tuple[float, float]]:
    """
    One aircraft, one stand: merge arrival + departure at the same airport into a single
    occupancy window [arr, dep).

    The stand is held from touchdown to pushback, and nothing after. The turnaround is
    already baked into the schedule — the rotation planner places each departure at
    arrival + turn_minutes — so the window from arrival to departure *is* the turn. The
    old model used [arr, dep + MTT), which added the turnaround a second time and left
    every aircraft holding its stand for 45 minutes after it had taken off. That made
    each visit twice its true length and manufactured collisions between tails that
    never actually share a stand.

    Each event is (time, 'A'|'D', mtt_hours). An orphan arrival — one whose departure
    falls outside the window being measured — holds for its own mtt as a minimum. An
    orphan departure is the mirror case: the aircraft was already parked, so the stand
    was occupied for the turn *before* pushback, [dep - mtt, dep).
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda x: (float(x[0]), 0 if x[1] == "A" else 1))
    out: list[tuple[float, float]] = []
    open_arr: Optional[float] = None
    open_mtt = _mtt_hours()
    for t, kind, mtt in ordered:
        t = float(t)
        mtt = float(mtt)
        if kind == "A":
            if open_arr is not None:
                out.append((open_arr, open_arr + open_mtt))
            open_arr = t
            open_mtt = mtt
        else:
            if open_arr is not None and t + 1e-9 >= open_arr:
                out.append((open_arr, t))
                open_arr = None
            else:
                out.append((t - mtt, t))
    if open_arr is not None:
        out.append((open_arr, open_arr + open_mtt))
    return out


def _visit_intervals_for_tail(mtt: float, events: list[tuple[float, str]]) -> list[tuple[float, float]]:
    """Legacy helper: uniform mtt for all events on one tail."""
    tagged = [(float(t), str(k), float(mtt)) for t, k in events]
    return _visit_intervals_for_tail_events(tagged)


def visit_intervals_for_tail(
    mtt: float, events: list[tuple[float, str]]
) -> list[tuple[float, float]]:
    """Merge arr/dep events for one tail into stand-occupancy windows."""
    return _visit_intervals_for_tail(mtt, events)


def gate_intervals_from_tail_events(
    by_tail: dict[str, list[tuple[float, str]]],
    mtt: float | None = None,
) -> list[tuple[float, float]]:
    m = _mtt_hours() if mtt is None else float(mtt)
    out: list[tuple[float, float]] = []
    for events in by_tail.values():
        tagged = [(float(t), str(k), m) for t, k in events]
        out.extend(_visit_intervals_for_tail_events(tagged))
    return out


def _ai_cycle_tail(competitor_id: str, flight_number: str) -> str:
    fn = str(flight_number).strip().upper()
    base = fn[:-1] if fn.endswith("R") else fn
    return f"{competitor_id}:{base}"


def _touch_tail_event(
    by_tail: dict[str, list[tuple[float, str]]],
    tail: str,
    airport: str,
    origin: str,
    dest: str,
    dep: float,
    arr: float,
) -> None:
    ap = str(airport).upper().strip()
    oi = str(origin).upper().strip()
    di = str(dest).upper().strip()
    t_tail = str(tail)
    if oi == ap:
        by_tail.setdefault(t_tail, []).append((float(dep), "D"))
    if di == ap:
        if not (float(arr) <= 0):
            by_tail.setdefault(t_tail, []).append((float(arr), "A"))


def gate_intervals_for_ai_at_airport(
    competitor_id: str,
    iata: str,
    game_week: int,
    *,
    extra_cycles: Optional[list[dict]] = None,
) -> list[tuple[float, float]]:
    """
    Gate-occupancy windows for one AI competitor at an airport.

    Round-trip cycles share a virtual tail (outbound + inbound flight numbers).
    """
    ap = str(iata).upper().strip()
    gw = int(game_week)
    cid = str(competitor_id)
    mtt = _mtt_hours()
    by_tail: dict[str, list[tuple[float, str]]] = {}

    for r in db.fetch_all(
        """
        SELECT scheduled_dep_game_hour AS dep_h, scheduled_arr_game_hour AS arr_h,
               origin_iata, dest_iata, flight_number
        FROM ai_flight_segments
        WHERE competitor_id = ? AND game_week = ? AND status != 'CANCELLED'
        """,
        (cid, gw),
    ):
        tail = _ai_cycle_tail(cid, str(r["flight_number"] or ""))
        _touch_tail_event(
            by_tail,
            tail,
            ap,
            str(r["origin_iata"] or ""),
            str(r["dest_iata"] or ""),
            float(r["dep_h"] or 0.0),
            float(r["arr_h"] or 0.0),
        )

    for cycle in extra_cycles or []:
        tail = str(cycle.get("tail") or f"{cid}:__NEW__")
        origin = str(cycle.get("origin_iata") or "").upper()
        dest = str(cycle.get("dest_iata") or "").upper()
        if "dep_out" in cycle:
            dep_out = float(cycle["dep_out"])
            arr_out = float(cycle["arr_out"])
            dep_in = float(cycle["dep_in"])
            arr_in = float(cycle["arr_in"])
            _touch_tail_event(by_tail, tail, ap, origin, dest, dep_out, arr_out)
            _touch_tail_event(by_tail, tail, ap, dest, origin, dep_in, arr_in)
        else:
            _touch_tail_event(
                by_tail,
                tail,
                ap,
                origin,
                dest,
                float(cycle.get("dep_abs") or 0.0),
                float(cycle.get("arr_abs") or 0.0),
            )

    return gate_intervals_from_tail_events(by_tail, mtt)


def player_gate_peak_at_airport(
    iata: str,
    game_week: int,
    *,
    extra_segments: Optional[list[dict]] = None,
    exclude_tails: Optional[set[str]] = None,
) -> int:
    return peak_concurrency(
        _gate_intervals_at_airport(
            iata,
            game_week,
            extra_segments=extra_segments,
            exclude_tails=exclude_tails,
        )
    )


def _gate_intervals_at_airport(
    iata: str,
    game_week: int,
    *,
    extra_segments: Optional[list[dict]] = None,
    exclude_tails: Optional[set[str]] = None,
) -> list[tuple[float, float]]:
    """
    Gate-occupancy windows at an airport for the player fleet.

    Each tail visit (arrive → turn → depart) uses one stand for [arr, dep + MTT).
    Peak concurrency across tails is what gate capacity must cover.
    """
    ap = iata.upper().strip()
    gw = int(game_week)
    default_m = _default_turn_minutes()
    # Stand occupancy is a function of absolute time, so select by the hours a flight
    # actually occupies this week rather than by its game_week label. Keying off the
    # label meant a leg mis-tagged to a neighbouring week — which every leg of a chain
    # running past the week boundary used to be — was invisible to that week's capacity
    # check, letting two aircraft share one stand with both weeks reporting a peak of 1.
    w0 = float(gw - 1) * 168.0
    w1 = w0 + 168.0
    rows = db.fetch_all(
        """
        SELECT
            fs.tail_number,
            fs.scheduled_dep_game_hour AS dep_h,
            fs.scheduled_arr_game_hour AS arr_h,
            COALESCE(fs.origin_iata, r.origin_iata) AS oi,
            COALESCE(fs.dest_iata,   r.dest_iata)   AS di,
            fs.turn_minutes AS turn_min
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.status != 'CANCELLED'
          AND (COALESCE(fs.origin_iata, r.origin_iata) = ? OR COALESCE(fs.dest_iata, r.dest_iata) = ?)
          AND (
                (fs.scheduled_dep_game_hour >= ? AND fs.scheduled_dep_game_hour < ?)
             OR (fs.scheduled_arr_game_hour >= ? AND fs.scheduled_arr_game_hour < ?)
          )
        """,
        (ap, ap, w0, w1, w0, w1),
    )
    by_tail: dict[str, list[tuple[float, str, float]]] = {}
    planned_tail = "__PLANNED__"

    def _touch(tail: str, t: float, kind: str, mtt_h: float) -> None:
        if t <= 0 and kind == "A":
            return
        # Count each event in the week that contains it, so a visit straddling the
        # boundary is not double-counted into both weeks.
        if not (w0 <= float(t) < w1):
            return
        by_tail.setdefault(str(tail), []).append((float(t), kind, float(mtt_h)))

    skip_tails = {str(t) for t in (exclude_tails or set()) if t}

    for r in rows:
        tail = str(r["tail_number"])
        if tail in skip_tails:
            continue
        dep = float(r["dep_h"] or 0.0)
        arr = float(r["arr_h"] or 0.0)
        oi = str(r["oi"] or "").upper()
        di = str(r["di"] or "").upper()
        turn_h = _turn_hours(r["turn_min"] if r["turn_min"] is not None else default_m)
        if oi == ap:
            _touch(tail, dep, "D", turn_h)
        if di == ap:
            _touch(tail, arr, "A", turn_h)

    for s in extra_segments or []:
        tail = str(s.get("tail_number") or planned_tail)
        dep = float(s.get("dep_abs") or 0.0)
        arr = float(s.get("arr_abs") or 0.0)
        oi = str(s.get("origin_iata") or "").upper().strip()
        di = str(s.get("dest_iata") or "").upper().strip()
        turn_h = _turn_hours(s.get("turn_minutes"))
        if oi == ap:
            _touch(tail, dep, "D", turn_h)
        if di == ap:
            _touch(tail, arr, "A", turn_h)

    out: list[tuple[float, float]] = []
    for _tail, events in by_tail.items():
        # Repeated segment rows for the same tail/time (spawn bug) must not multiply stands.
        deduped = list({(float(t), str(k), float(m)) for t, k, m in events})
        ordered = sorted(deduped, key=lambda x: (float(x[0]), 0 if x[1] == "A" else 1))
        out.extend(_visit_intervals_for_tail_events(ordered))
    return out


def _player_intervals_for_airport(iata: str, game_week: int) -> list[tuple[float, float]]:
    """Player gate-occupancy windows at an airport (one stand per tail visit)."""
    return _gate_intervals_at_airport(iata, game_week)


def _peak_concurrency(intervals: list[tuple[float, float]]) -> int:
    events: list[tuple[float, int]] = []
    for s, e in intervals:
        s2 = float(s)
        e2 = float(e)
        if e2 <= s2:
            continue
        events.append((s2, +1))
        events.append((e2, -1))
    # [start,end) semantics: when equal time, decrement before increment.
    events.sort(key=lambda x: (x[0], x[1]))
    cur = 0
    peak = 0
    for _t, d in events:
        cur += int(d)
        peak = max(peak, cur)
    return int(max(0, peak))


def peak_concurrency(intervals: list[tuple[float, float]]) -> int:
    """Peak simultaneous gate occupancy from half-open intervals [start, end)."""
    return _peak_concurrency(intervals)


def player_gate_free_times_by_unit(
    airport_iata: str,
    game_week: int,
    *,
    gate_units: int | None = None,
    max_times_per_gate: int = 6,
) -> list[list[float]]:
    """
    Heuristic planner for the UI: assign this week's gate-occupancy intervals to specific gate units,
    then return, for each gate, the sequence of times when that gate becomes free.

    Times are absolute game-hours (same scale as flight_segments scheduled_*_game_hour).
    """
    ap = str(airport_iata).upper().strip()
    gw = int(game_week)
    units = int(gate_units) if gate_units is not None else _allocated_gates(ap, "PLAYER")
    units = max(0, units)
    if units <= 0:
        return []

    intervals = _player_intervals_for_airport(ap, gw)
    intervals.sort(key=lambda x: (float(x[0]), float(x[1])))

    # Each heap item: (available_at_time, gate_index)
    heap: list[tuple[float, int]] = [(0.0, i) for i in range(units)]
    heapq.heapify(heap)

    frees: list[list[float]] = [[] for _ in range(units)]
    for s, e in intervals:
        s2 = float(s)
        e2 = float(e)
        avail, gi = heapq.heappop(heap)
        # If this interval starts before this gate is free, it means demand > capacity at that moment.
        # Still assign it to the earliest-free gate so the UI can reflect the crunch.
        heapq.heappush(heap, (max(avail, e2), gi))
        if len(frees[gi]) < int(max_times_per_gate):
            frees[gi].append(float(e2))

    return frees


def player_gate_gap_starts_by_unit(
    airport_iata: str,
    game_week: int,
    *,
    gate_units: int | None = None,
    max_gaps_per_gate: int = 6,
) -> list[list[tuple[float, float]]]:
    """
    UI planner: return per-gate *available gaps* as (gap_start, gap_end) absolute game-hours.

    This is more actionable than "free times" because a time only shows up if it starts an actual
    free window (and will disappear if you schedule something that begins at that time).
    """
    ap = str(airport_iata).upper().strip()
    gw = int(game_week)
    units = int(gate_units) if gate_units is not None else _allocated_gates(ap, "PLAYER")
    units = max(0, units)
    if units <= 0:
        return []

    intervals = _player_intervals_for_airport(ap, gw)
    intervals.sort(key=lambda x: (float(x[0]), float(x[1])))

    # Assign intervals to gates (same heuristic as free-times), but keep the full interval list per gate.
    heap: list[tuple[float, int]] = [(0.0, i) for i in range(units)]
    heapq.heapify(heap)
    assigned: list[list[tuple[float, float]]] = [[] for _ in range(units)]

    for s, e in intervals:
        s2 = float(s)
        e2 = float(e)
        avail, gi = heapq.heappop(heap)
        assigned[gi].append((s2, e2))
        heapq.heappush(heap, (max(avail, e2), gi))

    # Convert assigned intervals into gaps within the current week.
    w0 = float(gw - 1) * 168.0  # week start (absolute game hours)
    w1 = w0 + 168.0
    gaps: list[list[tuple[float, float]]] = [[] for _ in range(units)]
    for gi in range(units):
        segs = sorted(assigned[gi], key=lambda x: (x[0], x[1]))
        cur = w0
        for s, e in segs:
            if s > cur + 1e-9:
                gaps[gi].append((cur, s))
                if len(gaps[gi]) >= int(max_gaps_per_gate):
                    break
            cur = max(cur, e)
        if len(gaps[gi]) < int(max_gaps_per_gate) and cur < w1 - 1e-9:
            gaps[gi].append((cur, w1))

    return gaps


def assert_player_gate_capacity_for_new_segments(
    game_week: int,
    segments: list[dict],
    *,
    replace_tails: bool = True,
    shortfall_mode: bool = False,
) -> None:
    """
    Enforce concurrent gates at each auctioned airport for the new segments.

    segments: list of dicts with keys:
      - origin_iata, dest_iata (str)
      - dep_abs, arr_abs (float absolute game hours)
      - tail_number (optional; one scheduling batch defaults to one tail)

    replace_tails: when True (default), existing DB segments for tails in this batch are
    excluded before adding the new plan — used when rescheduling one aircraft. When False,
    new segments are checked against the full week already in the DB (weekly spawn adds).

    shortfall_mode: used by the weekly spawn, where the schedule is already published and
    the player is not at the keyboard. Instead of refusing — which silently dropped the
    aircraft's entire week — a shortfall event is opened and the flights are allowed to
    operate; the player then chooses to buy a stand or pay daily. Interactive scheduling
    leaves this off, because there the player can still fix the plan before committing it.

    Holding no stand at all at an auctioned airport always raises, in both modes: there is
    nothing to add a unit to, and operating somewhere you have never bid is not a shortfall.
    """
    gw = int(game_week)
    airports: set[str] = set()
    for s in segments or []:
        oi = str(s.get("origin_iata") or "").upper().strip()
        di = str(s.get("dest_iata") or "").upper().strip()
        if oi:
            airports.add(oi)
        if di:
            airports.add(di)

    # A chain anchored late in the week finishes in the next one, so its later legs
    # occupy stands during a week this call was not asked about. Check every week the
    # proposal actually touches; checking only `game_week` let those legs be validated
    # against the wrong week's traffic and never against the right one.
    weeks: set[int] = {gw}
    for s in segments or []:
        for key in ("dep_abs", "arr_abs"):
            try:
                t = float(s.get(key))
            except (TypeError, ValueError):
                continue
            weeks.add(int(t // 168.0) + 1)

    for ap in sorted(airports):
        if not ap or not is_auctioned_airport(ap):
            continue
        cap = _allocated_gates(ap, "PLAYER")
        if cap <= 0:
            raise ValueError(f"No gate allocation at {ap}. Bid in gate auctions to operate there.")
        exclude_tails = None
        if replace_tails:
            exclude_tails = {
                str(s.get("tail_number"))
                for s in (segments or [])
                if s.get("tail_number")
            } or None
        for wk in sorted(weeks):
            peak = player_gate_peak_at_airport(
                ap, wk, extra_segments=segments, exclude_tails=exclude_tails
            )
            if peak <= cap:
                continue
            if not shortfall_mode:
                raise ValueError(
                    f"Not enough concurrent gates at {ap}. Need peak {peak}, have {cap}. "
                    f"Bid for more gates or spread departures/arrivals out."
                )
            pairs = {
                (
                    str(s.get("origin_iata") or "").upper().strip(),
                    str(s.get("dest_iata") or "").upper().strip(),
                )
                for s in (segments or [])
                if ap
                in (
                    str(s.get("origin_iata") or "").upper().strip(),
                    str(s.get("dest_iata") or "").upper().strip(),
                )
            }
            if record_gate_shortfall(ap, wk, peak, pairs) is None:
                # Priced at nothing because no unit is held — fall back to refusing.
                raise ValueError(
                    f"Not enough concurrent gates at {ap}. Need peak {peak}, have {cap}. "
                    f"Bid for more gates or spread departures/arrivals out."
                )


def assert_player_gate_capacity_for_week(game_week: int, airports: list[str] | None = None) -> None:
    """
    Safety check: ensure the player's already-inserted segments for `game_week` do not exceed
    concurrent gate capacity at any auctioned airport.

    This is used as a post-insert verification so schedule creation cannot "succeed" and only
    fail later at departure time.
    """
    gw = int(game_week)
    aps = airports or []
    if not aps:
        # Only check airports the player is actually touching this week.
        rows = db.fetch_all(
            """
            SELECT DISTINCT COALESCE(fs.origin_iata, r.origin_iata) AS ap
            FROM flight_segments fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE fs.game_week = ?
              AND fs.status != 'CANCELLED'
            UNION
            SELECT DISTINCT COALESCE(fs.dest_iata, r.dest_iata) AS ap
            FROM flight_segments fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE fs.game_week = ?
              AND fs.status != 'CANCELLED'
            """,
            (gw, gw),
        )
        aps = [str(r["ap"]).upper() for r in (rows or []) if r and r["ap"]]

    for ap in sorted({str(a).upper().strip() for a in (aps or []) if str(a).strip()}):
        if not is_auctioned_airport(ap):
            continue
        cap = _allocated_gates(ap, "PLAYER")
        peak = _peak_concurrency(_player_intervals_for_airport(ap, gw))
        if peak > cap:
            raise ValueError(f"Not enough concurrent gates at {ap}. Need peak {peak}, have {cap}.")


def enforce_player_gate_capacity_for_week(game_week: int) -> int:
    """
    Hard enforcement for legacy / rollover cases:
    If the current week's already-inserted segments exceed concurrent gate capacity at an auctioned airport,
    cancel SCHEDULED/DELAYED/HOLDING legs until peak concurrency fits.

    Returns number of segments cancelled.
    """
    gw = int(game_week)
    cancelled = 0
    # Only airports where the player has (or had) an allocation row.
    aps = db.fetch_all(
        """
        SELECT airport_iata
        FROM airport_gate_allocations
        WHERE holder_id='PLAYER' AND status='ACTIVE'
        """
    )
    for r in aps or []:
        ap = str(r["airport_iata"]).upper()
        if not ap or not is_auctioned_airport(ap):
            continue
        cap = _allocated_gates(ap, "PLAYER")
        # Recompute and cancel until compliant (or nothing cancellable).
        while True:
            peak = _peak_concurrency(_player_intervals_for_airport(ap, gw))
            if peak <= cap:
                break
            # Find a cancellable segment touching this airport; cancel latest planned first.
            row = db.fetch_one(
                """
                SELECT fs.segment_id
                FROM flight_segments fs
                JOIN routes r ON r.route_id = fs.route_id
                WHERE fs.game_week = ?
                  AND fs.status IN ('SCHEDULED','DELAYED','HOLDING')
                  AND (COALESCE(fs.origin_iata, r.origin_iata) = ? OR COALESCE(fs.dest_iata, r.dest_iata) = ?)
                ORDER BY fs.scheduled_dep_game_hour DESC
                LIMIT 1
                """,
                (gw, ap, ap),
            )
            if not row:
                break
            sid = str(row["segment_id"])
            db.execute("UPDATE flight_segments SET status='CANCELLED' WHERE segment_id = ?", (sid,))
            cancelled += 1
            try:
                _notify_player(
                    gw,
                    "GATE_CAPACITY",
                    f"Cancelled flight segment {sid} due to gate overcapacity at {ap} (need peak {peak}, have {cap}).",
                )
            except Exception:
                pass
    return int(cancelled)


def ensure_weekly_airport_auctions(opens_week: int) -> int:
    """
    Ensure every auctioned airport has an OPEN auction for this week.
    Auction closes at the end of the same week (resolved at that week's settlement).
    units_available is computed from total_gate_units - active allocations.
    """
    ow = int(opens_week)
    airports = db.fetch_all(
        "SELECT iata FROM airports WHERE COALESCE(slot_level, 0) >= 2"
        "    OR (COALESCE(slot_level, 0) = 0 AND COALESCE(score, 0) >= ?)",
        (gate_score_threshold(),),
    )
    n = 0
    for r in airports:
        iata = str(r["iata"]).upper()
        exists = db.fetch_one(
            "SELECT auction_id, closes_week FROM airport_gate_auctions WHERE airport_iata = ? AND status = 'OPEN' AND opens_week = ?",
            (iata, ow),
        )
        if exists:
            # Backward-compat: older builds used closes_week = opens_week + 1.
            try:
                if int(exists["closes_week"] or 0) == ow + 1:
                    db.execute(
                        "UPDATE airport_gate_auctions SET closes_week = ? WHERE auction_id = ?",
                        (ow, str(exists["auction_id"])),
                    )
            except Exception:
                pass
            continue
        tot = total_gate_units(iata)
        alloc = db.fetch_one(
            """
            SELECT COALESCE(SUM(gate_units), 0) AS s
            FROM airport_gate_allocations
            WHERE airport_iata = ? AND status = 'ACTIVE'
            """,
            (iata,),
        )
        used = int(alloc["s"] or 0) if alloc else 0
        avail = max(0, tot - used)
        aid = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO airport_gate_auctions (
                auction_id, airport_iata, opens_week, closes_week,
                units_available, current_price_per_unit, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (aid, iata, ow, ow, int(avail), float(gate_min_price_per_unit())),
        )
        n += 1
    return n


def resolve_overdue_gate_auctions(before_week: int | None = None) -> int:
    """
    Resolve any OPEN auctions whose closes_week is already past.

    Needed because the clock advances `game_week` immediately while settlement
    runs on a background thread. Opening the Gates UI (or AI bidding) used to
    CANCEL those leftover OPEN rows — wiping player bids before settlement could
    award the stands.
    """
    gw = int(before_week if before_week is not None else current_game_week())
    weeks = db.fetch_all(
        """
        SELECT DISTINCT closes_week AS w
        FROM airport_gate_auctions
        WHERE status = 'OPEN' AND closes_week < ?
        ORDER BY closes_week
        """,
        (gw,),
    )
    n = 0
    for row in weeks or []:
        try:
            n += int(resolve_closing_gate_auctions(int(row["w"])) or 0)
        except Exception:
            pass
    return n


def list_open_gate_auctions() -> List[Dict[str, Any]]:
    # Make the command always useful: if the current week’s auctions haven’t been created yet
    # (e.g., player checks mid-week before settlement runs), create them on-demand.
    try:
        gw = current_game_week()
        # Award any prior-week auctions still OPEN (settlement lag / failed settle)
        # BEFORE creating this week's set, so supply reflects new allocations.
        resolve_overdue_gate_auctions(gw)
        ensure_weekly_airport_auctions(gw)
    except Exception:
        pass
    rows = db.fetch_all(
        """
        SELECT * FROM airport_gate_auctions
        WHERE status = 'OPEN'
          AND opens_week = ?
        ORDER BY airport_iata
        """
        ,
        (int(current_game_week()),),
    )
    return [dict(r) for r in rows]


def submit_gate_bid(auction_id: str, units: int, price_per_unit: float, bidder_id: str = "PLAYER") -> None:
    aid = str(auction_id)
    u = int(units)
    if u <= 0:
        raise ValueError("units must be >= 1")
    p = float(price_per_unit)
    if p < gate_min_price_per_unit():
        raise ValueError(f"price_per_unit must be at least ${gate_min_price_per_unit():,.0f}")
    a = db.fetch_one("SELECT * FROM airport_gate_auctions WHERE auction_id = ?", (aid,))
    if not a or str(a["status"]) != "OPEN":
        raise ValueError("Auction not found or not OPEN.")
    gw = current_game_week()
    if gw > int(a["closes_week"] or 0):
        raise ValueError("Auction already closed.")
    # cash check (winner pays later; but keep bidder honest)
    if bidder_id == "PLAYER":
        cash = db.fetch_one("SELECT cash FROM airline WHERE id = 1")
        if not cash or float(cash["cash"] or 0.0) < p * u:
            raise ValueError("Not enough cash to cover this max bid.")
    else:
        cash = db.fetch_one("SELECT cash FROM competitors WHERE competitor_id = ?", (bidder_id,))
        if not cash or float(cash["cash"] or 0.0) < p * u:
            return
    db.execute(
        "DELETE FROM airport_gate_bids WHERE auction_id = ? AND bidder_id = ?",
        (aid, bidder_id),
    )
    db.execute(
        """
        INSERT INTO airport_gate_bids (bid_id, auction_id, bidder_id, units_requested, price_per_unit, submitted_week)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), aid, bidder_id, u, p, gw),
    )
    cur = _round_up_step(p, gate_price_step())
    if cur > float(a["current_price_per_unit"] or 0.0):
        db.execute(
            "UPDATE airport_gate_auctions SET current_price_per_unit = ? WHERE auction_id = ?",
            (cur, aid),
        )


def _upsert_allocation(
    iata: str,
    holder_id: str,
    delta_units: int,
    *,
    effective_week: int | None = None,
    price_per_unit: float | None = None,
) -> None:
    """Add stands to a holding, tracking a weighted average of what was paid.

    The average is what a later sale is priced from, so buying two units cheaply and one
    expensively cannot be sold back as three expensive ones.
    """
    iata = iata.upper().strip()
    hid = str(holder_id)
    row = db.fetch_one(
        """
        SELECT allocation_id, gate_units, effective_week, price_paid_per_unit
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = ? AND status = 'ACTIVE'
        """,
        (iata, hid),
    )
    if not row:
        ew = int(effective_week or 1)
        db.execute(
            """
            INSERT INTO airport_gate_allocations (
                allocation_id, airport_iata, holder_id, gate_units,
                used_this_week, scheduled_this_week, below_threshold_weeks, effective_week, status
            ) VALUES (?, ?, ?, ?, 0, 0, 0, ?, 'ACTIVE')
            """,
            (str(uuid.uuid4()), iata, hid, int(max(0, delta_units)), ew),
        )
        if price_per_unit is not None:
            db.execute(
                "UPDATE airport_gate_allocations SET price_paid_per_unit = ?"
                " WHERE airport_iata = ? AND holder_id = ? AND status = 'ACTIVE'",
                (float(price_per_unit), iata, hid),
            )
        return
    new_units = max(0, int(row["gate_units"] or 0) + int(delta_units))
    # Keep the earlier effective week for existing stands. Pushing it forward when
    # you win *more* units would re-grace the whole pile and hide growth until next week.
    ew = int(row["effective_week"] or 1)
    if effective_week is not None:
        ew = min(ew, int(effective_week)) if int(row["gate_units"] or 0) > 0 else int(effective_week)
    old_units = int(row["gate_units"] or 0)
    if price_per_unit is None or new_units <= 0:
        db.execute(
            "UPDATE airport_gate_allocations SET gate_units = ?, effective_week = ?"
            " WHERE allocation_id = ?",
            (new_units, ew, str(row["allocation_id"])),
        )
        return
    prior = float(row["price_paid_per_unit"] or 0.0)
    added = max(0, int(delta_units))
    avg = ((prior * old_units) + (float(price_per_unit) * added)) / float(new_units)
    db.execute(
        "UPDATE airport_gate_allocations SET gate_units = ?, effective_week = ?,"
        " price_paid_per_unit = ? WHERE allocation_id = ?",
        (new_units, ew, round(avg, 2), str(row["allocation_id"])),
    )


def _claim_auction(auction_id: str) -> bool:
    """Atomically take ownership of an auction. True only for the caller that won it.

    One statement, one transaction: the conditional UPDATE and the row count that proves
    it applied happen together, so concurrent resolvers cannot both believe the auction
    is theirs. Everything else about resolution is safe to run once and only once.
    """
    from db.db import get_cursor

    with get_cursor() as cur:
        cur.execute(
            "UPDATE airport_gate_auctions SET status = 'RESOLVED'"
            " WHERE auction_id = ? AND status = 'OPEN'",
            (str(auction_id),),
        )
        return int(cur.rowcount or 0) == 1


def resolve_closing_gate_auctions(completed_game_week: int) -> int:
    """
    Resolve auctions where closes_week == completed_game_week.
    Allocate units to highest bids first. Pay-as-bid.
    """
    gw = int(completed_game_week)
    next_week = gw + 1
    auctions = db.fetch_all(
        "SELECT * FROM airport_gate_auctions WHERE status = 'OPEN' AND closes_week = ?",
        (gw,),
    )
    n = 0
    for a in auctions:
        aid = str(a["auction_id"])
        iata = str(a["airport_iata"]).upper()
        avail = int(a["units_available"] or 0)
        # Claim the auction BEFORE awarding anything, in one statement, and only proceed
        # if this call is the one that flipped it out of OPEN.
        #
        # The `status = 'OPEN'` filter on the SELECT above is not enough on its own:
        # awarding and marking RESOLVED were separate commits, so two callers could both
        # read OPEN and both award. That is not hypothetical — `player_gate_bids()` calls
        # resolve_overdue_gate_auctions() on the HTTP thread whenever the Gates window is
        # polled, entirely outside `_settlement_lock`, so a player with that window open
        # at the week roll raced the settlement thread. The result was every stand awarded
        # twice and charged twice: a 1-unit bid at ARN, CDG, ZRH, SVO and others became
        # 2 stands for $16,000, and concurrent inserts left duplicate allocation rows.
        if not _claim_auction(aid):
            continue
        player_bid = db.fetch_one(
            """
            SELECT units_requested, price_per_unit
            FROM airport_gate_bids
            WHERE auction_id = ? AND bidder_id = 'PLAYER'
            """,
            (aid,),
        )
        bids = db.fetch_all(
            """
            SELECT bidder_id, units_requested, price_per_unit
            FROM airport_gate_bids
            WHERE auction_id = ?
            ORDER BY price_per_unit DESC, units_requested DESC
            """,
            (aid,),
        )
        if not bids or avail <= 0:
            db.execute("UPDATE airport_gate_auctions SET status = 'CANCELLED' WHERE auction_id = ?", (aid,))
            if player_bid:
                _notify_player(
                    gw,
                    "GATE_AUCTION",
                    f"Gate auction cancelled: {iata} had 0 available units this week.",
                )
            continue
        remaining = avail
        player_won_units = 0
        player_cost = 0.0
        for b in bids:
            if remaining <= 0:
                break
            bidder = str(b["bidder_id"])
            want = max(0, int(b["units_requested"] or 0))
            if want <= 0:
                continue
            take = min(remaining, want)
            price = float(b["price_per_unit"] or 0.0)
            cost = price * float(take)
            # Deduct
            if bidder == "PLAYER":
                db.execute("UPDATE airline SET cash = cash - ? WHERE id = 1", (cost,))
                player_won_units += int(take)
                player_cost += float(cost)
            else:
                db.execute("UPDATE competitors SET cash = cash - ? WHERE competitor_id = ?", (cost, bidder))
            # New/increased gates become effective next week, and should not be evaluated
            # by the utilization rule for the week that just ended.
            _upsert_allocation(
                iata, bidder, take, effective_week=next_week, price_per_unit=price
            )
            remaining -= take
            if bidder != "PLAYER" and take > 0:
                try:
                    from engine.ai_log import (
                        append_ai_narrative,
                        competitor_display_name,
                        push_ai_news,
                    )

                    append_ai_narrative(bidder, f"WON_GATE:{iata}:{int(take)}")
                    push_ai_news(
                        f"🏛 {competitor_display_name(bidder)} wins {int(take)} gate(s) at {iata}"
                    )
                except Exception:
                    pass
        # Already marked RESOLVED by the claim above.
        n += 1
        if player_bid:
            want_u = int(player_bid["units_requested"] or 0)
            price = float(player_bid["price_per_unit"] or 0.0)
            if player_won_units > 0:
                _notify_player(
                    gw,
                    "GATE_AUCTION",
                    f"Gate auction result: {iata} — WON {player_won_units}/{want_u} gate(s) @ ${price:,.0f}/gate "
                    f"(paid ${player_cost:,.0f}).",
                )
            else:
                _notify_player(
                    gw,
                    "GATE_AUCTION",
                    f"Gate auction result: {iata} — LOST (bid {want_u} @ ${price:,.0f}/gate).",
                )
        try:
            from engine.news_feed import push_news
            push_news(f"GATE AUCTION RESOLVED: {iata} — {avail-remaining}/{avail} units allocated.")
        except Exception:
            pass
    return n


def reset_weekly_gate_counters_and_enforce(settled_game_week: int) -> None:
    """
    Apply hub vs non-hub utilization thresholds to ALL holders (player + AI) at auctioned airports.

    Concurrent-gates model: utilization is gate-hours / (gate_units * 168).
    gate-hours are approximated from scheduled segments touching the airport using MTT windows:
      each departure uses MTT hours at origin; each arrival uses MTT hours at destination.
    """
    grace = gate_utilization_grace_weeks()
    mtt = _mtt_hours()
    thr_hub = gate_utilization_threshold_hub()
    thr_nonhub = gate_utilization_threshold_nonhub()
    # Fallback: if hub/nonhub thresholds are missing, keep legacy single threshold.
    thr_legacy = gate_utilization_threshold()
    rows = db.fetch_all("SELECT * FROM airport_gate_allocations WHERE status = 'ACTIVE'")
    for r in rows:
        alloc_id = str(r["allocation_id"])
        airport_iata = str(r["airport_iata"]).upper()
        holder = str(r["holder_id"])
        units = int(r["gate_units"] or 0)
        below = int(r["below_threshold_weeks"] or 0)
        effective_week = int(r["effective_week"] or 1)

        # Do not judge a unit until it has had a full week in which it was actually usable.
        # A unit won at week N settlement becomes effective at N+1; with `>` it was already
        # being measured during N+1 — the first week the player could schedule against it —
        # so a new station was revoked before any flight could possibly have used it.
        if effective_week >= int(settled_game_week):
            db.execute(
                """
                UPDATE airport_gate_allocations
                SET used_this_week = 0,
                    scheduled_this_week = 0
                WHERE allocation_id = ?
                """,
                (alloc_id,),
            )
            continue

        if units <= 0:
            below = 0
        else:
            # Threshold depends on whether this airport is the holder's home hub.
            hub_iata = None
            if holder == "PLAYER":
                row_h = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
                hub_iata = str(row_h["home_hub_iata"]).upper() if row_h and row_h["home_hub_iata"] else None
            else:
                row_h = db.fetch_one(
                    "SELECT home_hub_iata FROM competitors WHERE competitor_id = ?",
                    (holder,),
                )
                hub_iata = str(row_h["home_hub_iata"]).upper() if row_h and row_h["home_hub_iata"] else None
            is_hub_here = bool(hub_iata and airport_iata == hub_iata)
            if holder == "PLAYER" and not is_hub_here:
                # Secondary hubs count too, but only once they carry real traffic: a hub
                # opened with two free stands and no routes would fail the 6% rule inside
                # the grace period and lose the grant before it could be used.
                try:
                    from engine.hubs import hub_is_mature

                    is_hub_here = hub_is_mature(airport_iata)
                except Exception:
                    is_hub_here = False
            thr = float(thr_hub) if is_hub_here else float(thr_nonhub)
            if thr <= 0:
                thr = float(thr_legacy)

            touches = 0
            busy_hours = 0.0
            if holder == "PLAYER":
                intervals = _player_intervals_for_airport(airport_iata, int(settled_game_week))
                busy_hours = sum(max(0.0, float(e) - float(s)) for s, e in intervals)
                cnt = db.fetch_one(
                    """
                    SELECT
                      SUM(CASE WHEN COALESCE(fs.origin_iata, r.origin_iata) = ? THEN 1 ELSE 0 END) +
                      SUM(CASE WHEN COALESCE(fs.dest_iata,   r.dest_iata)   = ? THEN 1 ELSE 0 END) AS n
                    FROM flight_segments fs
                    JOIN routes r ON r.route_id = fs.route_id
                    WHERE fs.game_week = ?
                      AND fs.status != 'CANCELLED'
                    """,
                    (airport_iata, airport_iata, int(settled_game_week)),
                )
                touches = int(cnt["n"] or 0) if cnt else 0
            else:
                cnt = db.fetch_one(
                    """
                    SELECT
                      SUM(CASE WHEN origin_iata = ? THEN 1 ELSE 0 END) +
                      SUM(CASE WHEN dest_iata   = ? THEN 1 ELSE 0 END) AS n
                    FROM ai_flight_segments
                    WHERE game_week = ?
                      AND competitor_id = ?
                      AND status != 'CANCELLED'
                    """,
                    (airport_iata, airport_iata, int(settled_game_week), holder),
                )
                touches = int(cnt["n"] or 0) if cnt else 0
                busy_hours = float(touches) * float(mtt)
            util = busy_hours / float(max(1, units) * 168.0)
            if util < thr:
                below += 1
            else:
                below = 0
        if below >= grace and units > 0:
            # Lose 1 unit (returns to pool via next week's auction).
            units -= 1
            below = 0
        db.execute(
            """
            UPDATE airport_gate_allocations
            SET gate_units = ?,
                used_this_week = 0,
                scheduled_this_week = 0,
                below_threshold_weeks = ?
            WHERE allocation_id = ?
            """,
            (int(max(0, units)), int(max(0, below)), alloc_id),
        )


def player_gate_capacity_remaining(iata: str) -> int:
    iata = iata.upper().strip()
    row = db.fetch_one(
        """
        SELECT gate_units, scheduled_this_week
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status='ACTIVE'
        """,
        (iata,),
    )
    if not row:
        return 0
    return int(row["gate_units"] or 0) - int(row["scheduled_this_week"] or 0)


def bump_player_scheduled_for_airport(iata: str, delta: int) -> None:
    iata = iata.upper().strip()
    row = db.fetch_one(
        "SELECT allocation_id FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER' AND status='ACTIVE'",
        (iata,),
    )
    if not row:
        return
    db.execute(
        "UPDATE airport_gate_allocations SET scheduled_this_week = COALESCE(scheduled_this_week,0) + ? WHERE allocation_id = ?",
        (int(delta), str(row["allocation_id"])),
    )


def increment_player_used_for_airport(iata: str) -> None:
    iata = iata.upper().strip()
    row = db.fetch_one(
        "SELECT allocation_id FROM airport_gate_allocations WHERE airport_iata=? AND holder_id='PLAYER' AND status='ACTIVE'",
        (iata,),
    )
    if not row:
        return
    db.execute(
        "UPDATE airport_gate_allocations SET used_this_week = COALESCE(used_this_week,0) + 1 WHERE allocation_id = ?",
        (str(row["allocation_id"]),),
    )



# ---------------------------------------------------------------------------
# Gate shortfalls: buy a stand outright, or pay daily until it clears.
#
# Running short of stands used to drop the aircraft's entire week silently. The
# flights now operate regardless and the cost surfaces as a choice: pay once for a
# permanent extra unit, or accrue a daily penalty until the peak fits again — either
# because the player won a unit at auction or thinned the schedule.
#
# Cash moves the moment each charge lands rather than at settlement, so these two
# figures are reported in week_ledger but deliberately kept out of `pretax`, exactly
# as asset_sale_proceeds and lease_return_penalties are. Folding them into pretax
# while also charging cash live would deduct them twice.
# ---------------------------------------------------------------------------

GATE_SHORTFALL_OPEN_STATUSES = ("PENDING", "DECLINED")


def emergency_gate_fee_rate() -> float:
    return _fc("emergency_gate_fee_rate", 0.20)


def emergency_gate_penalty_rate_per_day() -> float:
    return _fc("emergency_gate_penalty_rate_per_day", 0.10)


def gate_shortfall_penalty_total_multiplier() -> float:
    """Total penalty across the week, as a multiple of the outright price. >= 1.0."""
    return max(1.0, _fc("gate_shortfall_penalty_total_multiplier", 1.0))


def _chargeable_days_left(game_hours_elapsed: float | None = None) -> int:
    """Daily charges remaining before the week rolls, counting today's boundary onward."""
    if game_hours_elapsed is None:
        row = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
        game_hours_elapsed = float(row["game_hours_elapsed"] or 0.0) if row else 0.0
    day_of_week = int((float(game_hours_elapsed) % 168.0) // 24.0)
    return max(1, 7 - day_of_week)


def gate_shortfall_penalty_schedule(fee: float, days_total: int) -> List[float]:
    """Escalating daily charges whose sum is the outright price (times the multiplier).

    A flat rate made declining strictly cheaper than buying, so the purchase branch was
    never worth taking. The charge now ramps linearly — day k of D costs
    ``fee * k / (D(D+1)/2)`` — which has two properties worth keeping:

      * the sum over the whole week is exactly the outright price, and since declining
        leaves the player with no stand at the end of it, buying strictly dominates;
      * it costs least on the first day and most on the last, so acting early is cheap
        and procrastinating is expensive, whenever the shortfall is detected.
    """
    d = max(1, int(days_total))
    total = float(fee) * gate_shortfall_penalty_total_multiplier()
    denom = d * (d + 1) / 2.0
    return [round(total * k / denom, 2) for k in range(1, d + 1)]


def _route_pair_revenue_prev_week(iata: str, game_week: int, route_pairs: set[tuple[str, str]]) -> float:
    """Last week's gross revenue on the route pairs that caused the shortfall.

    Both directions count: a stand is occupied by the arrival and the departure of the
    same rotation, so charging only the leg that happened to trip the check first would
    make the fee depend on spawn ordering.
    """
    prev = int(game_week) - 1
    if prev < 1 or not route_pairs:
        return 0.0
    endpoints: set[str] = set()
    for a, b in route_pairs:
        endpoints.add(a)
        endpoints.add(b)
    if not endpoints:
        return 0.0
    marks = ",".join("?" for _ in endpoints)
    rows = db.fetch_all(
        f"""
        SELECT COALESCE(fs.origin_iata, r.origin_iata) AS oi,
               COALESCE(fs.dest_iata,   r.dest_iata)   AS di,
               fs.revenue_gross AS rev
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ?
          AND fs.status != 'CANCELLED'
          AND COALESCE(fs.origin_iata, r.origin_iata) IN ({marks})
          AND COALESCE(fs.dest_iata,   r.dest_iata)   IN ({marks})
        """,
        (prev, *endpoints, *endpoints),
    )
    total = 0.0
    for r in rows or []:
        pair = (str(r["oi"] or "").upper(), str(r["di"] or "").upper())
        if pair in route_pairs or (pair[1], pair[0]) in route_pairs:
            total += float(r["rev"] or 0.0)
    return float(total)


def gate_shortfall_fee(
    iata: str,
    game_week: int,
    route_pairs: set[tuple[str, str]] | None = None,
) -> Dict[str, float]:
    """Fee for one more stand at `iata`, and the per-day cost of going without.

    fee = route-pair revenue last week x rate x units already held at THIS airport,
    floored at the auction's minimum unit price x units held. Keying the multiplier to
    the units held at the airport in question — not the whole network — keeps the charge
    proportional to the presence that created the shortfall, so a quiet outstation stays
    cheap while a station you have built up is expensive.
    """
    ap = str(iata).upper().strip()
    gw = int(game_week)
    units = max(0, _allocated_gates(ap, "PLAYER"))
    basis = _route_pair_revenue_prev_week(ap, gw, route_pairs or set())
    fee = basis * emergency_gate_fee_rate() * float(units)
    floor = _fc("gate_min_price_per_unit", 5000.0) * float(units)
    fee = max(fee, floor)
    days_left = _chargeable_days_left()
    schedule = gate_shortfall_penalty_schedule(fee, days_left)
    return {
        "route_revenue_basis": round(basis, 2),
        "gates_held": units,
        "fee_amount": round(fee, 2),
        "penalty_days_total": days_left,
        "penalty_schedule": schedule,
        "penalty_total_if_declined": round(sum(schedule), 2),
        "daily_penalty": schedule[0] if schedule else 0.0,
    }


def _current_game_day() -> int:
    row = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
    try:
        return int(float(row["game_hours_elapsed"] or 0.0) // 24.0) if row else 0
    except (TypeError, ValueError):
        return 0


def record_gate_shortfall(
    iata: str,
    game_week: int,
    peak_needed: int,
    route_pairs: set[tuple[str, str]] | None = None,
) -> Optional[dict]:
    """Open a shortfall event, or return the one already open for this airport/week.

    Idempotent by (airport, holder, week): the weekly spawn runs more than once — at
    launch and again from settlement — and must not open a second event or re-price the
    first one after the player has answered it.
    """
    ap = str(iata).upper().strip()
    gw = int(game_week)
    existing = db.fetch_one(
        """
        SELECT * FROM gate_shortfall_events
        WHERE airport_iata = ? AND holder_id = 'PLAYER' AND game_week = ?
        """,
        (ap, gw),
    )
    if existing:
        return dict(existing)

    priced = gate_shortfall_fee(ap, gw, route_pairs)
    if int(priced["gates_held"]) <= 0:
        # No stand at all here is a different problem: there is nothing to add one to,
        # and the fee would price at zero. Callers keep raising in that case.
        return None
    event_id = str(uuid.uuid4())
    day = _current_game_day()
    db.execute(
        """
        INSERT INTO gate_shortfall_events (
            event_id, airport_iata, holder_id, game_week, detected_game_day,
            peak_needed, gates_held, route_revenue_basis, fee_amount, daily_penalty,
            status, penalty_days_charged, penalty_accrued, penalty_days_total
        ) VALUES (?, ?, 'PLAYER', ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, 0, ?)
        """,
        (
            event_id, ap, gw, day, int(peak_needed), int(priced["gates_held"]),
            float(priced["route_revenue_basis"]), float(priced["fee_amount"]),
            float(priced["daily_penalty"]), int(priced["penalty_days_total"]),
        ),
    )
    _notify_player(
        gw,
        "GATE_SHORTFALL",
        f"{ap}: your schedule needs {int(peak_needed)} stands but you hold "
        f"{int(priced['gates_held'])}. The flights are operating. Buy a permanent extra "
        f"stand for ${priced['fee_amount']:,.0f}, or pay a daily penalty that starts at "
        f"${priced['daily_penalty']:,.0f} and rises each day — "
        f"${priced['penalty_total_if_declined']:,.0f} if you wait out the week, and you "
        f"would still hold no extra stand.",
    )
    return dict(
        db.fetch_one("SELECT * FROM gate_shortfall_events WHERE event_id = ?", (event_id,))
    )


def open_gate_shortfalls(game_week: int | None = None, *, reconcile: bool = True) -> List[dict]:
    """Shortfall events still awaiting a decision or still accruing.

    `reconcile` closes any whose peak already fits the stands held — the player won a
    unit at auction, bought one, or thinned the schedule. Without it the prompt kept
    asking for a decision that no longer existed, because closing them depended entirely
    on the daily hook having run.
    """
    if reconcile:
        _close_cleared_gate_shortfalls()
    marks = ",".join("?" for _ in GATE_SHORTFALL_OPEN_STATUSES)
    if game_week is None:
        rows = db.fetch_all(
            f"SELECT * FROM gate_shortfall_events WHERE status IN ({marks})"
            " ORDER BY game_week DESC, airport_iata",
            tuple(GATE_SHORTFALL_OPEN_STATUSES),
        )
    else:
        rows = db.fetch_all(
            f"SELECT * FROM gate_shortfall_events WHERE game_week = ? AND status IN ({marks})"
            " ORDER BY airport_iata",
            (int(game_week), *GATE_SHORTFALL_OPEN_STATUSES),
        )
    return [dict(r) for r in rows or []]


def _close_cleared_gate_shortfalls() -> List[str]:
    """Mark open shortfalls RESOLVED once the airport's peak fits the stands held."""
    marks = ",".join("?" for _ in GATE_SHORTFALL_OPEN_STATUSES)
    rows = db.fetch_all(
        f"SELECT event_id, airport_iata, game_week FROM gate_shortfall_events"
        f" WHERE status IN ({marks})",
        tuple(GATE_SHORTFALL_OPEN_STATUSES),
    )
    closed: List[str] = []
    for r in rows or []:
        ap = str(r["airport_iata"]).upper()
        try:
            if not _shortfall_cleared(ap, int(r["game_week"])):
                continue
        except Exception:
            continue
        db.execute(
            "UPDATE gate_shortfall_events SET status = 'RESOLVED', resolved_game_day = ?"
            " WHERE event_id = ?",
            (_current_game_day(), str(r["event_id"])),
        )
        closed.append(ap)
        _notify_player(
            int(r["game_week"]),
            "GATE_SHORTFALL",
            f"{ap}: stand shortfall cleared — no further penalty.",
        )
    return closed


def resolve_gate_shortfall(event_id: str, accept: bool) -> Dict[str, Any]:
    """Buy the stand (accept) or elect to pay daily instead (decline).

    Accepting adds a permanent unit — the player owns it as if won at auction — and
    charges through update_cash, which refuses to overdraft: an optional purchase must
    not be able to bankrupt the airline. Penalties already accrued are not refunded,
    since those days were genuinely flown short.
    """
    from engine.setup import update_cash

    row = db.fetch_one("SELECT * FROM gate_shortfall_events WHERE event_id = ?", (str(event_id),))
    if not row:
        raise ValueError("Unknown gate shortfall.")
    ev = dict(row)
    if str(ev["status"]) not in GATE_SHORTFALL_OPEN_STATUSES:
        return {"event_id": ev["event_id"], "status": ev["status"], "changed": False}

    ap = str(ev["airport_iata"]).upper()
    if not accept:
        db.execute(
            "UPDATE gate_shortfall_events SET status = 'DECLINED' WHERE event_id = ?",
            (str(event_id),),
        )
        return {"event_id": ev["event_id"], "status": "DECLINED", "changed": True}

    fee = float(ev["fee_amount"] or 0.0)
    update_cash(-fee)          # raises ValueError if it would overdraft
    alloc = db.fetch_one(
        """
        SELECT allocation_id, gate_units FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'
        """,
        (ap,),
    )
    if alloc:
        db.execute(
            "UPDATE airport_gate_allocations SET gate_units = ? WHERE allocation_id = ?",
            (int(alloc["gate_units"] or 0) + 1, str(alloc["allocation_id"])),
        )
    db.execute(
        """
        UPDATE gate_shortfall_events
        SET status = 'PAID', resolved_game_day = ?
        WHERE event_id = ?
        """,
        (_current_game_day(), str(event_id)),
    )
    _notify_player(
        int(ev["game_week"]),
        "GATE_SHORTFALL",
        f"{ap}: bought an extra stand for ${fee:,.0f}. You now hold "
        f"{_allocated_gates(ap, 'PLAYER')} — it is yours permanently.",
    )
    return {"event_id": ev["event_id"], "status": "PAID", "charged": fee, "changed": True}


def _shortfall_cleared(iata: str, game_week: int) -> bool:
    """True once the week's scheduled peak fits inside the stands actually held."""
    ap = str(iata).upper().strip()
    return player_gate_peak_at_airport(ap, int(game_week)) <= _allocated_gates(ap, "PLAYER")


def accrue_gate_shortfall_penalties(game_day: int | None = None) -> Dict[str, Any]:
    """Charge one day of penalty per open shortfall. Wired to the clock's on_day.

    Called once per game day. `last_charged_game_day` makes it idempotent, so a repeated
    or replayed day cannot double-charge. An event closes as RESOLVED the moment the peak
    fits again — winning a unit at auction or thinning the schedule stops the bleeding
    without the player having to come back and dismiss anything.

    Penalties use apply_settlement_cash, not update_cash: this charge is involuntary and
    must land even if it pushes the airline negative, the same way operating losses do.
    """
    from engine.setup import apply_settlement_cash

    day = int(_current_game_day() if game_day is None else game_day)
    charged = 0.0
    resolved: List[str] = []
    billed: List[str] = []
    for ev in open_gate_shortfalls(reconcile=False):
        ap = str(ev["airport_iata"]).upper()
        gw = int(ev["game_week"])
        if int(ev["last_charged_game_day"] or -1) >= day:
            continue
        if _shortfall_cleared(ap, gw):
            db.execute(
                """
                UPDATE gate_shortfall_events
                SET status = 'RESOLVED', resolved_game_day = ?
                WHERE event_id = ?
                """,
                (day, str(ev["event_id"])),
            )
            resolved.append(ap)
            _notify_player(gw, "GATE_SHORTFALL", f"{ap}: stand shortfall cleared — no further penalty.")
            continue
        # Past the end of the week the schedule is a new one; stop charging the old event.
        if day // 7 + 1 > gw:
            db.execute(
                "UPDATE gate_shortfall_events SET status = 'RESOLVED', resolved_game_day = ?"
                " WHERE event_id = ?",
                (day, str(ev["event_id"])),
            )
            continue
        k = int(ev["penalty_days_charged"] or 0)          # 0-based index of today's charge
        d_total = int(ev["penalty_days_total"] or 7)
        schedule = gate_shortfall_penalty_schedule(float(ev["fee_amount"] or 0.0), d_total)
        # Past the planned ramp (a week that ran longer than expected) keep charging the
        # final, highest tranche rather than dropping to nothing.
        amount = schedule[k] if k < len(schedule) else (schedule[-1] if schedule else 0.0)
        if amount <= 0:
            continue
        apply_settlement_cash(-amount)
        db.execute(
            """
            UPDATE gate_shortfall_events
            SET penalty_days_charged = penalty_days_charged + 1,
                penalty_accrued = penalty_accrued + ?,
                last_charged_game_day = ?
            WHERE event_id = ?
            """,
            (amount, day, str(ev["event_id"])),
        )
        charged += amount
        billed.append(ap)
    if billed:
        _notify_player(
            _current_week_for_notice(),
            "GATE_SHORTFALL",
            "Stand shortfall penalty charged for " + ", ".join(sorted(set(billed)))
            + f": ${charged:,.0f} today.",
        )
    return {"game_day": day, "charged": round(charged, 2), "resolved": resolved, "billed": billed}


def _current_week_for_notice() -> int:
    row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    try:
        return int(row["game_week"] or 1) if row else 1
    except (TypeError, ValueError):
        return 1


def gate_shortfall_week_totals(game_week: int) -> Dict[str, float]:
    """Fees paid and penalties accrued in one week, for the ledger."""
    row = db.fetch_one(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status = 'PAID' THEN fee_amount ELSE 0 END), 0) AS fees,
          COALESCE(SUM(penalty_accrued), 0) AS penalties
        FROM gate_shortfall_events
        WHERE game_week = ?
        """,
        (int(game_week),),
    )
    return {
        "emergency_gate_fees": float(row["fees"] or 0.0) if row else 0.0,
        "gate_shortfall_penalties": float(row["penalties"] or 0.0) if row else 0.0,
    }


# ---------------------------------------------------------------------------
# Selling a stand back.
#
# The opposite lever to the emergency purchase above. It matters because
# utilisation is gate-hours / (gate_units * 168): shedding a unit *raises* the
# score at that station, so a stop below the retention threshold is better sold
# than surrendered for nothing after three weeks of grace.
#
# The sale is queued and executed at the week roll, deliberately AFTER the new
# week's segments have spawned, because that is the first moment the next week's
# true peak is knowable. Checking only the spawned present would let a player
# sell a stand their published schedule needs.
# ---------------------------------------------------------------------------


def gate_sale_haircut() -> float:
    return _fc("gate_sale_haircut", 0.85)


def _is_any_player_hub(iata: str) -> bool:
    """True for the primary hub and every secondary one."""
    try:
        from engine.hubs import is_player_hub

        return bool(is_player_hub(iata))
    except Exception:
        return str(iata).upper().strip() == _player_home_hub()


def _player_home_hub() -> str:
    row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
    return str(row["home_hub_iata"]).upper().strip() if row and row["home_hub_iata"] else ""


def _weeks_with_player_segments_at(iata: str) -> List[int]:
    """Weeks that currently hold player movements at this airport, present and future."""
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    cur = int(gs["game_week"] or 1) if gs else 1
    rows = db.fetch_all(
        """
        SELECT DISTINCT fs.game_week AS w
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.status != 'CANCELLED'
          AND fs.game_week >= ?
          AND (COALESCE(fs.origin_iata, r.origin_iata) = ?
            OR COALESCE(fs.dest_iata,   r.dest_iata)   = ?)
        """,
        (cur, str(iata).upper().strip(), str(iata).upper().strip()),
    )
    weeks = {int(r["w"]) for r in rows or []}
    weeks.add(cur)
    return sorted(weeks)


def gate_sale_blockers(iata: str, units: int = 1) -> List[str]:
    """Reasons this sale must be refused outright. Empty list means it may be queued.

    These are hard refusals, unlike aircraft disposal blockers, because there is no
    recovery path: unlike a schedule the engine can unwind for you, a stand you no longer
    hold cannot be conjured back mid-week.
    """
    ap = str(iata).upper().strip()
    n = max(1, int(units))
    out: List[str] = []
    held = _allocated_gates(ap, "PLAYER")
    if held <= 0:
        out.append(f"You hold no stands at {ap}.")
        return out
    if n > held:
        out.append(f"You hold {held} stand(s) at {ap}, cannot sell {n}.")
        return out

    remaining = held - n
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    cur = int(gs["game_week"] or 1) if gs else 1

    row = db.fetch_one(
        """
        SELECT effective_week, pending_sale_units FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'
        """,
        (ap,),
    )
    if row and int(row["pending_sale_units"] or 0) > 0:
        out.append(f"A sale at {ap} is already queued for the next week roll.")
    if row and int(row["effective_week"] or 1) > cur:
        out.append(
            f"Stands at {ap} become effective in week {int(row['effective_week'])}; "
            "they cannot be sold before then."
        )

    if remaining <= 0:
        if _is_any_player_hub(ap):
            out.append(f"{ap} is one of your hubs — you cannot sell your last stand there.")
        busy = [w for w in _weeks_with_player_segments_at(ap)
                if player_gate_peak_at_airport(ap, w) > 0]
        if busy:
            out.append(
                f"Selling your last stand at {ap} would leave you operating there with no "
                f"allocation; flights are scheduled in week(s) "
                f"{', '.join(str(w) for w in busy)}."
            )

    for w in _weeks_with_player_segments_at(ap):
        peak = player_gate_peak_at_airport(ap, w)
        if peak > remaining:
            out.append(
                f"Week {w} needs {peak} concurrent stands at {ap}; selling would leave "
                f"{remaining}."
            )

    if [e for e in open_gate_shortfalls() if str(e["airport_iata"]).upper() == ap]:
        out.append(
            f"There is an unresolved stand shortfall at {ap}; settle it before selling."
        )
    return out


def gate_sale_quote(iata: str, units: int = 1) -> Dict[str, Any]:
    """What selling `units` stands at this airport would pay, and why it might be refused."""
    ap = str(iata).upper().strip()
    n = max(1, int(units))
    held = _allocated_gates(ap, "PLAYER")
    row = db.fetch_one(
        """
        SELECT price_paid_per_unit, pending_sale_units, effective_week
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'
        """,
        (ap,),
    )
    paid = float(row["price_paid_per_unit"] or 0.0) if row else 0.0
    # Units predating price tracking have no recorded cost; the auction floor is the
    # only defensible stand-in, and it is what they would have cost at minimum.
    if paid <= 0:
        paid = _fc("gate_min_price_per_unit", 5000.0)
    haircut = gate_sale_haircut()
    weeks = _weeks_with_player_segments_at(ap)
    return {
        "airport_iata": ap,
        "units": n,
        "gates_held": held,
        "gates_after": max(0, held - n),
        "price_paid_per_unit": round(paid, 2),
        "haircut": haircut,
        "proceeds": round(paid * haircut * n, 2),
        "pending_sale_units": int(row["pending_sale_units"] or 0) if row else 0,
        "peak_by_week": {str(w): player_gate_peak_at_airport(ap, w) for w in weeks},
        "blockers": gate_sale_blockers(ap, n),
    }


def request_gate_sale(iata: str, units: int = 1) -> Dict[str, Any]:
    """Queue a stand sale for the next week roll. Refuses if any blocker applies."""
    ap = str(iata).upper().strip()
    n = max(1, int(units))
    blockers = gate_sale_blockers(ap, n)
    if blockers:
        raise ValueError(blockers[0])
    db.execute(
        "UPDATE airport_gate_allocations SET pending_sale_units = ?"
        " WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'",
        (n, ap),
    )
    q = gate_sale_quote(ap, n)
    _notify_player(
        _current_week_for_notice(),
        "GATE_SALE",
        f"{ap}: {n} stand(s) queued for sale at {q['proceeds']:,.0f}. Completes at the "
        f"next week roll, and can be cancelled until then.",
    )
    return {"airport_iata": ap, "units": n, "proceeds": q["proceeds"], "status": "PENDING"}


def cancel_gate_sale(iata: str) -> Dict[str, Any]:
    ap = str(iata).upper().strip()
    db.execute(
        "UPDATE airport_gate_allocations SET pending_sale_units = 0"
        " WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'",
        (ap,),
    )
    return {"airport_iata": ap, "status": "CANCELLED"}


def pending_gate_sales() -> List[dict]:
    rows = db.fetch_all(
        """
        SELECT airport_iata, gate_units, pending_sale_units, price_paid_per_unit
        FROM airport_gate_allocations
        WHERE holder_id = 'PLAYER' AND status = 'ACTIVE' AND pending_sale_units > 0
        ORDER BY airport_iata
        """
    )
    return [dict(r) for r in rows or []]


def process_pending_gate_sales() -> Dict[str, Any]:
    """Execute queued stand sales. MUST run after the new week's segments have spawned.

    Re-validates rather than trusting the request: a week has passed and the schedule may
    have grown, so a sale that was safe when queued may no longer be. A sale that no
    longer qualifies is *held*, not silently dropped — the player keeps the stand and is
    told why, and the queued sale stays for them to cancel or retry.
    """
    from engine.setup import apply_settlement_cash

    sold = 0
    proceeds_total = 0.0
    held_back: List[str] = []
    for row in pending_gate_sales():
        ap = str(row["airport_iata"]).upper()
        n = int(row["pending_sale_units"] or 0)
        if n <= 0:
            continue
        blockers = gate_sale_blockers(ap, n)
        # "Already queued" is the request-time guard; it must not block execution.
        blockers = [b for b in blockers if "already queued" not in b.lower()]
        if blockers:
            held_back.append(ap)
            _notify_player(
                _current_week_for_notice(),
                "GATE_SALE",
                f"{ap}: stand sale held — {blockers[0]} The stand is still yours.",
            )
            continue
        q = gate_sale_quote(ap, n)
        amount = float(q["proceeds"])
        db.execute(
            """
            UPDATE airport_gate_allocations
            SET gate_units = MAX(0, gate_units - ?), pending_sale_units = 0
            WHERE airport_iata = ? AND holder_id = 'PLAYER' AND status = 'ACTIVE'
            """,
            (n, ap),
        )
        apply_settlement_cash(amount)
        sold += n
        proceeds_total += amount
        _notify_player(
            _current_week_for_notice(),
            "GATE_SALE",
            f"{ap}: sold {n} stand(s) for {amount:,.0f}. You now hold "
            f"{_allocated_gates(ap, 'PLAYER')}.",
        )
    return {
        "units_sold": sold,
        "proceeds": round(proceeds_total, 2),
        "held": held_back,
    }
