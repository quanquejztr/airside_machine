"""
Player hubs: the airports the airline actually bases itself at.

The game shipped with exactly one, `airline.home_hub_iata`. A second hub is unlocked by
building the first into a real network rather than bought, and once open it behaves like
a base in every respect: fleet can be delivered, sold and reconfigured there.

Two things did NOT need changing to support this, which is worth knowing before reading
further. Routes were never constrained to touch a hub — the hub only auto-opens the
companion leg as a convenience. And connecting-traffic capture in `engine.demand` keys on
`od_share` and the route count *at that airport*, not on the hub column, so a second hub
earns feed traffic on its own merit the moment it has routes. The economics were already
multi-hub; only the bookkeeping was not.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db


def _fc(key: str, default: float) -> float:
    try:
        v = db.get_financial_constant(key)
        return float(default if v is None else v)
    except Exception:
        return float(default)


def hub_open_min_routes() -> int:
    """Routes each existing hub needs before another may be opened."""
    return max(1, int(_fc("hub_open_min_routes", 10)))


def hub_mature_routes() -> int:
    """Routes a hub needs before it is judged by the stricter hub gate-utilisation rule."""
    return max(1, int(_fc("hub_mature_routes", 10)))


def _current_week() -> int:
    row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    try:
        return int(row["game_week"] or 1) if row else 1
    except (TypeError, ValueError):
        return 1


def primary_hub() -> str:
    row = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
    return str(row["home_hub_iata"]).upper().strip() if row and row["home_hub_iata"] else ""


def player_hubs() -> List[Dict[str, Any]]:
    """Every hub the player holds, primary first."""
    rows = db.fetch_all(
        "SELECT iata, opened_game_week, is_primary FROM player_hubs"
        " ORDER BY is_primary DESC, opened_game_week, iata"
    )
    out = [dict(r) for r in rows or []]
    if out:
        return out
    # A save predating the table, or one where the migration has not run yet.
    hub = primary_hub()
    return [{"iata": hub, "opened_game_week": 1, "is_primary": 1}] if hub else []


def hub_codes() -> List[str]:
    return [str(h["iata"]).upper() for h in player_hubs() if h.get("iata")]


def is_player_hub(iata: str) -> bool:
    return str(iata or "").upper().strip() in set(hub_codes())


def routes_at(iata: str) -> int:
    """Player routes touching this airport, in either direction."""
    from engine.demand import player_routes_at

    try:
        return int(player_routes_at(str(iata).upper().strip()))
    except Exception:
        return 0


def hub_is_mature(iata: str) -> bool:
    """True once a hub carries enough routes to be held to the hub utilisation rule.

    A hub opens with two free stands and almost no flying. Judging it at the hub
    threshold immediately would shed those stands within the grace period — clawing back
    the grant before the hub could be built with it.
    """
    return is_player_hub(iata) and routes_at(iata) >= hub_mature_routes()


def hub_open_blockers(iata: str) -> List[str]:
    """Why this airport cannot be opened as a hub right now. Empty means it can."""
    ap = str(iata or "").upper().strip()
    out: List[str] = []
    if not ap:
        return ["No airport given."]
    if not db.fetch_one("SELECT 1 FROM airports WHERE iata = ?", (ap,)):
        return [f"Airport '{ap}' not found."]
    if is_player_hub(ap):
        return [f"{ap} is already one of your hubs."]

    need = hub_open_min_routes()
    # Every existing hub must be real, not just the newest. Checking only the most recent
    # would let an older hub wither while new ones are opened for their starter capacity.
    for h in player_hubs():
        code = str(h["iata"]).upper()
        n = routes_at(code)
        if n < need:
            out.append(f"{code} has {n} route(s); each existing hub needs {need} before you open another.")
    return out


def hub_open_status() -> Dict[str, Any]:
    """What the UI needs to show the unlock condition and progress toward it."""
    need = hub_open_min_routes()
    hubs = []
    for h in player_hubs():
        code = str(h["iata"]).upper()
        n = routes_at(code)
        hubs.append(
            {
                "iata": code,
                "routes": n,
                "required": need,
                "meets_requirement": n >= need,
                "is_primary": bool(h.get("is_primary")),
                "opened_game_week": int(h.get("opened_game_week") or 1),
                "mature": n >= hub_mature_routes(),
            }
        )
    return {
        "hubs": hubs,
        "required_routes_per_hub": need,
        "can_open_another": all(x["meets_requirement"] for x in hubs) if hubs else False,
    }


def preview_hub(iata: str) -> Dict[str, Any]:
    """What opening this airport as a hub would cost and grant, before committing."""
    from engine.gates import is_auctioned_airport
    from engine.slots import is_slot_controlled, seed_slot_controlled_airports
    from engine.setup import PLAYER_STARTER_HUB_GATES, PLAYER_STARTER_HUB_SLOTS

    ap = str(iata or "").upper().strip()
    try:
        seed_slot_controlled_airports()
    except Exception:
        pass
    row = db.fetch_one("SELECT iata, name, city, country FROM airports WHERE iata = ?", (ap,))
    auctioned = bool(is_auctioned_airport(ap)) if row else False
    slotted = bool(is_slot_controlled(ap)) if row else False
    return {
        "iata": ap,
        "name": (row["name"] if row else None),
        "city": (row["city"] if row else None),
        "country": (row["country"] if row else None),
        "grants_gates": PLAYER_STARTER_HUB_GATES if auctioned else 0,
        "grants_slots": PLAYER_STARTER_HUB_SLOTS if slotted else 0,
        "gate_auctioned": auctioned,
        "slot_controlled": slotted,
        "routes_here": routes_at(ap),
        "blockers": hub_open_blockers(ap),
        "status": hub_open_status(),
    }


def open_hub(iata: str) -> Dict[str, Any]:
    """Open a new hub, granting the same starter capacity the first one received."""
    from engine.setup import grant_player_hub_starter_capacity

    ap = str(iata or "").upper().strip()
    blockers = hub_open_blockers(ap)
    if blockers:
        raise ValueError(blockers[0])

    week = _current_week()
    db.execute(
        "INSERT OR IGNORE INTO player_hubs (iata, opened_game_week, is_primary) VALUES (?, ?, 0)",
        (ap, week),
    )
    granted = grant_player_hub_starter_capacity(ap, from_game_week=week)
    try:
        from engine.gates import _notify_player

        bits = []
        if granted.get("gates"):
            bits.append(f"{granted['gates']} stand(s)")
        if granted.get("slots"):
            bits.append(f"{granted['slots']} weekly runway movements")
        _notify_player(
            week,
            "HUB_OPENED",
            f"{ap} opened as a hub"
            + (f", with {' and '.join(bits)} granted." if bits else "."),
        )
    except Exception:
        pass
    return {"iata": ap, "opened_game_week": week, "granted": granted}
