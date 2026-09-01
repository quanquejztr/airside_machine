"""
AI gate inventory and bidding.

Peak-concurrency uses the same visit-merging model as player gates:
  one round-trip cycle occupies one stand through [arr, dep + MTT) at each airport.
Non-auctioned airports are free (shortfall 0).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from db import db
from engine.ai_log import append_ai_narrative
from engine.gates import (
    current_game_week,
    ensure_weekly_airport_auctions,
    gate_intervals_for_ai_at_airport,
    gate_intervals_from_tail_events,
    gate_min_price_per_unit,
    is_auctioned_airport,
    list_open_gate_auctions,
    peak_concurrency,
    submit_gate_bid,
)
from engine.routes import get_route
from engine.scheduling import week_base_hours


def _mtt_hours_default() -> float:
    try:
        v = db.get_financial_constant("mtt_minutes")
        return float(v or 30) / 60.0
    except Exception:
        return 0.5


def _flight_duration_hours(route_id: str, type_id: str) -> float:
    from engine.ai_flights import _flight_duration_hours as _fd

    return float(_fd(route_id, type_id))


def _type_for_pair(competitor_id: str, pair: str, fallback: Optional[str] = None) -> str:
    row = db.fetch_one(
        """
        SELECT aircraft_type_id FROM competitor_routes
        WHERE competitor_id = ? AND route_pair_id = ?
        """,
        (str(competitor_id), str(pair)),
    )
    if row and row["aircraft_type_id"]:
        return str(row["aircraft_type_id"])
    return str(fallback or "A320")


def _pair_endpoints(pair: str) -> tuple[str, str]:
    p = str(pair).upper().strip()
    if "-" not in p:
        return (p, p)
    a, b = p.split("-", 1)
    return (a.strip().upper(), b.strip().upper())


def ai_gates_held(competitor_id: str, iata: str) -> int:
    """ACTIVE gate units for this AI at iata, including next-week-effective wins."""
    ap = str(iata).upper().strip()
    cid = str(competitor_id)
    row = db.fetch_one(
        """
        SELECT gate_units
        FROM airport_gate_allocations
        WHERE airport_iata = ? AND holder_id = ? AND status = 'ACTIVE'
        """,
        (ap, cid),
    )
    return int(row["gate_units"] or 0) if row else 0


def cycle_fits_ai_gates(
    competitor_id: str,
    origin_iata: str,
    dest_iata: str,
    dep_out: float,
    arr_out: float,
    dep_in: float,
    arr_in: float,
    game_week: int,
    mtt: float,
) -> bool:
    """True if adding this round-trip still fits concurrent stands at auctioned airports."""
    cid = str(competitor_id)
    gw = int(game_week)
    oi = str(origin_iata).upper()
    di = str(dest_iata).upper()
    extra = [
        {
            "tail": f"{cid}:__NEW__",
            "origin_iata": oi,
            "dest_iata": di,
            "dep_out": float(dep_out),
            "arr_out": float(arr_out),
            "dep_in": float(dep_in),
            "arr_in": float(arr_in),
        }
    ]
    for ap in (oi, di):
        if not is_auctioned_airport(ap):
            continue
        held = ai_gates_held(cid, ap)
        if held <= 0:
            return False
        peak = peak_concurrency(
            gate_intervals_for_ai_at_airport(cid, ap, gw, extra_cycles=extra)
        )
        if peak > held:
            return False
    return True


def _intervals_for_pair_at(
    *,
    cid: str,
    pair: str,
    freq: int,
    iata: str,
    mtt: float,
    strategy: str,
    w0: float,
) -> list[tuple[float, float]]:
    from engine.ai_flights import bank_dep_hours

    a, b = _pair_endpoints(pair)
    ap = iata.upper().strip()
    if ap not in (a, b):
        return []
    out_id = f"{a}-{b}"
    in_id = f"{b}-{a}"
    out_rt = get_route(out_id)
    in_rt = get_route(in_id)
    if not out_rt or not in_rt:
        return []
    tid = _type_for_pair(cid, pair)
    out_dur = _flight_duration_hours(out_id, tid)
    in_dur = _flight_duration_hours(in_id, tid)
    slots = bank_dep_hours(max(1, int(freq)), strategy, w0, pair_id=pair)
    oi = str(out_rt["origin_iata"] or "").upper()
    di = str(out_rt["dest_iata"] or "").upper()
    in_oi = str(in_rt["origin_iata"] or "").upper()
    in_di = str(in_rt["dest_iata"] or "").upper()
    by_tail: dict[str, list[tuple[float, str]]] = {}
    for j, dep_out in enumerate(slots):
        tail = f"{cid}:{pair}:{j}"
        arr_out = dep_out + out_dur
        dep_in = arr_out + mtt
        arr_in = dep_in + in_dur
        events: list[tuple[float, str]] = []
        if oi == ap:
            events.append((dep_out, "D"))
        if di == ap:
            events.append((arr_out, "A"))
        if in_oi == ap:
            events.append((dep_in, "D"))
        if in_di == ap:
            events.append((arr_in, "A"))
        if events:
            by_tail[tail] = events
    return gate_intervals_from_tail_events(by_tail, mtt)


def _strategy(competitor_id: str) -> str:
    row = db.fetch_one(
        "SELECT strategy FROM competitors WHERE competitor_id = ?",
        (str(competitor_id),),
    )
    return str(row["strategy"] or "HUBSPOKE").upper() if row else "HUBSPOKE"


def _planned_freqs(
    competitor_id: str,
    candidate_pairs: list[str],
    freq_overrides: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    cid = str(competitor_id)
    out: Dict[str, int] = {}
    rows = db.fetch_all(
        """
        SELECT route_pair_id, frequency_per_week
        FROM competitor_routes
        WHERE competitor_id = ? AND status = 'ACTIVE'
        """,
        (cid,),
    )
    for r in rows:
        out[str(r["route_pair_id"]).upper()] = max(1, int(r["frequency_per_week"] or 1))
    for p in candidate_pairs or []:
        key = str(p).upper()
        if key not in out:
            out[key] = 2
    if freq_overrides:
        for k, v in freq_overrides.items():
            out[str(k).upper()] = max(1, int(v))
    return out


def ai_peak_concurrent_at(
    competitor_id: str,
    iata: str,
    candidate_pairs: list[str],
    mtt_hours: float,
    freq_overrides: Optional[Dict[str, int]] = None,
) -> int:
    """
    Peak concurrent stands needed at `iata` for ACTIVE routes plus candidate pairs.
    Uses the same bank / even-spread dep hours as AI segment spawn.
    """
    ap = str(iata).upper().strip()
    if not ap:
        return 0
    cid = str(competitor_id)
    mtt = float(mtt_hours if mtt_hours is not None else _mtt_hours_default())
    strat = _strategy(cid)
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    w0 = week_base_hours(gw)
    planned = _planned_freqs(cid, candidate_pairs, freq_overrides)
    intervals: list[tuple[float, float]] = []
    for pair, freq in planned.items():
        intervals.extend(
            _intervals_for_pair_at(
                cid=cid, pair=pair, freq=freq, iata=ap, mtt=mtt, strategy=strat, w0=w0
            )
        )
    return peak_concurrency(intervals)


def ai_gate_shortfall(
    competitor_id: str,
    iata: str,
    candidate_pairs: list[str],
    mtt_hours: float,
    freq_overrides: Optional[Dict[str, int]] = None,
) -> int:
    ap = str(iata).upper().strip()
    if not ap or not is_auctioned_airport(ap):
        return 0
    need = ai_peak_concurrent_at(
        competitor_id, ap, candidate_pairs, mtt_hours, freq_overrides=freq_overrides
    )
    held = ai_gates_held(competitor_id, ap)
    return int(max(0, int(need) - int(held)))


def ai_bid_for_airport(
    competitor_id: str,
    iata: str,
    units_needed: int,
    game_week: int,
    *,
    wtp_total: Optional[float] = None,
    spend_remaining: Optional[float] = None,
) -> bool:
    """
    Bid `units_needed` on this week's OPEN auction for `iata`.
    Price is capped at min(WTP per unit, floor × 2.5).
    """
    cid = str(competitor_id)
    ap = str(iata).upper().strip()
    want = int(units_needed)
    if want <= 0 or not is_auctioned_airport(ap):
        return False
    gw = int(game_week) if game_week else current_game_week()
    ensure_weekly_airport_auctions(current_game_week())
    auctions = list_open_gate_auctions()
    target = None
    for a in auctions:
        if str(a.get("airport_iata") or "").upper() == ap:
            target = a
            break
    if not target:
        return False
    comp = db.fetch_one(
        """
        SELECT aggressiveness, cash, weekly_slot_budget, weekly_route_budget
        FROM competitors WHERE competitor_id = ?
        """,
        (cid,),
    )
    if not comp:
        return False
    aggr = float(comp["aggressiveness"] or 1.0)
    cash = float(comp["cash"] or 0.0)
    slot_budget = float(comp["weekly_slot_budget"] or 0.0)
    if slot_budget <= 0:
        slot_budget = float(comp["weekly_route_budget"] or 0.0)
    cur = float(target["current_price_per_unit"] or 0.0)
    floor = float(gate_min_price_per_unit())
    price = max(1.0, cur * (0.90 + 0.30 * aggr))
    price = min(price, floor * 2.5)
    if wtp_total is not None and want > 0:
        price = min(price, max(floor, float(wtp_total) / float(want)))
    total = price * float(want)
    cap = min(cash, slot_budget) if slot_budget > 0 else cash
    if spend_remaining is not None:
        cap = min(cap, float(spend_remaining))
    if total > cap or cash < total:
        return False
    try:
        submit_gate_bid(str(target["auction_id"]), want, price, bidder_id=cid)
    except Exception:
        return False
    placed = db.fetch_one(
        "SELECT 1 FROM airport_gate_bids WHERE auction_id = ? AND bidder_id = ?",
        (str(target["auction_id"]), cid),
    )
    if not placed:
        return False
    append_ai_narrative(cid, f"BID_GATE:{ap}:{want}@{price:.0f}")
    return True


def _hub_gate_floor(strategy: str) -> int:
    return {
        "HUBSPOKE": 4,
        "BUDGET": 2,
        "PREMIUM": 3,
        "POINTTOPOINT": 2,
    }.get(str(strategy or "").upper(), 2)


def _hub_gate_cap() -> int:
    try:
        return max(4, int(float(db.get_financial_constant("ai_hub_gate_cap") or 24)))
    except (TypeError, ValueError):
        return 24


def sync_hub_gates_to_network(
    competitor_id: str,
    *,
    strategy: str | None = None,
    hub: str | None = None,
    mtt_hours: float | None = None,
) -> int:
    """
    Grant enough hub stands for the ACTIVE network's peak concurrency (+1 buffer).

    Starter megas were seeded with only 2–4 stands while running 6×5 weekly banks,
    so spawn hit GATE_PEAK every week while routes stayed ACTIVE.
    """
    from engine.gates import _upsert_allocation, current_game_week

    cid = str(competitor_id)
    if strategy is None or hub is None:
        row = db.fetch_one(
            "SELECT strategy, home_hub_iata FROM competitors WHERE competitor_id = ?",
            (cid,),
        )
        if not row:
            return 0
        strategy = str(row["strategy"] or "HUBSPOKE")
        hub = str(row["home_hub_iata"] or "")
    ap = str(hub).upper().strip()
    if not ap or not is_auctioned_airport(ap):
        return 0
    mtt = float(mtt_hours if mtt_hours is not None else _mtt_hours_default())
    peak = ai_peak_concurrent_at(cid, ap, [], mtt)
    base = _hub_gate_floor(strategy)
    target = min(_hub_gate_cap(), max(base, int(peak) + 1))
    held = ai_gates_held(cid, ap)
    if held >= target:
        return 0
    _upsert_allocation(ap, cid, target - held, effective_week=max(1, current_game_week()))
    append_ai_narrative(cid, f"HUB_GATES:{ap}:{held}->{target}")
    return target - held


def seed_spoke_gate_if_needed(competitor_id: str, iata: str) -> None:
    """Give 1 stand at an auctioned spoke so a starter route is legal on week 1."""
    from engine.gates import _upsert_allocation

    cid = str(competitor_id)
    ap = str(iata).upper().strip()
    if not ap or not is_auctioned_airport(ap):
        return
    if ai_gates_held(cid, ap) > 0:
        return
    _upsert_allocation(ap, cid, 1, effective_week=1)


def seed_competitor_hub_gates(competitor_id: str, strategy: str, hub: str) -> None:
    """Ensure hub stands cover the current ACTIVE network (see sync_hub_gates_to_network)."""
    sync_hub_gates_to_network(competitor_id, strategy=strategy, hub=hub)
