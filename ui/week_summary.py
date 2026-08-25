"""
Week-end summary UI: queue from settlement worker, optional auto-pause, on-demand view.
"""

from __future__ import annotations

import queue
from typing import Any, Mapping

from db import db
from engine.settlement import build_week_summary_payload, get_week_summary_queue


def get_pause_on_week_summary() -> bool:
    row = db.fetch_one("SELECT pause_on_week_summary FROM game_state WHERE id = 1")
    if not row:
        return False
    return int(row["pause_on_week_summary"] or 0) != 0


def set_pause_on_week_summary(on: bool) -> None:
    db.execute(
        "UPDATE game_state SET pause_on_week_summary = ? WHERE id = 1",
        (1 if on else 0,),
    )


def _fmt_money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _render_week_summary(console, payload: Mapping[str, Any]) -> None:
    if payload.get("skipped"):
        r = payload.get("reason") or payload.get("error") or "unknown"
        console.print(f"[dim]Week summary skipped: {r}[/dim]\n")
        return

    gw = int(payload.get("game_week") or 0)
    src = payload.get("summary_source")
    partial = payload.get("partial_week")
    title = f"Week {gw} — summary"
    if src == "live" or partial:
        title += " [dim](live / in-progress)[/dim]"
    elif src == "ledger":
        title += " [dim](settled)[/dim]"
    console.print(f"\n[bold cyan]{title}[/bold cyan]\n")

    if "revenue_gross" in payload:
        console.print(f"  Revenue (gross):     {_fmt_money(payload.get('revenue_gross'))}")
    cab = payload.get("cabin_revenue")
    if isinstance(cab, dict):
        console.print("  Cabin revenue:")
        for k, v in cab.items():
            console.print(f"    · {k}: {_fmt_money(v)}")

    for label, key in (
        ("Excise tax", "excise_tax"),
        ("Segment fees", "segment_fees"),
        ("Security fees", "security_fees"),
        ("PFC fees", "pfc_fees"),
        ("Landing fees", "landing_fees"),
        ("Gate fees", "gate_fees"),
    ):
        if key in payload:
            console.print(f"  {label + ':':<20} {_fmt_money(payload.get(key))}")

    if "total_taxes_and_fees" in payload:
        console.print(f"  [bold]Taxes & fees:[/bold]    {_fmt_money(payload.get('total_taxes_and_fees'))}")

    if "fuel_cost" in payload:
        console.print(f"  Fuel cost:           {_fmt_money(payload.get('fuel_cost'))}")
    if "lease_costs" in payload:
        console.print(f"  Lease costs:         {_fmt_money(payload.get('lease_costs'))}")
    if "loan_payments" in payload:
        console.print(f"  Loan payments:       {_fmt_money(payload.get('loan_payments'))}")
    if "corporate_tax" in payload:
        console.print(f"  Corporate tax:       {_fmt_money(payload.get('corporate_tax'))}")
    if "net_income" in payload:
        console.print(f"  [bold]Net income:[/bold]       {_fmt_money(payload.get('net_income'))}")

    if "cash_start" in payload:
        console.print(f"  Cash (start):        {_fmt_money(payload.get('cash_start'))}")
    lbl = payload.get("cash_row_label") or "Cash (end)"
    if "cash_end_of_week" in payload:
        console.print(f"  {lbl + ':':<20} {_fmt_money(payload.get('cash_end_of_week'))}")

    fc = payload.get("flights_count")
    if fc is not None:
        console.print(f"  Flights in rollup:   {int(fc)}")

    prev = payload.get("prev_week")
    if isinstance(prev, dict) and prev:
        console.print(
            f"\n  [dim]Prior week — net {_fmt_money(prev.get('net_income'))}, "
            f"cash EOW {_fmt_money(prev.get('cash_end_of_week'))}, "
            f"revenue {_fmt_money(prev.get('revenue_gross'))}[/dim]"
        )

    events = payload.get("event_log") or []
    if events:
        console.print("\n  [bold]Event log (this week)[/bold]")
        for ev in events[:12]:
            et = ev.get("event_type", "")
            msg = (ev.get("message") or "")[:120]
            console.print(f"    · [{et}] {msg}")
        if len(events) > 12:
            console.print(f"    [dim]… {len(events) - 12} more[/dim]")

    sp = payload.get("spawn")
    if isinstance(sp, dict) and sp.get("inserted") is not None:
        tails = sp.get("tails") or []
        console.print(
            f"\n  [dim]Next week segments spawned: {int(sp['inserted'])} rows "
            f"({len(tails)} tail(s))[/dim]"
        )

    nc = payload.get("new_calendar_week")
    if nc is not None:
        console.print(f"  [dim]Calendar advanced to week {int(nc)}.[/dim]")

    console.print("")


def try_show_pending_week_summary(console) -> None:
    """Drain non-blocking settlement queue; optionally pause clock; print each summary."""
    _quiet_skip = frozenset(
        {"already settled", "no completed week", "invalid week", "no airline"}
    )
    q = get_week_summary_queue()
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if not isinstance(item, dict):
            continue
        if item.get("skipped"):
            if item.get("error"):
                console.print(f"[yellow]Week rollover: {item['error']}[/yellow]\n")
            elif str(item.get("reason") or "") not in _quiet_skip:
                console.print(f"[dim]Week summary skipped: {item.get('reason')}[/dim]\n")
            continue
        if get_pause_on_week_summary():
            try:
                from engine.clock import get_global_clock

                clk = get_global_clock()
                if clk is not None and clk.is_alive():
                    clk.pause(player_initiated=False)
            except Exception:
                pass
        _render_week_summary(console, item)


def show_week_summary_interactive(console) -> None:
    """Prompt for a game week and show ledger or live snapshot (no settlement)."""
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    default_gw = int(gs["game_week"]) if gs else 1
    raw = input(f"Game week to summarize [default {default_gw}, Enter]: ").strip()
    try:
        gw = int(raw) if raw else default_gw
    except ValueError:
        console.print("[red]Invalid week number.[/red]\n")
        return
    payload = build_week_summary_payload(gw)
    _render_week_summary(console, payload)
