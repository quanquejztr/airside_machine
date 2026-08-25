"""
Airline flight-number allocation.

Rules:
- Default format: {callsign}{NNNN} with random 1000–9999 (not sequential 001…).
- Sticky by directed route_id so recurring ONT–TPA keeps the same code.
- Same number may appear multiple times per day if time windows do not overlap.
- Overlap of [dep, arr) for the same flight_number in a week is rejected.
- Round-trip return legs prefer outbound_number + 1 (then −1) when free.
- Ferry codes (*9FR) are reserved and never allocated as product numbers.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from db import db
from engine.rng import seeded_rng

Interval = Tuple[float, float]

_FN_RE = re.compile(r"^([A-Z]{2,3})(\d{1,4})$")
_FERRY_SUFFIX = "9FR"
_NUM_MIN = 1000
_NUM_MAX = 9999
_MAX_RANDOM_TRIES = 80


def ensure_route_flight_numbers_table() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS route_flight_numbers (
            route_id TEXT PRIMARY KEY,
            flight_number TEXT NOT NULL,
            updated_week INTEGER NOT NULL DEFAULT 1
        )
        """
    )


def player_callsign() -> str:
    row = db.fetch_one("SELECT callsign FROM airline WHERE id = 1")
    cs = str(row["callsign"] or "").strip().upper() if row else ""
    return cs if cs else "FL"


def normalize_flight_number(raw: str, callsign: Optional[str] = None) -> str:
    s = str(raw or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    if s.endswith(_FERRY_SUFFIX):
        return s
    m = _FN_RE.match(s)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    # Digits only → prefix with callsign
    if s.isdigit():
        cs = (callsign or player_callsign()).upper()
        return f"{cs}{int(s)}"
    return s


def is_ferry_flight_number(fn: str) -> bool:
    return str(fn or "").strip().upper().endswith(_FERRY_SUFFIX)


def parse_flight_number(fn: str) -> Tuple[str, Optional[int]]:
    s = normalize_flight_number(fn)
    if is_ferry_flight_number(s):
        return s[: -len(_FERRY_SUFFIX)], None
    m = _FN_RE.match(s)
    if not m:
        return s, None
    return m.group(1), int(m.group(2))


def format_flight_number(callsign: str, number: int, width: int = 4) -> str:
    cs = str(callsign or "FL").strip().upper() or "FL"
    n = int(number)
    w = max(3, min(4, int(width)))
    if n > 9999:
        n = n % 10000
    if n < 0:
        n = abs(n) % 10000
    return f"{cs}{n:0{w}d}"


def intervals_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    """Half-open [start, end) overlap."""
    return float(a0) < float(b1) and float(a1) > float(b0)


def _active_segments_with_fn(flight_number: str, game_week: int) -> List[Dict[str, Any]]:
    fn = normalize_flight_number(flight_number)
    return (
        db.fetch_all(
            """
            SELECT segment_id, scheduled_dep_game_hour, scheduled_arr_game_hour, status
            FROM flight_segments
            WHERE game_week = ?
              AND UPPER(TRIM(flight_number)) = ?
              AND status != 'CANCELLED'
            """,
            (int(game_week), fn),
        )
        or []
    )


def assert_flight_number_ok(
    flight_number: str,
    intervals: Sequence[Interval],
    game_week: int,
    *,
    exclude_segment_ids: Optional[Iterable[str]] = None,
) -> None:
    """
    Raise ValueError if flight_number's intervals overlap each other or any
    existing non-cancelled segment with the same number this week.
    """
    fn = normalize_flight_number(flight_number)
    if not fn:
        raise ValueError("Flight number is empty.")
    if is_ferry_flight_number(fn):
        raise ValueError(f"Flight number '{fn}' is reserved for ferry repositioning.")

    ints = [(float(a), float(b)) for a, b in intervals if b > a]
    if not ints:
        raise ValueError(f"No valid time window for flight {fn}.")

    for i in range(len(ints)):
        for j in range(i + 1, len(ints)):
            if intervals_overlap(ints[i][0], ints[i][1], ints[j][0], ints[j][1]):
                raise ValueError(
                    f"Flight {fn} overlaps itself in this plan "
                    f"({ints[i][0]:.2f}–{ints[i][1]:.2f}h vs {ints[j][0]:.2f}–{ints[j][1]:.2f}h)."
                )

    exclude = {str(x) for x in (exclude_segment_ids or []) if x}
    for row in _active_segments_with_fn(fn, game_week):
        sid = str(row["segment_id"] or "")
        if sid in exclude:
            continue
        d0 = float(row["scheduled_dep_game_hour"] or 0)
        d1 = float(row["scheduled_arr_game_hour"] or 0)
        for a0, a1 in ints:
            if intervals_overlap(a0, a1, d0, d1):
                raise ValueError(
                    f"Flight {fn} overlaps an existing service "
                    f"({a0:.2f}–{a1:.2f}h vs {d0:.2f}–{d1:.2f}h)."
                )


def get_sticky_flight_number(route_id: str) -> Optional[str]:
    ensure_route_flight_numbers_table()
    row = db.fetch_one(
        "SELECT flight_number FROM route_flight_numbers WHERE route_id = ?",
        (str(route_id),),
    )
    if not row:
        # Fall back to most recent scheduled use of this route.
        used = db.fetch_one(
            """
            SELECT flight_number FROM flight_segments
            WHERE route_id = ? AND status != 'CANCELLED'
              AND flight_number NOT LIKE '%9FR'
            ORDER BY game_week DESC, scheduled_dep_game_hour DESC
            LIMIT 1
            """,
            (str(route_id),),
        )
        if used and used["flight_number"]:
            return normalize_flight_number(str(used["flight_number"]))
        return None
    return normalize_flight_number(str(row["flight_number"] or ""))


def remember_route_flight_number(route_id: str, flight_number: str, game_week: int = 1) -> None:
    ensure_route_flight_numbers_table()
    fn = normalize_flight_number(flight_number)
    if not fn or is_ferry_flight_number(fn):
        return
    db.execute(
        """
        INSERT INTO route_flight_numbers (route_id, flight_number, updated_week)
        VALUES (?, ?, ?)
        ON CONFLICT(route_id) DO UPDATE SET
            flight_number = excluded.flight_number,
            updated_week = excluded.updated_week
        """,
        (str(route_id), fn, int(game_week)),
    )


def _used_numeric_suffixes(callsign: str, game_week: int) -> set:
    cs = callsign.upper()
    rows = db.fetch_all(
        """
        SELECT DISTINCT flight_number FROM flight_segments
        WHERE game_week = ? AND status != 'CANCELLED'
        """,
        (int(game_week),),
    )
    sticky = db.fetch_all("SELECT flight_number FROM route_flight_numbers") or []
    out = set()
    for r in list(rows or []) + list(sticky):
        prefix, num = parse_flight_number(str(r["flight_number"] or ""))
        if num is not None and prefix == cs:
            out.add(int(num))
    return out


def _is_reverse_route(a_id: str, b_id: str) -> bool:
    a = db.fetch_one(
        "SELECT origin_iata, dest_iata FROM routes WHERE route_id = ?", (str(a_id),)
    )
    b = db.fetch_one(
        "SELECT origin_iata, dest_iata FROM routes WHERE route_id = ?", (str(b_id),)
    )
    if not a or not b:
        return False
    return (
        str(a["origin_iata"]).upper() == str(b["dest_iata"]).upper()
        and str(a["dest_iata"]).upper() == str(b["origin_iata"]).upper()
    )


def _try_candidate(
    fn: str,
    intervals: Sequence[Interval],
    game_week: int,
    *,
    exclude_segment_ids: Optional[Iterable[str]] = None,
    reserved: Optional[set] = None,
) -> bool:
    if not fn or (reserved and fn in reserved):
        return False
    try:
        assert_flight_number_ok(
            fn, intervals, game_week, exclude_segment_ids=exclude_segment_ids
        )
        return True
    except ValueError:
        return False


def allocate_flight_number(
    route_id: str,
    intervals: Sequence[Interval],
    game_week: int,
    *,
    preferred: Optional[str] = None,
    pair_with: Optional[str] = None,
    callsign: Optional[str] = None,
    exclude_segment_ids: Optional[Iterable[str]] = None,
    reserved: Optional[set] = None,
    remember: bool = True,
) -> str:
    """
    Pick a flight number for one directed route over the given time windows.
    preferred (manual) wins if it passes overlap checks.
    """
    ensure_route_flight_numbers_table()
    cs = (callsign or player_callsign()).upper()
    reserved = set(reserved or set())

    pref = normalize_flight_number(preferred or "", cs)
    if pref:
        assert_flight_number_ok(
            pref, intervals, game_week, exclude_segment_ids=exclude_segment_ids
        )
        if remember:
            remember_route_flight_number(route_id, pref, game_week)
        return pref

    candidates: List[str] = []
    sticky = get_sticky_flight_number(route_id)
    if sticky:
        candidates.append(sticky)

    if pair_with:
        pfx, num = parse_flight_number(pair_with)
        if num is not None:
            for delta in (1, -1, 2, -2):
                n = num + delta
                if _NUM_MIN <= n <= _NUM_MAX:
                    candidates.append(format_flight_number(pfx or cs, n, 4))

    for cand in candidates:
        if _try_candidate(
            cand,
            intervals,
            game_week,
            exclude_segment_ids=exclude_segment_ids,
            reserved=reserved,
        ):
            if remember:
                remember_route_flight_number(route_id, cand, game_week)
            return cand

    used = _used_numeric_suffixes(cs, game_week)
    rng = seeded_rng("fn", cs, route_id, int(game_week), str(intervals[:1]))
    for _ in range(_MAX_RANDOM_TRIES):
        n = rng.randint(_NUM_MIN, _NUM_MAX)
        if n in used:
            continue
        fn = format_flight_number(cs, n, 4)
        if _try_candidate(
            fn,
            intervals,
            game_week,
            exclude_segment_ids=exclude_segment_ids,
            reserved=reserved,
        ):
            if remember:
                remember_route_flight_number(route_id, fn, game_week)
            return fn
        used.add(n)

    # Exhaustive sweep as last resort
    for n in range(_NUM_MIN, _NUM_MAX + 1):
        if n in used:
            continue
        fn = format_flight_number(cs, n, 4)
        if _try_candidate(
            fn,
            intervals,
            game_week,
            exclude_segment_ids=exclude_segment_ids,
            reserved=reserved,
        ):
            if remember:
                remember_route_flight_number(route_id, fn, game_week)
            return fn

    raise ValueError(
        f"Could not allocate a free flight number for {route_id} "
        f"(overlap constraints exhausted)."
    )


def allocate_flight_numbers_for_legs(
    legs: Sequence[Dict[str, Any]],
    game_week: int,
    *,
    callsign: Optional[str] = None,
    remember: bool = True,
) -> List[str]:
    """
    legs: [{route_id, intervals: [(dep,arr),...], preferred?: str}]
    Returns one flight_number per leg (same order).

    Same number may be reused on later legs when intervals do not overlap.
    """
    cs = (callsign or player_callsign()).upper()
    out: List[str] = []
    for i, leg in enumerate(legs):
        route_id = str(leg.get("route_id") or "")
        intervals = list(leg.get("intervals") or [])
        preferred = str(leg.get("preferred") or "").strip() or None
        pair_with = None
        if i > 0 and _is_reverse_route(str(legs[i - 1].get("route_id") or ""), route_id):
            pair_with = out[i - 1]
        # Already-chosen numbers in this plan count as "existing" for overlap via
        # provisional inserts into a synthetic check: pass prior intervals by
        # asserting against them after allocation using assert among plan.
        fn = allocate_flight_number(
            route_id,
            intervals,
            game_week,
            preferred=preferred,
            pair_with=pair_with,
            callsign=cs,
            exclude_segment_ids=leg.get("exclude_segment_ids"),
            reserved=None,
            remember=remember,
        )
        # Overlap vs earlier legs in this same allocation batch.
        conflict = False
        for j, prev_fn in enumerate(out):
            if prev_fn != fn:
                continue
            prev_ints = list(legs[j].get("intervals") or [])
            for a0, a1 in intervals:
                for b0, b1 in prev_ints:
                    if intervals_overlap(a0, a1, b0, b1):
                        conflict = True
                        break
                if conflict:
                    break
            if conflict:
                break
        if conflict:
            if preferred:
                raise ValueError(
                    f"Flight {fn} overlaps another leg in this plan with the same number."
                )
            fn = allocate_flight_number(
                route_id,
                intervals,
                game_week,
                preferred=None,
                pair_with=pair_with,
                callsign=cs,
                exclude_segment_ids=leg.get("exclude_segment_ids"),
                reserved={fn},
                remember=remember,
            )
        out.append(fn)
    return out


def propose_flight_numbers_for_routes(
    route_ids: Sequence[str],
    *,
    callsign: Optional[str] = None,
) -> List[str]:
    """
    Preview helpers: sticky or random/paired proposals without writing sticky
    and without overlap checks (no times yet). Assign path re-validates.
    """
    cs = (callsign or player_callsign()).upper()
    ensure_route_flight_numbers_table()
    out: List[str] = []
    used_nums = _used_numeric_suffixes(cs, 0)
    # Also avoid proposing duplicates within this chain
    used_fns: set = set()
    rng = seeded_rng("fn-propose", cs, ",".join(route_ids))

    for i, rid in enumerate(route_ids):
        sticky = get_sticky_flight_number(str(rid))
        if sticky and sticky not in used_fns:
            out.append(sticky)
            used_fns.add(sticky)
            _, n = parse_flight_number(sticky)
            if n is not None:
                used_nums.add(n)
            continue

        if i > 0 and _is_reverse_route(str(route_ids[i - 1]), str(rid)):
            pfx, num = parse_flight_number(out[i - 1])
            if num is not None:
                for delta in (1, -1):
                    n = num + delta
                    fn = format_flight_number(pfx or cs, n, 4)
                    if fn not in used_fns and n not in used_nums:
                        out.append(fn)
                        used_fns.add(fn)
                        used_nums.add(n)
                        break
                else:
                    pass
                if len(out) == i + 1:
                    continue

        for _ in range(_MAX_RANDOM_TRIES):
            n = rng.randint(_NUM_MIN, _NUM_MAX)
            if n in used_nums:
                continue
            fn = format_flight_number(cs, n, 4)
            if fn in used_fns:
                continue
            out.append(fn)
            used_fns.add(fn)
            used_nums.add(n)
            break
        else:
            out.append(format_flight_number(cs, 1000 + i, 4))
    return out
