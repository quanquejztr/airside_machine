"""Phase 8 — fuel price ticker line and weekly close sparkline."""

from __future__ import annotations

from db import db


def format_fuel_ticker_line() -> str:
    """One line: current barrel vs this week's open (from fuel_price_history if present)."""
    gs = db.fetch_one("SELECT fuel_price_current, game_week FROM game_state WHERE id = 1")
    if not gs:
        return ""
    cur = float(gs["fuel_price_current"] or 195.0)
    gw = int(gs["game_week"] or 1)
    row = db.fetch_one(
        "SELECT open_price FROM fuel_price_history WHERE game_week = ?",
        (gw,),
    )
    op = float(row["open_price"]) if row else cur
    delta = cur - op
    pct = (delta / op * 100.0) if op else 0.0
    arrow = "↑" if delta >= 0 else "↓"
    return f"Fuel {arrow} ${cur:.2f}/bbl · week open ${op:.2f} ({pct:+.2f}%)"


def fuel_sparkline(cols: int = 20) -> str:
    """ASCII sparkline of last N weekly closes (oldest → newest)."""
    rows = db.fetch_all(
        """
        SELECT close_price FROM fuel_price_history
        ORDER BY game_week DESC LIMIT ?
        """,
        (cols,),
    )
    if not rows:
        return "(no weekly history yet — play through a settlement)"
    vals = [float(r["close_price"]) for r in reversed(rows)]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    parts = []
    for v in vals:
        idx = int((v - lo) / span * (len(blocks) - 1))
        parts.append(blocks[idx])
    return "".join(parts)
