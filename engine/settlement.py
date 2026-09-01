"""
Weekly settlement — Phase 4.

run_settlement(completed_game_week) is invoked from a background worker after the clock
crosses a 168h boundary so the GameClock thread returns immediately.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

from db import db
from engine.setup import apply_settlement_cash, get_airline


def _row_fuel_cost(r) -> float:
    if r["fuel_cost"] is not None:
        return float(r["fuel_cost"] or 0)
    gal = float(r["fuel_burned_gallons"] or 0)
    spot = r["fuel_spot_price_per_gallon"]
    if gal and spot is not None:
        return gal * float(spot)
    return 0.0


def compute_lease_costs() -> float:
    leased = db.fetch_all(
        """
        SELECT lease_weekly_cost, COALESCE(lease_prepaid, 0) AS prepaid
        FROM fleet WHERE ownership = 'LEASED'
        """
    )
    total = 0.0
    for r in leased or []:
        if int(r["prepaid"] or 0):
            continue
        total += float(r["lease_weekly_cost"] or 0)
    return total


def tick_leases_after_settlement() -> List[str]:
    """Decrement remaining lease weeks; return and remove expired tails."""
    rows = db.fetch_all(
        """
        SELECT tail_number, lease_weeks_remaining
        FROM fleet WHERE ownership = 'LEASED'
        """
    )
    expired: List[str] = []
    for raw in rows or []:
        tail = str(raw["tail_number"])
        rem = max(0, int(raw["lease_weeks_remaining"] or 0) - 1)
        db.execute(
            """
            UPDATE fleet
            SET lease_weeks_remaining = ?, lease_prepaid = 0
            WHERE tail_number = ?
            """,
            (rem, tail),
        )
        if rem <= 0:
            expired.append(tail)
    for tail in expired:
        try:
            from engine.scheduling import cancel_rotation

            cancel_rotation(tail, wipe_completed_this_week=True)
        except Exception:
            pass
        db.execute("DELETE FROM flight_segments WHERE tail_number = ?", (tail,))
        db.execute("DELETE FROM weekly_rotations WHERE tail_number = ?", (tail,))
        db.execute("DELETE FROM fleet_cabin_config WHERE tail_number = ?", (tail,))
        db.execute("DELETE FROM fleet WHERE tail_number = ?", (tail,))
        try:
            from engine.news_feed import push_news

            push_news(f"Lease expired — {tail} returned")
        except Exception:
            pass
    return expired


def _settlement_flags(game_week: int) -> Dict[str, int]:
    row = db.fetch_one(
        "SELECT cash_applied, banking_done, post_ops_done, loan_collected FROM settlement_flags WHERE game_week = ?",
        (game_week,),
    )
    if not row:
        db.execute(
            "INSERT OR IGNORE INTO settlement_flags (game_week) VALUES (?)",
            (game_week,),
        )
        return {"cash_applied": 0, "banking_done": 0, "post_ops_done": 0, "loan_collected": 0.0}
    return {
        "cash_applied": int(row["cash_applied"] or 0),
        "banking_done": int(row["banking_done"] or 0),
        "post_ops_done": int(row["post_ops_done"] or 0),
        "loan_collected": float(row["loan_collected"] or 0),
    }


def _mark_settlement_flag(game_week: int, column: str) -> None:
    if column not in ("cash_applied", "banking_done", "post_ops_done"):
        return
    db.execute(
        f"UPDATE settlement_flags SET {column} = 1 WHERE game_week = ?",
        (game_week,),
    )


def compute_corporate_tax(pretax: float) -> float:
    if pretax <= 0:
        return 0.0
    if pretax <= 1_000_000:
        return 0.0
    if pretax <= 10_000_000:
        return (pretax - 1_000_000) * 0.15
    return (10_000_000 - 1_000_000) * 0.15 + (pretax - 10_000_000) * 0.35


def aggregate_landed_segments_week(game_week: int) -> Dict[str, Any]:
    """Sum P&amp;L-related fields from LANDED flight_segments for one game_week."""
    rows = db.fetch_all(
        """
        SELECT revenue_gross, excise_tax, segment_fee, security_fee, pfc_fee,
               landing_fee, gate_fee, fuel_burned_gallons, fuel_spot_price_per_gallon,
               fuel_cost, net_contribution,
               revenue_economy, revenue_premium_economy, revenue_business_cabin, revenue_first
        FROM flight_segments
        WHERE game_week = ? AND status = 'LANDED'
        """,
        (game_week,),
    )

    revenue_gross = sum(float(r["revenue_gross"] or 0) for r in rows)
    excise_tax = sum(float(r["excise_tax"] or 0) for r in rows)
    segment_fees = sum(float(r["segment_fee"] or 0) for r in rows)
    security_fees = sum(float(r["security_fee"] or 0) for r in rows)
    pfc_fees = sum(float(r["pfc_fee"] or 0) for r in rows)
    landing_fees = sum(float(r["landing_fee"] or 0) for r in rows)
    gate_fees = sum(float(r["gate_fee"] or 0) for r in rows)

    fuel_cost_total = 0.0
    net_from_flights = 0.0
    for r in rows:
        fc = _row_fuel_cost(r)
        fuel_cost_total += fc
        rev = float(r["revenue_gross"] or 0)
        net_from_flights += (
            rev
            - float(r["excise_tax"] or 0)
            - float(r["segment_fee"] or 0)
            - float(r["security_fee"] or 0)
            - float(r["pfc_fee"] or 0)
            - float(r["landing_fee"] or 0)
            - float(r["gate_fee"] or 0)
            - fc
        )

    cabin_revenue = {
        "economy": sum(float(r["revenue_economy"] or 0) for r in rows),
        "premium_economy": sum(float(r["revenue_premium_economy"] or 0) for r in rows),
        "business": sum(float(r["revenue_business_cabin"] or 0) for r in rows),
        "first": sum(float(r["revenue_first"] or 0) for r in rows),
    }

    total_taxes_and_fees = (
        excise_tax + segment_fees + security_fees + pfc_fees + landing_fees + gate_fees
    )

    return {
        "flights_count": len(rows),
        "revenue_gross": revenue_gross,
        "excise_tax": excise_tax,
        "segment_fees": segment_fees,
        "security_fees": security_fees,
        "pfc_fees": pfc_fees,
        "landing_fees": landing_fees,
        "gate_fees": gate_fees,
        "total_taxes_and_fees": total_taxes_and_fees,
        "fuel_cost_total": fuel_cost_total,
        "net_from_flights": net_from_flights,
        "cabin_revenue": cabin_revenue,
    }


def aggregate_live_week_financials(game_week: int) -> Dict[str, Any]:
    """
    Sum P&L-related fields from flight_segments for one game_week, including LANDED,
    DIVERTED, and IN_AIR statuses. This ensures flights still in the air at week
    boundary are counted in the settlement.
    
    Uses scheduled_dep_game_hour within the 168-hour window to avoid game_week drift issues.
    """
    from engine.scheduling import week_base_hours
    
    w_base = week_base_hours(game_week)
    w_end = w_base + 168.0
    
    rows = db.fetch_all(
        """
        SELECT revenue_gross, excise_tax, segment_fee, security_fee, pfc_fee,
               landing_fee, gate_fee, fuel_burned_gallons, fuel_spot_price_per_gallon,
               fuel_cost, net_contribution,
               revenue_economy, revenue_premium_economy, revenue_business_cabin, revenue_first
        FROM flight_segments
        WHERE scheduled_dep_game_hour >= ? AND scheduled_dep_game_hour < ?
          AND status IN ('LANDED', 'DIVERTED', 'IN_AIR')
        """,
        (w_base, w_end),
    )

    revenue_gross = sum(float(r["revenue_gross"] or 0) for r in rows)
    excise_tax = sum(float(r["excise_tax"] or 0) for r in rows)
    segment_fees = sum(float(r["segment_fee"] or 0) for r in rows)
    security_fees = sum(float(r["security_fee"] or 0) for r in rows)
    pfc_fees = sum(float(r["pfc_fee"] or 0) for r in rows)
    landing_fees = sum(float(r["landing_fee"] or 0) for r in rows)
    gate_fees = sum(float(r["gate_fee"] or 0) for r in rows)

    fuel_cost_total = 0.0
    net_from_flights = 0.0
    for r in rows:
        fc = _row_fuel_cost(r)
        fuel_cost_total += fc
        rev = float(r["revenue_gross"] or 0)
        net_from_flights += (
            rev
            - float(r["excise_tax"] or 0)
            - float(r["segment_fee"] or 0)
            - float(r["security_fee"] or 0)
            - float(r["pfc_fee"] or 0)
            - float(r["landing_fee"] or 0)
            - float(r["gate_fee"] or 0)
            - fc
        )

    cabin_revenue = {
        "economy": sum(float(r["revenue_economy"] or 0) for r in rows),
        "premium_economy": sum(float(r["revenue_premium_economy"] or 0) for r in rows),
        "business": sum(float(r["revenue_business_cabin"] or 0) for r in rows),
        "first": sum(float(r["revenue_first"] or 0) for r in rows),
    }

    total_taxes_and_fees = (
        excise_tax + segment_fees + security_fees + pfc_fees + landing_fees + gate_fees
    )

    return {
        "flights_count": len(rows),
        "revenue_gross": revenue_gross,
        "excise_tax": excise_tax,
        "segment_fees": segment_fees,
        "security_fees": security_fees,
        "pfc_fees": pfc_fees,
        "landing_fees": landing_fees,
        "gate_fees": gate_fees,
        "total_taxes_and_fees": total_taxes_and_fees,
        "fuel_cost_total": fuel_cost_total,
        "net_from_flights": net_from_flights,
        "cabin_revenue": cabin_revenue,
    }


def build_week_summary_payload(for_game_week: Optional[int] = None) -> Dict[str, Any]:
    """
    Read-only week summary for the menu (no cash mutation).

    If week_ledger has the week, uses recorded totals; otherwise computes a live snapshot
    from landed segments + weekly leases/tax (same rules as settlement) and shows current cash.
    """
    airline = get_airline()
    if not airline:
        return {"skipped": True, "reason": "no airline"}

    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    current_gw = int(gs["game_week"]) if gs else 1
    target = int(for_game_week) if for_game_week is not None else current_gw
    if target < 1:
        target = 1

    agg = aggregate_live_week_financials(target)
    lease_costs = compute_lease_costs()
    maintenance_costs = 0.0
    try:
        from engine.banking import scheduled_weekly_service

        loan_payments = float(scheduled_weekly_service())
    except Exception:
        loan_payments = 0.0
    pretax = agg["net_from_flights"] - lease_costs - maintenance_costs
    corporate_tax_live = compute_corporate_tax(pretax)
    net_income_live = pretax - corporate_tax_live

    ledger = db.fetch_one("SELECT * FROM week_ledger WHERE game_week = ?", (target,))

    prev = db.fetch_one(
        "SELECT net_income, cash_end_of_week, revenue_gross FROM week_ledger WHERE game_week = ?",
        (target - 1,),
    )
    events = _events_for_week(target)

    base = {
        "skipped": False,
        "game_week": target,
        "on_demand": True,
        "flights_count": agg["flights_count"],
        "cabin_revenue": agg["cabin_revenue"],
        "event_log": events,
        "prev_week": dict(prev) if prev else None,
        "maintenance_costs": maintenance_costs,
        "loan_payments": loan_payments,
    }

    if ledger:
        excise = float(ledger["excise_tax"])
        seg = float(ledger["segment_fees"])
        sec = float(ledger["security_fees"])
        pfc = float(ledger["pfc_fees"])
        land = float(ledger["landing_fees"])
        gate = float(ledger["gate_fees"])
        total_tf = excise + seg + sec + pfc + land + gate
        return {
            **base,
            "summary_source": "ledger",
            "partial_week": False,
            "revenue_gross": float(ledger["revenue_gross"]),
            "excise_tax": excise,
            "segment_fees": seg,
            "security_fees": sec,
            "pfc_fees": pfc,
            "landing_fees": land,
            "gate_fees": gate,
            "total_taxes_and_fees": total_tf,
            "fuel_cost": float(ledger["fuel_cost"]),
            "lease_costs": float(ledger["lease_costs"]),
            "loan_payments": float(ledger["loan_payments"] or 0),
            "corporate_tax": float(ledger["corporate_tax"]),
            "net_income": float(ledger["net_income"]),
            "cash_end_of_week": float(ledger["cash_end_of_week"]),
            "cash_row_label": "Cash (end of week, settled)",
        }

    return {
        **base,
        "summary_source": "live",
        "partial_week": True,
        "pretax": pretax,
        "revenue_gross": agg["revenue_gross"],
        "excise_tax": agg["excise_tax"],
        "segment_fees": agg["segment_fees"],
        "security_fees": agg["security_fees"],
        "pfc_fees": agg["pfc_fees"],
        "landing_fees": agg["landing_fees"],
        "gate_fees": agg["gate_fees"],
        "total_taxes_and_fees": agg["total_taxes_and_fees"],
        "fuel_cost": agg["fuel_cost_total"],
        "lease_costs": lease_costs,
        "corporate_tax": corporate_tax_live,
        "net_income": net_income_live,
        "cash_end_of_week": float(airline["cash"]),
        "cash_row_label": "Cash (now)",
    }

_settlement_lock = threading.Lock()
_week_summary_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=16)
_catch_up_lock = threading.Lock()
_catch_up_running = False


def get_week_summary_queue() -> "queue.Queue[Dict[str, Any]]":
    """UI polls this for completed week summaries (non-blocking)."""
    return _week_summary_queue


def calendar_week_from_state() -> int:
    """Current 1-based calendar week from game_state (fallback to hours)."""
    gs = db.fetch_one("SELECT game_week, game_hours_elapsed FROM game_state WHERE id = 1")
    if not gs:
        return 1
    try:
        from engine.clock import get_display_game_hours

        ghe = float(get_display_game_hours())
    except Exception:
        ghe = float(gs["game_hours_elapsed"] or 0)
    by_hours = int(ghe // 168) + 1
    by_col = int(gs["game_week"] or 1)
    return max(1, by_hours, by_col)


def last_settled_week() -> int:
    row = db.fetch_one("SELECT COALESCE(MAX(game_week), 0) AS m FROM week_ledger")
    return int(row["m"] or 0) if row else 0


def missing_settlement_weeks(up_to_week: Optional[int] = None) -> List[int]:
    """
    Completed weeks (1 .. current-1) that have no week_ledger row.

    up_to_week defaults to the live calendar week; only weeks strictly before it are due.
    """
    cur = int(up_to_week) if up_to_week is not None else calendar_week_from_state()
    if cur <= 1:
        return []
    settled = {
        int(r["game_week"])
        for r in (db.fetch_all("SELECT game_week FROM week_ledger") or [])
        if r and r["game_week"] is not None
    }
    return [w for w in range(1, cur) if w not in settled]


def _incomplete_ai_settlement_weeks() -> List[int]:
    """Weeks with economics settled but AI post-ops not finished."""
    try:
        rows = db.fetch_all(
            """
            SELECT sf.game_week
            FROM settlement_flags sf
            INNER JOIN week_ledger wl ON wl.game_week = sf.game_week
            WHERE sf.cash_applied = 1 AND sf.post_ops_done = 0
            ORDER BY sf.game_week
            """
        )
        return [int(r["game_week"]) for r in (rows or []) if r and r["game_week"] is not None]
    except Exception:
        return []


def _retry_ai_turn_only(completed_game_week: int) -> Dict[str, Any]:
    """Re-run AI weekly turn for a week whose cash settlement already completed."""
    gw = int(completed_game_week)
    flags = _settlement_flags(gw)
    if flags["post_ops_done"]:
        return {"skipped": True, "reason": "post_ops_done", "game_week": gw}
    if not flags["cash_applied"]:
        return {"skipped": True, "reason": "cash_not_applied", "game_week": gw}
    ai_result: Dict[str, Any]
    try:
        from engine.ai import ai_weekly_turn

        ai_result = ai_weekly_turn(gw)
    except Exception as e:
        import traceback

        ai_result = {"error": str(e), "traceback": traceback.format_exc(), "errors": 1}
    ai_errors = int((ai_result or {}).get("errors") or 0)
    if (ai_result or {}).get("error"):
        ai_errors = max(ai_errors, 1)
    if ai_errors <= 0:
        _mark_settlement_flag(gw, "post_ops_done")
    else:
        try:
            from engine.news_feed import push_news

            push_news(f"⚠ Week {gw} AI turn still incomplete ({ai_errors} errors) — will retry")
        except Exception:
            pass
    return {"game_week": gw, "ai_turn": ai_result, "ai_errors": ai_errors}


def catch_up_missing_settlements(*, limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Settle any completed weeks missing from week_ledger (idempotent via run_settlement).

    Applies full economics (cash, leases, reputation, AI turn, …) for each missing week.
    Concurrent callers share one run (boot + Books open).
    """
    global _catch_up_running
    with _catch_up_lock:
        if _catch_up_running:
            return {
                "missing_before": missing_settlement_weeks(),
                "attempted": [],
                "settled": [],
                "errors": [],
                "results": [],
                "last_settled_week": last_settled_week(),
                "calendar_week": calendar_week_from_state(),
                "still_missing": missing_settlement_weeks(),
                "skipped_busy": True,
            }
        _catch_up_running = True
    try:
        return _catch_up_missing_settlements_body(limit=limit)
    finally:
        with _catch_up_lock:
            _catch_up_running = False


def _catch_up_missing_settlements_body(*, limit: Optional[int] = None) -> Dict[str, Any]:
    ai_retries: List[Dict[str, Any]] = []
    for w in _incomplete_ai_settlement_weeks():
        try:
            ai_retries.append(_retry_ai_turn_only(w))
        except Exception as e:
            ai_retries.append({"game_week": w, "error": str(e)})
    all_missing = missing_settlement_weeks()
    missing = list(all_missing)
    if limit is not None:
        missing = missing[: max(0, int(limit))]
    settled: List[int] = []
    errors: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for w in missing:
        try:
            out = run_settlement(w)
            if out.get("skipped") and out.get("reason") == "already settled":
                # Concurrent catch-up / race: treat as done, not newly settled.
                continue
            if out.get("skipped"):
                errors.append({"game_week": w, "error": out.get("reason") or "skipped"})
                # No airline / invalid week — stop so we do not keep failing.
                if out.get("reason") in ("no airline", "invalid week"):
                    break
                continue
            settled.append(w)
            results.append(out)
        except Exception as e:
            errors.append({"game_week": w, "error": str(e)})
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ Settlement catch-up failed for week {w}: {e}")
            except Exception:
                pass
            break
    if settled:
        try:
            from engine.news_feed import push_news

            if len(settled) == 1:
                push_news(f"Settled backlog: week {settled[0]}")
            else:
                push_news(f"Settled backlog: weeks {settled[0]}–{settled[-1]}")
        except Exception:
            pass
    return {
        "missing_before": all_missing,
        "attempted": missing,
        "settled": settled,
        "errors": errors,
        "results": results,
        "last_settled_week": last_settled_week(),
        "calendar_week": calendar_week_from_state(),
        "still_missing": missing_settlement_weeks(),
    }


def spawn_segments_for_calendar_week(new_calendar_week: int) -> Dict[str, Any]:
    """
    Spawn player + AI segments for a calendar week on the clock thread at week flip.

    Idempotent (spawn helpers dedupe). Called synchronously from on_week so the map
    is not empty while async settlement runs.
    """
    from engine.scheduling import (
        reset_operational_schedule_for_new_calendar_week,
        spawn_rotation_segments_for_week,
    )
    from engine.ai_flights import spawn_ai_segments_for_week

    gw = int(new_calendar_week)
    out: Dict[str, Any] = {"new_calendar_week": gw}
    try:
        out["schedule_reset_to_baseline"] = reset_operational_schedule_for_new_calendar_week(gw)
    except Exception as e:
        out["schedule_reset_error"] = str(e)
    try:
        out["spawn"] = spawn_rotation_segments_for_week(gw)
    except Exception as e:
        out["spawn_error"] = str(e)
    try:
        out["spawn_ai"] = spawn_ai_segments_for_week(gw)
    except Exception as e:
        out["spawn_ai_error"] = str(e)
    return out


def enqueue_settlement_after_week_boundary(new_calendar_week: int) -> None:
    """
    Run settlement for the completed week and spawn next week's segments off the clock thread.
    Pushes one dict onto get_week_summary_queue() for the Week Summary popup.
    """
    def job() -> None:
        from engine.scheduling import (
            reset_operational_schedule_for_new_calendar_week,
            spawn_rotation_segments_for_week,
        )
        from engine.ai_flights import spawn_ai_segments_for_week

        try:
            completed = new_calendar_week - 1
            result: Dict[str, Any]
            if completed >= 1:
                result = run_settlement(completed)
            else:
                result = {"skipped": True, "reason": "no completed week"}
            # Snap future legs back to published plan; clears delay cascades for the new week.
            result["schedule_reset_to_baseline"] = reset_operational_schedule_for_new_calendar_week(
                new_calendar_week
            )
            sp = spawn_rotation_segments_for_week(new_calendar_week)
            result["spawn"] = sp
            result["spawn_ai"] = spawn_ai_segments_for_week(new_calendar_week)
            result["new_calendar_week"] = new_calendar_week
            try:
                from engine.events import schedule_weekly_events

                schedule_weekly_events(new_calendar_week)
            except Exception:
                pass
            _week_summary_queue.put(result, block=False)
        except queue.Full:
            pass
        except Exception as e:
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ Week {new_calendar_week} settlement failed: {e}")
            except Exception:
                pass
            try:
                _week_summary_queue.put(
                    {"skipped": True, "error": str(e), "new_calendar_week": new_calendar_week},
                    block=False,
                )
            except queue.Full:
                pass

    threading.Thread(target=job, daemon=True).start()


def _events_for_week(game_week: int) -> List[Dict[str, Any]]:
    # Inspect schema to choose a query that doesn't reference missing columns.
    cols = []
    try:
        info = db.fetch_all("PRAGMA table_info(event_log)")
        cols = [str(r["name"]) for r in info] if info else []
    except Exception as ex:
        cols = []

    has_message = "message" in cols
    has_desc = "description" in cols
    has_ghe = "game_hours_elapsed" in cols
    has_gth = "game_time_hours" in cols

    if has_message:
        q = """
            SELECT event_type, message AS message, game_hours_elapsed
            FROM event_log
            WHERE game_week = ?
            ORDER BY event_id
        """
    elif has_desc:
        q = """
            SELECT event_type, description AS message, game_time_hours AS game_hours_elapsed
            FROM event_log
            WHERE game_week = ?
            ORDER BY event_id
        """
    else:
        # Unknown / very old schema; just return types if available
        q = """
            SELECT event_type
            FROM event_log
            WHERE game_week = ?
            ORDER BY event_id
        """

    try:
        rows = db.fetch_all(q, (game_week,))
        out = [dict(r) for r in rows]
        return out
    except Exception as ex:
        raise


def run_settlement(completed_game_week: int) -> Dict[str, Any]:
    """
    End-of-week settlement for completed_game_week (1-based).

    Idempotent: skips ledger work if week_ledger already has this week.
    """
    with _settlement_lock:
        return _run_settlement_impl(completed_game_week)


def _run_settlement_impl(completed_game_week: int) -> Dict[str, Any]:
    if completed_game_week < 1:
        return {"skipped": True, "reason": "invalid week"}

    existing = db.fetch_one(
        "SELECT game_week FROM week_ledger WHERE game_week = ?",
        (completed_game_week,),
    )
    flags = _settlement_flags(completed_game_week)
    if existing and flags["post_ops_done"]:
        return {"skipped": True, "reason": "already settled", "game_week": completed_game_week}
    if existing and not flags["post_ops_done"]:
        return _retry_ai_turn_only(completed_game_week)

    airline = get_airline()
    if not airline:
        return {"skipped": True, "reason": "no airline"}

    # Step 1 — Event financial impact (Phase 9)
    event_financial_impact = 0.0
    try:
        cols = []
        try:
            info = db.fetch_all("PRAGMA table_info(event_log)")
            cols = [str(r["name"]) for r in (info or [])]
        except Exception:
            cols = []
        if "financial_impact" in cols:
            row = db.fetch_one(
                "SELECT COALESCE(SUM(financial_impact), 0) AS s FROM event_log WHERE game_week = ?",
                (completed_game_week,),
            )
            event_financial_impact = float(row["s"] or 0.0) if row else 0.0
    except Exception:
        event_financial_impact = 0.0

    # Step 2 — Aggregate flight_segments
    # Use live rollup (LANDED + DIVERTED + IN_AIR) so flights still airborne at week
    # boundary are counted. Previously only LANDED was included, causing $0 settlements.
    agg = aggregate_live_week_financials(completed_game_week)
    revenue_gross = agg["revenue_gross"]
    excise_tax = agg["excise_tax"]
    segment_fees = agg["segment_fees"]
    security_fees = agg["security_fees"]
    pfc_fees = agg["pfc_fees"]
    landing_fees = agg["landing_fees"]
    gate_fees = agg["gate_fees"]
    fuel_cost_total = agg["fuel_cost_total"]
    net_from_flights = agg["net_from_flights"]
    cabin_revenue = agg["cabin_revenue"]
    total_taxes_and_fees = agg["total_taxes_and_fees"]

    lease_costs = compute_lease_costs()

    maintenance_costs = 0.0

    pretax = (
        net_from_flights
        - lease_costs
        - maintenance_costs
        + event_financial_impact
    )

    corporate_tax = compute_corporate_tax(pretax)

    net_income = pretax - corporate_tax

    cash_start = float(airline["cash"])
    flags = _settlement_flags(completed_game_week)

    expired_leases: List[str] = []
    if not flags["cash_applied"]:
        # Operating result may go negative; purchases still use update_cash which cannot overdraft.
        apply_settlement_cash(net_income)
        expired_leases = tick_leases_after_settlement()
        _mark_settlement_flag(completed_game_week, "cash_applied")
        flags["cash_applied"] = 1

    loan_payments = 0.0
    banking_result = None
    if not flags["banking_done"]:
        try:
            from engine.banking import check_bankruptcy, process_loan_payments, update_credit_score

            paid = process_loan_payments(completed_game_week)
            loan_payments = float(paid.get("collected") or 0.0)
            update_credit_score(net_income)
            banking_result = check_bankruptcy(completed_game_week)
            if banking_result:
                paid["chapter11"] = banking_result
        except Exception as e:
            banking_result = None
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ Banking week failed: {e}")
            except Exception:
                pass
        _mark_settlement_flag(completed_game_week, "banking_done")
        db.execute(
            "UPDATE settlement_flags SET loan_collected = ? WHERE game_week = ?",
            (loan_payments, completed_game_week),
        )
        flags["banking_done"] = 1
    else:
        loan_payments = float(flags.get("loan_collected") or 0.0)

    airline_after = get_airline() or {}
    cash_end = float(airline_after.get("cash") or 0.0)

    # Phase 9 / 10 / 11 post-ops — once per week
    rep_delta = None
    rep_score = None
    brand_power = None
    gate_result = None
    ai_result = None
    if not flags["post_ops_done"]:
        db.execute(
            """
            UPDATE fleet
            SET status = 'IDLE'
            WHERE status = 'LANDED'
            """
        )
        try:
            db.execute(
                """
                UPDATE fleet
                SET weeks_since_maintenance = COALESCE(weeks_since_maintenance, 0) + 1
                WHERE status != 'AOG'
                """
            )
        except Exception:
            pass
        db.execute("UPDATE game_state SET demand_noise_seed = demand_noise_seed + 1 WHERE id = 1")
        try:
            from engine.fuel import tick_down_hedge_after_settlement, write_fuel_price_history_and_shock

            write_fuel_price_history_and_shock(completed_game_week)
            tick_down_hedge_after_settlement()
        except Exception:
            pass
        try:
            from engine.reputation import update_reputation, apply_brand_power_from_reputation

            rep_delta, rep_score = update_reputation(completed_game_week)
            brand_power = apply_brand_power_from_reputation()
        except Exception:
            rep_delta = None
            rep_score = None
            brand_power = None

        try:
            from engine.gates import (
                ensure_weekly_airport_auctions,
                resolve_closing_gate_auctions,
                resolve_overdue_gate_auctions,
                reset_weekly_gate_counters_and_enforce,
            )

            # Resolve the week that just ended first, then any older OPEN leftovers
            # (e.g. settlement was skipped once and the UI used to cancel those rows).
            resolved = int(resolve_closing_gate_auctions(completed_game_week) or 0)
            resolved += int(resolve_overdue_gate_auctions(completed_game_week + 1) or 0)
            ensure_weekly_airport_auctions(completed_game_week + 1)
            reset_weekly_gate_counters_and_enforce(completed_game_week)
            gate_result = {"gate_auctions_resolved": int(resolved)}
        except Exception as e:
            gate_result = {"gate_auctions_resolved": 0, "error": str(e)}
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ Gate auction settlement failed: {e}")
            except Exception:
                pass

        try:
            from engine.slots import (
                enforce_slot_utilization,
                ensure_slot_allocations_for_week,
                ensure_weekly_slot_auctions,
                grandfather_historic_slot_holdings,
                rebuild_slot_usages_for_week,
                resolve_closing_slot_auctions,
                seed_slot_controlled_airports,
            )

            seed_slot_controlled_airports()
            grandfather_historic_slot_holdings(completed_game_week)
            rebuild_slot_usages_for_week(completed_game_week)
            enforce_slot_utilization(completed_game_week)
            ensure_slot_allocations_for_week(completed_game_week + 1)
            resolve_closing_slot_auctions(completed_game_week)
            ensure_weekly_slot_auctions(completed_game_week + 1)
        except Exception as e:
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ Slot market week failed: {e}")
            except Exception:
                pass

        try:
            from engine.ai import ai_weekly_turn

            ai_result = ai_weekly_turn(completed_game_week)
        except Exception as e:
            import traceback

            ai_result = {"error": str(e), "traceback": traceback.format_exc(), "errors": 1}
            try:
                from engine.news_feed import push_news

                push_news(f"⚠ AI weekly turn failed: {e}")
            except Exception:
                pass
        ai_errors = int((ai_result or {}).get("errors") or 0)
        if (ai_result or {}).get("error"):
            ai_errors = max(ai_errors, 1)
        if ai_errors <= 0:
            _mark_settlement_flag(completed_game_week, "post_ops_done")
        else:
            try:
                from engine.news_feed import push_news

                push_news(
                    f"⚠ Week {completed_game_week} AI turn incomplete ({ai_errors} errors) — will retry"
                )
            except Exception:
                pass

    db.execute(
        """
        INSERT INTO week_ledger (
            game_week, revenue_gross, excise_tax, segment_fees, security_fees,
            pfc_fees, landing_fees, gate_fees, fuel_cost, lease_costs,
            maintenance_costs, loan_payments, corporate_tax, net_income,
            cash_end_of_week
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            completed_game_week,
            revenue_gross,
            excise_tax,
            segment_fees,
            security_fees,
            pfc_fees,
            landing_fees,
            gate_fees,
            fuel_cost_total,
            lease_costs,
            maintenance_costs,
            loan_payments,
            corporate_tax,
            net_income,
            cash_end,
        ),
    )

    db.atomic_save()

    prev = db.fetch_one(
        "SELECT net_income, cash_end_of_week, revenue_gross FROM week_ledger WHERE game_week = ?",
        (completed_game_week - 1,),
    )

    events_week = _events_for_week(completed_game_week)

    return {
        "skipped": False,
        "game_week": completed_game_week,
        "revenue_gross": revenue_gross,
        "cabin_revenue": cabin_revenue,
        "excise_tax": excise_tax,
        "segment_fees": segment_fees,
        "security_fees": security_fees,
        "pfc_fees": pfc_fees,
        "landing_fees": landing_fees,
        "gate_fees": gate_fees,
        "total_taxes_and_fees": total_taxes_and_fees,
        "fuel_cost": fuel_cost_total,
        "lease_costs": lease_costs,
        "maintenance_costs": maintenance_costs,
        "loan_payments": loan_payments,
        "pretax": pretax,
        "event_financial_impact": event_financial_impact,
        "corporate_tax": corporate_tax,
        "net_income": net_income,
        "cash_start": cash_start,
        "cash_end_of_week": cash_end,
        "flights_count": agg["flights_count"],
        "event_log": events_week,
        "reputation_delta": rep_delta,
        "reputation_score": rep_score,
        "brand_power": brand_power,
        "ai_turn": ai_result,
        "gate_settlement": gate_result,
        "banking": banking_result,
        "ai_retry_pending": not bool(_settlement_flags(completed_game_week)["post_ops_done"]),
        "auctions_resolved": int((gate_result or {}).get("gate_auctions_resolved") or 0),
        "prev_week": dict(prev) if prev else None,
    }
