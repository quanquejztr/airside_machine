"""
Phase 8 — Fuel market ticks, weekly OHLC history, shocks, hedging, and reserves.
"""

from __future__ import annotations

import math
import random
import sqlite3
import threading
from typing import Any, Dict, Optional, Tuple

from db import db
from engine.setup import get_airline, update_cash

# Barrel $/bbl — hard bounds so ticks/shocks cannot explode (tick runs ~1× per game hour, not per real second).
BARREL_MIN = 35.0
BARREL_MAX = 450.0

_ohlc_lock = threading.Lock()
# game_week -> {open, high, low, last}
_week_ohlc: Dict[int, Dict[str, float]] = {}
_dip_alert_fired: set[int] = set()
# Apply stochastic fuel move at most once per integer game hour (avoids compounding every real-time second).
_last_fuel_tick_game_hour: Optional[int] = None


def _tax_per_gallon() -> float:
    v = db.get_financial_constant("fuel_tax_per_gallon")
    return float(v) if v is not None else 0.043


def barrel_to_all_in_per_gallon(barrel: float) -> float:
    return float(barrel) / 42.0 + _tax_per_gallon()


def all_in_spot_from_state() -> float:
    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    b = float(gs["fuel_price_current"] or 195.0) if gs else 195.0
    b = _clamp_barrel(b)
    return barrel_to_all_in_per_gallon(b)


def _base_barrel_price() -> float:
    v = db.get_financial_constant("fuel_base_price_bbl")
    return float(v) if v is not None else 195.0


def _clamp_barrel(p: float) -> float:
    if not math.isfinite(p):
        return _base_barrel_price()
    return max(BARREL_MIN, min(BARREL_MAX, p))


def sanitize_fuel_price_in_db() -> None:
    """Reset absurd or corrupted barrel prices (e.g. from old per-second compounding)."""
    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    if not gs:
        return
    p = float(gs["fuel_price_current"] or _base_barrel_price())
    if not math.isfinite(p) or p < BARREL_MIN or p > BARREL_MAX:
        base = _base_barrel_price()
        db.execute(
            "UPDATE game_state SET fuel_price_current = ? WHERE id = 1",
            (_clamp_barrel(base),),
        )


def tick_fuel_price(game_hours_elapsed: float, speed_multiplier: int) -> None:
    """
    Random walk on barrel price (~once per simulated game hour); track weekly OHLC.
    No-op when paused.

    Note: The game clock calls on_tick every *real* second. We must NOT compound fuel
    every second or the price explodes; we only step when int(game_hours_elapsed) advances.
    """
    global _last_fuel_tick_game_hour
    if speed_multiplier == 0:
        return
    sanitize_fuel_price_in_db()
    gs = db.fetch_one("SELECT fuel_price_current, fuel_price_trend FROM game_state WHERE id = 1")
    if not gs:
        return
    hi = int(math.floor(float(game_hours_elapsed)))
    if _last_fuel_tick_game_hour is None:
        _last_fuel_tick_game_hour = hi
        cur0 = _clamp_barrel(float(gs["fuel_price_current"] or _base_barrel_price()))
        db.execute(
            "UPDATE game_state SET fuel_price_current = ? WHERE id = 1",
            (cur0,),
        )
        cal_week = int(float(game_hours_elapsed) // 168) + 1
        with _ohlc_lock:
            d = _week_ohlc.setdefault(cal_week, {})
            if "open" not in d:
                d["open"] = cur0
                d["high"] = cur0
                d["low"] = cur0
            d["last"] = cur0
        return
    if hi <= _last_fuel_tick_game_hour:
        return
    _last_fuel_tick_game_hour = hi

    cur = _clamp_barrel(float(gs["fuel_price_current"] or _base_barrel_price()))
    tr = float(gs["fuel_price_trend"] or 0.0)
    tr = max(-0.02, min(0.02, tr))
    noise = random.gauss(0.0, 0.002)
    new_price = _clamp_barrel(cur * (1.0 + tr + noise))
    db.execute(
        "UPDATE game_state SET fuel_price_current = ? WHERE id = 1",
        (new_price,),
    )
    cal_week = int(float(game_hours_elapsed) // 168) + 1
    with _ohlc_lock:
        d = _week_ohlc.setdefault(cal_week, {})
        if "open" not in d:
            d["open"] = new_price
            d["high"] = new_price
            d["low"] = new_price
        else:
            d["high"] = max(d["high"], new_price)
            d["low"] = min(d["low"], new_price)
        d["last"] = new_price
    _check_dip_alert(cal_week, new_price)


def _check_dip_alert(calendar_week: int, barrel_price: float) -> None:
    al = get_airline()
    if not al:
        return
    thr = al.get("fuel_dip_alert_price")
    if thr is None:
        return
    try:
        t = float(thr)
    except (TypeError, ValueError):
        return
    if barrel_price >= t:
        return
    if calendar_week in _dip_alert_fired:
        return
    _dip_alert_fired.add(calendar_week)
    try:
        from engine.news_feed import push_news

        push_news(
            f"Fuel dip alert: ${barrel_price:.2f}/bbl below your ${t:.2f} threshold (buy reserve / hedge in menu 17)"
        )
    except Exception:
        pass


def estimated_weekly_burn_gallons() -> float:
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"]) if gs else 1
    if gw <= 1:
        return 100_000.0
    try:
        row = db.fetch_one(
            """
            SELECT COALESCE(SUM(fuel_burned_gallons), 0) AS g
            FROM flight_segments
            WHERE game_week = ? AND status = 'LANDED'
            """,
            (gw - 1,),
        )
    except sqlite3.OperationalError:
        return 100_000.0
    g = float(row["g"] or 0) if row else 0.0
    return g if g > 5000 else 100_000.0


def hedge_fuel(weeks: int) -> Dict[str, Any]:
    if weeks not in (2, 4, 8):
        raise ValueError("Hedge weeks must be 2, 4, or 8.")
    al = get_airline()
    if not al:
        raise ValueError("No airline.")
    if al.get("fuel_hedged_weeks_remaining"):
        raise ValueError("Cancel existing hedge first (cancel_hedge).")
    spot_gal = all_in_spot_from_state()
    burn = estimated_weekly_burn_gallons()
    prem_rate = float(db.get_financial_constant("fuel_hedge_premium_rate") or 0.05)
    premium = burn * spot_gal * weeks * prem_rate
    cash = float(al["cash"] or 0)
    if cash < premium:
        raise ValueError(f"Need ${premium:,.2f} premium; cash ${cash:,.2f}.")
    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    barrel = float(gs["fuel_price_current"] or 195.0) if gs else 195.0
    update_cash(-premium)
    db.execute(
        """
        UPDATE airline SET fuel_hedged_price = ?, fuel_hedged_weeks_remaining = ?
        WHERE id = 1
        """,
        (barrel, weeks),
    )
    try:
        from engine.news_feed import push_news

        push_news(
            f"Hedge {weeks}wk @ ${barrel:.2f}/bbl — premium ${premium:,.0f} paid"
        )
    except Exception:
        pass
    return {"weeks": weeks, "premium": premium, "hedged_barrel": barrel}


def cancel_hedge() -> None:
    al = get_airline()
    if not al:
        raise ValueError("No airline.")
    db.execute(
        """
        UPDATE airline SET fuel_hedged_price = NULL, fuel_hedged_weeks_remaining = NULL
        WHERE id = 1
        """
    )
    try:
        from engine.news_feed import push_news

        push_news("Fuel hedge cancelled (no premium refund).")
    except Exception:
        pass


def buy_reserve(gallons: float) -> Dict[str, Any]:
    if gallons <= 0:
        raise ValueError("Gallons must be positive.")
    cap = float(db.get_financial_constant("fuel_reserve_cap_gallons") or 500_000)
    al = get_airline()
    if not al:
        raise ValueError("No airline.")
    cur_res = float(al.get("fuel_reserve_gallons") or 0)
    if cur_res + gallons > cap + 1e-6:
        raise ValueError(f"Reserve cap {cap:,.0f} gal (have {cur_res:,.0f}).")
    spot_gal = all_in_spot_from_state()
    cost = gallons * spot_gal
    update_cash(-cost)
    old_g = cur_res
    new_g = old_g + gallons
    old_avg = al.get("fuel_reserve_avg_price")
    try:
        oa = float(old_avg) if old_avg is not None else None
    except (TypeError, ValueError):
        oa = None
    if old_g <= 0 or oa is None:
        new_avg = spot_gal
    else:
        new_avg = ((old_g * oa) + (gallons * spot_gal)) / new_g
    db.execute(
        """
        UPDATE airline SET fuel_reserve_gallons = ?, fuel_reserve_avg_price = ?
        WHERE id = 1
        """,
        (new_g, new_avg),
    )
    try:
        from engine.news_feed import push_news

        push_news(f"Bought {gallons:,.0f} gal reserve @ ${spot_gal:.3f}/gal (${cost:,.0f})")
    except Exception:
        pass
    return {"gallons": new_g, "avg_price": new_avg, "cost": cost}


def resolve_fuel_cost(gallons_burned: float, spot_all_in_per_gallon: float) -> Tuple[float, float]:
    """
    Pay for fuel: draw reserve inventory first (at stored avg $/gal), remainder at hedge or spot.
    Updates airline fuel_reserve_gallons. Returns (total_usd, effective_avg_per_gallon).
    """
    al = get_airline()
    if not al or gallons_burned <= 0:
        return 0.0, 0.0
    reserve = float(al.get("fuel_reserve_gallons") or 0)
    avg_stored = al.get("fuel_reserve_avg_price")
    try:
        avg = float(avg_stored) if avg_stored is not None else None
    except (TypeError, ValueError):
        avg = None
    take_r = min(gallons_burned, reserve)
    cost = 0.0
    price_used = spot_all_in_per_gallon
    if take_r > 0:
        pu = avg if avg is not None else spot_all_in_per_gallon
        cost += take_r * pu
        price_used = pu
        new_res = reserve - take_r
        db.execute(
            "UPDATE airline SET fuel_reserve_gallons = ? WHERE id = 1",
            (new_res,),
        )
    rem = gallons_burned - take_r
    if rem > 0:
        hedged_b = al.get("fuel_hedged_price")
        hed_w = al.get("fuel_hedged_weeks_remaining")
        if hedged_b is not None and hed_w is not None and int(hed_w) > 0:
            hp = barrel_to_all_in_per_gallon(float(hedged_b))
            cost += rem * hp
            price_used = hp
        else:
            cost += rem * spot_all_in_per_gallon
            price_used = spot_all_in_per_gallon
    eff = cost / gallons_burned if gallons_burned > 0 else 0.0
    return cost, eff


def write_fuel_price_history_and_shock(completed_game_week: int) -> None:
    """
    At week boundary: persist OHLC for completed week (close = last tick before shock),
    then optional shock moves spot for the new week.
    """
    week = int(completed_game_week)
    already = db.fetch_one(
        "SELECT game_week FROM fuel_price_history WHERE game_week = ?",
        (week,),
    )
    if already:
        return

    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    fallback = _clamp_barrel(float(gs["fuel_price_current"] or _base_barrel_price()) if gs else _base_barrel_price())
    with _ohlc_lock:
        d = _week_ohlc.pop(week, None)
    if d:
        o = _clamp_barrel(float(d.get("open", fallback)))
        h = _clamp_barrel(float(d.get("high", fallback)))
        lo = _clamp_barrel(float(d.get("low", fallback)))
        cl = _clamp_barrel(float(d.get("last", fallback)))
    else:
        o = h = lo = cl = fallback

    shock_txt: Optional[str] = None
    newp: Optional[float] = None
    if random.random() < 0.02:
        m = random.uniform(0.05, 0.15)
        sign = random.choice([-1, 1])
        newp = _clamp_barrel(cl * (1.0 + sign * m))
        shock_txt = f"{'+' if sign > 0 else '-'}{m*100:.1f}% shock"

    db.execute(
        """
        INSERT OR REPLACE INTO fuel_price_history
        (game_week, open_price, high_price, low_price, close_price, shock_event)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (week, o, h, lo, cl, shock_txt),
    )

    if newp is not None:
        db.execute(
            """
            UPDATE game_state SET fuel_price_current = ?, fuel_shock_pending = 1, fuel_shock_message = ?
            WHERE id = 1
            """,
            (newp, shock_txt),
        )
        try:
            from engine.clock import get_global_clock

            clk = get_global_clock()
            if clk:
                clk.request_auto_pause(
                    f"Fuel shock {shock_txt} — ${cl:.2f} → ${newp:.2f}/bbl (menu 17 ack)"
                )
        except Exception:
            pass
        try:
            from engine.news_feed import push_news

            push_news(f"FUEL SHOCK: {shock_txt} — ${cl:.2f} → ${newp:.2f}/bbl")
        except Exception:
            pass

    global _dip_alert_fired
    _dip_alert_fired = set()


def tick_down_hedge_after_settlement() -> None:
    """Decrement hedge weeks once per settled week; clear when expired."""
    al = get_airline()
    if not al:
        return
    rem = al.get("fuel_hedged_weeks_remaining")
    if rem is None:
        return
    r = int(rem)
    if r <= 1:
        db.execute(
            """
            UPDATE airline SET fuel_hedged_price = NULL, fuel_hedged_weeks_remaining = NULL
            WHERE id = 1
            """
        )
    else:
        db.execute(
            "UPDATE airline SET fuel_hedged_weeks_remaining = ? WHERE id = 1",
            (r - 1,),
        )


def acknowledge_fuel_shock() -> None:
    db.execute(
        "UPDATE game_state SET fuel_shock_pending = 0, fuel_shock_message = NULL WHERE id = 1"
    )
    try:
        from engine.clock import get_global_clock

        clk = get_global_clock()
        if clk:
            clk.clear_auto_pause_alert()
    except Exception:
        pass


def set_dip_alert(price: Optional[float]) -> None:
    if price is not None and price <= 0:
        raise ValueError("Alert price must be positive.")
    if price is None:
        db.execute("UPDATE airline SET fuel_dip_alert_price = NULL WHERE id = 1")
    else:
        db.execute("UPDATE airline SET fuel_dip_alert_price = ? WHERE id = 1", (float(price),))


def debug_force_shock() -> None:
    """Test helper: always applies a positive shock."""
    gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
    close_px = _clamp_barrel(float(gs["fuel_price_current"] or _base_barrel_price()) if gs else _base_barrel_price())
    m = 0.10
    newp = _clamp_barrel(close_px * (1.0 + m))
    shock_txt = f"+{m*100:.0f}% (debug)"
    db.execute(
        """
        UPDATE game_state SET fuel_price_current = ?, fuel_shock_pending = 1, fuel_shock_message = ?
        WHERE id = 1
        """,
        (newp, shock_txt),
    )
    try:
        from engine.clock import get_global_clock

        clk = get_global_clock()
        if clk:
            clk.request_auto_pause(f"Fuel shock (debug) {shock_txt}")
    except Exception:
        pass
    try:
        from engine.news_feed import push_news

        push_news(f"DEBUG fuel shock ${close_px:.2f} → ${newp:.2f}/bbl")
    except Exception:
        pass
