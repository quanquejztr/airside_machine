"""
Phase 6 — KPI & route analytics (in-game week semantics).

Uses scheduled_dep_game_hour windows aligned with the game clock (see scheduling.route_weekly_passenger_accounting).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from db import db
from engine.cabin import fare_for_class, get_cabin_config
from engine.demand import compute_demand, compute_revenue
from engine.routes import get_route
from engine.scheduling import (
    _per_leg_demand_from_weekly_pool,
    get_tail_weekly_utilization,
    max_weekly_airborne_hours_cap,
    route_weekly_passenger_accounting,
    week_base_hours,
)


def _clock_context() -> Optional[Dict[str, Any]]:
    gs = db.fetch_one(
        "SELECT game_hours_elapsed, game_week, current_month FROM game_state WHERE id = 1"
    )
    if not gs:
        return None
    ghe = float(gs["game_hours_elapsed"] if gs["game_hours_elapsed"] is not None else 0.0)
    week = int(ghe // 168.0) + 1
    w0 = week_base_hours(week)
    return {
        "game_hours_elapsed": ghe,
        "game_week": week,
        "current_month": int(gs["current_month"]),
        "week_start_h": w0,
        "week_end_h": w0 + 168.0,
    }


def _window_for_game_week(game_week: int) -> Tuple[float, float]:
    w0 = week_base_hours(int(game_week))
    return w0, w0 + 168.0


def trend_arrow(cur: Optional[float], prev: Optional[float], eps: float = 0.02) -> str:
    if cur is None or prev is None:
        return "—"
    if prev == 0:
        return "↑" if cur > 0 else "→"
    ratio = cur / prev
    if ratio > 1.0 + eps:
        return "↑"
    if ratio < 1.0 - eps:
        return "↓"
    return "→"


def _infer_cabin_pax(route_id: str, seg: Any) -> Dict[str, int]:
    pe = int(round(float(seg["revenue_economy"] or 0) / fare_for_class("economy", route_id))) if float(
        seg["revenue_economy"] or 0
    ) > 0 else 0
    pp = int(round(float(seg["revenue_premium_economy"] or 0) / fare_for_class("premium_economy", route_id))) if float(
        seg["revenue_premium_economy"] or 0
    ) > 0 else 0
    pb = int(round(float(seg["revenue_business_cabin"] or 0) / fare_for_class("business", route_id))) if float(
        seg["revenue_business_cabin"] or 0
    ) > 0 else 0
    pf = int(round(float(seg["revenue_first"] or 0) / fare_for_class("first", route_id))) if float(
        seg["revenue_first"] or 0
    ) > 0 else 0
    return {"economy": pe, "premium_economy": pp, "business": pb, "first": pf}


def _cabin_config_for_tail(tail: str) -> Dict[str, int]:
    cfg = get_cabin_config(tail)
    if cfg:
        return {
            "seats_economy": int(cfg["seats_economy"]),
            "seats_premium_economy": int(cfg["seats_premium_economy"]),
            "seats_business": int(cfg["seats_business"]),
            "seats_first": int(cfg["seats_first"]),
            "eec_used": int(cfg["eec_used"]),
        }
    return {
        "seats_economy": 138,
        "seats_premium_economy": 0,
        "seats_business": 12,
        "seats_first": 0,
        "eec_used": 0,
    }


def _segments_in_route_window(route_id: str, week_start_h: float, week_end_h: float) -> List[Any]:
    return db.fetch_all(
        """
        SELECT * FROM flight_segments
        WHERE route_id = ?
          AND scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status != 'CANCELLED'
        ORDER BY scheduled_dep_game_hour ASC, segment_id ASC
        """,
        (route_id, week_start_h, week_end_h),
    )


def _aggregate_route_metrics(
    route_id: str,
    game_week: int,
    current_month: int,
    week_start_h: float,
    week_end_h: float,
) -> Dict[str, Any]:
    route = get_route(route_id)
    if not route:
        return {"error": f"Route '{route_id}' not found."}

    distance_nm = float(route["distance_nm"])
    segs = _segments_in_route_window(route_id, week_start_h, week_end_h)

    flown = [s for s in segs if s["status"] in ("IN_AIR", "LANDED")]
    scheduled_all = len(segs)

    seats_offer_flown = 0
    asm_flown = 0.0
    rpm_sum = 0.0
    revenue = 0.0
    cost_var = 0.0

    pax_cabin = {"economy": 0, "premium_economy": 0, "business": 0, "first": 0}
    seats_cabin = {"economy": 0, "premium_economy": 0, "business": 0, "first": 0}

    spill_leisure = 0
    spill_business = 0
    on_time = 0
    on_sample = 0

    demand_cache: Dict[int, Any] = {}

    for s in flown:
        tail = str(s["tail_number"])
        cabin = _cabin_config_for_tail(tail)
        total_seats = (
            cabin["seats_economy"]
            + cabin["seats_premium_economy"]
            + cabin["seats_business"]
            + cabin["seats_first"]
        )
        seats_offer_flown += total_seats
        asm_flown += distance_nm * total_seats

        cp = _infer_cabin_pax(route_id, s)
        for k in pax_cabin:
            pax_cabin[k] += cp[k]
            # seats per class from config (same for every flight on this tail)
        seats_cabin["economy"] += cabin["seats_economy"]
        seats_cabin["premium_economy"] += cabin["seats_premium_economy"]
        seats_cabin["business"] += cabin["seats_business"]
        seats_cabin["first"] += cabin["seats_first"]

        rev_g = float(s["revenue_gross"] or 0)
        revenue += rev_g

        cost_var += (
            float(s["excise_tax"] or 0)
            + float(s["segment_fee"] or 0)
            + float(s["security_fee"] or 0)
            + float(s["pfc_fee"] or 0)
            + float(s["landing_fee"] or 0)
            + float(s["gate_fee"] or 0)
            + float(s["fuel_cost"] or 0)
        )

        total_pax_leg = cp["economy"] + cp["premium_economy"] + cp["business"] + cp["first"]
        rpm_sum += total_pax_leg * distance_nm

        if s["status"] == "LANDED":
            aa = s["actual_arr_game_hour"]
            sa = s["scheduled_arr_game_hour"]
            if aa is not None and sa is not None:
                on_sample += 1
                if float(aa) <= float(sa) + 1e-6:
                    on_time += 1

        gws = int(s["game_week"])
        if gws not in demand_cache:
            demand_cache[gws] = compute_demand(route_id, gws, current_month)
        dseg = demand_cache[gws]
        leg_l, leg_b = _per_leg_demand_from_weekly_pool(
            route_id,
            gws,
            str(s["segment_id"]),
            int(dseg["leisure_pax"]),
            int(dseg["business_pax"]),
        )
        cr = compute_revenue(route_id, cabin, leg_l, leg_b)
        spill_leisure += int(cr.get("leisure_spilled") or 0)
        spill_business += int(cr.get("business_spilled") or 0)

    total_pax = sum(pax_cabin.values())
    lf_total = (total_pax / seats_offer_flown) if seats_offer_flown > 0 else 0.0
    lf_class = {}
    for k in seats_cabin:
        lf_class[k] = (pax_cabin[k] / seats_cabin[k]) if seats_cabin[k] > 0 else 0.0

    rasm = (revenue / asm_flown) if asm_flown > 0 else 0.0
    casm = (cost_var / asm_flown) if asm_flown > 0 else 0.0
    yield_rpk = (revenue / rpm_sum) if rpm_sum > 0 else 0.0
    eec_units = 0.0
    for s in flown:
        eu = float(_cabin_config_for_tail(str(s["tail_number"]))["eec_used"] or 0)
        if eu > 0:
            eec_units += eu
    revenue_per_eec = (revenue / eec_units) if eec_units > 0 else 0.0

    otp = (on_time / on_sample) if on_sample > 0 else None

    return {
        "route_id": route_id,
        "game_week": game_week,
        "distance_nm": distance_nm,
        "segments_scheduled": scheduled_all,
        "segments_flown": len(flown),
        "load_factor_total": lf_total,
        "load_factor_class": lf_class,
        "pax_class": pax_cabin,
        "seats_class": seats_cabin,
        "revenue_gross": revenue,
        "rasm": rasm,
        "casm": casm,
        "yield_per_rpm": yield_rpk,
        "revenue_per_eec": revenue_per_eec,
        "spill_leisure_market": spill_leisure,
        "spill_business_market": spill_business,
        "on_time_rate": otp,
        "asm_flown": asm_flown,
        "rpm_flown": rpm_sum,
    }


def route_card(route_id: str, game_week: Optional[int] = None) -> Dict[str, Any]:
    """
    Route analytics for the current in-game week (or specified week): load factors, RASM/CASM,
    revenue/EEC, spill, and week-over-week trend arrows vs the prior game week.
    """
    ctx = _clock_context()
    if not ctx:
        return {"error": "No game state."}

    gw = int(game_week) if game_week is not None else int(ctx["game_week"])
    month = int(ctx["current_month"])
    w0, w1 = _window_for_game_week(gw)

    rid = route_id.upper().strip()
    cur = _aggregate_route_metrics(rid, gw, month, w0, w1)
    if cur.get("error"):
        return cur

    market = route_weekly_passenger_accounting(rid, gw)

    pw = gw - 1
    prev_metrics = None
    if pw >= 1:
        p0, p1 = _window_for_game_week(pw)
        prev_metrics = _aggregate_route_metrics(route_id.upper().strip(), pw, month, p0, p1)
        if prev_metrics.get("error"):
            prev_metrics = None

    def arr(metric: str, key: Optional[str] = None):
        if not prev_metrics:
            return "—"
        if key:
            a = cur.get(metric, {}).get(key)
            b = prev_metrics.get(metric, {}).get(key)
        else:
            a = cur.get(metric)
            b = prev_metrics.get(metric)
        return trend_arrow(
            float(a) if a is not None else None,
            float(b) if b is not None else None,
        )

    trends = {
        "load_factor_total": arr("load_factor_total"),
        "rasm": arr("rasm"),
        "casm": arr("casm"),
        "revenue_gross": arr("revenue_gross"),
        "revenue_per_eec": arr("revenue_per_eec"),
        "yield_per_rpm": arr("yield_per_rpm"),
    }
    trends_class = {}
    for cls in cur["load_factor_class"]:
        if prev_metrics and prev_metrics.get("load_factor_class"):
            trends_class[cls] = trend_arrow(
                cur["load_factor_class"].get(cls),
                prev_metrics["load_factor_class"].get(cls),
            )
        else:
            trends_class[cls] = "—"

    # Cabin reconfiguration hint
    lf = cur["load_factor_class"]
    hi = max(lf.values()) if lf else 0.0
    lo = min((v for k, v in lf.items() if cur["seats_class"].get(k, 0) > 0), default=0.0)
    hint = None
    if hi - lo >= 0.35 and hi >= 0.75:
        tight = [k for k, v in lf.items() if v == hi and cur["seats_class"].get(k, 0) > 0]
        loose = [k for k, v in lf.items() if v == lo and cur["seats_class"].get(k, 0) > 0]
        if tight and loose:
            hint = (
                f"Premium cabin [{tight[0]}] is much fuller than [{loose[0]}] — consider shifting seats "
                f"toward {tight[0].replace('_', ' ')} if demand stays skewed."
            )

    out = {
        **cur,
        "trends": trends,
        "trends_class_lf": trends_class,
        "prior_week": pw if pw >= 1 else None,
        "reconfig_hint": hint,
    }
    if market:
        out["remaining_market_business"] = market.get("remaining_business")
        out["remaining_market_leisure"] = market.get("remaining_leisure")
    return out


def fleet_utilization(game_week: Optional[int] = None) -> Dict[str, Any]:
    """Per-tail airborne hours vs weekly cap and cabin fill (flown legs this week)."""
    ctx = _clock_context()
    if not ctx:
        return {"error": "No game state."}
    gw = int(game_week) if game_week is not None else int(ctx["game_week"])
    w0, w1 = _window_for_game_week(gw)
    cap = max_weekly_airborne_hours_cap()

    tails = db.fetch_all("SELECT tail_number, type_id FROM fleet ORDER BY tail_number")
    rows = []
    for t in tails:
        tn = str(t["tail_number"])
        util = get_tail_weekly_utilization(tn, gw)
        cabin = _cabin_config_for_tail(tn)
        total_seats = (
            cabin["seats_economy"]
            + cabin["seats_premium_economy"]
            + cabin["seats_business"]
            + cabin["seats_first"]
        )

        segs = db.fetch_all(
            """
            SELECT * FROM flight_segments
            WHERE tail_number = ?
              AND scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
              AND status IN ('IN_AIR', 'LANDED')
            """,
            (tn, w0, w1),
        )
        seat_cap = 0
        pax_tot = 0
        for s in segs:
            seat_cap += total_seats
            rid = str(s["route_id"])
            cp = _infer_cabin_pax(rid, s)
            pax_tot += sum(cp.values())

        fill = (pax_tot / seat_cap) if seat_cap > 0 else 0.0
        rows.append(
            {
                "tail_number": tn,
                "type_id": str(t["type_id"]),
                "airborne_hours": util["airborne_hours"],
                "cap_hours": cap,
                "hours_utilization_pct": util["utilization_pct"],
                "cabin_fill_rate": fill,
                "flights_flown": len(segs),
                "eec_used": cabin["eec_used"],
            }
        )

    return {"game_week": gw, "tails": rows}


def _week_ops_from_segments(game_week: int) -> Dict[str, Any]:
    """Aggregate RPM/CASM/OTP from all LANDED segments in a game week (uses segment.game_week)."""
    segs = db.fetch_all(
        """
        SELECT fs.*, r.distance_nm AS dist
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.game_week = ? AND fs.status = 'LANDED'
        """,
        (game_week,),
    )
    revenue = 0.0
    asm = 0.0
    rpm = 0.0
    cost_v = 0.0
    on_time = 0
    n_otp = 0
    for s in segs:
        revenue += float(s["revenue_gross"] or 0)
        dist = float(s["dist"] or 0)
        tail = str(s["tail_number"])
        cabin = _cabin_config_for_tail(tail)
        ts = (
            cabin["seats_economy"]
            + cabin["seats_premium_economy"]
            + cabin["seats_business"]
            + cabin["seats_first"]
        )
        asm += dist * ts
        cp = _infer_cabin_pax(str(s["route_id"]), s)
        pax = sum(cp.values())
        rpm += pax * dist
        cost_v += (
            float(s["excise_tax"] or 0)
            + float(s["segment_fee"] or 0)
            + float(s["security_fee"] or 0)
            + float(s["pfc_fee"] or 0)
            + float(s["landing_fee"] or 0)
            + float(s["gate_fee"] or 0)
            + float(s["fuel_cost"] or 0)
        )
        aa, sa = s["actual_arr_game_hour"], s["scheduled_arr_game_hour"]
        if aa is not None and sa is not None:
            n_otp += 1
            if float(aa) <= float(sa) + 1e-6:
                on_time += 1

    return {
        "revenue_gross": revenue,
        "rasm": (revenue / asm) if asm > 0 else 0.0,
        "casm": (cost_v / asm) if asm > 0 else 0.0,
        "on_time_rate": (on_time / n_otp) if n_otp > 0 else None,
        "flights": len(segs),
    }


def kpi_summary() -> Dict[str, Any]:
    """
    Airline-wide RPM/RASM, CASM, on-time, net margin — last 4 completed ledger weeks plus trends.
    Current in-progress week uses live segment aggregates where the ledger row does not exist yet.
    """
    ctx = _clock_context()
    if not ctx:
        return {"error": "No game state."}

    cur_week = int(ctx["game_week"])
    ledger = db.fetch_all(
        """
        SELECT * FROM week_ledger
        WHERE game_week < ?
        ORDER BY game_week DESC
        LIMIT 4
        """,
        (cur_week,),
    )
    ledger = list(reversed(ledger))

    weeks_out: List[Dict[str, Any]] = []
    for row in ledger:
        gw = int(row["game_week"])
        rev = float(row["revenue_gross"] or 0)
        ni = float(row["net_income"] or 0)
        margin = (ni / rev) if rev > 0 else None
        ops = _week_ops_from_segments(gw)
        weeks_out.append(
            {
                "game_week": gw,
                "from_ledger": True,
                "revenue_gross": rev,
                "net_income": ni,
                "net_margin": margin,
                "rasm": ops["rasm"],
                "casm": ops["casm"],
                "on_time_rate": ops["on_time_rate"],
                "flights_completed": ops["flights"],
            }
        )

    # Live snapshot for current week (partial): segments in clock window, flown
    w0, w1 = _window_for_game_week(cur_week)
    live = db.fetch_all(
        """
        SELECT fs.*, r.distance_nm AS dist
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.scheduled_dep_game_hour >= ? AND fs.scheduled_dep_game_hour < ?
          AND fs.status IN ('IN_AIR', 'LANDED')
        """,
        (w0, w1),
    )
    revenue = 0.0
    asm = 0.0
    cost_v = 0.0
    on_time = 0
    n_otp = 0
    for s in live:
        revenue += float(s["revenue_gross"] or 0)
        dist = float(s["dist"] or 0)
        tail = str(s["tail_number"])
        cabin = _cabin_config_for_tail(tail)
        ts = (
            cabin["seats_economy"]
            + cabin["seats_premium_economy"]
            + cabin["seats_business"]
            + cabin["seats_first"]
        )
        asm += dist * ts
        cp = _infer_cabin_pax(str(s["route_id"]), s)
        cost_v += (
            float(s["excise_tax"] or 0)
            + float(s["segment_fee"] or 0)
            + float(s["security_fee"] or 0)
            + float(s["pfc_fee"] or 0)
            + float(s["landing_fee"] or 0)
            + float(s["gate_fee"] or 0)
            + float(s["fuel_cost"] or 0)
        )
        if s["status"] == "LANDED":
            aa, sa = s["actual_arr_game_hour"], s["scheduled_arr_game_hour"]
            if aa is not None and sa is not None:
                n_otp += 1
                if float(aa) <= float(sa) + 1e-6:
                    on_time += 1

    airline = db.fetch_one("SELECT cash FROM airline WHERE id = 1")
    cash = float(airline["cash"]) if airline else 0.0

    current_snap = {
        "game_week": cur_week,
        "from_ledger": False,
        "revenue_gross": revenue,
        "net_income": None,
        "net_margin": None,
        "rasm": (revenue / asm) if asm > 0 else 0.0,
        "casm": (cost_v / asm) if asm > 0 else 0.0,
        "on_time_rate": (on_time / n_otp) if n_otp > 0 else None,
        "flights_completed": len([s for s in live if s["status"] == "LANDED"]),
        "flights_in_progress": len([s for s in live if s["status"] == "IN_AIR"]),
        "cash": cash,
    }

    last = weeks_out[-1] if weeks_out else None
    trends = {
        "rasm": trend_arrow(current_snap["rasm"], last["rasm"] if last else None),
        "casm": trend_arrow(current_snap["casm"], last["casm"] if last else None),
        "on_time_rate": trend_arrow(
            current_snap["on_time_rate"], last["on_time_rate"] if last else None
        ),
    }
    if len(weeks_out) >= 2:
        trends["net_margin_closed_wow"] = trend_arrow(
            weeks_out[-1].get("net_margin"),
            weeks_out[-2].get("net_margin"),
        )
    else:
        trends["net_margin_closed_wow"] = "—"

    return {
        "current_week": cur_week,
        "ledger_weeks": weeks_out,
        "current_week_live": current_snap,
        "trends_vs_last_closed_week": trends,
    }


def compare_routes(route_a: str, route_b: str) -> Dict[str, Any]:
    return {
        "a": route_card(route_a),
        "b": route_card(route_b),
    }


def compare_fleet(tail_a: str, tail_b: str) -> Dict[str, Any]:
    fu = fleet_utilization()
    if fu.get("error"):
        return fu
    ta = tail_a.strip().upper()
    tb = tail_b.strip().upper()
    ma = next((x for x in fu["tails"] if x["tail_number"] == ta), None)
    mb = next((x for x in fu["tails"] if x["tail_number"] == tb), None)
    return {"tail_a": ma, "tail_b": mb, "game_week": fu["game_week"]}
