from __future__ import annotations

import json
import random
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from db import db
from engine.rng import stable_hash
from engine.ai_gates import (
    ai_bid_for_airport,
    ai_gate_shortfall,
    seed_competitor_hub_gates,
    seed_spoke_gate_if_needed,
)
from engine.slots import seed_competitor_slot_capacity, slot_freq_cap
from engine.ai_log import (
    append_ai_narrative,
    begin_ai_narrative,
    competitor_display_name,
    push_ai_news,
    take_ai_narrative,
)
from engine.cabin import default_premium_first_ticket_fares
from engine.routes import get_route, haversine_distance, calculate_route_acquisition_cost

# =============================================================================
# Phase 11+ AI System (per ai_system_design.pdf)
# =============================================================================


def _rget(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        keys = row.keys() if hasattr(row, "keys") else None
        if keys is not None and key not in keys:
            return default
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError, TypeError):
        return default


def _fc(key: str, default: float) -> float:
    row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
    if not row:
        return float(default)
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return float(default)


def _ai_eval_interval() -> int:
    return int(_fc("ai_evaluation_interval_weeks", 2))


def _ai_candidate_pool_size() -> int:
    return int(_fc("ai_candidate_pool_size", 15))


def _ai_exit_loss_threshold() -> int:
    return int(_fc("ai_exit_loss_threshold", 4))


def _ai_min_profit_to_open() -> float:
    return float(_fc("ai_min_profit_to_open", 8000.0))


def _ai_hub_radius_nm() -> float:
    return float(_fc("ai_hub_radius_nm", 2500.0))


def _ai_price_undercut_pct() -> float:
    return float(_fc("ai_price_undercut_pct", 0.08))


def _ai_price_recover_pct() -> float:
    return float(_fc("ai_price_recover_pct", 0.03))


def _ai_suspension_threshold() -> float:
    return float(_fc("ai_suspension_threshold", 0.15))


def _current_month() -> int:
    gs = db.fetch_one("SELECT current_month FROM game_state WHERE id = 1")
    return int(gs["current_month"] or 1) if gs else 1


def _mtt_hours_ai() -> float:
    try:
        v = db.get_financial_constant("mtt_minutes")
        return float(v or 30) / 60.0
    except Exception:
        return 0.5


def canonical_pair_for_competitor(competitor_id: str, route_id: str) -> tuple[str, str, str]:
    """
    Returns (route_pair_id, outbound_route_id, inbound_route_id) for a directional route_id.
    Canonical: if one endpoint is competitor's home hub -> hub-other; else alphabetical min-max.
    """
    rid = str(route_id).upper().strip()
    if "-" not in rid:
        return (rid, rid, rid)
    a, b = rid.split("-", 1)
    a = a.strip().upper()
    b = b.strip().upper()
    hub = db.fetch_one("SELECT home_hub_iata FROM competitors WHERE competitor_id = ?", (competitor_id,))
    hub_i = str(hub["home_hub_iata"]).upper() if hub and hub["home_hub_iata"] else ""
    if hub_i and (a == hub_i or b == hub_i):
        other = b if a == hub_i else a
        pair = f"{hub_i}-{other}"
        out_id = pair
        in_id = f"{other}-{hub_i}"
        return (pair, out_id, in_id)
    pair = f"{a}-{b}" if a <= b else f"{b}-{a}"
    x, y = pair.split("-", 1)
    return (pair, f"{x}-{y}", f"{y}-{x}")


def _route_pair_components(route_pair_id: str) -> tuple[str, str]:
    rp = str(route_pair_id).upper().strip()
    if "-" not in rp:
        return (rp, rp)
    a, b = rp.split("-", 1)
    return (a.strip().upper(), b.strip().upper())


def _ensure_route_rows_exist_for_pair(route_pair_id: str) -> tuple[str, str]:
    """
    Ensure both directional `routes` rows exist for a pair. Returns (outbound_id, inbound_id).
    This is required because the AI candidate generator enumerates airport pairs, not existing routes.
    """
    a, b = _route_pair_components(route_pair_id)
    out_id = f"{a}-{b}"
    in_id = f"{b}-{a}"

    def _insert_dir(o: str, d: str) -> None:
        rid = f"{o}-{d}"
        if get_route(rid):
            return
        ao = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (o,))
        ad = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (d,))
        if not ao or not ad:
            return
        dist = haversine_distance(float(ao["lat"]), float(ao["lon"]), float(ad["lat"]), float(ad["lon"]))
        from engine.route_demand import compute_base_demand

        demand_info = compute_base_demand(dist, dict(ao), dict(ad))
        bd_b = int(demand_info["base_demand_business"])
        bd_l = int(demand_info["base_demand_leisure"])
        demand_source = str(demand_info.get("demand_source") or "LEGACY")
        pb = round(dist * 0.20, 2)
        pl = round(dist * 0.10, 2)
        pp, pf = default_premium_first_ticket_fares(float(pl), float(pb))
        db.execute(
            """
            INSERT INTO routes (
                route_id, origin_iata, dest_iata, distance_nm,
                base_demand_business, base_demand_leisure,
                price_business, price_leisure, price_premium_economy, price_first,
                competitor_share_this_week, is_active, demand_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 1, ?)
            """,
            (rid, o, d, dist, bd_b, bd_l, float(pb), float(pl), float(pp), float(pf), demand_source),
        )

    _insert_dir(a, b)
    _insert_dir(b, a)
    return (out_id, in_id)

_COMPETITORS_JSON = Path(__file__).resolve().parent.parent / "data" / "competitors.json"

_FALLBACK_COMPETITOR_SPECS: List[Dict[str, Any]] = [
    {
        "competitor_id": "AI_MERIDIAN",
        "name": "Meridian Airways",
        "callsign": "MER",
        "home_hub_iata": "ATL",
        "cash": 80_000_000.0,
        "strategy": "HUBSPOKE",
        "aggressiveness": 1.5,
        "risk_tolerance": 0.55,
        "expansion_rate": 3,
        "fleet_size": 8,
        "max_fleet_size": 20,
        "weekly_route_budget": 650_000.0,
        "bid_probability": 0.82,
        "weekly_slot_budget": 220_000.0,
        "reputation": 72.0,
        "starter_spokes": ["MIA", "JFK", "DFW", "BOS"],
        "starter_count": 4,
        "growth": {"style": "aggressive", "deepen_per_eval": 2, "extra_exit_grace_weeks": 3, "require_flown_pnl_to_exit": True},
    },
]


def load_competitor_specs() -> List[Dict[str, Any]]:
    """Roster from data/competitors.json (other models can fill this file)."""
    try:
        raw = json.loads(_COMPETITORS_JSON.read_text(encoding="utf-8"))
        rows = list(raw.get("competitors") or [])
        if rows:
            return rows
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return list(_FALLBACK_COMPETITOR_SPECS)


def _spec_for(competitor_id: str) -> Dict[str, Any]:
    cid = str(competitor_id)
    for spec in load_competitor_specs():
        if str(spec.get("competitor_id") or "") == cid:
            return spec
    return {}


def _growth_profile(competitor_id: str) -> Dict[str, Any]:
    spec = _spec_for(competitor_id)
    g = spec.get("growth") if isinstance(spec.get("growth"), dict) else {}
    style = str(g.get("style") or spec.get("strategy") or "measured").lower()
    agr = float(spec.get("aggressiveness") or 1.0)
    if style == "aggressive" or agr >= 1.5:
        deepen, extra = 2, 3
    elif style == "selective":
        deepen, extra = 1, 5
    else:
        deepen, extra = 1, 2
    if g.get("deepen_per_eval") is not None:
        try:
            deepen = max(1, int(g["deepen_per_eval"]))
        except (TypeError, ValueError):
            pass
    if g.get("extra_exit_grace_weeks") is not None:
        try:
            extra = max(0, int(g["extra_exit_grace_weeks"]))
        except (TypeError, ValueError):
            pass
    need_pnl = g.get("require_flown_pnl_to_exit")
    if need_pnl is None:
        need_pnl = True
    exp = int(spec.get("expansion_rate") or 1)
    # Tails added per evaluation. One was hardcoded, which caps growth at ~26
    # aircraft a year however rich or aggressive a carrier is -- far too slow for
    # a fleet ceiling in the hundreds to ever be approached.
    fleet_growth = 2 if style == "aggressive" else 1
    if g.get("fleet_growth_per_eval") is not None:
        try:
            fleet_growth = max(1, int(g["fleet_growth_per_eval"]))
        except (TypeError, ValueError):
            pass
    return {
        "style": style,
        "deepen_per_eval": deepen,
        "extra_exit_grace_weeks": extra,
        "require_flown_pnl_to_exit": bool(need_pnl),
        "expansion_rate": max(1, exp),
        "fleet_growth_per_eval": fleet_growth,
    }


def _competitor_stance(competitor_id: str) -> str:
    row = db.fetch_one("SELECT stance FROM competitors WHERE competitor_id = ?", (str(competitor_id),))
    return str(_rget(row, "stance", "GROW") or "GROW").upper()


def _ai_stance(competitor_id: str, game_week: int) -> str:
    """GROW / DEFEND / ATTACK / CONSOLIDATE from cash runway and flown net."""
    from engine.ai_economics import weekly_fixed_cost

    cid = str(competitor_id)
    gw = int(game_week)
    comp = db.fetch_one("SELECT * FROM competitors WHERE competitor_id = ?", (cid,))
    if not comp:
        return "GROW"
    cash = float(comp["cash"] or 0.0)
    strat = str(comp["strategy"] or "HUBSPOKE").upper()
    agr = float(comp["aggressiveness"] or 1.0)
    fixed = max(1.0, weekly_fixed_cost(cid))
    runway = cash / fixed
    agg = db.fetch_one(
        """
        SELECT COALESCE(SUM(actual_weekly_net_avg), 0) AS net,
               COALESCE(SUM(CASE WHEN contested = 1 THEN 1 ELSE 0 END), 0) AS n
        FROM competitor_routes
        WHERE competitor_id = ? AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
        """,
        (cid,),
    )
    flown_net = float(agg["net"] or 0.0) if agg else 0.0
    contested_n = int(agg["n"] or 0) if agg else 0
    if strat == "BUDGET" or agr >= 1.5:
        floor = float(_fc("ai_runway_weeks_budget", 8.0))
    elif strat == "PREMIUM":
        floor = float(_fc("ai_runway_weeks_premium", 16.0))
    else:
        floor = float(_fc("ai_runway_weeks_hubspoke", 20.0))
    grace = int(_fc("ai_stance_grace_weeks", 4))
    if runway < floor or cash < 0:
        # Starter networks need a few weeks to fly before lease/utilization math
        # looks sane; instant CONSOLIDATE froze the whole roster on week 1.
        if gw <= grace and cash > 0:
            desired = "GROW" if flown_net >= 0 else "DEFEND"
        else:
            desired = "CONSOLIDATE"
    elif contested_n >= 2 and agr >= 1.35 and runway > floor * 1.15 and flown_net >= 0:
        desired = "ATTACK"
    elif contested_n >= 1 and flown_net < 0:
        desired = "DEFEND"
    elif runway >= floor:
        desired = "GROW"
    else:
        desired = "DEFEND"
    old = str(_rget(comp, "stance", "GROW") or "GROW").upper()
    since = int(_rget(comp, "stance_since_week", 0) or 0)
    if desired != old:
        if desired == "CONSOLIDATE" or since <= 0 or (gw - since) >= 2:
            old, since = desired, gw
    db.execute(
        "UPDATE competitors SET stance = ?, stance_since_week = ? WHERE competitor_id = ?",
        (old, int(since), cid),
    )
    sign = "+" if flown_net >= 0 else ""
    append_ai_narrative(cid, f"STANCE:{old}:runway{runway:.0f}w:net{sign}{flown_net / 1000.0:.0f}k")
    return old


def _stance_expand_cap(competitor_id: str, base_expansion: int) -> int:
    stance = _competitor_stance(competitor_id)
    if stance == "CONSOLIDATE":
        return 1
    profile = _growth_profile(competitor_id)
    extra = 2 if str(profile["style"]) == "aggressive" else 1
    if stance == "GROW":
        return max(0, int(base_expansion) + extra)
    if stance == "ATTACK":
        return max(0, int(base_expansion))
    return max(0, int(base_expansion) - 1)


def _hours_to_add_pair(competitor_id: str, route_pair_id: str, frequency: int, type_id: str) -> float:
    from engine.ai_economics import block_hours

    out_id, _in = _ensure_route_rows_exist_for_pair(route_pair_id)
    rt = get_route(out_id)
    dist = float((rt["distance_nm"] if rt else 0) or 800.0)
    return 2.0 * max(1, int(frequency)) * block_hours(dist, type_id)


def _ensure_ai_hub_airports() -> None:
    """Insert any competitor home hubs missing from the airport catalogue (existing saves)."""
    for spec in load_competitor_specs():
        ap = str(spec.get("home_hub_iata") or "").upper()
        if not ap or db.fetch_one("SELECT 1 FROM airports WHERE iata = ?", (ap,)):
            continue
        meta = spec.get("hub_airport") if isinstance(spec.get("hub_airport"), dict) else None
        if not meta:
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO airports (
                iata, icao, name, city, country, lat, lon,
                runway_length_ft, gate_count, timezone, score, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ap,
                str(meta.get("icao") or ap),
                str(meta.get("name") or ap),
                str(meta.get("city") or ""),
                str(meta.get("country") or ""),
                float(meta.get("lat") or 0.0),
                float(meta.get("lon") or 0.0),
                int(meta.get("runway_length_ft") or 10000),
                int(meta.get("gate_count") or 20),
                str(meta.get("timezone") or "UTC"),
                int(meta.get("score") or 500000),
                str(meta.get("category") or "large_airport"),
            ),
        )


def _brand_power_from_rep(rep: float) -> float:
    return 0.85 + (float(rep) / 100.0) * (1.20 - 0.85)


def _apply_competitor_personality(spec: Dict[str, Any]) -> None:
    """Refresh knobs from the roster without touching cash or live network."""
    cid = str(spec["competitor_id"])
    rep = float(spec.get("reputation") or 60.0)
    db.execute(
        """
        UPDATE competitors SET
            name = ?, callsign = ?, home_hub_iata = ?,
            strategy = ?, aggressiveness = ?, risk_tolerance = ?, expansion_rate = ?,
            max_fleet_size = ?, weekly_route_budget = ?, bid_probability = ?,
            weekly_slot_budget = ?, reputation = ?, brand_power = ?
        WHERE competitor_id = ?
        """,
        (
            str(spec["name"]),
            str(spec["callsign"]).upper()[:3],
            str(spec["home_hub_iata"]).upper(),
            str(spec.get("strategy") or "HUBSPOKE").upper(),
            float(spec.get("aggressiveness") or 1.0),
            float(spec.get("risk_tolerance") or 0.5),
            int(spec.get("expansion_rate") or 1),
            int(spec.get("max_fleet_size") or 16),
            float(spec.get("weekly_route_budget") or 300000.0),
            float(spec.get("bid_probability") or 0.55),
            float(spec.get("weekly_slot_budget") or 100000.0),
            rep,
            _brand_power_from_rep(rep),
            cid,
        ),
    )


_competitors_seed_lock = threading.Lock()
_competitors_seeded = False


def ensure_competitors_seeded() -> None:
    """Insert new roster airlines; refresh personality knobs for existing ones."""
    global _competitors_seeded
    if _competitors_seeded:
        return
    with _competitors_seed_lock:
        if _competitors_seeded:
            return
        _ensure_competitors_seeded_body()
        _competitors_seeded = True


def reset_competitor_seed_cache() -> None:
    """
    Forget that the roster has been seeded this process.

    Must be called after anything that wipes competitor state (purge / reset airline),
    otherwise the one-shot _competitors_seeded flag makes ensure_competitors_seeded() a
    no-op for the rest of the session and the AI never gets routes or aircraft back.
    """
    global _competitors_seeded
    with _competitors_seed_lock:
        _competitors_seeded = False


def _ensure_competitors_seeded_body() -> None:
    _ensure_ai_hub_airports()
    for spec in load_competitor_specs():
        cid = str(spec.get("competitor_id") or "").strip()
        if not cid:
            continue
        row = db.fetch_one("SELECT competitor_id FROM competitors WHERE competitor_id = ?", (cid,))
        if row:
            _apply_competitor_personality(spec)
            continue
        hub = str(spec.get("home_hub_iata") or "").upper()
        strat = str(spec.get("strategy") or "HUBSPOKE").upper()
        fleet_sz = int(spec.get("fleet_size") or 6)
        rep = float(spec.get("reputation") or 60.0)
        db.execute(
            """
            INSERT INTO competitors (
                competitor_id, name, callsign, home_hub_iata, cash,
                strategy, aggressiveness, risk_tolerance, expansion_rate,
                fleet_size, max_fleet_size, weekly_route_budget,
                bid_probability, weekly_slot_budget,
                reputation, brand_power,
                consecutive_loss_weeks, last_evaluation_week
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                cid,
                str(spec.get("name") or cid),
                str(spec.get("callsign") or "AI").upper()[:3],
                hub,
                float(spec.get("cash") or 25_000_000.0),
                strat,
                float(spec.get("aggressiveness") or 1.0),
                float(spec.get("risk_tolerance") or 0.5),
                int(spec.get("expansion_rate") or 1),
                fleet_sz,
                int(spec.get("max_fleet_size") or 16),
                float(spec.get("weekly_route_budget") or 300_000.0),
                float(spec.get("bid_probability") or 0.55),
                float(spec.get("weekly_slot_budget") or 100_000.0),
                rep,
                _brand_power_from_rep(rep),
            ),
        )
        _seed_ai_fleet_for_competitor(cid, fleet_sz, strat)
        seed_competitor_hub_gates(cid, strat, hub)
    seed_competitor_initial_routes()
    # Existing save games: grant hub stands if the AI still has none.
    for row in db.fetch_all("SELECT competitor_id, strategy, home_hub_iata FROM competitors"):
        seed_competitor_hub_gates(
            str(row["competitor_id"]),
            str(row["strategy"] or "HUBSPOKE"),
            str(row["home_hub_iata"] or ""),
        )
    # Existing save games: backfill ai_fleet. _seed_ai_fleet_for_competitor only runs on the
    # INSERT branch above, so competitors that predate a roster change end up with zero tails —
    # which starves aircraft choice ("prefer a type already owned"), the block-hour budget, and
    # the weekly lease charge, and makes every candidate look unaffordable.
    for row in db.fetch_all(
        """
        SELECT c.competitor_id, c.strategy, c.fleet_size
        FROM competitors c
        WHERE NOT EXISTS (SELECT 1 FROM ai_fleet f WHERE f.competitor_id = c.competitor_id)
        """
    ):
        _seed_ai_fleet_for_competitor(
            str(row["competitor_id"]),
            int(row["fleet_size"] or 0),
            str(row["strategy"] or "HUBSPOKE"),
        )
    try:
        from engine.slots import seed_slot_controlled_airports

        seed_slot_controlled_airports()
    except Exception:
        pass
    try:
        from engine.gates import current_game_week, ensure_weekly_airport_auctions

        ensure_weekly_airport_auctions(current_game_week())
    except Exception:
        pass
    # Legalize already-seeded spokes that land at auctioned airports.
    for r in db.fetch_all(
        """
        SELECT competitor_id, route_pair_id
        FROM competitor_routes
        WHERE status IN ('ACTIVE','SUSPENDED')
        """
    ):
        a, b = _route_pair_components(str(r["route_pair_id"]))
        seed_spoke_gate_if_needed(str(r["competitor_id"]), a)
        seed_spoke_gate_if_needed(str(r["competitor_id"]), b)
    for row in db.fetch_all("SELECT competitor_id FROM competitors"):
        _sync_competitor_slots(str(row["competitor_id"]), 1)


def _airport_freq_map(competitor_id: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in db.fetch_all(
        """
        SELECT route_pair_id, frequency_per_week FROM competitor_routes
        WHERE competitor_id = ? AND COALESCE(status, 'ACTIVE') IN ('ACTIVE', 'SUSPENDED')
        """,
        (str(competitor_id),),
    ):
        pair = str(r["route_pair_id"] or "")
        parts = pair.split("-", 1)
        if len(parts) != 2:
            continue
        freq = max(1, int(r["frequency_per_week"] or 1))
        for ap in parts:
            out[ap] = max(out.get(ap, 0), freq)
    return out


def _sync_competitor_slots(competitor_id: str, game_week: int | None = None) -> None:
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(game_week if game_week is not None else (gs["game_week"] if gs else 1))
    seed_competitor_slot_capacity(str(competitor_id), _airport_freq_map(competitor_id), start_week=gw)


def _seed_ai_fleet_for_competitor(competitor_id: str, fleet_size: int, strategy: str) -> None:
    """
    Seed ai_fleet rows for an AI. For now: generate N tails of a strategy-appropriate type.
    """
    cid = str(competitor_id)
    n = max(0, int(fleet_size))
    if n <= 0:
        return
    # Strategy-appropriate types; avoid always picking max-range (= max lease).
    if strategy == "PREMIUM":
        pick = db.fetch_one(
            "SELECT type_id FROM aircraft_types WHERE type_id = 'B789' LIMIT 1"
        ) or db.fetch_one(
            "SELECT type_id FROM aircraft_types WHERE category = 'WIDE' "
            "ORDER BY weekly_lease_cost ASC LIMIT 1"
        )
    elif strategy == "BUDGET":
        pick = db.fetch_one(
            "SELECT type_id FROM aircraft_types WHERE type_id IN ('CRJ7','E175','A320') "
            "ORDER BY weekly_lease_cost ASC LIMIT 1"
        )
    else:
        pick = db.fetch_one(
            "SELECT type_id FROM aircraft_types WHERE type_id IN ('A320','B738') "
            "ORDER BY weekly_lease_cost ASC LIMIT 1"
        )
    type_id = str(pick["type_id"]) if pick and pick["type_id"] else "A320"
    for i in range(1, n + 1):
        tail = f"{cid.split('_')[-1][:2]}-{i:03d}"
        if db.fetch_one("SELECT 1 FROM ai_fleet WHERE ai_tail = ?", (tail,)):
            continue
        db.execute(
            "INSERT OR REPLACE INTO ai_fleet (ai_tail, competitor_id, type_id, status, assigned_route_pair_id) VALUES (?, ?, ?, 'ACTIVE', NULL)",
            (tail, cid, type_id),
        )


def _seed_distance_ok(strategy: str, dist_nm: float) -> bool:
    d = float(dist_nm)
    strat = str(strategy or "").upper()
    if strat == "BUDGET":
        return 80.0 <= d < 1200.0
    if strat == "PREMIUM":
        return 1400.0 <= d <= 4200.0
    return 150.0 <= d <= 2200.0


def _insert_starter_route(cid: str, hub: str, other: str, frequency: int = 2) -> bool:
    pair_id = f"{hub}-{other}"
    out_id, in_id = _ensure_route_rows_exist_for_pair(pair_id)
    rt = get_route(out_id)
    if not rt:
        return False
    fb = float(rt["price_business"]) * random.uniform(0.95, 1.08)
    fl = float(rt["price_leisure"]) * random.uniform(0.95, 1.08)
    tid = ai_resolve_aircraft_type(cid, pair_id) or "A320"
    freq = max(1, min(7, int(frequency or 2)))
    db.execute(
        """
        INSERT OR REPLACE INTO competitor_routes (
            competitor_id, route_pair_id, outbound_route_id, inbound_route_id,
            fare_business, fare_leisure, frequency_per_week, aircraft_type_id,
            opened_week, status, estimated_weekly_profit, actual_weekly_revenue_avg,
            consecutive_loss_weeks, contested, market_share
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', 0.0, 0.0, 0, 0, 0.0)
        """,
        (cid, pair_id, out_id, in_id, fb, fl, freq, tid),
    )
    return True


def seed_competitor_initial_routes() -> None:
    """
    Seed a small starter network from the roster. Full evaluation expands from there.
    Existing save networks are left alone.
    """
    for spec in load_competitor_specs():
        cid = str(spec.get("competitor_id") or "")
        hub = str(spec.get("home_hub_iata") or "").upper()
        strat = str(spec.get("strategy") or "HUBSPOKE").upper()
        exists_any = db.fetch_one("SELECT 1 FROM competitor_routes WHERE competitor_id = ? LIMIT 1", (cid,))
        if exists_any:
            continue
        hub_row = db.fetch_one(
            "SELECT lat, lon, country FROM airports WHERE iata = ?", (hub,)
        )
        if not hub_row:
            continue
        hlat, hlon = float(hub_row["lat"]), float(hub_row["lon"])
        hub_country = str(hub_row["country"] or "")
        spokes = [str(x).upper() for x in (spec.get("starter_spokes") or [])]
        want = max(3, min(6, int(spec.get("starter_count") or 3)))
        freq0 = int(spec.get("starter_frequency") or 2)
        picked = 0
        used: set[str] = set()

        def _try(other: str) -> None:
            nonlocal picked
            if picked >= want:
                return
            oth = str(other).upper()
            if oth == hub or oth in used:
                return
            ap = db.fetch_one(
                "SELECT iata, lat, lon, score, category FROM airports WHERE iata = ?",
                (oth,),
            )
            if not ap:
                return
            dist = haversine_distance(hlat, hlon, float(ap["lat"]), float(ap["lon"]))
            if not _seed_distance_ok(strat, dist):
                return
            if strat == "PREMIUM" and (
                float(ap["score"] or 0) < 600_000 or str(ap["category"]) != "large_airport"
            ):
                return
            if _insert_starter_route(cid, hub, oth, freq0):
                seed_spoke_gate_if_needed(cid, oth)
                used.add(oth)
                picked += 1

        for other in spokes:
            _try(other)
            if picked >= want:
                break

        if picked >= want:
            continue
        cand = db.fetch_all(
            """
            SELECT a.iata, a.lat, a.lon, a.score, a.category, a.country
            FROM airports a
            WHERE a.iata != ?
            """,
            (hub,),
        )
        ranked: List[tuple[float, str]] = []
        for row in cand:
            other = str(row["iata"]).upper()
            dist = haversine_distance(hlat, hlon, float(row["lat"]), float(row["lon"]))
            if not _seed_distance_ok(strat, dist):
                continue
            if strat == "PREMIUM" and (
                float(row["score"] or 0) < 600_000 or str(row["category"]) != "large_airport"
            ):
                continue
            score = float(row["score"] or 0)
            if strat != "PREMIUM" and str(row["country"] or "") == hub_country:
                score += 400_000.0
            if strat == "PREMIUM":
                rank = score
            else:
                rank = score - dist * 40.0
            ranked.append((rank, other))
        ranked.sort(key=lambda x: x[0], reverse=True)
        for _rank, other in ranked:
            _try(other)
            if picked >= want:
                break


def ai_update_load_streaks(competitor_id: str, demand_week: int, current_month: int) -> None:
    from engine.demand import estimate_competitor_route_load_factor

    cid = str(competitor_id)
    rows = db.fetch_all(
        """
        SELECT outbound_route_id, route_pair_id, COALESCE(consecutive_loss_weeks, 0) AS lw,
               COALESCE(actual_weekly_revenue_avg, 0) AS actual_rev
        FROM competitor_routes
        WHERE competitor_id = ?
        """,
        (cid,),
    )
    for r in rows:
        if float(r["actual_rev"] or 0) <= 0:
            continue
        rid = str(r["outbound_route_id"])
        lf = estimate_competitor_route_load_factor(
            cid, rid, game_week=int(demand_week), current_month=int(current_month)
        )
        pair = str(r["route_pair_id"])
        if lf is None:
            continue
        lw = int(r["lw"] or 0)
        if float(lf) < 0.55:
            lw += 1
        else:
            lw = 0
        db.execute(
            """
            UPDATE competitor_routes
            SET consecutive_loss_weeks = ?
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (int(lw), cid, pair),
        )


def _full_eval_due(competitor_id: str, game_week: int) -> bool:
    cid = str(competitor_id)
    gw = int(game_week)
    row = db.fetch_one(
        "SELECT last_evaluation_week FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    last = int(row["last_evaluation_week"] or 0) if row else 0
    if last == gw:
        return False
    if gw <= 1:
        return True
    k = _ai_eval_interval()
    if k <= 1:
        return True
    # Stagger by competitor_id hash to spread work.
    return (gw % k) == (stable_hash(cid) % k)


def ai_bootstrap_if_needed(game_week: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Week-1 only: run a full turn so opening bids land on this week's auctions.
    Mid-week UI/CLI starts must not replay an in-progress week's light pass.
    """
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(game_week if game_week is not None else (gs["game_week"] if gs else 1) or 1)
    ever = db.fetch_one("SELECT 1 FROM ai_turn_log LIMIT 1")
    if gw > 1 and ever:
        return None
    already = db.fetch_one(
        "SELECT 1 FROM ai_turn_log WHERE game_week = ? LIMIT 1",
        (gw,),
    )
    if already:
        return None
    return ai_weekly_turn(gw)


def ai_light_pass(competitor_id: str, game_week: int) -> None:
    """
    Weekly light pass: stance, leases, paper book, fares, route health, queued opens.
    Gate/slot bids run once after the weekly turn (including full eval).
    """
    cid = str(competitor_id)
    gw = int(game_week)
    month = _current_month()
    _ai_stance(cid, gw)
    from engine.ai_economics import charge_weekly_fixed_costs

    lease = charge_weekly_fixed_costs(cid)
    if lease > 0:
        append_ai_narrative(cid, f"LEASE:-{lease:.0f}")
    try:
        ai_update_load_streaks(cid, gw, month)
    except Exception:
        pass
    _update_profitability_estimates(cid, gw, month)
    _adjust_fares_light(cid, gw, month)
    _roll_actual_weekly_revenue(cid, gw)
    _update_loss_tracking_and_status(cid)
    _open_queued_candidates(cid, gw)
    _remove_closed_routes(cid, gw)
    _financial_distress_check(cid, gw)


def ai_full_evaluation(competitor_id: str, game_week: int) -> None:
    """
    Full evaluation: generate & score candidates, bid for needed gates, queue/open,
    deepen existing, fleet growth, cooldowns.
    """
    cid = str(competitor_id)
    gw = int(game_week)
    candidates = ai_generate_candidates(cid)
    scored = []
    for rid in candidates:
        scored.append((rid, ai_score_candidate(cid, rid, gw)))
    scored.sort(key=lambda x: x[1], reverse=True)
    _gate_feasibility_and_queue(cid, gw, scored)
    _open_queued_candidates(cid, gw)
    _deepen_existing_routes(cid, gw)
    _fleet_growth_decision(cid, gw)
    _decrement_cooldowns(cid, gw)
    thresh = 0.50
    comp = db.fetch_one("SELECT risk_tolerance FROM competitors WHERE competitor_id = ?", (cid,))
    if comp:
        thresh = 0.50 + (1.0 - float(comp["risk_tolerance"] or 0.5)) * 0.20
    for pair, sc in scored:
        if float(sc) < thresh:
            append_ai_narrative(cid, f"SKIPPED:{pair}:score{float(sc):.2f}<{thresh:.2f}")
            break
    db.execute("UPDATE competitors SET last_evaluation_week = ? WHERE competitor_id = ?", (gw, cid))
    _sync_competitor_slots(cid, gw)


def ai_generate_candidates(competitor_id: str) -> List[str]:
    """
    Candidate generation per PDF. Returns list of route_pair_ids.
    """
    cid = str(competitor_id)
    comp = db.fetch_one("SELECT home_hub_iata, strategy FROM competitors WHERE competitor_id = ?", (cid,))
    if not comp:
        return []
    hub = str(comp["home_hub_iata"]).upper()
    strat = str(comp["strategy"] or "HUBSPOKE").upper()
    hub_row = db.fetch_one("SELECT lat, lon FROM airports WHERE iata = ?", (hub,))
    if not hub_row:
        return []
    hub_lat = float(hub_row["lat"])
    hub_lon = float(hub_row["lon"])
    rad = _ai_hub_radius_nm()
    # Widebody long-haul: 2500nm from DXB excludes LHR/CDG and empties the pool.
    if strat == "PREMIUM":
        rad = max(rad, 4200.0)
    elif strat == "BUDGET":
        rad = min(rad, 1500.0)

    # Filter airports by radius and strategy gates.
    arows = db.fetch_all("SELECT iata, lat, lon, score, category FROM airports")
    scored_allowed: List[tuple[float, str]] = []
    allowed: List[str] = []
    for a in arows:
        iata = str(a["iata"]).upper()
        if iata == hub:
            allowed.append(iata)
            continue
        dist = haversine_distance(hub_lat, hub_lon, float(a["lat"]), float(a["lon"]))
        if dist > rad:
            continue
        if strat == "PREMIUM":
            if float(a["score"] or 0) < 600_000:
                continue
            if str(a["category"]) != "large_airport":
                continue
        scored_allowed.append((float(a["score"] or 0), iata))
        allowed.append(iata)
    # BUDGET cartesian must stay small: keep the highest-score nearby airports.
    if strat == "BUDGET" and len(scored_allowed) > 60:
        scored_allowed.sort(key=lambda x: x[0], reverse=True)
        keep = {iata for _s, iata in scored_allowed[:60]}
        keep.add(hub)
        allowed = [a for a in allowed if a in keep]

    by_iata = {str(a["iata"]).upper(): dict(a) for a in arows}

    # Build unique route pairs. Do not call canonical_pair_for_competitor in the inner loop
    # (it hits SQLite per pair; BUDGET can enumerate 10^6 combinations).
    def _pair_id(a: str, b: str) -> str:
        if a == hub or b == hub:
            other = b if a == hub else a
            return f"{hub}-{other}"
        return f"{a}-{b}" if a <= b else f"{b}-{a}"

    pairs: List[str] = []
    if strat in ("HUBSPOKE", "PREMIUM"):
        for b in allowed:
            if b == hub:
                continue
            pairs.append(_pair_id(hub, b))
    else:
        for a in allowed:
            for b in allowed:
                if a >= b:
                    continue
                if strat == "BUDGET":
                    ar = by_iata.get(a)
                    br = by_iata.get(b)
                    if not ar or not br:
                        continue
                    dnm = haversine_distance(
                        float(ar["lat"]), float(ar["lon"]), float(br["lat"]), float(br["lon"])
                    )
                    if dnm >= 1500:
                        continue
                pairs.append(_pair_id(a, b))

    pairs = sorted(set(pairs))
    op = db.fetch_all("SELECT route_pair_id FROM competitor_routes WHERE competitor_id = ? AND status IN ('ACTIVE','SUSPENDED')", (cid,))
    operating = {str(r["route_pair_id"]) for r in op}
    pairs = [p for p in pairs if p not in operating]

    # Rank on airport scores (no per-pair INSERT); mix trophy dests with
    # pairs that do not require a new foreign gate this week.
    from engine.gates import gate_score_threshold

    auc_cut = float(gate_score_threshold())

    def _needs_foreign_gate(pair_id: str) -> bool:
        oa, ob = _route_pair_components(pair_id)
        for ap in (oa, ob):
            if ap == hub:
                continue
            if float((by_iata.get(ap) or {}).get("score") or 0) >= auc_cut:
                return True
        return False

    scored = []
    for p in pairs:
        oa, ob = _route_pair_components(p)
        sa = float((by_iata.get(oa) or {}).get("score") or 0)
        sb = float((by_iata.get(ob) or {}).get("score") or 0)
        scored.append((p, sa + sb))
    scored.sort(key=lambda x: x[1], reverse=True)
    score_of = {p: s for p, s in scored}
    gated: List[str] = []
    ungated: List[str] = []
    for p, _s in scored:
        (gated if _needs_foreign_gate(p) else ungated).append(p)
    ungated.sort(key=lambda p: (0 if hub in (p.split("-", 1)[0], p.split("-", 1)[-1]) else 1, -score_of.get(p, 0)))
    pool = max(5, _ai_candidate_pool_size())
    n_free = max(4, pool // 3)
    top = gated[: max(0, pool - n_free)] + ungated[:n_free]
    # de-dupe, preserve order
    seen = set()
    ordered = []
    for p in top:
        if p in seen:
            continue
        seen.add(p)
        ordered.append(p)
    materialized = []
    for p in ordered:
        out_id, _in_id = _ensure_route_rows_exist_for_pair(p)
        if get_route(out_id):
            materialized.append(p)
    return materialized


def ai_breakeven_fare(route_id: str, type_id: str, frequency: int) -> tuple[float, float]:
    """Unit-cost floors from the shared book (leisure / business)."""
    from engine.ai_economics import weekly_pair_pnl

    rt = get_route(str(route_id))
    if not rt:
        return (75.0, 115.0)
    from engine.ai import canonical_pair_for_competitor  # noqa: late
    # Need a competitor — caller always has one via type; use dummy share via pair.
    pair = str(rt["route_id"])
    book = weekly_pair_pnl("AI_MERIDIAN", f"{rt['origin_iata']}-{rt['dest_iata']}", 150.0, 300.0, frequency, type_id)
    u = max(20.0, float(book.get("unit_cost") or 75.0))
    return (u * 1.05, u * 1.05 * 1.6)


def ai_logit_share(
    competitor_id: str, route_id: str, fare_leisure: float, fare_business: float
) -> float:
    from engine.ai_economics import segment_shares

    sl, sb = segment_shares(competitor_id, route_id, fare_leisure, fare_business)
    return 0.5 * (sl + sb)


def ai_estimate_profit(
    competitor_id: str, route_pair_id: str, fare_leisure: float, fare_business: float, frequency: int, type_id: str
) -> tuple[float, float]:
    from engine.ai_economics import weekly_pair_pnl

    book = weekly_pair_pnl(
        competitor_id, route_pair_id, fare_leisure, fare_business, frequency, type_id
    )
    return (float(book["profit"]), float(book["share"]))


def ai_resolve_aircraft_type(competitor_id: str, route_pair_id: str) -> Optional[str]:
    """Smallest type that covers distance; prefer a tail the AI already owns."""
    cid = str(competitor_id)
    pair = str(route_pair_id).upper().strip()
    out_id, _in_id = _ensure_route_rows_exist_for_pair(pair)
    rt = get_route(out_id)
    if not rt:
        return None
    dist = float(rt["distance_nm"] or 0.0)
    comp = db.fetch_one("SELECT strategy FROM competitors WHERE competitor_id = ?", (cid,))
    strat = str(comp["strategy"] or "HUBSPOKE").upper() if comp else "HUBSPOKE"
    if strat == "PREMIUM" or dist >= 3200:
        cats = "('WIDE')"
    elif strat == "BUDGET":
        cats = "('NARROW','REGIONAL_JET')"
    else:
        cats = "('NARROW','REGIONAL_JET')" if dist < 3000 else "('WIDE','NARROW')"
    owned = {
        str(r["type_id"])
        for r in (
            db.fetch_all(
                "SELECT DISTINCT type_id FROM ai_fleet WHERE competitor_id = ?",
                (cid,),
            )
            or []
        )
    }
    rows = db.fetch_all(
        f"SELECT type_id FROM aircraft_types WHERE category IN {cats} AND range_nm >= ? ORDER BY range_nm ASC",
        (dist,),
    )
    if not rows:
        rows = db.fetch_all(
            "SELECT type_id FROM aircraft_types WHERE range_nm >= ? ORDER BY range_nm ASC",
            (dist,),
        )
    ids = [str(r["type_id"]) for r in (rows or [])]
    for t in ids:
        if t in owned:
            return t
    return ids[0] if ids else None


def ai_score_candidate(competitor_id: str, route_pair_id: str, game_week: int) -> float:
    """
    Composite score 0..1 and write ai_route_candidates row with estimates.
    """
    cid = str(competitor_id)
    pair = str(route_pair_id).upper().strip()
    out_id, _in_id = _ensure_route_rows_exist_for_pair(pair)
    rt = get_route(out_id)
    if not rt:
        return 0.0
    comp = db.fetch_one("SELECT * FROM competitors WHERE competitor_id = ?", (cid,))
    if not comp:
        return 0.0
    strat = str(comp["strategy"] or "HUBSPOKE").upper()
    risk = float(comp["risk_tolerance"] or 0.5)

    # Component 1 demand — calibrated pool, not the legacy template on routes.
    from engine.ai_economics import _live_route_bases

    base_b, base_l = _live_route_bases(dict(rt))
    est_pax = (base_b + base_l) * 2.0
    demand_score = min(1.0, est_pax / 1600.0)

    # Entry fares vs real market (player listed) or distance reference — never placeholder.
    from engine.ai_economics import market_anchor_fares, weekly_pair_pnl, fleet_block_hours_cap, fleet_block_hours_used, block_hours

    market_b, market_l = market_anchor_fares(out_id)
    if strat == "BUDGET":
        entry_l = market_l * (1.0 - 0.20)
        entry_b = entry_l * 1.3
    elif strat == "PREMIUM":
        entry_l = market_l * 1.10
        entry_b = market_b * 1.25
    else:
        entry_l = market_l * 0.97
        entry_b = market_b * 0.97

    type_id = ai_resolve_aircraft_type(cid, pair)
    if not type_id:
        return 0.0
    book = weekly_pair_pnl(cid, pair, entry_l, entry_b, 2, type_id)
    est_profit, est_share = float(book["profit"]), float(book["share"])

    # If we don't already own a fitting tail, the estimate must pay a full extra lease.
    owned = db.fetch_one(
        "SELECT 1 FROM ai_fleet WHERE competitor_id = ? AND type_id = ? LIMIT 1",
        (cid, type_id),
    )
    if not owned:
        ac = db.fetch_one("SELECT weekly_lease_cost FROM aircraft_types WHERE type_id = ?", (type_id,))
        est_profit -= float((ac["weekly_lease_cost"] if ac else None) or 0)

    hub = str(comp["home_hub_iata"]).upper()
    a, b = _route_pair_components(pair)
    try:
        from engine.gates import gate_min_price_per_unit

        mtt = _mtt_hours_ai()
        payback = max(4.0, 6.0 + 12.0 * risk)
        gate_need = ai_gate_shortfall(cid, a, [pair], mtt) + ai_gate_shortfall(cid, b, [pair], mtt)
        est_profit -= (float(gate_need) * float(gate_min_price_per_unit())) / payback
    except Exception:
        pass

    used_h = fleet_block_hours_used(cid)
    cap_h = fleet_block_hours_cap(cid)
    extra_h = _hours_to_add_pair(cid, pair, 2, type_id)
    hours_ok = (used_h + extra_h) <= (cap_h + 0.5)

    import math
    mp = _ai_min_profit_to_open()
    profit_score = 1.0 / (1.0 + math.exp(-((est_profit - mp) / max(1.0, mp))))

    hub_bonus = 0.3 if (a == hub or b == hub) else 0.0
    if db.fetch_one(
        "SELECT 1 FROM competitor_routes WHERE competitor_id = ? AND (outbound_route_id LIKE ? OR inbound_route_id LIKE ?) LIMIT 1",
        (cid, f"{a}-%", f"{a}-%"),
    ):
        hub_bonus += 0.15
    if strat == "HUBSPOKE":
        strategy_bonus = 1.0 if (a == hub or b == hub) else 0.4
    elif strat == "POINTTOPOINT":
        strategy_bonus = 1.0 if (a != hub and b != hub) else 0.3
    else:
        strategy_bonus = 0.5
    network_fit = max(0.0, min(1.0, hub_bonus + strategy_bonus))

    # Component 4 competition
    existing = 0
    if db.fetch_one("SELECT 1 FROM flight_segments WHERE route_id IN (?, ?) LIMIT 1", (out_id, _in_id)):
        existing += 1
    other_ai = int(
        db.fetch_one(
            "SELECT COUNT(*) AS c FROM competitor_routes WHERE (outbound_route_id IN (?, ?) OR inbound_route_id IN (?, ?)) AND status IN ('ACTIVE','SUSPENDED') AND competitor_id != ?",
            (out_id, _in_id, out_id, _in_id, cid),
        )["c"]
        or 0
    )
    # Punish 3rd+ AI crowding; player overlap is priced separately.
    competition = max(0.0, 1.0 - (other_ai * 0.25))
    player_freq = 0
    prow = db.fetch_one(
        "SELECT COUNT(*) AS c FROM flight_schedules WHERE route_id IN (?, ?) AND COALESCE(active,1)=1",
        (out_id, _in_id),
    )
    player_freq = int(prow["c"] or 0) if prow else 0
    stance = str((comp["stance"] if "stance" in comp.keys() else None) or "GROW").upper()
    if player_freq >= 6:
        if stance != "ATTACK":
            competition *= 0.45
    elif 0 < player_freq <= 3:
        competition = min(1.0, competition + 0.15)

    served = db.fetch_one(
        """
        SELECT country FROM airports WHERE iata = ?
        """,
        ((b if a == hub else a),),
    )
    if served:
        fam = db.fetch_one(
            """
            SELECT 1 FROM competitor_routes cr
            JOIN airports ap ON ap.iata = CASE
                WHEN cr.outbound_route_id LIKE ? THEN substr(cr.inbound_route_id, 1, 3)
                ELSE substr(cr.outbound_route_id, 1, 3)
            END
            WHERE cr.competitor_id = ? AND ap.country = ? LIMIT 1
            """,
            (f"{hub}-%", cid, str(served["country"])),
        )
        if fam:
            network_fit = min(1.0, network_fit + 0.08)

    conn_n = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM competitor_routes
        WHERE competitor_id = ? AND status = 'ACTIVE'
          AND (outbound_route_id LIKE ? OR inbound_route_id LIKE ?
               OR outbound_route_id LIKE ? OR inbound_route_id LIKE ?)
        """,
        (cid, f"{a}-%", f"%-{a}", f"{b}-%", f"%-{b}"),
    )
    if int(conn_n["c"] or 0) > 2:
        network_fit = min(1.0, network_fit + 0.12)

    # Component 5 feasibility
    cash_ok = 1.0 if float(comp["cash"] or 0.0) > 50_000.0 else 0.0
    fleet_ok = 1.0 if int(comp["fleet_size"] or 0) < int(comp["max_fleet_size"] or 0) else 0.0
    feas = (1.0 + cash_ok + fleet_ok + (1.0 if hours_ok else 0.0)) / 4.0

    mem = db.fetch_one(
        """
        SELECT COALESCE(MAX(cooldown_weeks), 0) AS cd
        FROM ai_memory WHERE competitor_id = ? AND route_pair_id = ?
        """,
        (cid, pair),
    )
    cooldown = int(mem["cd"] or 0) if mem else 0

    comp_score = (demand_score * 0.30) + (profit_score * 0.35) + (network_fit * 0.20) + (competition * 0.10) + (feas * 0.05)
    if cooldown > 0:
        comp_score *= max(0.15, 1.0 - 0.07 * cooldown)

    profit_floor = mp * (1.0 - risk)
    thresh = 0.50 + (1.0 - risk) * 0.20
    reason = None
    stance_now = str((comp["stance"] if "stance" in comp.keys() else None) or "GROW").upper()
    if est_profit < profit_floor:
        if stance_now == "CONSOLIDATE":
            comp_score = 0.0
            reason = "PROFIT_FLOOR"
        elif stance_now in ("GROW", "ATTACK") and (
            demand_score >= 0.12 or network_fit >= 0.99
        ):
            # Marginal-route PnL is pessimistic while most of the fleet is idle;
            # hub spokes with real demand should still enter the queue.
            comp_score = max(comp_score, thresh + 0.05)
            reason = "GROWTH_DEMAND"
        else:
            comp_score *= max(0.2, 1.0 + (est_profit / max(1.0, abs(profit_floor))))
            reason = "PROFIT_SOFT"
    elif not hours_ok and not owned:
        reason = "NO_HOURS"

    decision = "PENDING"
    if comp_score >= thresh:
        decision = "WATCHING"
    elif reason is None and comp_score > 0:
        reason = "SCORE"
    db.execute(
        """
        INSERT OR REPLACE INTO ai_route_candidates (
            candidate_id, competitor_id, route_pair_id, score,
            estimated_weekly_profit, estimated_market_share,
            estimated_entry_fare_leisure, estimated_entry_fare_business,
            evaluated_week, decision, rejection_reason
        ) VALUES (
            COALESCE((SELECT candidate_id FROM ai_route_candidates WHERE competitor_id=? AND route_pair_id=?), ?),
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            cid,
            pair,
            str(uuid.uuid4()),
            cid,
            pair,
            float(comp_score),
            float(est_profit),
            float(est_share),
            float(entry_l),
            float(entry_b),
            int(game_week),
            decision,
            reason,
        ),
    )
    return float(comp_score)


def _update_profitability_estimates(competitor_id: str, game_week: int, current_month: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    month = int(current_month)
    rows = db.fetch_all(
        """
        SELECT * FROM competitor_routes
        WHERE competitor_id = ? AND status IN ('ACTIVE','SUSPENDED','CLOSING')
        """,
        (cid,),
    )
    for r in rows:
        pair = str(r["route_pair_id"])
        freq = int(r["frequency_per_week"] or 1)
        fl = float(r["fare_leisure"])
        fb = float(r["fare_business"])
        tid = str(r["aircraft_type_id"] or ai_resolve_aircraft_type(cid, pair) or "A320")
        est_profit, share = ai_estimate_profit(cid, pair, fl, fb, freq, tid)
        contested = 1 if (
            _player_contests_route(str(r["outbound_route_id"]))
            or _player_contests_route(str(r["inbound_route_id"]))
        ) else 0
        db.execute(
            """
            UPDATE competitor_routes
            SET estimated_weekly_profit = ?,
                market_share = ?,
                contested = ?
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (float(est_profit), float(share), int(contested), cid, pair),
        )


def _player_opened_route_first(outbound_route_id: str, ai_opened_week: int) -> bool:
    rid = str(outbound_route_id)
    pr = db.fetch_one("SELECT opened_week FROM player_routes WHERE route_id = ?", (rid,))
    if pr and pr["opened_week"] is not None:
        return int(pr["opened_week"]) < int(ai_opened_week or 10**9)
    fs = db.fetch_one(
        "SELECT MIN(created_week) AS w FROM flight_schedules WHERE route_id = ? AND COALESCE(active, 1) = 1",
        (rid,),
    )
    if fs and fs["w"] is not None:
        return int(fs["w"]) < int(ai_opened_week or 10**9)
    return False


def _adjust_fares_light(competitor_id: str, game_week: int, current_month: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    month = int(current_month)
    from engine.ai_economics import market_anchor_fares, weekly_pair_pnl
    from engine.demand import estimate_competitor_route_load_factor

    comp = db.fetch_one("SELECT * FROM competitors WHERE competitor_id = ?", (cid,))
    if not comp:
        return
    strat = str(comp["strategy"] or "HUBSPOKE").upper()
    aggr = float(comp["aggressiveness"] or 1.0)
    stance = str(comp["stance"] if "stance" in comp.keys() else "GROW") if comp else "GROW"
    cash = float(comp["cash"] or 0)
    target_lf = 0.85 if strat == "BUDGET" else (0.70 if strat == "PREMIUM" else 0.78)
    contested_budget = 0

    rows = db.fetch_all(
        "SELECT * FROM competitor_routes WHERE competitor_id = ? AND status IN ('ACTIVE','SUSPENDED')",
        (cid,),
    )
    for r in rows:
        pair = str(r["route_pair_id"])
        out_id = str(r["outbound_route_id"])
        rt = get_route(out_id)
        if not rt:
            continue
        mb, ml = market_anchor_fares(out_id)
        fl = float(r["fare_leisure"])
        fb = float(r["fare_business"])
        freq = int(r["frequency_per_week"] or 1)
        tid = str(r["aircraft_type_id"] or ai_resolve_aircraft_type(cid, pair) or "A320")
        book = weekly_pair_pnl(cid, pair, fl, fb, freq, tid, month=month)
        unit = max(15.0, float(book["unit_cost"]))
        lo, hi = unit * 1.05, ml * 1.35
        lf = estimate_competitor_route_load_factor(cid, out_id, game_week=gw, current_month=month)
        if lf is None:
            lf = float(r["actual_lf_avg"] or 0) or 0.55
        war = int(r["fare_war_weeks"] or 0) if "fare_war_weeks" in r.keys() else 0
        contested = int(r["contested"] or 0) == 1
        fare_event = None
        old_freq = freq

        if contested:
            contested_budget += 1
        if not contested or contested_budget > 3:
            if lf > target_lf + 0.07:
                fl *= 1.04
                fb *= 1.04
                fare_event = "YIELD_UP"
            elif lf < target_lf - 0.12:
                if strat == "PREMIUM":
                    fl *= 1.03
                    fb *= 1.03
                    fare_event = "YIELD_DEFEND"
                else:
                    fl *= 0.95
                    fb *= 0.95
                    fare_event = "YIELD_DOWN"
        else:
            player_l = ml
            can_fight = unit <= player_l * 0.90 and stance != "CONSOLIDATE"
            if can_fight and lf < target_lf:
                cut = min(0.18, 0.06 + 0.08 * aggr)
                fl = max(unit * 1.05, fl * (1.0 - cut))
                fb = max(unit * 1.05 * 1.5, fb * (1.0 - cut))
                war += 1
                fare_event = "UNDERCUT"
                if war >= 6:
                    fl = fl * 0.97 + ml * 0.03
                    fb = fb * 0.97 + mb * 0.03
                    fare_event = "WAR_FADE"
            else:
                if war > 0:
                    fl = fl * 0.97 + ml * 0.03
                    fb = fb * 0.97 + mb * 0.03
                    war = max(0, war - 1)
                    fare_event = "WAR_FADE"
        fl = min(hi, max(lo, fl))
        fb = min(mb * 1.45 if mb else fb, max(lo * 1.4, fb))
        cap = _max_freq_for_type(tid, strat)
        if contested and str(stance).upper() != "ATTACK":
            pf = db.fetch_one(
                "SELECT COUNT(*) AS c FROM flight_schedules WHERE route_id IN (?, ?) AND COALESCE(active,1)=1",
                (out_id, str(r["inbound_route_id"])),
            )
            pfreq = int(pf["c"] or 0) if pf else 0
            if pfreq > 0:
                cap = min(cap, max(1, pfreq + 2))
        freq = min(freq, cap)
        db.execute(
            """
            UPDATE competitor_routes
            SET fare_leisure = ?, fare_business = ?, frequency_per_week = ?, fare_war_weeks = ?
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (float(fl), float(fb), int(freq), int(war), cid, pair),
        )
        if fare_event:
            append_ai_narrative(cid, f"{fare_event}:{pair}:L{fl:.0f}/B{fb:.0f}")
        if freq != old_freq:
            append_ai_narrative(cid, f"THINNED:{pair}:{old_freq}->{freq}x")


def _update_loss_tracking_and_status(competitor_id: str) -> None:
    """Paper P&L thins frequency. Only flown net / load factor may close a route."""
    cid = str(competitor_id)
    thresh = _ai_exit_loss_threshold()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    rows = db.fetch_all("SELECT * FROM competitor_routes WHERE competitor_id = ?", (cid,))
    profile = _growth_profile(cid)
    extra_grace = int(profile["extra_exit_grace_weeks"])
    hub_row = db.fetch_one(
        "SELECT home_hub_iata, strategy FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    hub = str(hub_row["home_hub_iata"] or "").upper() if hub_row else ""
    hub_pairs = 0
    if hub:
        hub_pairs = int(
            db.fetch_one(
                """
                SELECT COUNT(*) AS c FROM competitor_routes
                WHERE competitor_id = ? AND status IN ('ACTIVE','SUSPENDED')
                  AND (outbound_route_id LIKE ? OR inbound_route_id LIKE ?)
                """,
                (cid, f"{hub}-%", f"%-{hub}"),
            )["c"]
            or 0
        )
    for r in rows:
        pair = str(r["route_pair_id"])
        status = str(r["status"] or "ACTIVE")
        if status == "CLOSING":
            continue
        opened = int(r["opened_week"] or 1)
        est = float(r["estimated_weekly_profit"] or 0.0)
        paper = int(_rget(r, "paper_loss_weeks", 0) or 0)
        low_lf = int(_rget(r, "low_lf_weeks", 0) or 0)
        flown_loss = int(r["consecutive_loss_weeks"] or 0)
        net_avg = float(_rget(r, "actual_weekly_net_avg", 0.0) or 0.0)
        lf_avg = float(_rget(r, "actual_lf_avg", 0.0) or 0.0)
        freq = max(1, int(r["frequency_per_week"] or 1))
        strat = str((hub_row["strategy"] if hub_row else None) or "HUBSPOKE").upper()
        keep = gw <= opened + 4 + extra_grace
        if hub and hub_pairs < 4 and hub in pair.upper():
            keep = True
        if est < 0:
            paper += 1
        else:
            paper = 0
        if (not keep) and paper >= 2 and freq > 1:
            min_freq = 2 if strat == "PREMIUM" and gw < opened + 12 else 1
            if freq > min_freq:
                freq -= 1
                append_ai_narrative(cid, f"THINNED:{pair}:paper{paper}w->{freq}x")
        flown = abs(net_avg) > 1.0 or lf_avg > 0.01
        if keep or not flown:
            db.execute(
                """
                UPDATE competitor_routes
                SET paper_loss_weeks = ?, frequency_per_week = ?
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (int(paper), int(freq), cid, pair),
            )
            continue
        if net_avg < 0:
            flown_loss += 1
        else:
            flown_loss = 0
        if 0 < lf_avg < 0.35:
            low_lf += 1
        else:
            low_lf = 0
        if flown_loss >= thresh or low_lf >= 6:
            status = "CLOSING"
            append_ai_narrative(cid, f"CLOSING:{pair}:net{net_avg:.0f}:lf{lf_avg:.2f}")
        db.execute(
            """
            UPDATE competitor_routes
            SET consecutive_loss_weeks = ?, paper_loss_weeks = ?, low_lf_weeks = ?,
                frequency_per_week = ?, status = ?
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (int(flown_loss), int(paper), int(low_lf), int(freq), status, cid, pair),
        )


def _open_queued_candidates(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    mtt = _mtt_hours_ai()
    from engine.ai_economics import fleet_block_hours_cap, fleet_block_hours_used

    comp = db.fetch_one(
        "SELECT cash, weekly_route_budget, expansion_rate FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    if not comp:
        return
    budget = float(comp["weekly_route_budget"] or 0.0)
    cap = _stance_expand_cap(cid, max(1, int(comp["expansion_rate"] or 1), int(_growth_profile(cid)["expansion_rate"])))
    cash = float(comp["cash"] or 0.0)
    opened = 0
    rows = db.fetch_all(
        """
        SELECT * FROM ai_route_candidates
        WHERE competitor_id = ? AND decision = 'OPEN_NEXT_WEEK'
        ORDER BY score DESC
        """,
        (cid,),
    )
    for r in rows:
        if opened >= cap or budget <= 0:
            break
        pair = str(r["route_pair_id"])
        already = db.fetch_one(
            """
            SELECT 1 FROM competitor_routes
            WHERE competitor_id = ? AND route_pair_id = ? AND status IN ('ACTIVE','SUSPENDED')
            """,
            (cid, pair),
        )
        if already:
            db.execute(
                "UPDATE ai_route_candidates SET decision = 'OPENED' WHERE competitor_id = ? AND route_pair_id = ?",
                (cid, pair),
            )
            continue
        origin, dest = _route_pair_components(pair)
        short_o = ai_gate_shortfall(cid, origin, [pair], mtt)
        short_d = ai_gate_shortfall(cid, dest, [pair], mtt)
        if short_o > 0 or short_d > 0:
            db.execute(
                """
                UPDATE ai_route_candidates
                SET decision = 'WATCHING', rejection_reason = 'AWAITING_GATES'
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            continue
        out_id, in_id = _ensure_route_rows_exist_for_pair(pair)
        out_rt = get_route(out_id)
        if not out_rt:
            continue
        ao = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (out_rt["origin_iata"],))
        ad = db.fetch_one("SELECT * FROM airports WHERE iata = ?", (out_rt["dest_iata"],))
        if not ao or not ad:
            continue
        acq = calculate_route_acquisition_cost(dict(ao), dict(ad), float(out_rt["distance_nm"] or 0.0))
        cost = float(acq) * 2.0
        if cost > budget or cost > cash:
            db.execute(
                """
                UPDATE ai_route_candidates
                SET decision = 'WATCHING', rejection_reason = 'CASH_CONSTRAINT'
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            continue
        tid = ai_resolve_aircraft_type(cid, pair) or "A320"
        extra_h = _hours_to_add_pair(cid, pair, 2, tid)
        if fleet_block_hours_used(cid) + extra_h > fleet_block_hours_cap(cid) + 0.5:
            db.execute(
                """
                UPDATE ai_route_candidates
                SET decision = 'WATCHING', rejection_reason = 'NO_HOURS'
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            continue
        fl = float(r["estimated_entry_fare_leisure"])
        fb = float(r["estimated_entry_fare_business"])
        freq = 2
        db.execute(
            """
            INSERT OR REPLACE INTO competitor_routes (
                competitor_id, route_pair_id, outbound_route_id, inbound_route_id,
                fare_business, fare_leisure, frequency_per_week, aircraft_type_id,
                opened_week, status, estimated_weekly_profit, actual_weekly_revenue_avg,
                consecutive_loss_weeks, contested, market_share
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 0.0, 0.0, 0, 0, 0.0)
            """,
            (cid, pair, out_id, in_id, fb, fl, freq, tid, gw),
        )
        db.execute("UPDATE competitors SET cash = cash - ? WHERE competitor_id = ?", (cost, cid))
        cash -= cost
        budget -= cost
        opened += 1
        db.execute(
            "UPDATE ai_route_candidates SET decision = 'OPENED', rejection_reason = NULL WHERE competitor_id = ? AND route_pair_id = ?",
            (cid, pair),
        )
        append_ai_narrative(cid, f"OPENED:{pair}:{freq}x")
        push_ai_news(f"✈ {competitor_display_name(cid)} opens {pair} {freq}x/wk")
    if opened:
        _sync_competitor_slots(cid, gw)


def _remove_closed_routes(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    rows = db.fetch_all(
        "SELECT route_pair_id FROM competitor_routes WHERE competitor_id = ? AND status = 'CLOSING'",
        (cid,),
    )
    for r in rows:
        pair = str(r["route_pair_id"])
        db.execute("DELETE FROM competitor_routes WHERE competitor_id = ? AND route_pair_id = ?", (cid, pair))
        db.execute(
            """
            INSERT INTO ai_memory (memory_id, competitor_id, route_pair_id, event, game_week, cooldown_weeks)
            VALUES (?, ?, ?, 'CLOSED_UNPROFITABLE', ?, 12)
            """,
            (str(uuid.uuid4()), cid, pair, gw),
        )


def _max_freq_for_type(type_id: str, strategy: Optional[str] = None) -> int:
    row = db.fetch_one("SELECT category FROM aircraft_types WHERE type_id = ?", (str(type_id),))
    cat = str(row["category"] or "NARROW").upper() if row else "NARROW"
    cap = 7 if cat == "WIDE" else 14
    if str(strategy or "").upper() == "BUDGET":
        cap = min(cap, 10)
    return cap


def _gate_feasibility_and_queue(
    competitor_id: str, game_week: int, scored: List[tuple[str, float]]
) -> None:
    cid = str(competitor_id)
    mtt = _mtt_hours_ai()
    from engine.ai_economics import fleet_block_hours_cap, fleet_block_hours_used

    comp = db.fetch_one(
        "SELECT risk_tolerance, expansion_rate, strategy FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    if not comp:
        return
    risk = float(comp["risk_tolerance"] or 0.5)
    thresh = 0.50 + (1.0 - risk) * 0.20
    expand_cap = _stance_expand_cap(
        cid, max(1, int(comp["expansion_rate"] or 1), int(_growth_profile(cid)["expansion_rate"]))
    )
    queued = 0
    used_h = fleet_block_hours_used(cid)
    cap_h = fleet_block_hours_cap(cid)
    for pair, score in scored:
        origin, dest = _route_pair_components(pair)
        short_o = ai_gate_shortfall(cid, origin, [pair], mtt)
        short_d = ai_gate_shortfall(cid, dest, [pair], mtt)
        can_open = short_o == 0 and short_d == 0
        strong = float(score) >= thresh
        tid = ai_resolve_aircraft_type(cid, pair) or "A320"
        extra_h = _hours_to_add_pair(cid, pair, 2, tid)
        hours_ok = (used_h + extra_h) <= (cap_h + 0.5)
        if can_open and strong and hours_ok and queued < expand_cap:
            db.execute(
                """
                UPDATE ai_route_candidates
                SET decision = 'OPEN_NEXT_WEEK', rejection_reason = NULL
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (cid, pair),
            )
            queued += 1
            used_h += extra_h
        else:
            if short_o > 0 or short_d > 0:
                why = "AWAITING_GATES"
            elif not hours_ok:
                why = "NO_HOURS"
            else:
                why = "SCORE"
            db.execute(
                """
                UPDATE ai_route_candidates
                SET decision = 'WATCHING', rejection_reason = ?
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (why, cid, pair),
            )


def _deepen_existing_routes(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    month = _current_month()
    mtt = _mtt_hours_ai()
    from engine.demand import estimate_competitor_route_load_factor
    from engine.ai_economics import fleet_block_hours_cap, fleet_block_hours_used

    stance = _competitor_stance(cid)
    if stance == "CONSOLIDATE":
        return
    rows = db.fetch_all(
        """
        SELECT * FROM competitor_routes
        WHERE competitor_id = ? AND status = 'ACTIVE'
        ORDER BY estimated_weekly_profit DESC
        """,
        (cid,),
    )
    deepen_cap = int(_growth_profile(cid)["deepen_per_eval"])
    strat_row = db.fetch_one("SELECT strategy FROM competitors WHERE competitor_id = ?", (cid,))
    strat = str(strat_row["strategy"] or "HUBSPOKE").upper() if strat_row else "HUBSPOKE"
    deepened = 0
    thinned = 0
    used_h = fleet_block_hours_used(cid)
    cap_h = fleet_block_hours_cap(cid)
    for r in rows:
        pair = str(r["route_pair_id"])
        out_id = str(r["outbound_route_id"])
        in_id = str(r["inbound_route_id"])
        freq = max(1, int(r["frequency_per_week"] or 1))
        opened = int(r["opened_week"] or 1)
        tid = str(r["aircraft_type_id"] or ai_resolve_aircraft_type(cid, pair) or "A320")
        cap = _max_freq_for_type(tid, strat)
        origin, dest = _route_pair_components(pair)
        cap = min(cap, slot_freq_cap(cid, origin, dest, gw))
        prow = db.fetch_one(
            "SELECT COUNT(*) AS c FROM flight_schedules WHERE route_id IN (?, ?) AND COALESCE(active,1)=1",
            (out_id, in_id),
        )
        player_n = int(prow["c"] or 0) if prow else 0
        if player_n > 0 and stance != "ATTACK":
            cap = min(cap, max(1, player_n + 2))
        lf = estimate_competitor_route_load_factor(cid, out_id, game_week=gw, current_month=month)
        if lf is None:
            lf = float(_rget(r, "actual_lf_avg", 0) or 0) or None
        if lf is None:
            continue
        extra_h = _hours_to_add_pair(cid, pair, 1, tid)
        if lf > 0.72 and freq < cap and deepened < deepen_cap and gw > opened:
            if used_h + extra_h > cap_h + 0.5:
                continue
            new_freq = freq + 1
            ov = {pair: new_freq}
            so = ai_gate_shortfall(cid, origin, [], mtt, freq_overrides=ov)
            sd = ai_gate_shortfall(cid, dest, [], mtt, freq_overrides=ov)
            if so == 0 and sd == 0:
                db.execute(
                    """
                    UPDATE competitor_routes SET frequency_per_week = ?
                    WHERE competitor_id = ? AND route_pair_id = ?
                    """,
                    (new_freq, cid, pair),
                )
                append_ai_narrative(cid, f"DEEPENED:{pair}:{freq}->{new_freq}x")
                deepened += 1
                used_h += extra_h
        elif lf < 0.40 and freq > 1 and thinned < 1 and gw > opened:
            min_freq = 2 if strat == "PREMIUM" and gw < opened + 12 else 1
            if freq <= min_freq:
                continue
            new_freq = freq - 1
            db.execute(
                """
                UPDATE competitor_routes SET frequency_per_week = ?
                WHERE competitor_id = ? AND route_pair_id = ?
                """,
                (new_freq, cid, pair),
            )
            append_ai_narrative(cid, f"THINNED:{pair}:{freq}->{new_freq}x")
            thinned += 1
    if deepened:
        _sync_competitor_slots(cid, gw)


def _gate_demand_map(competitor_id: str, game_week: int) -> Dict[str, Dict[str, float]]:
    cid = str(competitor_id)
    mtt = _mtt_hours_ai()
    need: Dict[str, Dict[str, float]] = {}
    awaiting = db.fetch_all(
        """
        SELECT route_pair_id, estimated_weekly_profit
        FROM ai_route_candidates
        WHERE competitor_id = ?
          AND rejection_reason = 'AWAITING_GATES'
          AND decision IN ('WATCHING', 'OPEN_NEXT_WEEK')
        """,
        (cid,),
    )
    pairs = [str(r["route_pair_id"]) for r in (awaiting or [])]
    profit_sum = sum(float(r["estimated_weekly_profit"] or 0) for r in (awaiting or []))
    airports: set[str] = set()
    for p in pairs:
        a, b = _route_pair_components(p)
        airports.add(a)
        airports.add(b)
    for ap in airports:
        units = ai_gate_shortfall(cid, ap, pairs, mtt)
        if units > 0:
            need[ap] = {"units": float(units), "wtp": max(0.0, profit_sum)}
    month = _current_month()
    from engine.demand import estimate_competitor_route_load_factor

    rows = db.fetch_all(
        "SELECT * FROM competitor_routes WHERE competitor_id = ? AND status = 'ACTIVE'",
        (cid,),
    )
    for r in rows or []:
        pair = str(r["route_pair_id"])
        out_id = str(r["outbound_route_id"])
        freq = max(1, int(r["frequency_per_week"] or 1))
        lf = estimate_competitor_route_load_factor(
            cid, out_id, game_week=int(game_week), current_month=month
        )
        if lf is None or lf <= 0.72:
            continue
        ov = {pair: freq + 1}
        origin, dest = _route_pair_components(pair)
        wtp = max(0.0, float(r["estimated_weekly_profit"] or 0))
        for ap in (origin, dest):
            u = ai_gate_shortfall(cid, ap, [], mtt, freq_overrides=ov)
            if u > 0:
                ent = need.setdefault(ap, {"units": 0.0, "wtp": 0.0})
                ent["units"] = max(ent["units"], float(u))
                ent["wtp"] = max(ent["wtp"], wtp)
    return need


def _bid_on_open_auctions(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    try:
        from engine.gates import list_open_gate_auctions

        comp = db.fetch_one(
            "SELECT aggressiveness, bid_probability, cash, weekly_route_budget, weekly_slot_budget, home_hub_iata, risk_tolerance FROM competitors WHERE competitor_id = ?",
            (cid,),
        )
        if not comp:
            return
        cash = float(comp["cash"] or 0.0)
        budget = float(comp["weekly_slot_budget"] or 0.0) or float(comp["weekly_route_budget"] or 0.0)
        spent_cap = min(cash, budget) if budget > 0 else cash
        if spent_cap <= 0:
            return
        risk = float(comp["risk_tolerance"] or 0.5)
        payback = 6.0 + 12.0 * risk
        need = _gate_demand_map(cid, gw)
        hub = str(comp["home_hub_iata"] or "").upper()
        bidp = float(comp["bid_probability"] or 0.5)
        if hub and hub not in need and random.random() <= bidp:
            need[hub] = {"units": 1.0, "wtp": max(50000.0, spent_cap * 0.05)}
        open_airports = {
            str(a.get("airport_iata") or "").upper() for a in (list_open_gate_auctions() or [])
        }
        for iata, ent in need.items():
            if spent_cap <= 0:
                break
            if iata not in open_airports:
                continue
            units = int(ent["units"] or 1)
            wtp = float(ent["wtp"] or 0.0) * payback
            if ai_bid_for_airport(cid, iata, units, gw, wtp_total=wtp, spend_remaining=spent_cap):
                last = db.fetch_one(
                    """
                    SELECT b.units_requested, b.price_per_unit
                    FROM airport_gate_bids b
                    JOIN airport_gate_auctions a ON a.auction_id = b.auction_id
                    WHERE b.bidder_id = ? AND a.airport_iata = ?
                    ORDER BY b.submitted_week DESC
                    LIMIT 1
                    """,
                    (cid, iata),
                )
                if last:
                    spent_cap -= float(last["units_requested"] or 0) * float(last["price_per_unit"] or 0)
    except Exception:
        pass


def _bid_on_slot_auctions(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    try:
        from engine.slots import (
            current_game_week as slot_week,
            ensure_weekly_slot_auctions,
            hourly_movements_at,
            list_open_slot_auctions,
            slots_held,
            submit_slot_bid,
        )

        live_week = slot_week()
        ensure_weekly_slot_auctions(live_week)
        auctions = list_open_slot_auctions()
        comp = db.fetch_one(
            """
            SELECT aggressiveness, cash, weekly_slot_budget, risk_tolerance
            FROM competitors WHERE competitor_id = ?
            """,
            (cid,),
        )
        if not comp:
            return
        cash = float(comp["cash"] or 0.0)
        budget = float(comp["weekly_slot_budget"] or 0.0)
        if budget <= 0:
            budget = cash
        spent_cap = min(cash, budget)
        need_airports = set(_gate_demand_map(cid, int(game_week)).keys())
        for a in auctions:
            if spent_cap <= 0:
                break
            iata = str(a.get("airport_iata") or "").upper()
            held = slots_held(iata, cid, live_week)
            used = int(sum(hourly_movements_at(iata, live_week, holder_id=cid).values()))
            planned = used + (4 if iata in need_airports else 0)
            short = max(0, planned - held)
            if short <= 0 and not (used > 0 and held <= used):
                continue
            units = short if short > 0 else 2
            price = float(a["current_price_per_unit"] or 0) * (0.90 + 0.20 * float(comp["aggressiveness"] or 1.0))
            cost = price * units
            if cost > spent_cap:
                continue
            submit_slot_bid(str(a["auction_id"]), units, price, bidder_id=cid)
            spent_cap -= cost
            append_ai_narrative(cid, f"BID_SLOT:{iata}:{units}@{price:.0f}")
    except Exception:
        pass


def _fleet_growth_decision(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    if _competitor_stance(cid) == "CONSOLIDATE":
        return
    style = str(_growth_profile(cid)["style"])
    comp = db.fetch_one(
        "SELECT fleet_size, max_fleet_size, cash, strategy FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    if not comp:
        return
    fs = int(comp["fleet_size"] or 0)
    mx = int(comp["max_fleet_size"] or 0)
    if fs >= mx:
        return
    blocked_gates = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM ai_route_candidates
        WHERE competitor_id = ? AND decision = 'WATCHING' AND rejection_reason = 'AWAITING_GATES'
        """,
        (cid,),
    )
    hours_backlog = db.fetch_one(
        """
        SELECT route_pair_id FROM ai_route_candidates
        WHERE competitor_id = ? AND rejection_reason = 'NO_HOURS'
        ORDER BY score DESC LIMIT 1
        """,
        (cid,),
    )
    queued = db.fetch_one(
        """
        SELECT route_pair_id FROM ai_route_candidates
        WHERE competitor_id = ? AND decision = 'OPEN_NEXT_WEEK'
        ORDER BY score DESC LIMIT 1
        """,
        (cid,),
    )
    if int(blocked_gates["c"] or 0) > 0 and not hours_backlog and not queued:
        return
    target_pair = None
    if hours_backlog:
        target_pair = str(hours_backlog["route_pair_id"])
    elif queued:
        target_pair = str(queued["route_pair_id"])
    if not target_pair:
        return
    cash = float(comp["cash"] or 0.0)
    type_id = ai_resolve_aircraft_type(cid, target_pair) or "A320"
    lease = db.fetch_one("SELECT weekly_lease_cost FROM aircraft_types WHERE type_id = ?", (type_id,))
    weekly = float(lease["weekly_lease_cost"] or 25000.0) if lease else 25000.0
    years = 3.0 if style == "aggressive" else 5.0
    if cash < (weekly * 52.0 * years):
        return
    # Headroom is rechecked per tail so max_fleet_size still binds exactly, and
    # the affordability test is re-run because each aircraft adds lease cost.
    want = int(_growth_profile(cid)["fleet_growth_per_eval"])
    added = 0
    for i in range(max(1, want)):
        if fs + i >= mx:
            break
        if cash < (weekly * 52.0 * years * (i + 1)):
            break
        tail = f"{cid.split('_')[-1][:2]}-{fs + i + 1:03d}"
        db.execute(
            "INSERT OR REPLACE INTO ai_fleet (ai_tail, competitor_id, type_id, status, assigned_route_pair_id) VALUES (?, ?, ?, 'ACTIVE', NULL)",
            (tail, cid, type_id),
        )
        added += 1
    if not added:
        return
    db.execute(
        "UPDATE competitors SET fleet_size = fleet_size + ? WHERE competitor_id = ?",
        (added, cid),
    )
    append_ai_narrative(cid, f"FLEET+{added}:{type_id}:{target_pair}")


def _decrement_cooldowns(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    k = _ai_eval_interval()
    db.execute(
        "UPDATE ai_memory SET cooldown_weeks = MAX(0, cooldown_weeks - ?) WHERE competitor_id = ?",
        (int(k), cid),
    )
    db.execute("DELETE FROM ai_memory WHERE competitor_id = ? AND cooldown_weeks <= 0", (cid,))


def _financial_distress_check(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    comp = db.fetch_one(
        "SELECT cash, consecutive_loss_weeks FROM competitors WHERE competitor_id = ?",
        (cid,),
    )
    if not comp:
        return
    cash = float(comp["cash"] or 0.0)
    streak = int(comp["consecutive_loss_weeks"] or 0)
    if cash >= 2_000_000:
        streak = 0
    elif cash < 0:
        streak += 1
    elif cash < 500_000:
        streak += 1
    db.execute(
        "UPDATE competitors SET consecutive_loss_weeks = ? WHERE competitor_id = ?",
        (int(streak), cid),
    )
    if streak < 3 or cash >= 0:
        return
    weak = db.fetch_one(
        """
        SELECT route_pair_id FROM competitor_routes
        WHERE competitor_id = ? AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
        ORDER BY estimated_weekly_profit ASC
        LIMIT 1
        """,
        (cid,),
    )
    if not weak:
        return
    pair = str(weak["route_pair_id"])
    db.execute(
        """
        UPDATE competitor_routes SET status = 'SUSPENDED'
        WHERE competitor_id = ? AND route_pair_id = ?
        """,
        (cid, pair),
    )
    append_ai_narrative(cid, f"DISTRESS_SUSPEND:{pair}")


def _roll_actual_weekly_revenue(competitor_id: str, game_week: int) -> None:
    cid = str(competitor_id)
    gw = int(game_week)
    rows = db.fetch_all(
        """
        SELECT route_pair_id, outbound_route_id, inbound_route_id,
               actual_weekly_revenue_avg, actual_weekly_net_avg, actual_lf_avg
        FROM competitor_routes WHERE competitor_id = ?
        """,
        (cid,),
    )
    for r in rows:
        out_id = str(r["outbound_route_id"])
        in_id = str(r["inbound_route_id"])
        agg = db.fetch_one(
            """
            SELECT COALESCE(SUM(simulated_revenue), 0) AS rev,
                   COALESCE(SUM(simulated_net), 0) AS net,
                   AVG(simulated_load_factor) AS lf,
                   COUNT(*) AS n
            FROM ai_flight_segments
            WHERE competitor_id = ? AND game_week = ?
              AND route_id IN (?, ?)
              AND status IN ('COMPLETED', 'LANDED')
            """,
            (cid, gw, out_id, in_id),
        )
        if not agg or int(agg["n"] or 0) <= 0:
            continue
        rev = float(agg["rev"] or 0)
        net = float(agg["net"] or 0)
        lf = float(agg["lf"] or 0)
        prev_r = float(_rget(r, "actual_weekly_revenue_avg", 0.0) or 0.0)
        prev_n = float(_rget(r, "actual_weekly_net_avg", 0.0) or 0.0)
        prev_l = float(_rget(r, "actual_lf_avg", 0.0) or 0.0)
        avg_r = rev if prev_r <= 0 else (0.65 * prev_r + 0.35 * rev)
        avg_n = net if abs(prev_n) <= 1e-6 else (0.65 * prev_n + 0.35 * net)
        avg_l = lf if prev_l <= 0 else (0.65 * prev_l + 0.35 * lf)
        db.execute(
            """
            UPDATE competitor_routes
            SET actual_weekly_revenue_avg = ?, actual_weekly_net_avg = ?, actual_lf_avg = ?
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (float(avg_r), float(avg_n), float(avg_l), cid, str(r["route_pair_id"])),
        )


def ai_post_win_activation(competitor_id: str, route_pair_id: str, awarded_week: int) -> None:
    """
    Called by licences.resolve_auction() when an AI wins a large-market licence.
    Since our Phase 11 licence covers both directions, we activate the full pair immediately.
    """
    cid = str(competitor_id)
    pair = str(route_pair_id).upper().strip()
    comp = db.fetch_one("SELECT * FROM competitors WHERE competitor_id = ?", (cid,))
    if not comp:
        return
    out_id, in_id = _ensure_route_rows_exist_for_pair(pair)
    rt = get_route(out_id)
    if not rt:
        return
    from engine.ai_economics import market_anchor_fares

    strat = str(comp["strategy"] or "HUBSPOKE").upper()
    market_b, market_l = market_anchor_fares(out_id)
    if strat == "BUDGET":
        fl = market_l * 0.80
        fb = fl * 1.3
        freq = 3
    elif strat == "PREMIUM":
        fl = market_l * 1.10
        fb = market_b * 1.25
        freq = 2
    else:
        fl = market_l * 0.97
        fb = market_b * 0.97
        freq = 3
    type_id = ai_resolve_aircraft_type(cid, pair) or "A320"
    est_profit, share = ai_estimate_profit(cid, pair, fl, fb, freq, type_id)
    db.execute(
        """
        INSERT OR REPLACE INTO competitor_routes (
            competitor_id, route_pair_id, outbound_route_id, inbound_route_id,
            fare_business, fare_leisure, frequency_per_week, aircraft_type_id,
            opened_week, status, estimated_weekly_profit, actual_weekly_revenue_avg,
            consecutive_loss_weeks, contested, market_share
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, 0.0, 0, 1, ?)
        """,
        (cid, pair, out_id, in_id, float(fb), float(fl), int(freq), type_id, int(awarded_week), float(est_profit), float(share)),
    )
    # Mark candidate as opened if present.
    try:
        db.execute(
            "UPDATE ai_route_candidates SET decision = 'OPENED' WHERE competitor_id = ? AND route_pair_id = ?",
            (cid, pair),
        )
    except Exception:
        pass



def ai_weekly_turn(settled_game_week: int) -> Dict[str, Any]:
    """
    Settlement Step 8 (Phase 10–11): per competitor — load tracking, evaluate routes,
    scout routes, adjust fares; sealed bids on open route licence auctions.
    """
    ensure_competitors_seeded()
    gw = int(settled_game_week)
    comps = db.fetch_all("SELECT * FROM competitors ORDER BY competitor_id")
    out: Dict[str, Any] = {
        "game_week": gw,
        "competitors": len(comps),
        "light_pass": 0,
        "full_eval": 0,
        "errors": 0,
    }
    for c in comps:
        cid = str(c["competitor_id"])
        import time

        t0 = time.time()
        err = None
        begin_ai_narrative(cid)
        try:
            ai_light_pass(cid, gw)
            out["light_pass"] += 1
        except Exception as e:
            out["errors"] += 1
            err = f"light_pass: {e}"
        try:
            if _full_eval_due(cid, gw):
                ai_full_evaluation(cid, gw)
                out["full_eval"] += 1
        except Exception as e:
            out["errors"] += 1
            err2 = f"full_eval: {e}"
            err = (err + " | " + err2) if err else err2
        try:
            _bid_on_open_auctions(cid, gw)
            _bid_on_slot_auctions(cid, gw)
        except Exception as e:
            out["errors"] += 1
            err3 = f"bids: {e}"
            err = (err + " | " + err3) if err else err3
        dt_ms = (time.time() - t0) * 1000.0
        narrative = take_ai_narrative(cid)
        try:
            db.execute(
                """
                INSERT INTO ai_turn_log (
                    log_id, competitor_id, game_week,
                    routes_opened, routes_closed, fares_adjusted, auctions_bid,
                    candidates_evaluated, duration_ms, error, narrative
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                """,
                (str(uuid.uuid4()), cid, gw, float(dt_ms), err, narrative or None),
            )
        except Exception:
            try:
                db.execute(
                    """
                    INSERT INTO ai_turn_log (
                        log_id, competitor_id, game_week,
                        routes_opened, routes_closed, fares_adjusted, auctions_bid,
                        candidates_evaluated, duration_ms, error
                    ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?)
                    """,
                    (str(uuid.uuid4()), cid, gw, float(dt_ms), err),
                )
            except Exception:
                pass
    return out


def ai_evaluate_routes(competitor_id: str, game_week: int) -> int:
    raise NotImplementedError("Legacy Phase 10 function removed; use ai_light_pass().")


def ai_scout_routes(competitor_id: str, game_week: int) -> int:
    raise NotImplementedError("Legacy Phase 10 function removed; use ai_full_evaluation().")


def _player_contests_route(route_id: str) -> bool:
    if db.fetch_one("SELECT 1 FROM flight_segments WHERE route_id = ? LIMIT 1", (route_id,)):
        return True
    if db.fetch_one(
        "SELECT 1 FROM flight_schedules WHERE route_id = ? AND active = 1 LIMIT 1",
        (route_id,),
    ):
        return True
    return False


def ai_adjust_fares(competitor_id: str, _game_week: int, _current_month: int) -> None:
    raise NotImplementedError("Legacy Phase 10 function removed; use _adjust_fares_light().")


def estimate_weekly_profit_for_competitor(competitor_id: str, route_id: str, game_week: int) -> float:
    cid = str(competitor_id)
    rid = str(route_id or "").strip().upper()
    row = db.fetch_one(
        """
        SELECT route_pair_id, fare_leisure, fare_business, frequency_per_week, aircraft_type_id
        FROM competitor_routes
        WHERE competitor_id = ?
          AND (outbound_route_id = ? OR inbound_route_id = ? OR route_pair_id = ?)
        LIMIT 1
        """,
        (cid, rid, rid, rid),
    )
    if not row:
        return 0.0
    try:
        profit, _share = ai_estimate_profit(
            cid,
            str(row["route_pair_id"]),
            float(row["fare_leisure"] or 0),
            float(row["fare_business"] or 0),
            max(1, int(row["frequency_per_week"] or 1)),
            str(row["aircraft_type_id"] or "A320"),
        )
        return float(profit)
    except Exception:
        return 0.0


def estimate_competitor_weekly_revenue(competitor_id: str) -> float:
    """Rough weekly revenue for market intel (operating routes only)."""
    cid = str(competitor_id)
    rows = db.fetch_all(
        """
        SELECT cr.route_pair_id, cr.outbound_route_id, cr.inbound_route_id,
               cr.fare_business, cr.fare_leisure, cr.frequency_per_week, cr.aircraft_type_id, cr.status
        FROM competitor_routes cr
        WHERE cr.competitor_id = ?
        """,
        (cid,),
    )
    total = 0.0
    for r in rows:
        r = dict(r)
        if str(r.get("status") or "ACTIVE") not in ("ACTIVE", "SUSPENDED"):
            continue
        avg = db.fetch_one(
            """
            SELECT actual_weekly_revenue_avg FROM competitor_routes
            WHERE competitor_id = ? AND route_pair_id = ?
            """,
            (cid, str(r["route_pair_id"])),
        )
        actual = float(avg["actual_weekly_revenue_avg"] or 0) if avg else 0.0
        if actual > 0:
            total += actual
            continue
        pair = str(r["route_pair_id"])
        freq = max(1, int(r["frequency_per_week"] or 1))
        fb = float(r["fare_business"] or 0.0)
        fl = float(r["fare_leisure"] or 0.0)
        type_id = str(r.get("aircraft_type_id") or "A320")
        try:
            profit, _share = ai_estimate_profit(cid, pair, fl, fb, freq, type_id)
        except Exception:
            profit = 0.0
        total += max(0.0, float(profit) + 50_000.0)
    return float(total)
