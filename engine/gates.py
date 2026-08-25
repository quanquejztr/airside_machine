"""
Phase 11 (revised): airport gate-use auctions.

- Airports with score >= gate_auction_score_threshold are auctioned.
- Gate units are concurrent stands: peak simultaneous occupancy must fit allocated units.
  One tail visit (arrive → turn → depart) uses one stand for [arr, dep + MTT).
- Ascending auction model: bids specify (units_requested, price_per_unit). The auction's current
  price is the max bid rounded up to gate_price_step.
- Resolution at settlement: allocate units to highest price bidders until supply, deduct cash, update allocations.
- Utilization = gate-hours / (gate_units * 168), where gate-hours are movements * MTT. That is a
  time-occupancy ratio, so realistic values are small: at MTT=30min a stand doing 10 flights a day
  scores about 0.06. Thresholds are calibrated to that scale (hub 0.06, non-hub 0.02), NOT to
  "fraction of stands used". Below threshold for grace_weeks consecutive weeks loses 1 unit, and a
  unit is never judged in a week before it was usable (see reset_weekly_gate_counters_and_enforce).
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
    row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    return int(row["game_week"] or 1) if row else 1


def is_auctioned_airport(iata: str) -> bool:
    ap = db.fetch_one("SELECT score FROM airports WHERE iata = ?", (iata.strip().upper(),))
    if not ap:
        return False
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


def _mtt_hours() -> float:
    """Minimum turnaround time (gate-occupancy window) in hours."""
    try:
        v = db.get_financial_constant("mtt_minutes")
        return float(v or 30) / 60.0
    except Exception:
        return 0.5


def _allocated_gates(iata: str, holder_id: str) -> int:
    row = db.fetch_one(
        """
        SELECT gate_units
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = ? AND status='ACTIVE'
        """,
        (iata.upper().strip(), str(holder_id)),
    )
    return int(row["gate_units"] or 0) if row else 0


def _visit_intervals_for_tail(mtt: float, events: list[tuple[float, str]]) -> list[tuple[float, float]]:
    """
    One aircraft, one stand: merge arrival + departure at the same airport into a single
    occupancy window [arr, dep + MTT) instead of double-counting deplaning and boarding.
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda x: (float(x[0]), 0 if x[1] == "A" else 1))
    out: list[tuple[float, float]] = []
    open_arr: Optional[float] = None
    for t, kind in ordered:
        t = float(t)
        if kind == "A":
            if open_arr is not None:
                out.append((open_arr, open_arr + mtt))
            open_arr = t
        else:
            if open_arr is not None and t + 1e-9 >= open_arr:
                out.append((open_arr, t + mtt))
                open_arr = None
            else:
                out.append((t, t + mtt))
    if open_arr is not None:
        out.append((open_arr, open_arr + mtt))
    return out


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
        out.extend(_visit_intervals_for_tail(m, events))
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
    mtt = _mtt_hours()
    rows = db.fetch_all(
        """
        SELECT
            fs.tail_number,
            fs.scheduled_dep_game_hour AS dep_h,
            fs.scheduled_arr_game_hour AS arr_h,
            COALESCE(fs.origin_iata, r.origin_iata) AS oi,
            COALESCE(fs.dest_iata,   r.dest_iata)   AS di
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ?
          AND fs.status != 'CANCELLED'
          AND (COALESCE(fs.origin_iata, r.origin_iata) = ? OR COALESCE(fs.dest_iata, r.dest_iata) = ?)
        """,
        (gw, ap, ap),
    )
    by_tail: dict[str, list[tuple[float, str]]] = {}
    planned_tail = "__PLANNED__"

    def _touch(tail: str, t: float, kind: str) -> None:
        if t <= 0 and kind == "A":
            return
        by_tail.setdefault(str(tail), []).append((float(t), kind))

    skip_tails = {str(t) for t in (exclude_tails or set()) if t}

    for r in rows:
        tail = str(r["tail_number"])
        if tail in skip_tails:
            continue
        dep = float(r["dep_h"] or 0.0)
        arr = float(r["arr_h"] or 0.0)
        oi = str(r["oi"] or "").upper()
        di = str(r["di"] or "").upper()
        if oi == ap:
            _touch(tail, dep, "D")
        if di == ap:
            _touch(tail, arr, "A")

    for s in extra_segments or []:
        tail = str(s.get("tail_number") or planned_tail)
        dep = float(s.get("dep_abs") or 0.0)
        arr = float(s.get("arr_abs") or 0.0)
        oi = str(s.get("origin_iata") or "").upper().strip()
        di = str(s.get("dest_iata") or "").upper().strip()
        if oi == ap:
            _touch(tail, dep, "D")
        if di == ap:
            _touch(tail, arr, "A")

    out: list[tuple[float, float]] = []
    for _tail, events in by_tail.items():
        out.extend(_visit_intervals_for_tail(mtt, events))
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


def assert_player_gate_capacity_for_new_segments(game_week: int, segments: list[dict]) -> None:
    """
    Enforce concurrent gates at each auctioned airport for the new segments.

    segments: list of dicts with keys:
      - origin_iata, dest_iata (str)
      - dep_abs, arr_abs (float absolute game hours)
      - tail_number (optional; one scheduling batch defaults to one tail)
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

    for ap in sorted(airports):
        if not ap or not is_auctioned_airport(ap):
            continue
        cap = _allocated_gates(ap, "PLAYER")
        if cap <= 0:
            raise ValueError(f"No gate allocation at {ap}. Bid in gate auctions to operate there.")
        exclude_tails = {
            str(s.get("tail_number"))
            for s in (segments or [])
            if s.get("tail_number")
        }
        peak = player_gate_peak_at_airport(
            ap, gw, extra_segments=segments, exclude_tails=exclude_tails or None
        )
        if peak > cap:
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
    airports = db.fetch_all("SELECT iata FROM airports WHERE score >= ?", (gate_score_threshold(),))
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


def _upsert_allocation(iata: str, holder_id: str, delta_units: int, *, effective_week: int | None = None) -> None:
    iata = iata.upper().strip()
    hid = str(holder_id)
    row = db.fetch_one(
        """
        SELECT allocation_id, gate_units, effective_week
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
        return
    new_units = max(0, int(row["gate_units"] or 0) + int(delta_units))
    # Keep the earlier effective week for existing stands. Pushing it forward when
    # you win *more* units would re-grace the whole pile and hide growth until next week.
    ew = int(row["effective_week"] or 1)
    if effective_week is not None:
        ew = min(ew, int(effective_week)) if int(row["gate_units"] or 0) > 0 else int(effective_week)
    db.execute(
        "UPDATE airport_gate_allocations SET gate_units = ?, effective_week = ? WHERE allocation_id = ?",
        (new_units, ew, str(row["allocation_id"])),
    )


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
            _upsert_allocation(iata, bidder, take, effective_week=next_week)
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
        db.execute("UPDATE airport_gate_auctions SET status = 'RESOLVED' WHERE auction_id = ?", (aid,))
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
            if hub_iata and airport_iata == hub_iata:
                thr = float(thr_hub)
            else:
                thr = float(thr_nonhub)
            if thr <= 0:
                thr = float(thr_legacy)

            touches = 0
            if holder == "PLAYER":
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

