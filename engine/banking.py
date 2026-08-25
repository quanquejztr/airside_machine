"""
Phase 12 — player banking and credit. AI does not borrow.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from db import db
from engine.reputation import reputation_to_interest_modifier
from engine.setup import apply_settlement_cash, get_airline


MIN_TICKET = 100_000.0
BORROW_FLOOR = 500_000.0
CREDIT_MIN = 300
CREDIT_MAX = 850

# (lo_score, hi_score, annual_rate, max_weeks)
_BRACKETS = (
    (300, 499, 0.12, 26),
    (500, 649, 0.08, 52),
    (650, 799, 0.05, 78),
    (800, 850, 0.035, 104),
)


def _current_week() -> int:
    row = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    return int(row["game_week"] or 1) if row else 1


def _clamp_credit(score: int) -> int:
    return max(CREDIT_MIN, min(CREDIT_MAX, int(score)))


def _notify(body: str, game_week: Optional[int] = None) -> None:
    gw = int(game_week if game_week is not None else _current_week())
    try:
        db.execute(
            """
            INSERT INTO player_notifications (notification_id, game_week, type, route_pair_id, body, read)
            VALUES (?, ?, 'BANK', NULL, ?, 0)
            """,
            (str(uuid.uuid4()), gw, str(body)),
        )
    except Exception:
        pass
    try:
        from engine.news_feed import push_news

        push_news(str(body))
    except Exception:
        pass


def _record_credit_event(event_type: str, score_delta: int, description: str, apply_delta: bool = True) -> None:
    gw = _current_week()
    db.execute(
        """
        INSERT INTO credit_events (event_id, game_week, event_type, score_delta, description)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), gw, str(event_type), int(score_delta), str(description)),
    )
    if not apply_delta:
        return
    al = get_airline()
    if not al:
        return
    new = _clamp_credit(int(al["credit_score"] or 720) + int(score_delta))
    db.execute("UPDATE airline SET credit_score = ? WHERE id = 1", (new,))


def current_debt() -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(principal_remaining), 0) AS d
        FROM loans WHERE status = 'ACTIVE'
        """
    )
    return float(row["d"] or 0.0) if row else 0.0


def sync_total_debt() -> float:
    debt = current_debt()
    db.execute("UPDATE airline SET total_debt = ? WHERE id = 1", (debt,))
    return debt


def weekly_revenue_for_cap() -> float:
    last = db.fetch_one(
        "SELECT revenue_gross FROM week_ledger ORDER BY game_week DESC LIMIT 1"
    )
    if last and float(last["revenue_gross"] or 0.0) > 0:
        return float(last["revenue_gross"])
    try:
        from engine.settlement import aggregate_live_week_financials

        live = aggregate_live_week_financials(_current_week())
        return float(live.get("revenue_gross") or 0.0)
    except Exception:
        return 0.0


def borrowing_cap() -> float:
    return max(BORROW_FLOOR, 5.0 * weekly_revenue_for_cap())


def headroom() -> float:
    return max(0.0, borrowing_cap() - current_debt())


def scheduled_weekly_service() -> float:
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(weekly_payment), 0) AS p
        FROM loans WHERE status = 'ACTIVE'
        """
    )
    return float(row["p"] or 0.0) if row else 0.0


def debt_and_headroom() -> Dict[str, Any]:
    al = get_airline() or {}
    debt = current_debt()
    cap = borrowing_cap()
    score = int(al.get("credit_score") or 720)
    br = credit_bracket(score)
    return {
        "credit_score": score,
        "debt": debt,
        "borrowing_cap": cap,
        "headroom": max(0.0, cap - debt),
        "weekly_service": scheduled_weekly_service(),
        "weekly_revenue": weekly_revenue_for_cap(),
        "apr": float(br["annual_rate"]),
        "max_weeks": int(br["max_weeks"]),
        "bracket_label": br["label"],
        "negative_cash_weeks": int(al.get("negative_cash_weeks") or 0),
        "cash": float(al.get("cash") or 0.0),
        "reputation_score": float(al.get("reputation_score") or 0.0),
    }


def credit_bracket(score: Optional[int] = None) -> Dict[str, Any]:
    al = get_airline()
    if score is None:
        score = int(al["credit_score"] or 720) if al else 720
    s = _clamp_credit(score)
    annual = 0.12
    max_w = 26
    for lo, hi, rate, weeks in _BRACKETS:
        if lo <= s <= hi:
            annual = rate
            max_w = weeks
            break
    rep = float(al["reputation_score"] or 50.0) if al else 50.0
    annual = max(0.01, float(annual) + float(reputation_to_interest_modifier(rep)))
    return {
        "credit_score": s,
        "annual_rate": annual,
        "max_weeks": max_w,
        "label": f"{annual * 100:.2f}% APR / up to {max_w} weeks",
    }


def amortizing_payment(principal: float, weekly_rate: float, weeks: int) -> float:
    p = float(principal)
    n = int(weeks)
    r = float(weekly_rate)
    if p <= 0 or n <= 0:
        return 0.0
    if r <= 1e-12:
        return p / float(n)
    factor = (1.0 + r) ** n
    return p * (r * factor) / (factor - 1.0)


def get_loan_offers(amount: float) -> List[Dict[str, Any]]:
    amt = float(amount)
    if amt + 0.5 < MIN_TICKET:
        raise ValueError(f"Minimum loan is ${MIN_TICKET:,.0f}.")
    room = headroom()
    if amt > room + 0.5:
        raise ValueError(
            f"Amount exceeds borrowing room (${room:,.0f}; cap ${borrowing_cap():,.0f}, "
            f"5× weekly revenue with a ${BORROW_FLOOR:,.0f} floor)."
        )
    br = credit_bracket()
    max_w = int(br["max_weeks"])
    terms = sorted({max(1, int(max_w * 0.5)), max(1, int(max_w * 0.75)), max_w})
    weekly_rate = float(br["annual_rate"]) / 52.0
    out = []
    for n in terms:
        pmt = amortizing_payment(amt, weekly_rate, n)
        out.append(
            {
                "amount": amt,
                "weeks": n,
                "annual_rate": float(br["annual_rate"]),
                "weekly_interest_rate": weekly_rate,
                "weekly_payment": pmt,
                "total_paid": pmt * n,
                "label": br["label"],
            }
        )
    return out


def originate_loan(amount: float, weeks: int, rate: Optional[float] = None) -> Dict[str, Any]:
    amt = float(amount)
    n = int(weeks)
    offers = get_loan_offers(amt)
    match = next((o for o in offers if int(o["weeks"]) == n), None)
    if match is None:
        allowed = ", ".join(str(o["weeks"]) for o in offers)
        raise ValueError(f"Term must be one of: {allowed} weeks.")
    if rate is not None and abs(float(rate) - float(match["annual_rate"])) > 0.005:
        raise ValueError("Offered rate no longer matches your credit bracket.")
    gst = db.fetch_one("SELECT 1 FROM airline WHERE id = 1")
    if not gst:
        raise ValueError("No airline found.")
    lid = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO loans (
            loan_id, principal_original, principal_remaining,
            weekly_interest_rate, weekly_payment, weeks_remaining,
            originated_week, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """,
        (
            lid,
            amt,
            amt,
            float(match["weekly_interest_rate"]),
            float(match["weekly_payment"]),
            n,
            _current_week(),
        ),
    )
    apply_settlement_cash(amt)
    sync_total_debt()
    _record_credit_event("LOAN_ORIGINATED", -5, f"Originated ${amt:,.0f} over {n} weeks.")
    _notify(
        f"Loan funded: ${amt:,.0f} over {n} weeks · "
        f"{match['annual_rate'] * 100:.2f}% APR · weekly ${match['weekly_payment']:,.0f}."
    )
    return {
        "loan_id": lid,
        "amount": amt,
        "weeks": n,
        "weekly_payment": float(match["weekly_payment"]),
        "annual_rate": float(match["annual_rate"]),
        "debt": sync_total_debt(),
        "credit_score": int((get_airline() or {}).get("credit_score") or 0),
    }


def payoff_loan(loan_id: str) -> Dict[str, Any]:
    aid = str(loan_id)
    row = db.fetch_one("SELECT * FROM loans WHERE loan_id = ? AND status = 'ACTIVE'", (aid,))
    if not row:
        raise ValueError("No active loan with that id.")
    due = float(row["principal_remaining"] or 0.0)
    al = get_airline()
    cash = float(al["cash"] or 0.0) if al else 0.0
    if cash + 0.01 < due:
        raise ValueError(f"Need ${due:,.0f} cash to pay off this loan (have ${cash:,.0f}).")
    apply_settlement_cash(-due)
    db.execute(
        "UPDATE loans SET principal_remaining = 0, weeks_remaining = 0, status = 'CLOSED' WHERE loan_id = ?",
        (aid,),
    )
    sync_total_debt()
    _record_credit_event("LOAN_REPAID", 15, f"Paid off loan {aid[:8]} (${due:,.0f}).")
    _notify(f"Loan paid off: ${due:,.0f}. Credit +15.")
    return {"loan_id": aid, "paid": due, "debt": sync_total_debt()}


def list_loans(*, active_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM loans ORDER BY originated_week DESC, status"
    if active_only:
        sql = "SELECT * FROM loans WHERE status = 'ACTIVE' ORDER BY originated_week DESC"
    rows = db.fetch_all(sql)
    return [dict(r) for r in (rows or [])]


def _apply_toward_loan(loan: Dict[str, Any], cash_available: float) -> tuple[float, bool]:
    """
    Pay as much of this week's due as cash_available allows.
    Returns (amount_taken, fully_paid_this_installment).
    """
    remaining = float(loan["principal_remaining"] or 0.0)
    rate = float(loan["weekly_interest_rate"] or 0.0)
    weeks_left = int(loan["weeks_remaining"] or 0)
    pmt = float(loan["weekly_payment"] or 0.0)
    interest = remaining * rate
    payoff = remaining + interest
    due = payoff if weeks_left <= 1 or payoff <= pmt + 0.01 else pmt
    due = max(0.0, due)
    take = min(float(cash_available), due)
    if take + 0.0001 < due:
        # Capitalize unpaid interest into principal; do not burn a week.
        new_rem = max(0.0, remaining + interest - take)
        db.execute(
            "UPDATE loans SET principal_remaining = ? WHERE loan_id = ?",
            (new_rem, str(loan["loan_id"])),
        )
        return take, False
    toward_prin = max(0.0, take - interest)
    new_rem = max(0.0, remaining - toward_prin)
    new_weeks = max(0, weeks_left - 1)
    status = "ACTIVE"
    if new_rem < 0.50 or new_weeks <= 0:
        new_rem = 0.0
        new_weeks = 0
        status = "CLOSED"
    db.execute(
        """
        UPDATE loans
        SET principal_remaining = ?, weeks_remaining = ?, status = ?
        WHERE loan_id = ?
        """,
        (new_rem, new_weeks, status, str(loan["loan_id"])),
    )
    return take, True


def process_loan_payments(game_week: int) -> Dict[str, Any]:
    """Settlement debt service. Call after operating net hits cash."""
    gw = int(game_week)
    collected = 0.0
    missed = False
    closed: List[str] = []
    rows = db.fetch_all("SELECT * FROM loans WHERE status = 'ACTIVE' ORDER BY originated_week")
    for raw in rows or []:
        loan = dict(raw)
        al = get_airline()
        cash = float(al["cash"] or 0.0) if al else 0.0
        payable = max(0.0, cash)
        taken, full = _apply_toward_loan(loan, payable)
        if taken > 0:
            apply_settlement_cash(-taken)
            collected += taken
        if not full:
            missed = True
        refreshed = db.fetch_one("SELECT status FROM loans WHERE loan_id = ?", (str(loan["loan_id"]),))
        if refreshed and str(refreshed["status"]) == "CLOSED":
            closed.append(str(loan["loan_id"]))
    if missed:
        _record_credit_event("LOAN_MISSED", -25, f"Week {gw}: missed or short loan payment.")
        _notify(f"Missed loan payment in week {gw}. Credit −25.", gw)
    for lid in closed:
        _record_credit_event("LOAN_REPAID", 15, f"Completed loan {lid[:8]}.")
        _notify(f"Loan {lid[:8]} paid in full. Credit +15.", gw)
    debt = sync_total_debt()
    return {"collected": collected, "missed": missed, "closed": len(closed), "debt": debt}


def update_credit_score(net_income: float) -> int:
    """Weekly P&L nudge after settlement ops (not after debt service)."""
    delta = 1 if float(net_income) > 0 else -3
    kind = "REPUTATION_BONUS" if delta > 0 else "WEEK_LOSS"
    _record_credit_event(kind, delta, f"Week P&L credit {delta:+d} (ops net ${float(net_income):,.0f}).")
    al = get_airline()
    return int(al["credit_score"] or 720) if al else 720


def _seize_costliest_owned() -> Optional[str]:
    row = db.fetch_one(
        """
        SELECT f.tail_number, f.type_id, t.purchase_price
        FROM fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE f.ownership = 'OWNED'
        ORDER BY t.purchase_price DESC
        LIMIT 1
        """
    )
    if not row:
        return None
    tail = str(row["tail_number"])
    try:
        from engine.scheduling import cancel_rotation

        cancel_rotation(tail)
    except Exception:
        db.execute("DELETE FROM weekly_rotations WHERE tail_number = ?", (tail,))
        db.execute("DELETE FROM flight_schedules WHERE tail_number = ?", (tail,))
    db.execute(
        """
        DELETE FROM flight_segments
        WHERE tail_number = ? AND status IN ('SCHEDULED')
        """,
        (tail,),
    )
    try:
        db.execute("DELETE FROM fleet_cabin_config WHERE tail_number = ?", (tail,))
    except Exception:
        pass
    try:
        db.execute("DELETE FROM fleet WHERE tail_number = ?", (tail,))
    except Exception:
        db.execute(
            "UPDATE fleet SET status = 'AOG', aog_reason = 'CHAPTER11_SEIZURE' WHERE tail_number = ?",
            (tail,),
        )
    return tail


def _rebuild_payment(remaining: float, weekly_rate: float, weeks: int) -> float:
    return amortizing_payment(remaining, weekly_rate, max(1, int(weeks)))


def check_bankruptcy(game_week: int) -> Optional[Dict[str, Any]]:
    """Chapter 11 if cash negative/zero-streak of 3 weeks with debt outstanding."""
    gw = int(game_week)
    al = get_airline()
    if not al:
        return None
    cash = float(al["cash"] or 0.0)
    debt = sync_total_debt()
    streak = int(al.get("negative_cash_weeks") or 0)
    last_ch11 = al.get("last_chapter11_week")
    cooldown = 26
    if last_ch11 is not None and (gw - int(last_ch11)) < cooldown:
        if cash < 0:
            streak += 1
        else:
            streak = 0
        db.execute("UPDATE airline SET negative_cash_weeks = ? WHERE id = 1", (streak,))
        return None
    if cash < 0:
        streak += 1
    else:
        streak = 0
    db.execute("UPDATE airline SET negative_cash_weeks = ? WHERE id = 1", (streak,))
    if streak < 3 or debt <= 0:
        return None

    loans = db.fetch_all("SELECT * FROM loans WHERE status = 'ACTIVE'")
    for raw in loans or []:
        remaining = float(raw["principal_remaining"] or 0.0) * 0.5
        weeks = int(raw["weeks_remaining"] or 0)
        rate = float(raw["weekly_interest_rate"] or 0.0)
        lid = str(raw["loan_id"])
        if remaining < 1.0 or weeks <= 0:
            db.execute(
                "UPDATE loans SET principal_remaining = 0, weeks_remaining = 0, status = 'CLOSED' WHERE loan_id = ?",
                (lid,),
            )
            continue
        pmt = _rebuild_payment(remaining, rate, weeks)
        db.execute(
            """
            UPDATE loans
            SET principal_remaining = ?, weekly_payment = ?
            WHERE loan_id = ?
            """,
            (remaining, pmt, lid),
        )
    seized = _seize_costliest_owned()
    db.execute(
        "UPDATE airline SET credit_score = ?, negative_cash_weeks = 0, last_chapter11_week = ? WHERE id = 1",
        (350, gw),
    )
    _record_credit_event(
        "BANKRUPTCY",
        0,
        f"Chapter 11 week {gw}: 50% debt forgiven"
        + (f", seized {seized}" if seized else ", no owned aircraft to seize")
        + ".",
        apply_delta=False,
    )
    sync_total_debt()
    msg = (
        f"Chapter 11: half of remaining debt forgiven"
        + (f"; {seized} seized" if seized else "")
        + ". Credit reset to 350."
    )
    _notify(msg, gw)
    return {"chapter11": True, "seized_tail": seized, "debt": sync_total_debt(), "credit_score": 350}
