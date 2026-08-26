"""JSON API for the overlay UI. Calls existing engine modules; no new game rules."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from db import db
from engine import aircraft, airports, routes, setup
from engine.gates import current_game_week, list_open_gate_auctions, submit_gate_bid


def _row(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "keys"):
        try:
            return {k: obj[k] for k in obj.keys()}
        except Exception:
            return dict(obj)
    return obj


def _rows(items) -> list:
    return [_row(x) for x in (items or [])]


def _ok(data: Optional[dict] = None) -> dict:
    out = {"ok": True}
    if data:
        out.update(data)
    return out


def _err(message: str, **extra) -> dict:
    return {"ok": False, "error": str(message), **extra}


_hud_finance_cache: dict[str, Any] = {"t": 0.0, "v": None}


def _hud_finance() -> Optional[dict]:
    """Week-to-date gross, flight cash flow, and current game-day revenue for the HUD hover."""
    now = time.time()
    cached = _hud_finance_cache.get("v")
    if cached is not None and (now - float(_hud_finance_cache.get("t") or 0)) < 1.25:
        return cached
    if not setup.airline_exists():
        return None
    try:
        from engine.clock import get_display_game_hours
        from engine.scheduling import week_base_hours
    except Exception:
        return None
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    try:
        ghe = float(get_display_game_hours())
    except Exception:
        ghe = 0.0
    w_base = float(week_base_hours(gw))
    hour_in_week = max(0.0, ghe - w_base)
    day_i = int(min(6, max(0, hour_in_week // 24)))
    d0 = w_base + day_i * 24.0
    d1 = d0 + 24.0
    w_end = w_base + 168.0
    week_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(revenue_gross), 0) AS revenue,
               COALESCE(SUM(net_contribution), 0) AS cash_flow
        FROM flight_segments
        WHERE scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status IN ('LANDED', 'DIVERTED', 'IN_AIR')
        """,
        (w_base, w_end),
    )
    revenue = float(week_row["revenue"] or 0) if week_row else 0.0
    cash_flow = float(week_row["cash_flow"] or 0) if week_row else 0.0
    day_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(revenue_gross), 0) AS rev
        FROM flight_segments
        WHERE scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status IN ('LANDED', 'DIVERTED', 'IN_AIR')
        """,
        (d0, d1),
    )
    days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    debt = 0.0
    weekly_loan = 0.0
    try:
        from engine.banking import debt_and_headroom

        snap = debt_and_headroom()
        debt = float(snap.get("debt") or 0)
        weekly_loan = float(snap.get("weekly_service") or 0)
    except Exception:
        pass
    fuel_spot = None
    try:
        gs_fuel = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
        if gs_fuel and gs_fuel["fuel_price_current"] is not None:
            fuel_spot = float(gs_fuel["fuel_price_current"])
    except Exception:
        fuel_spot = None
    out = {
        "week": gw,
        "day": days[day_i],
        "revenue": revenue,
        "cash_flow": cash_flow,
        "today_revenue": float(day_row["rev"] or 0) if day_row else 0.0,
        "debt": debt,
        "weekly_loan": weekly_loan,
        "fuel_spot_bbl": fuel_spot,
    }
    _hud_finance_cache["t"] = now
    _hud_finance_cache["v"] = out
    return out


def get_state() -> dict:
    al = setup.get_airline()
    gs = db.fetch_one("SELECT * FROM game_state WHERE id = 1")
    clock = {}
    try:
        from engine.clock import get_global_clock, get_display_game_hours

        clk = get_global_clock()
        if clk is not None and clk.is_alive():
            clock = clk.get_status()
        else:
            ghe = float(get_display_game_hours()) if gs else 0.0
            sp = int(gs["speed_multiplier"] or 0) if gs else 0
            week = int(ghe // 168) + 1 if gs else 1
            hiw = ghe % 168.0
            day = int(hiw // 24) + 1
            hod = hiw % 24.0
            hh = int(hod)
            mm = int((hod - hh) * 60.0) % 60
            spd = "paused" if sp == 0 else f"{sp}×"
            clock = {
                "game_hours_elapsed": ghe,
                "current_game_hour": ghe,
                "current_week": week,
                "current_day": day,
                "current_hour": hh,
                "speed_multiplier": sp,
                "is_paused": sp == 0,
                "real_seconds_per_game_hour": 30,
                "time_display": f"Week {week} · Day {day} · {hh:02d}:{mm:02d} · {spd}",
                "auto_pause_alert": None,
            }
    except Exception:
        clock = {"time_display": "—", "speed_multiplier": 0, "is_paused": True}
    news = []
    try:
        from engine.news_feed import recent_lines

        news = recent_lines(12)
    except Exception:
        pass
    airline = None
    if al:
        airline = {
            "name": al.get("name"),
            "callsign": al.get("callsign"),
            "home_hub_iata": al.get("home_hub_iata"),
            "cash": float(al.get("cash") or 0),
            "total_debt": float(al.get("total_debt") or 0),
            "reputation_score": float(al.get("reputation_score") or 0),
            "brand_power": float(al.get("brand_power") or 1.0),
            "credit_score": int(al.get("credit_score") or 0),
        }
    finance = None
    try:
        finance = _hud_finance()
    except Exception:
        finance = None
    return _ok(
        {
            "airline": airline,
            "clock": clock,
            "game_week": int(gs["game_week"] or 1) if gs else 1,
            "news": news,
            "finance": finance,
            "notifications": _unread_notifications(),
        }
    )


def _unread_notifications(limit: int = 8) -> list:
    try:
        rows = db.fetch_all(
            """
            SELECT notification_id, game_week, type, body
            FROM player_notifications
            WHERE read = 0
            ORDER BY game_week DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return _rows(rows)
    except Exception:
        return []


def player_notifications() -> dict:
    return _ok({"notifications": _unread_notifications(20)})


def ack_notifications(body: dict) -> dict:
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        return _ok({"acked": 0})
    clean = [str(x) for x in ids if str(x).strip()][:50]
    if not clean:
        return _ok({"acked": 0})
    try:
        db.execute(
            f"UPDATE player_notifications SET read = 1 WHERE notification_id IN ({','.join(['?'] * len(clean))})",
            tuple(clean),
        )
    except Exception as e:
        return _err(str(e))
    return _ok({"acked": len(clean)})


def _abs_to_label(abs_hour: float) -> str:
    from engine.scheduling import hhmm_from_absolute_game_hour

    h_in_w = float(abs_hour) % 168.0
    di = int(h_in_w // 24.0)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return f"{dows[max(0, min(6, di))]} {hhmm_from_absolute_game_hour(float(abs_hour))}"


def create_airline(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    callsign = str(body.get("callsign") or "").strip().upper()
    hub = str(body.get("home_hub_iata") or "").strip().upper()
    if not name:
        return _err("Airline name is required.")
    if len(callsign) != 3 or not callsign.isalpha():
        return _err("Callsign must be exactly 3 letters.")
    if not hub:
        return _err("Home hub IATA is required.")
    try:
        created = setup.create_airline(name=name, callsign=callsign, home_hub_iata=hub)
    except Exception as e:
        return _err(str(e))
    return _ok({"airline": created})


def reset_airline() -> dict:
    try:
        created = setup.reset_airline()
    except Exception as e:
        return _err(str(e))
    return _ok({"airline": created, "message": "Airline reset to a new-game start."})


def delete_airline() -> dict:
    try:
        setup.delete_airline()
    except Exception as e:
        return _err(str(e))
    return _ok({"airline": None, "message": "Airline deleted."})


def search_airports(q: str) -> dict:
    q = (q or "").strip()
    if len(q) < 1:
        return _ok({"airports": []})
    found = airports.search_airports(q)
    slim = []
    for a in found:
        if not a:
            continue
        slim.append(
            {
                "iata": a.get("iata"),
                "name": a.get("name"),
                "city": a.get("city"),
                "country": a.get("country"),
                "score": a.get("score"),
                "category": a.get("category"),
            }
        )
    return _ok({"airports": slim})


def list_catalog(category: Optional[str] = None) -> dict:
    cat = (category or "").strip().upper() or None
    rows = aircraft.list_catalog(category=cat, sort_by="range_nm")
    out = []
    for r in rows[:80]:
        d = _row(r)
        out.append(
            {
                "type_id": d.get("type_id"),
                "display_name": d.get("display_name"),
                "category": d.get("category"),
                "range_nm": d.get("range_nm"),
                "cruise_speed_kts": d.get("cruise_speed_kts"),
                "purchase_price": d.get("purchase_price"),
                "weekly_lease_cost": d.get("weekly_lease_cost"),
            }
        )
    return _ok({"aircraft": out})


def _eec_costs() -> dict:
    keys = (
        ("economy", "eec_cost_economy"),
        ("premium_economy", "eec_cost_prem_eco"),
        ("business", "eec_cost_business"),
        ("first", "eec_cost_first"),
    )
    out = {}
    for name, key in keys:
        row = db.fetch_one("SELECT value FROM financial_constants WHERE key = ?", (key,))
        out[name] = float(row["value"]) if row else 1.0
    return out


def cabin_layout(type_id: str) -> dict:
    from engine.cabin import compute_eec_used, get_default_config, validate_config

    tid = str(type_id or "").strip().upper()
    if not tid:
        return _err("type_id is required.")
    at = _row(aircraft.get_aircraft_type(tid))
    if not at:
        return _err(f"Aircraft type '{tid}' not found.")
    cfg = get_default_config(tid) or {}
    eco = int(cfg.get("seats_economy") or 0)
    prem = int(cfg.get("seats_premium_economy") or 0)
    biz = int(cfg.get("seats_business") or 0)
    first = int(cfg.get("seats_first") or 0)
    eec_limit = int(at.get("eec") or 0)
    used = compute_eec_used(eco, prem, biz, first)
    valid, message = validate_config(eco, prem, biz, first, eec_limit)
    return _ok(
        {
            "type_id": tid,
            "display_name": at.get("display_name"),
            "purchase_price": float(at.get("purchase_price") or 0),
            "weekly_lease_cost": float(at.get("weekly_lease_cost") or 0),
            "eec_limit": eec_limit,
            "eec_used": used,
            "valid": bool(valid),
            "message": message,
            "seats_economy": eco,
            "seats_premium_economy": prem,
            "seats_business": biz,
            "seats_first": first,
            "costs": _eec_costs(),
        }
    )


def list_fleet() -> dict:
    from engine.scheduling import max_weekly_airborne_hours_cap

    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    cap = float(max_weekly_airborne_hours_cap() or 168.0)
    hours_by_tail: dict[str, float] = {}
    for r in db.fetch_all(
        """
        SELECT tail_number,
               COALESCE(SUM(scheduled_arr_game_hour - scheduled_dep_game_hour), 0) AS h
        FROM flight_segments
        WHERE game_week = ?
          AND status IN ('SCHEDULED', 'IN_AIR', 'LANDED')
        GROUP BY tail_number
        """,
        (gw,),
    ) or []:
        hours_by_tail[str(r["tail_number"])] = float(r["h"] or 0)
    rows = []
    for r in aircraft.get_fleet() or []:
        d = _row(r)
        tail = str(d.get("tail_number") or "")
        used = float(hours_by_tail.get(tail) or 0)
        pct = (used / cap * 100.0) if cap > 0 else 0.0
        d["utilization"] = {
            "game_week": gw,
            "airborne_hours": used,
            "cap_hours": cap,
            "utilization_pct": min(pct, 999.0),
            "at_or_over_limit": used >= cap,
        }
        rows.append(d)
    return _ok({"fleet": rows})


def _cabin_seats_from_body(body: dict) -> Optional[tuple]:
    keys = ("seats_economy", "seats_premium_economy", "seats_business", "seats_first")
    if not any(k in body and body[k] not in (None, "") for k in keys):
        return None
    try:
        return (
            int(body.get("seats_economy") or 0),
            int(body.get("seats_premium_economy") or 0),
            int(body.get("seats_business") or 0),
            int(body.get("seats_first") or 0),
        )
    except (TypeError, ValueError) as e:
        raise ValueError("Seat counts must be whole numbers.") from e


def acquire_aircraft(body: dict) -> dict:
    type_id = str(body.get("type_id") or "").strip()
    mode = str(body.get("mode") or "buy").strip().lower()
    if not type_id:
        return _err("type_id is required.")
    try:
        qty = int(body.get("quantity") if body.get("quantity") not in (None, "") else (body.get("qty") or 1))
    except (TypeError, ValueError):
        return _err("Quantity must be a whole number.")
    if qty < 1:
        return _err("Quantity must be at least 1.")
    if qty > 50:
        return _err("Order at most 50 aircraft at a time.")
    try:
        cabin = _cabin_seats_from_body(body)
        spec = aircraft.get_aircraft_type(type_id)
        if not spec:
            return _err(f"Aircraft type '{type_id}' not found in catalog.")
        al = setup.get_airline()
        if not al:
            return _err("Create an airline first.")
        if mode != "lease":
            unit = float(spec["purchase_price"] or 0)
            need = unit * qty
            cash = float(al["cash"] or 0)
            if cash < need:
                return _err(
                    f"Insufficient funds for {qty}× {type_id}. "
                    f"Need ${need:,.0f}, available ${cash:,.0f}."
                )
        else:
            unit = float(spec["weekly_lease_cost"] or 0)
            need = unit * qty
            cash = float(al["cash"] or 0)
            if cash < need:
                return _err(
                    f"Need ${need:,.0f} cash for first-week lease on {qty}× {type_id} "
                    f"(available ${cash:,.0f})."
                )
        if mode == "lease":
            try:
                weeks = int(body.get("weeks") or body.get("lease_weeks") or 52)
            except (TypeError, ValueError):
                return _err("Lease weeks must be a whole number.")
            if weeks < 1:
                return _err("Lease weeks must be at least 1.")
            if weeks > 520:
                return _err("Lease term cannot exceed 520 weeks.")
        else:
            weeks = 0
        got = []
        for _ in range(qty):
            if mode == "lease":
                got.append(aircraft.lease_aircraft(type_id, weeks, cabin_seats=cabin))
            else:
                got.append(aircraft.buy_aircraft(type_id, cabin_seats=cabin))
    except Exception as e:
        return _err(str(e))
    first = got[0] if got else {}
    return _ok({"aircraft": first, "fleet": got, "quantity": qty})


def list_player_routes() -> dict:
    return _ok({"routes": routes.get_player_routes()})


def player_routes_overview() -> dict:
    """
    All opened routes as one table payload: distance, block time, demand, and
    which flights/tails operate each route this week.
    """
    from engine.scheduling import calendar_game_week_from_state

    gw = int(calendar_game_week_from_state())
    player_rts = routes.get_player_routes()
    if not player_rts:
        return _ok({"week": gw, "routes": []})

    # Typical cruise for flight-time estimates when no segment has flown yet.
    cruise_row = db.fetch_one(
        """
        SELECT AVG(t.cruise_speed_kts) AS kts
        FROM fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE t.cruise_speed_kts > 0
        """
    )
    default_kts = float(cruise_row["kts"] or 450.0) if cruise_row else 450.0

    ops_by_route: dict[str, list[dict]] = {}
    hours_by_route: dict[str, list[float]] = {}
    try:
        rows = db.fetch_all(
            """
            SELECT
                fs.route_id,
                fs.flight_number,
                fs.tail_number,
                f.type_id,
                fs.scheduled_dep_game_hour AS dep_h,
                fs.scheduled_arr_game_hour AS arr_h,
                COALESCE(fs.is_ferry, 0) AS is_ferry
            FROM flight_segments fs
            JOIN fleet f ON f.tail_number = fs.tail_number
            JOIN player_routes pr ON pr.route_id = fs.route_id
            WHERE fs.game_week = ?
              AND fs.status != 'CANCELLED'
            ORDER BY fs.route_id, fs.scheduled_dep_game_hour, fs.flight_number
            """,
            (gw,),
        )
        seen_keys: dict[str, set] = {}
        for r in rows or []:
            rid = str(r["route_id"] or "").upper()
            if not rid:
                continue
            dep = float(r["dep_h"] or 0.0)
            arr = float(r["arr_h"] or 0.0)
            if arr > dep:
                hours_by_route.setdefault(rid, []).append(arr - dep)
            if int(r["is_ferry"] or 0):
                continue
            key = (str(r["flight_number"] or ""), str(r["tail_number"] or ""), str(r["type_id"] or ""))
            bag = seen_keys.setdefault(rid, set())
            if key in bag:
                continue
            bag.add(key)
            ops_by_route.setdefault(rid, []).append(
                {
                    "flight_number": key[0],
                    "tail_number": key[1],
                    "type_id": key[2],
                }
            )
    except Exception:
        pass

    out = []
    for rt in player_rts:
        rid = str(rt["route_id"]).upper()
        dist = float(rt.get("distance_nm") or 0.0)
        hours = hours_by_route.get(rid) or []
        if hours:
            flight_hours = sum(hours) / len(hours)
        else:
            flight_hours = (dist / default_kts) if default_kts > 0 and dist > 0 else 0.0
        origin = airports.get_airport(str(rt["origin_iata"]))
        dest = airports.get_airport(str(rt["dest_iata"]))
        flights = ops_by_route.get(rid) or []
        src = str(rt.get("demand_source") or "")
        floored = False
        try:
            from engine.demand_display import floor_flag_for_route, source_badge

            src2, floored = floor_flag_for_route(
                str(rt["origin_iata"]),
                str(rt["dest_iata"]),
                dist,
                origin_airport=dict(origin) if origin else None,
                dest_airport=dict(dest) if dest else None,
            )
            if src2:
                src = src2
            badge = source_badge(src)
        except Exception:
            badge = src or "—"
        out.append(
            {
                "route_id": rid,
                "origin_iata": rt["origin_iata"],
                "dest_iata": rt["dest_iata"],
                "origin_city": (origin or {}).get("city"),
                "dest_city": (dest or {}).get("city"),
                "distance_nm": dist,
                "flight_hours": round(float(flight_hours), 2),
                "flight_hours_estimated": not bool(hours),
                "base_demand_business": int(rt.get("base_demand_business") or 0),
                "base_demand_leisure": int(rt.get("base_demand_leisure") or 0),
                "demand_source": src,
                "demand_source_badge": badge,
                "market_floor_applied": bool(floored),
                "price_leisure": float(rt.get("price_leisure") or 0),
                "price_premium_economy": float(rt.get("price_premium_economy") or 0),
                "price_business": float(rt.get("price_business") or 0),
                "price_first": float(rt.get("price_first") or 0),
                "flights": flights,
                "flights_label": "; ".join(
                    f"{f['flight_number']} ({f['tail_number']} · {f['type_id']})"
                    for f in flights
                )
                if flights
                else "—",
                "weekly_ops": len(hours),
            }
        )
    return _ok({"week": gw, "routes": out})


def _route_preview_detail(origin: str, dest: str, prev: dict) -> dict:
    """
    Explain a route-opening quote: why the price is what it is, the market it buys, and
    what each end will demand operationally. Read-only.
    """
    from engine.airports import get_airport as _ap

    out: dict = {}
    o = _ap(str(origin).upper())
    d = _ap(str(dest).upper())
    dist = float(prev.get("distance_nm") or 0.0)

    # Why is the total what it is?
    fwd_has = bool((prev.get("forward") or {}).get("player_has"))
    rev_has = bool((prev.get("reverse") or {}).get("player_has"))
    free_legs = [x for x in (prev.get("opens") or []) if not x.get("charge_acquisition")]
    total = float(prev.get("total_new_cost") or 0.0)
    if fwd_has and rev_has:
        reason = "You already operate both directions — nothing to buy."
    elif fwd_has or rev_has:
        reason = ("You already operate one direction of this hub pair, so the return leg is "
                  "added free.")
    elif total <= 0 and free_legs:
        reason = "Companion leg of a hub pair — covered by the other direction's fee."
    elif prev.get("mode") == "hub_pair":
        reason = ("One acquisition fee opens both directions because your hub is on this pair.")
    else:
        reason = "Neither airport is your hub, so only this one direction is opened."
    out["cost_reason"] = reason
    out["already_operated"] = bool(fwd_has and rev_has)

    # Market being bought
    try:
        from engine.demand import preview_weekly_demand_before_open

        if o and d and dist > 0:
            dem = preview_weekly_demand_before_open(dict(o), dict(d), dist)
            from engine.demand_display import summary_from_preview

            summary = summary_from_preview(dem)
            out["demand"] = {
                "business_pax": int(dem.get("business_pax") or 0),
                "leisure_pax": int(dem.get("leisure_pax") or 0),
                "total_pax": int(dem.get("total_pax") or 0),
                "weekly_market_total": int(summary.get("weekly_market_total") or 0),
                "base_demand_business": int(dem.get("base_demand_business") or 0),
                "base_demand_leisure": int(dem.get("base_demand_leisure") or 0),
                "demand_source": str(summary.get("demand_source") or ""),
                "demand_source_badge": str(summary.get("demand_source_badge") or ""),
                "market_floor_applied": bool(summary.get("market_floor_applied")),
                "economy_pax": int(dem.get("economy_pax") or 0),
                "premium_economy_pax": int(dem.get("premium_economy_pax") or 0),
                "business_cabin_pax": int(dem.get("business_cabin_pax") or 0),
                "first_pax": int(dem.get("first_pax") or 0),
                "game_week": dem.get("game_week"),
                "current_month": dem.get("current_month"),
                "price_leisure_default": float(dem.get("price_leisure_default") or 0.0),
                "price_business_default": float(dem.get("price_business_default") or 0.0),
                "labels": summary.get("labels") or {},
            }
    except Exception:
        pass

    # Operational facts per endpoint
    def _end(ap):
        if not ap:
            return None
        iata = str(ap["iata"]).upper()
        info = {
            "iata": iata,
            "name": str(ap["name"] or ""),
            "city": str(ap["city"] or ""),
            "category": str(ap["category"] or ""),
            "score": int(ap["score"] or 0),
            "runway_length_ft": int(ap["runway_length_ft"] or 0),
            "landing_fee_per_1000": float(ap.get("landing_fee_per_1000") or 0.0),
            "gate_fee": float(ap.get("gate_fee") or 0.0),
        }
        try:
            from engine.gates import is_auctioned_airport

            info["gate_auctioned"] = bool(is_auctioned_airport(iata))
        except Exception:
            info["gate_auctioned"] = False
        try:
            from engine.slots import is_slot_controlled

            info["slot_controlled"] = bool(is_slot_controlled(iata))
        except Exception:
            info["slot_controlled"] = False
        return info

    out["endpoints"] = [x for x in (_end(o), _end(d)) if x]
    return out


def preview_route(origin: str, dest: str) -> dict:
    al = setup.get_airline()
    hub = str(al["home_hub_iata"]) if al else None
    try:
        prev = routes.preview_route_opening(origin, dest, hub_iata=hub)
    except Exception as e:
        return _err(str(e))
    try:
        prev = {**prev, **_route_preview_detail(origin, dest, prev)}
    except Exception:
        pass
    return _ok(prev)


def open_player_route(body: dict) -> dict:
    origin = str(body.get("origin") or "").strip().upper()
    dest = str(body.get("dest") or "").strip().upper()
    al = setup.get_airline()
    hub = str(al["home_hub_iata"]) if al else None
    try:
        prev = routes.preview_route_opening(origin, dest, hub_iata=hub)
        result = routes.execute_route_opens(prev["opens"])
    except Exception as e:
        return _err(str(e))
    return _ok(result)


def route_detail(route_id: str) -> dict:
    rid = str(route_id or "").strip().upper()
    if not rid:
        return _err("route_id is required.")
    rt = routes.get_route(rid)
    if not rt:
        return _err(f"Route '{rid}' not found.")
    if not routes.player_route_exists(str(rt["origin_iata"]), str(rt["dest_iata"])):
        return _err(f"'{rid}' is not one of your opened routes.")
    origin = airports.get_airport(str(rt["origin_iata"]))
    dest = airports.get_airport(str(rt["dest_iata"]))
    perf = None
    try:
        from engine.demand import estimate_route_performance

        raw = estimate_route_performance(rid)
        from engine.demand_display import build_demand_summary, floor_flag_for_route

        src = str(rt.get("demand_source") or "")
        floored = False
        try:
            src2, floored = floor_flag_for_route(
                str(rt["origin_iata"]),
                str(rt["dest_iata"]),
                float(rt.get("distance_nm") or 0),
            )
            if src2:
                src = src2
        except Exception:
            pass
        gw = cm = None
        try:
            gs = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
            if gs:
                gw, cm = gs.get("game_week"), gs.get("current_month")
        except Exception:
            pass
        summary = build_demand_summary(
            business_pax=raw.get("business_pax"),
            leisure_pax=raw.get("leisure_pax"),
            demand_source=src,
            market_floor_applied=floored,
            base_demand_business=rt.get("base_demand_business"),
            base_demand_leisure=rt.get("base_demand_leisure"),
            economy_pax=raw.get("economy_pax"),
            premium_economy_pax=raw.get("premium_economy_pax"),
            business_cabin_pax=raw.get("business_cabin_pax"),
            first_pax=raw.get("first_pax"),
            weekly_market_total=raw.get("weekly_market_total"),
            aircraft_fill_pax=raw.get("aircraft_fill_pax", raw.get("total_pax")),
            game_week=gw,
            current_month=cm,
        )
        perf = {
            "business_pax": int(raw.get("business_pax") or 0),
            "leisure_pax": int(raw.get("leisure_pax") or 0),
            "economy_pax": int(raw.get("economy_pax") or 0),
            "premium_economy_pax": int(raw.get("premium_economy_pax") or 0),
            "business_cabin_pax": int(raw.get("business_cabin_pax") or 0),
            "first_pax": int(raw.get("first_pax") or 0),
            "pax_economy": int(raw.get("pax_economy") or 0),
            "pax_premium_economy": int(raw.get("pax_premium_economy") or 0),
            "pax_business": int(raw.get("pax_business") or 0),
            "pax_first": int(raw.get("pax_first") or 0),
            "total_pax": int(raw.get("total_pax") or 0),
            "weekly_market_total": int(summary.get("weekly_market_total") or 0),
            "aircraft_fill_pax": int(summary.get("aircraft_fill_pax") or 0),
            "demand_source": str(summary.get("demand_source") or ""),
            "demand_source_badge": str(summary.get("demand_source_badge") or ""),
            "market_floor_applied": bool(summary.get("market_floor_applied")),
            "labels": summary.get("labels") or {},
            "load_factor": float(raw.get("load_factor") or 0),
            "revenue_economy": float(raw.get("revenue_economy") or 0),
            "revenue_premium_economy": float(raw.get("revenue_premium_economy") or 0),
            "revenue_business": float(raw.get("revenue_business") or 0),
            "revenue_first": float(raw.get("revenue_first") or 0),
            "gross_revenue": float(raw.get("gross_revenue") or 0),
            "avg_fare": float(raw.get("avg_fare") or 0),
            "reputation_multiplier": float(raw.get("reputation_multiplier") or 1),
        }
    except Exception:
        pass
    ops = None
    sched = None
    try:
        from engine.scheduling import route_current_schedule_summary, route_weekly_passenger_accounting

        sched = route_current_schedule_summary(rid)
        raw_ops = route_weekly_passenger_accounting(rid)
        if raw_ops:
            rc = raw_ops.get("remaining_cabin_demand") or {}
            ops = {
                "game_week": raw_ops.get("game_week"),
                "weekly_business": raw_ops.get("weekly_business"),
                "weekly_leisure": raw_ops.get("weekly_leisure"),
                "carried_business": raw_ops.get("carried_business"),
                "carried_leisure": raw_ops.get("carried_leisure"),
                "remaining_business": raw_ops.get("remaining_business"),
                "remaining_leisure": raw_ops.get("remaining_leisure"),
                "remaining_cabin": {
                    "F": int(rc.get("F") or 0),
                    "J": int(rc.get("J") or 0),
                    "W": int(rc.get("W") or 0),
                    "Y": int(rc.get("Y") or 0),
                },
            }
    except Exception:
        pass
    return _ok(
        {
            "route": {
                "route_id": rt["route_id"],
                "origin_iata": rt["origin_iata"],
                "dest_iata": rt["dest_iata"],
                "origin_city": (origin or {}).get("city"),
                "dest_city": (dest or {}).get("city"),
                "distance_nm": float(rt.get("distance_nm") or 0),
                "price_leisure": float(rt.get("price_leisure") or 0),
                "price_premium_economy": float(rt.get("price_premium_economy") or 0),
                "price_business": float(rt.get("price_business") or 0),
                "price_first": float(rt.get("price_first") or 0),
                "base_demand_business": int(rt.get("base_demand_business") or 0),
                "base_demand_leisure": int(rt.get("base_demand_leisure") or 0),
                "demand_source": str(rt.get("demand_source") or ""),
            },
            "schedule": sched,
            "performance": perf,
            "ops": ops,
        }
    )


def update_route_prices(body: dict) -> dict:
    rid = str(body.get("route_id") or "").strip().upper()
    rt = routes.get_route(rid)
    if not rt:
        return _err(f"Route '{rid}' not found.")
    if not routes.player_route_exists(str(rt["origin_iata"]), str(rt["dest_iata"])):
        return _err(f"'{rid}' is not one of your opened routes.")
    try:
        pl = float(body.get("price_leisure"))
        pw = float(body.get("price_premium_economy"))
        pb = float(body.get("price_business"))
        pf = float(body.get("price_first"))
    except (TypeError, ValueError):
        return _err("All four fares must be numbers.")
    try:
        routes.update_route_prices(rid, pb, pl, pw, pf)
    except Exception as e:
        return _err(str(e))
    return route_detail(rid)


def _parse_route_plan(plan: str) -> list[str]:
    from engine.scheduling import route_ids_from_airport_chain

    raw = (plan or "").strip()
    if not raw:
        raise ValueError("Enter an airport chain (TPA-MCO-TPA) or comma-separated route IDs.")
    if "," in raw:
        return [r.strip().upper() for r in raw.split(",") if r.strip()]
    return route_ids_from_airport_chain(raw)


WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI"]
WEEKEND_DAYS = ["SAT", "SUN"]
ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _days_of_week_from_body(body: dict) -> str:
    """CLI-equivalent DAILY / weekday / weekend / custom day list for templates."""
    days = body.get("days")
    selected: list[str] = []
    if isinstance(days, list):
        alias = {
            "1": "MON", "2": "TUE", "3": "WED", "4": "THU", "5": "FRI", "6": "SAT", "7": "SUN",
            "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED", "THURSDAY": "THU",
            "FRIDAY": "FRI", "SATURDAY": "SAT", "SUNDAY": "SUN",
        }
        for d in days:
            token = str(d or "").strip().upper()
            token = alias.get(token, token[:3] if len(token) >= 3 else token)
            if token in ALL_DAYS and token not in selected:
                selected.append(token)
    else:
        token = str(days or "DAILY").strip().upper()
        if token in ("DAILY", ""):
            return "DAILY"
        if token == "WEEKDAYS":
            return json.dumps(WEEKDAYS)
        if token in ("WEEKENDS", "WEEKEND"):
            return json.dumps(WEEKEND_DAYS)
        if token.startswith("["):
            parsed = json.loads(token)
            if isinstance(parsed, list):
                return _days_of_week_from_body({"days": parsed})
    if not selected:
        raise ValueError("Pick at least one operating day (Mon–Sun).")
    ordered = [d for d in ALL_DAYS if d in selected]
    if ordered == ALL_DAYS:
        return "DAILY"
    return json.dumps(ordered)


def _departure_time_from_body(body: dict) -> str:
    dep = str(body.get("departure_time") or "08:00").strip() or "08:00"
    if dep.count(":") == 2:
        dep = dep.rsplit(":", 1)[0]
    parts = dep.split(":")
    if len(parts) != 2:
        raise ValueError("First departure must be HH:MM.")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("First departure must be HH:MM.")
    return f"{h:02d}:{m:02d}"


def _tail_schedule_view(tail: str) -> dict:
    from ui.tail_schedule_grid import json_tail_schedule_view

    return json_tail_schedule_view(str(tail).strip().upper())


def preview_schedule(body: dict) -> dict:
    from engine.scheduling import format_rotation_airport_chain, get_tail_weekly_utilization

    tail = str(body.get("tail_number") or "").strip().upper()
    plan = str(body.get("chain") or body.get("plan") or "").strip()
    if not tail:
        return _err("Pick a tail number.")
    try:
        rids = _parse_route_plan(plan)
        util = get_tail_weekly_utilization(tail)
    except Exception as e:
        return _err(str(e))
    ac = aircraft.get_fleet_aircraft(tail)
    if not ac:
        return _err(f"Aircraft '{tail}' not found in fleet")
    al = setup.get_airline() or {}
    cs = str(al.get("callsign") or "FL")
    try:
        mtt_min = float(db.get_financial_constant("mtt_minutes") or 30)
    except Exception:
        mtt_min = 30.0
    from engine.scheduling.flight_numbers import propose_flight_numbers_for_routes

    proposed = propose_flight_numbers_for_routes(rids, callsign=cs)
    legs = [
        {
            "route_id": rid,
            "flight_number": proposed[i] if i < len(proposed) else "",
            "turn_minutes": mtt_min,
        }
        for i, rid in enumerate(rids)
    ]
    return _ok(
        {
            "tail_number": tail,
            "route_ids": rids,
            "chain": format_rotation_airport_chain(rids),
            "utilization": util,
            "legs": legs,
            "min_turn_minutes": mtt_min,
            "schedule": _tail_schedule_view(tail),
        }
    )


def assign_schedule(body: dict) -> dict:
    from engine.scheduling import (
        assign_rotation,
        cancel_rotation,
        create_chained_detailed_rotation,
        create_flight_schedule,
        format_rotation_airport_chain,
        get_tail_weekly_utilization,
        merge_detailed_weekly_template,
    )

    tail = str(body.get("tail_number") or "").strip().upper()
    mode = str(body.get("mode") or "quick").strip().lower()
    plan = str(body.get("chain") or body.get("plan") or "").strip()
    if not tail:
        return _err("tail_number is required.")
    if mode == "clear":
        try:
            res = cancel_rotation(tail, wipe_completed_this_week=True)
        except Exception as e:
            return _err(str(e))
        ferry = res if isinstance(res, dict) else None
        out = {"cleared": True, "tail_number": tail, "schedule": _tail_schedule_view(tail)}
        if ferry:
            out["ferry"] = ferry
            out["message"] = (
                f"Schedule cleared. {tail} positions {ferry['from']}→{ferry['to']} "
                f"at {ferry['dep_label']} with no passengers."
            )
        else:
            out["message"] = f"Schedule cleared. {tail} is already at your hub."
        return _ok(out)
    try:
        rids = _parse_route_plan(plan)
    except Exception as e:
        return _err(str(e))

    # Per-leg overrides from the schedule box: [{route_id, flight_number, turn_minutes}]
    legs_in = body.get("legs") if isinstance(body.get("legs"), list) else []
    fn_override, turn_override = [], []
    for i in range(len(rids)):
        leg = legs_in[i] if i < len(legs_in) and isinstance(legs_in[i], dict) else {}
        fn_override.append(str(leg.get("flight_number") or "").strip().upper())
        turn_override.append(leg.get("turn_minutes"))
    if not any(fn_override):
        fn_override = []
    if all(t in (None, "") for t in turn_override):
        turn_override = None
    else:
        turn_override = [t if t not in (None, "") else None for t in turn_override]

    try:
        if mode == "detailed":
            days_of_week = _days_of_week_from_body(body)
            dep = _departure_time_from_body(body)
            al = setup.get_airline() or {}
            cs = str(al.get("callsign") or "FL")
            # Blank → engine allocates sticky/random; non-blank → override (overlap-checked).
            fns = [
                (fn_override[i] if fn_override and fn_override[i] else "")
                for i in range(len(rids))
            ]
            if len(rids) >= 2:
                rot = create_chained_detailed_rotation(
                    tail, rids, fns, days_of_week, dep, turn_minutes=turn_override
                )
                n = int(rot.get("segments_planned") or 0)
            else:
                from engine.gates import current_game_week as _gw

                sched = create_flight_schedule(tail, rids[0], fns[0], days_of_week, dep)
                merge_detailed_weekly_template(tail, [sched["template_item"]], _gw())
                n = 1
                rot = sched
        else:
            rot = assign_rotation(
                tail, rids, flight_numbers=(fn_override or None), turn_minutes=turn_override
            )
            n = len(rot.get("segments") or [])
    except Exception as e:
        return _err(str(e))
    util = {}
    try:
        util = get_tail_weekly_utilization(tail)
    except Exception:
        pass
    return _ok(
        {
            "tail_number": tail,
            "route_ids": rids,
            "chain": format_rotation_airport_chain(rids),
            "flights_created": n,
            "utilization": util,
            "schedule": _tail_schedule_view(tail),
        }
    )


def tail_schedule(tail: str) -> dict:
    from engine.scheduling import (
        _day_of_week_label,
        hhmm_from_absolute_game_hour,
        tail_position_and_free_hour,
    )

    t = str(tail or "").strip().upper()
    if not t:
        return _err("tail is required.")
    ac = aircraft.get_fleet_aircraft(t)
    if not ac:
        return _err(f"Aircraft '{t}' not found in fleet")
    pos, free_hour = tail_position_and_free_hour(t)
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    atype = aircraft.get_aircraft_type(str(ac.get("type_id") or ""))
    return _ok(
        {
            "schedule": _tail_schedule_view(t),
            "aircraft": {
                "tail_number": t,
                "type_id": ac.get("type_id"),
                "ownership": ac.get("ownership"),
                "status": ac.get("status"),
                "current_airport_iata": pos or ac.get("current_airport_iata"),
                "lease_weeks_remaining": ac.get("lease_weeks_remaining"),
                "lease_weekly_cost": ac.get("lease_weekly_cost"),
                "range_nm": float(atype["range_nm"]) if atype else None,
                "earliest_dep_day": _day_of_week_label(gw, free_hour),
                "earliest_dep": hhmm_from_absolute_game_hour(free_hour),
                "game_week": gw,
            },
        }
    )


def reposition_aircraft(body: dict) -> dict:
    from engine.scheduling import schedule_ferry_reposition

    tail = str(body.get("tail_number") or "").strip().upper()
    dest = str(body.get("dest_iata") or body.get("destination") or "").strip().upper()
    day = str(body.get("day") or body.get("day_of_week") or "").strip().upper()
    dep = str(body.get("departure_time") or body.get("dep_time") or "").strip()
    if not tail:
        return _err("tail_number is required.")
    if not dest:
        return _err("dest_iata is required.")
    if not day:
        return _err("day is required (MON..SUN).")
    if not dep:
        return _err("departure_time is required (HH:MM).")
    try:
        ferry = schedule_ferry_reposition(tail, dest, day, dep)
    except Exception as e:
        return _err(str(e))
    try:
        from engine.news_feed import push_news

        push_news(
            f"↩ {tail} reposition {ferry['from']}→{ferry['to']} "
            f"{ferry['day']} {ferry['dep_label']} (no passengers)"
        )
    except Exception:
        pass
    return _ok(
        {
            "ferry": ferry,
            "schedule": _tail_schedule_view(tail),
            "message": (
                f"{tail} reposition {ferry['from']}→{ferry['to']} "
                f"departs {ferry['day']} {ferry['dep_label']} · arrives {ferry['arr_label']}"
            ),
        }
    )


def gate_auctions() -> dict:
    rows = list_open_gate_auctions()
    out = []
    for a in rows:
        d = _row(a)
        out.append(
            {
                "auction_id": d.get("auction_id"),
                "airport_iata": d.get("airport_iata"),
                "opens_week": d.get("opens_week"),
                "closes_week": d.get("closes_week"),
                "units_available": d.get("units_available"),
                "current_price_per_unit": d.get("current_price_per_unit"),
            }
        )
    return _ok({"week": current_game_week(), "auctions": out})


def place_gate_bid(body: dict) -> dict:
    iata = str(body.get("airport_iata") or "").strip().upper()
    aid = str(body.get("auction_id") or "").strip()
    try:
        units = int(body.get("units") or 0)
        price = float(body.get("price_per_unit") or 0)
    except (TypeError, ValueError):
        return _err("units and price_per_unit must be numbers.")
    if units <= 0 or price <= 0:
        return _err("units and price must be positive.")
    if not aid:
        auctions = list_open_gate_auctions()
        match = next((a for a in auctions if str(a.get("airport_iata") or "").upper() == iata), None)
        if not match:
            return _err(f"No open auction for {iata or '(missing airport)'}.")
        aid = str(match["auction_id"])
    try:
        submit_gate_bid(aid, units, price, bidder_id="PLAYER")
    except Exception as e:
        return _err(str(e))
    return _ok({"auction_id": aid, "units": units, "price_per_unit": price})


def player_gate_bids() -> dict:
    from engine.gates import resolve_overdue_gate_auctions

    try:
        resolve_overdue_gate_auctions()
    except Exception:
        pass
    rows = db.fetch_all(
        """
        SELECT a.auction_id, a.airport_iata, a.opens_week, a.closes_week, a.status,
               a.units_available, b.units_requested, b.price_per_unit, b.submitted_week
        FROM airport_gate_bids b
        JOIN airport_gate_auctions a ON a.auction_id = b.auction_id
        WHERE b.bidder_id = 'PLAYER'
        ORDER BY a.closes_week DESC, a.airport_iata
        """
    )
    open_bids = []
    recent = []
    for r in rows or []:
        d = _row(r)
        st = str(d.get("status") or "").upper()
        if st == "OPEN":
            open_bids.append(d)
        else:
            recent.append(d)
    return _ok({"bids": open_bids, "recent": recent[:20], "all_bids": _rows(rows)})


def player_gates() -> dict:
    from engine.gates import _mtt_hours, player_gate_gap_starts_by_unit

    gw = current_game_week()
    try:
        mtt = float(_mtt_hours())
    except Exception:
        mtt = 0.5
    rows = db.fetch_all(
        """
        SELECT airport_iata, gate_units, effective_week, status
        FROM airport_gate_allocations
        WHERE holder_id = 'PLAYER' AND status = 'ACTIVE'
        ORDER BY airport_iata
        """
    )
    out = []
    for r in rows or []:
        d = _row(r)
        ap = str(d.get("airport_iata") or "").upper()
        u = int(d.get("gate_units") or 0)
        cnt = db.fetch_one(
            """
            SELECT
              COALESCE(SUM(CASE WHEN COALESCE(fs.origin_iata, ro.origin_iata) = ? THEN 1 ELSE 0 END), 0) +
              COALESCE(SUM(CASE WHEN COALESCE(fs.dest_iata,   ro.dest_iata)   = ? THEN 1 ELSE 0 END), 0) AS n
            FROM flight_segments fs
            JOIN routes ro ON ro.route_id = fs.route_id
            WHERE fs.game_week = ?
              AND fs.status != 'CANCELLED'
            """,
            (ap, ap, int(gw)),
        )
        touches = int(cnt["n"] or 0) if cnt else 0
        busy = float(touches) * float(mtt)
        util = (busy / (float(max(1, u)) * 168.0) * 100.0) if u > 0 else 0.0
        gaps = []
        try:
            raw = player_gate_gap_starts_by_unit(ap, int(gw), gate_units=u, max_gaps_per_gate=80)
            for i, gs in enumerate(raw or [], 1):
                if not gs:
                    continue
                by_day: dict[int, tuple[float, float]] = {}
                for a, b in gs:
                    di = int((float(a) % 168.0) // 24.0)
                    cur = by_day.get(di)
                    if cur is None or float(a) < float(cur[0]):
                        by_day[di] = (float(a), float(b))
                windows = [
                    f"{_abs_to_label(a)}→{_abs_to_label(b)}"
                    for di in range(7)
                    if di in by_day
                    for a, b in [by_day[di]]
                ]
                gaps.append({"gate": i, "windows": windows})
        except Exception:
            gaps = []
        d["touches"] = touches
        d["busy_hours"] = round(busy, 1)
        d["util_pct"] = round(util, 0)
        d["gaps"] = gaps
        out.append(d)
    return _ok({"week": gw, "gates": out})


def slot_status() -> dict:
    from engine.slots import (
        clock_hour_label,
        ensure_weekly_slot_auctions,
        grandfather_historic_slot_holdings,
        list_open_slot_auctions,
        list_slot_airport_rows,
        seed_slot_controlled_airports,
        slot_min_price_per_unit,
    )

    gw = current_game_week()
    seed_slot_controlled_airports()
    grandfather_historic_slot_holdings(gw)
    ensure_weekly_slot_auctions(gw)
    rows = []
    for r in list_slot_airport_rows(gw, holder_id="PLAYER"):
        r = dict(r)
        r["peak_label"] = clock_hour_label(int(r.get("peak_hour") or -1))
        rows.append(r)
    auctions = []
    for a in list_open_slot_auctions():
        d = dict(a)
        auctions.append(
            {
                "auction_id": d.get("auction_id"),
                "airport_iata": d.get("airport_iata"),
                "opens_week": d.get("opens_week"),
                "closes_week": d.get("closes_week"),
                "units_available": d.get("units_available"),
                "current_price_per_unit": d.get("current_price_per_unit"),
            }
        )
    return _ok(
        {
            "week": gw,
            "airports": rows,
            "auctions": auctions,
            "min_price_per_unit": slot_min_price_per_unit(),
        }
    )


def place_slot_bid(body: dict) -> dict:
    from engine.slots import list_open_slot_auctions, submit_slot_bid

    iata = str(body.get("airport_iata") or "").strip().upper()
    aid = str(body.get("auction_id") or "").strip()
    try:
        units = int(body.get("units") or 0)
        price = float(body.get("price_per_unit") or 0)
    except (TypeError, ValueError):
        return _err("units and price_per_unit must be numbers.")
    if units <= 0 or price <= 0:
        return _err("units and price must be positive.")
    if not aid:
        auctions = list_open_slot_auctions()
        match = next((a for a in auctions if str(a.get("airport_iata") or "").upper() == iata), None)
        if not match:
            return _err(f"No open slot auction for {iata or '(missing airport)'}.")
        aid = str(match["auction_id"])
    try:
        submit_slot_bid(aid, units, price, bidder_id="PLAYER")
    except Exception as e:
        return _err(str(e))
    return _ok({"auction_id": aid, "units": units, "price_per_unit": price})


def player_slot_bids() -> dict:
    rows = db.fetch_all(
        """
        SELECT a.auction_id, a.airport_iata, a.opens_week, a.closes_week, a.status,
               b.units_requested, b.price_per_unit
        FROM slot_bids b
        JOIN slot_auctions a ON a.auction_id = b.auction_id
        WHERE b.bidder_id = 'PLAYER'
        ORDER BY a.closes_week DESC, a.airport_iata
        """
    )
    return _ok({"bids": _rows(rows)})


def flight_board() -> dict:
    gw = current_game_week()
    player = db.fetch_all(
        """
        SELECT fs.flight_number, fs.tail_number, fs.status,
               fs.scheduled_dep_game_hour, fs.scheduled_arr_game_hour,
               COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
               COALESCE(fs.dest_iata, r.dest_iata) AS dest_iata
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ? AND fs.status != 'CANCELLED'
        ORDER BY fs.scheduled_dep_game_hour
        LIMIT 80
        """,
        (gw,),
    )
    ai = db.fetch_all(
        """
        SELECT flight_number, competitor_id, status, origin_iata, dest_iata,
               scheduled_dep_game_hour, scheduled_arr_game_hour
        FROM ai_flight_segments
        WHERE game_week = ? AND status != 'CANCELLED'
        ORDER BY scheduled_dep_game_hour
        LIMIT 80
        """,
        (gw,),
    )
    return _ok({"week": gw, "player": _rows(player), "ai": _rows(ai)})


def airport_flight_board(iata: str) -> dict:
    from engine.scheduling import hhmm_from_absolute_game_hour
    from ui.airport_board import get_airport_board_rows

    ap = str(iata or "").strip().upper()
    if not ap:
        return _err("iata is required.")
    meta = airports.get_airport(ap)
    if not meta:
        return _err(f"Airport '{ap}' not found.")
    gw = current_game_week()
    al = setup.get_airline() or {}
    player_label = str(al.get("callsign") or al.get("name") or "PLAYER")
    comp_labels: dict[str, str] = {}
    for c in db.fetch_all("SELECT competitor_id, callsign, name FROM competitors") or []:
        cid = str(c["competitor_id"] or "")
        comp_labels[cid] = str(c["callsign"] or c["name"] or cid)

    dows = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    enriched = []
    for r in get_airport_board_rows(ap, gw):
        d = _row(r)
        gh = float(d.get("game_hour") or 0.0)
        h_in_week = gh % 168.0
        dow_idx = max(0, min(6, int(h_in_week // 24.0)))
        d["time_label"] = f"{dows[dow_idx]} {hhmm_from_absolute_game_hour(gh)}"
        d["calendar_week"] = int(gh // 168.0) + 1
        op = str(d.get("operator_id") or "")
        d["operator_label"] = player_label if op == "PLAYER" else comp_labels.get(op, op)
        d["route"] = f"{d.get('origin_iata') or '?'}→{d.get('dest_iata') or '?'}"
        pax = d.get("pax")
        if pax is not None and op != "PLAYER":
            d["pax_label"] = f"~{int(pax)}"
        else:
            d["pax_label"] = "—"
        enriched.append(d)

    deps = [x for x in enriched if str(x.get("direction") or "").upper() == "DEP"]
    arrs = [x for x in enriched if str(x.get("direction") or "").upper() == "ARR"]
    return _ok(
        {
            "airport_iata": ap,
            "airport_name": meta.get("name"),
            "airport_city": meta.get("city"),
            "week": gw,
            "flights": enriched,
            "departures": deps,
            "arrivals": arrs,
        }
    )


def competitors_overview() -> dict:
    from engine.ai import ensure_competitors_seeded

    ensure_competitors_seeded()
    comps = db.fetch_all(
        """
        SELECT c.competitor_id, c.name, c.callsign, c.home_hub_iata, c.reputation,
               c.strategy, c.fleet_size, c.cash, c.brand_power,
               COUNT(cr.route_pair_id) AS routes_count,
               COALESCE(SUM(
                   CASE WHEN cr.status IN ('ACTIVE', 'SUSPENDED')
                        THEN COALESCE(cr.actual_weekly_revenue_avg, 0) ELSE 0 END
               ), 0) AS weekly_revenue,
               COALESCE(SUM(cr.estimated_weekly_profit), 0) AS route_profit_est,
               COALESCE(SUM(cr.actual_weekly_revenue_avg), 0) AS route_revenue_avg
        FROM competitors c
        LEFT JOIN competitor_routes cr ON cr.competitor_id = c.competitor_id
        GROUP BY c.competitor_id
        ORDER BY c.competitor_id
        """
    )
    out = []
    for c in comps:
        d = _row(c)
        out.append(
            {
                "competitor_id": str(d.get("competitor_id") or ""),
                "name": d.get("name"),
                "callsign": d.get("callsign"),
                "home_hub_iata": d.get("home_hub_iata"),
                "reputation": float(d.get("reputation") or 0),
                "strategy": d.get("strategy"),
                "fleet_size": int(d.get("fleet_size") or 0),
                "cash": float(d.get("cash") or 0),
                "routes_count": int(d.get("routes_count") or 0),
                "weekly_revenue": float(d.get("weekly_revenue") or 0),
                "route_profit_est": float(d.get("route_profit_est") or 0),
                "route_revenue_avg": float(d.get("route_revenue_avg") or 0),
            }
        )
    return _ok({"competitors": out})


def competitor_routes(competitor_id: str = "") -> dict:
    from engine.ai import ensure_competitors_seeded

    ensure_competitors_seeded()
    cid = str(competitor_id or "").strip().upper()
    if cid:
        rows = db.fetch_all(
            """
            SELECT cr.competitor_id, c.name, cr.route_pair_id, cr.outbound_route_id, cr.inbound_route_id,
                   cr.fare_business, cr.fare_leisure, cr.frequency_per_week, cr.aircraft_type_id,
                   cr.status, cr.estimated_weekly_profit, cr.actual_weekly_revenue_avg,
                   cr.contested, cr.market_share, cr.opened_week
            FROM competitor_routes cr
            JOIN competitors c ON c.competitor_id = cr.competitor_id
            WHERE cr.competitor_id = ?
            ORDER BY cr.route_pair_id
            """,
            (cid,),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT cr.competitor_id, c.name, cr.route_pair_id, cr.outbound_route_id, cr.inbound_route_id,
                   cr.fare_business, cr.fare_leisure, cr.frequency_per_week, cr.aircraft_type_id,
                   cr.status, cr.estimated_weekly_profit, cr.actual_weekly_revenue_avg,
                   cr.contested, cr.market_share, cr.opened_week
            FROM competitor_routes cr
            JOIN competitors c ON c.competitor_id = cr.competitor_id
            ORDER BY cr.competitor_id, cr.route_pair_id
            LIMIT 400
            """
        )
    slim = []
    for r in rows:
        d = _row(r)
        slim.append(
            {
                "competitor_id": d.get("competitor_id"),
                "name": d.get("name"),
                "route_pair_id": d.get("route_pair_id"),
                "outbound_route_id": d.get("outbound_route_id"),
                "inbound_route_id": d.get("inbound_route_id"),
                "fare_business": float(d.get("fare_business") or 0),
                "fare_leisure": float(d.get("fare_leisure") or 0),
                "frequency_per_week": int(d.get("frequency_per_week") or 0),
                "aircraft_type_id": d.get("aircraft_type_id"),
                "status": d.get("status") or "ACTIVE",
                "estimated_weekly_profit": float(d.get("estimated_weekly_profit") or 0),
                "actual_weekly_revenue_avg": float(d.get("actual_weekly_revenue_avg") or 0),
                "contested": int(d.get("contested") or 0),
                "market_share": float(d.get("market_share") or 0),
                "opened_week": int(d.get("opened_week") or 0),
            }
        )
    return _ok({"competitor_id": cid or None, "routes": slim})


def contested_markets() -> dict:
    from engine.ai import ensure_competitors_seeded
    from engine.demand import compute_route_contested_intel
    from engine.scheduling import calendar_game_week_from_state

    ensure_competitors_seeded()
    gw = calendar_game_week_from_state()
    gs = db.fetch_one("SELECT current_month FROM game_state WHERE id = 1")
    month = int(gs["current_month"] or 1) if gs else 1
    rows = db.fetch_all(
        """
        SELECT DISTINCT pr.route_id
        FROM player_routes pr
        WHERE EXISTS (
            SELECT 1 FROM competitor_routes cr
            WHERE cr.outbound_route_id = pr.route_id
               OR cr.inbound_route_id = pr.route_id
               OR cr.route_pair_id = pr.route_id
        )
        ORDER BY pr.route_id
        LIMIT 24
        """
    )
    markets = []
    for r in rows:
        rid = str(r["route_id"])
        intel = compute_route_contested_intel(rid, game_week=gw, current_month=month)
        if not intel:
            continue
        markets.append(
            {
                "route_id": rid,
                "player_fare_business": float(intel.get("player_fare_business") or 0),
                "player_fare_leisure": float(intel.get("player_fare_leisure") or 0),
                "player_share_business": float(intel.get("player_share_business") or 0),
                "player_share_leisure": float(intel.get("player_share_leisure") or 0),
                "competitors": intel.get("competitors") or [],
            }
        )
    return _ok({"week": gw, "markets": markets})


def bank_status(amount: str = "") -> dict:
    from engine.banking import debt_and_headroom, get_loan_offers, list_loans

    if not setup.airline_exists():
        return _err("Create an airline first.")
    snap = debt_and_headroom()
    preview = None
    raw = str(amount or "").strip().replace(",", "")
    if raw:
        try:
            preview = float(raw)
        except ValueError:
            return _err("amount must be a number.")
    else:
        room = float(snap.get("headroom") or 0)
        preview = min(10_000_000.0, room) if room >= 100_000 else 100_000.0
    offers = []
    try:
        offers = get_loan_offers(float(preview))
    except Exception as e:
        snap["offer_error"] = str(e)
    return _ok({**snap, "preview_amount": preview, "offers": offers, "loans": list_loans()})


def bank_originate(body: dict) -> dict:
    from engine.banking import originate_loan

    try:
        amount = float(body.get("amount") or 0)
        weeks = int(body.get("weeks") or 0)
    except (TypeError, ValueError):
        return _err("amount and weeks must be numbers.")
    try:
        out = originate_loan(amount, weeks)
    except Exception as e:
        return _err(str(e))
    return _ok(out)


def bank_payoff(body: dict) -> dict:
    from engine.banking import payoff_loan

    lid = str(body.get("loan_id") or "").strip()
    if not lid:
        return _err("loan_id is required.")
    try:
        out = payoff_loan(lid)
    except Exception as e:
        return _err(str(e))
    return _ok(out)


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


def books_status() -> dict:
    """Week P&L (live or settled) + fuel desk snapshot for the Books overlay."""
    if not setup.airline_exists():
        return _err("Create an airline first.")
    from engine.settlement import (
        build_week_summary_payload,
        calendar_week_from_state,
        catch_up_missing_settlements,
        last_settled_week,
        missing_settlement_weeks,
    )
    from engine.reputation import preview_reputation
    from engine import fuel as fin
    from ui.fuel_ticker import fuel_sparkline

    catchup = None
    backlog_before = missing_settlement_weeks()
    if backlog_before:
        try:
            catchup = catch_up_missing_settlements()
        except Exception as e:
            catchup = {"errors": [{"error": str(e)}], "settled": []}

    week = _json_safe(build_week_summary_payload())
    al = setup.get_airline() or {}
    gs = db.fetch_one(
        """
        SELECT fuel_price_current, fuel_shock_pending, fuel_shock_message
        FROM game_state WHERE id = 1
        """
    )
    barrel = float(gs["fuel_price_current"] or 195.0) if gs else 195.0
    spot_gal = float(fin.all_in_spot_from_state())
    burn = float(fin.estimated_weekly_burn_gallons())
    prem_rate = float(db.get_financial_constant("fuel_hedge_premium_rate") or 0.05)
    hp = al.get("fuel_hedged_price")
    hw = al.get("fuel_hedged_weeks_remaining")
    dip = al.get("fuel_dip_alert_price")
    avg = al.get("fuel_reserve_avg_price")
    fuel = {
        "spot_bbl": barrel,
        "spot_gal": spot_gal,
        "sparkline": fuel_sparkline(20),
        "est_weekly_burn_gal": burn,
        "reserve_gal": float(al.get("fuel_reserve_gallons") or 0),
        "reserve_avg_price": float(avg) if avg is not None else None,
        "hedged_bbl": float(hp) if hp is not None else None,
        "hedge_weeks_remaining": int(hw) if hw is not None else None,
        "dip_alert_bbl": float(dip) if dip is not None else None,
        "shock_pending": bool(gs and int(gs["fuel_shock_pending"] or 0)),
        "shock_message": (gs["fuel_shock_message"] if gs else None) or None,
        "premium_2wk": burn * spot_gal * 2 * prem_rate,
        "premium_4wk": burn * spot_gal * 4 * prem_rate,
        "premium_8wk": burn * spot_gal * 8 * prem_rate,
    }
    cal = calendar_week_from_state()
    last = last_settled_week()
    still = missing_settlement_weeks(cal)
    settlement = {
        "calendar_week": cal,
        "last_settled_week": last,
        "missing_weeks": still,
        "catchup_settled": (catchup or {}).get("settled") or [],
        "catchup_errors": (catchup or {}).get("errors") or [],
    }
    reputation = preview_reputation(cal)
    return _ok({
        "week": week,
        "fuel": fuel,
        "settlement": settlement,
        "reputation": reputation,
    })


def fuel_action(body: dict) -> dict:
    """Books fuel desk: hedge / cancel / reserve / dip / ack shock."""
    from engine import fuel as fin

    if not setup.airline_exists():
        return _err("Create an airline first.")
    action = str(body.get("action") or "").strip().lower()
    try:
        if action in ("hedge", "hedge_fuel"):
            weeks = int(body.get("weeks") or 0)
            out = fin.hedge_fuel(weeks)
            return _ok({"action": "hedge", **out})
        if action in ("cancel_hedge", "cancel"):
            fin.cancel_hedge()
            return _ok({"action": "cancel_hedge"})
        if action in ("buy_reserve", "reserve"):
            gallons = float(body.get("gallons") or 0)
            out = fin.buy_reserve(gallons)
            return _ok({"action": "buy_reserve", **out})
        if action in ("set_dip", "dip"):
            price = float(body.get("price") or 0)
            fin.set_dip_alert(price)
            return _ok({"action": "set_dip", "price": price})
        if action in ("clear_dip",):
            fin.set_dip_alert(None)
            return _ok({"action": "clear_dip"})
        if action in ("ack_shock", "ack"):
            fin.acknowledge_fuel_shock()
            return _ok({"action": "ack_shock"})
    except Exception as e:
        return _err(str(e))
    return _err("Unknown fuel action. Use hedge, cancel_hedge, buy_reserve, set_dip, clear_dip, ack_shock.")


def pop_week_summaries(limit: int = 4) -> dict:
    """Drain settlement week-summary queue for overlay toasts (non-blocking)."""
    from engine.settlement import get_week_summary_queue

    q = get_week_summary_queue()
    items: list[dict] = []
    n = max(1, min(8, int(limit or 4)))
    while len(items) < n:
        try:
            item = q.get_nowait()
        except Exception:
            break
        if not isinstance(item, dict):
            continue
        if item.get("skipped"):
            continue
        items.append(
            {
                "game_week": int(item.get("game_week") or 0),
                "net_income": float(item.get("net_income") or 0),
                "revenue_gross": float(item.get("revenue_gross") or 0),
                "summary_source": item.get("summary_source"),
            }
        )
    return _ok({"summaries": items})


def set_clock(body: dict) -> dict:
    from engine.clock import get_global_clock

    clk = get_global_clock()
    if clk is None:
        return _err("Create an airline before starting the clock.")
    speed = body.get("speed")
    try:
        speed_i = int(speed)
    except (TypeError, ValueError):
        return _err("speed must be 0, 1, 2, 4, or 20.")
    ok, msg = clk.set_speed(speed_i, player_initiated=True)
    if not ok:
        return _err(msg)
    return _ok({"message": msg, "clock": clk.get_status()})


def flight_map() -> dict:
    from engine.flight_map_data import get_flight_map_payload

    try:
        return get_flight_map_payload()
    except Exception as e:
        return _err(str(e))
