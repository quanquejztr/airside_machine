"""
Phase 6 — KPI dashboard & route analytics UI (Rich).

Does not pause the game clock; safe to call from the clock submenu.
"""

from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from engine import analytics


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{100.0 * x:.1f}%"


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def render_kpi_summary(console: Console) -> None:
    data = analytics.kpi_summary()
    if data.get("error"):
        console.print(f"[red]{data['error']}[/red]\n")
        return

    cw = data["current_week"]
    live = data["current_week_live"]
    tr = data["trends_vs_last_closed_week"]

    t = Table(title=f"KPI summary — game week {cw} (live ops)", box=None)
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right")
    t.add_column("vs last closed", justify="center")

    t.add_row(
        "RASM (revenue / seat-mile)",
        f"{live['rasm'] * 100:.2f}¢",
        tr.get("rasm", "—"),
    )
    t.add_row(
        "CASM (variable cost / seat-mile)",
        f"{live['casm'] * 100:.2f}¢",
        tr.get("casm", "—"),
    )
    otp = live["on_time_rate"]
    t.add_row(
        "On-time arrivals (landed legs)",
        _fmt_pct(otp) if otp is not None else "—",
        tr.get("on_time_rate", "—"),
    )
    t.add_row(
        "Gross revenue (week-to-date)",
        _fmt_money(live["revenue_gross"]),
        "—",
    )
    t.add_row(
        "Net income (after settlement)",
        "—",
        "when week closes",
    )
    t.add_row("Cash", _fmt_money(live["cash"]), "—")
    t.add_row(
        "Flights landed / in air",
        f"{live['flights_completed']} / {live['flights_in_progress']}",
        "—",
    )

    console.print(t)
    console.print(
        "[dim]Gross revenue above is from flights departed this week (live). "
        "Net income is not finalized until the game clock finishes the week and "
        "[bold]settlement[/bold] posts to [bold]week_ledger[/bold] (leases, taxes, fuel at landing, etc.). "
        "Use menu 14 Week Summary (live) for an estimated P&L before the week closes.[/dim]\n"
    )
    console.print(
        f"[dim]Net margin WoW (closed weeks only): {tr.get('net_margin_closed_wow', '—')}[/dim]\n"
    )

    led = data.get("ledger_weeks") or []
    if led:
        lt = Table(title="Last closed weeks (settlement → week_ledger)", show_lines=False)
        lt.add_column("Week", justify="right")
        lt.add_column("Net margin", justify="right")
        lt.add_column("RASM ¢", justify="right")
        lt.add_column("CASM ¢", justify="right")
        lt.add_column("OTP", justify="right")
        lt.add_column("Net income", justify="right")
        for row in led:
            m = row.get("net_margin")
            otp2 = row.get("on_time_rate")
            lt.add_row(
                str(row["game_week"]),
                _fmt_pct(m) if m is not None else "—",
                f"{row['rasm'] * 100:.2f}",
                f"{row['casm'] * 100:.2f}",
                _fmt_pct(otp2) if otp2 is not None else "—",
                _fmt_money(float(row["net_income"] or 0)),
            )
        console.print(lt)
        console.print(
            "[dim]Net income here is the figure written at week-end settlement. "
            "$0.00 means that week’s ledger net was zero (e.g. no landed revenue in that week, "
            "or costs matched revenue). RASM/CASM columns use [bold]LANDED[/bold] legs only.[/dim]\n"
        )


def render_route_card(console: Console, route_id: str) -> None:
    d = analytics.route_card(route_id)
    if d.get("error"):
        console.print(f"[red]{d['error']}[/red]\n")
        return

    title = f"Route card — {d['route_id']} — game week {d['game_week']}"
    info = Text()
    info.append(f"Segments: {d['segments_flown']} flown / {d['segments_scheduled']} scheduled\n", style="white")
    info.append(f"Distance: {d['distance_nm']:.0f} nm\n\n", style="dim")

    info.append("Load factor (flown legs)\n", style="bold")
    info.append(f"  Total: {_fmt_pct(d['load_factor_total'])}  {d['trends']['load_factor_total']}\n", style="green")
    for cls, lf in d["load_factor_class"].items():
        if d["seats_class"].get(cls, 0) <= 0:
            continue
        arr = d["trends_class_lf"].get(cls, "—")
        label = cls.replace("_", " ").title()
        info.append(
            f"  {label}: {_fmt_pct(lf)}  {arr}  "
            f"({d['pax_class'][cls]} / {d['seats_class'][cls]} pax)\n",
            style="yellow",
        )

    info.append("\nUnit economics\n", style="bold")
    info.append(
        f"  RASM: {d['rasm'] * 100:.2f}¢ / seat-mi   {d['trends']['rasm']}\n"
        f"  CASM (variable): {d['casm'] * 100:.2f}¢ / seat-mi   {d['trends']['casm']}\n"
        f"  Yield / RPM: {_fmt_money(d['yield_per_rpm'])}  {d['trends']['yield_per_rpm']}\n"
        f"  Revenue / EEC unit: {_fmt_money(d['revenue_per_eec'])}  {d['trends']['revenue_per_eec']}\n",
        style="white",
    )
    info.append(
        f"  Gross revenue: {_fmt_money(d['revenue_gross'])}  {d['trends']['revenue_gross']}\n",
        style="magenta",
    )

    otp = d.get("on_time_rate")
    info.append(f"\nOn-time (landed): {_fmt_pct(otp) if otp is not None else '—'}\n", style="cyan")

    rb = d.get("remaining_market_leisure")
    rbb = d.get("remaining_market_business")
    if rb is not None and rbb is not None:
        info.append(
            f"\nWeekly market not yet on your aircraft (forecast − carried)\n"
            f"  Leisure: {rb} pax · Business: {rbb} pax\n",
            style="cyan",
        )
    info.append(
        f"\nPer-leg spill (sum of modeled leisure/business overflow per departure)\n"
        f"  Leisure funnel: {d['spill_leisure_market']} pax\n"
        f"  Business funnel: {d['spill_business_market']} pax\n",
        style="red",
    )

    console.print(Panel(info, title=title, border_style="blue"))
    if d.get("reconfig_hint"):
        console.print(Panel(Text(d["reconfig_hint"], style="bold yellow"), title="Cabin mix hint"))
    console.print()


def render_fleet_utilization(console: Console) -> None:
    fu = analytics.fleet_utilization()
    if fu.get("error"):
        console.print(f"[red]{fu['error']}[/red]\n")
        return

    t = Table(title=f"Fleet utilization — game week {fu['game_week']}")
    t.add_column("Tail")
    t.add_column("Type")
    t.add_column("Airborne h", justify="right")
    t.add_column("Cap h", justify="right")
    t.add_column("Hours %", justify="right")
    t.add_column("Cabin fill", justify="right")
    t.add_column("Flights", justify="right")

    for row in fu["tails"]:
        t.add_row(
            row["tail_number"],
            row["type_id"],
            f"{row['airborne_hours']:.1f}",
            f"{row['cap_hours']:.1f}",
            f"{row['hours_utilization_pct']:.1f}%",
            _fmt_pct(row["cabin_fill_rate"]),
            str(row["flights_flown"]),
        )
    console.print(t)
    console.print()


def render_compare_routes(console: Console, a: str, b: str) -> None:
    cmp = analytics.compare_routes(a, b)
    console.print("[bold]Side-by-side route cards[/bold]\n")
    for label, key in (("A", "a"), ("B", "b")):
        d = cmp.get(key) or {}
        if d.get("error"):
            console.print(f"[red]{label}: {d['error']}[/red]\n")
            continue
        console.print(f"[bold cyan]{label}: {d.get('route_id')}[/bold cyan]")
        console.print(
            f"  LF total {_fmt_pct(d.get('load_factor_total'))}  "
            f"RASM {d.get('rasm', 0) * 100:.2f}¢  CASM {d.get('casm', 0) * 100:.2f}¢  "
            f"Rev/EEC {_fmt_money(d.get('revenue_per_eec', 0))}\n"
        )
    console.print()


def render_compare_fleet(console: Console, t1: str, t2: str) -> None:
    c = analytics.compare_fleet(t1, t2)
    if c.get("error"):
        console.print(f"[red]{c['error']}[/red]\n")
        return
    for label, row in (("A", c.get("tail_a")), ("B", c.get("tail_b"))):
        if not row:
            console.print(f"[yellow]{label}: tail not found[/yellow]\n")
            continue
        console.print(
            f"[bold]{label} {row['tail_number']}[/bold] ({row['type_id']}) — "
            f"airborne {row['airborne_hours']:.1f} / {row['cap_hours']:.1f} h "
            f"({row['hours_utilization_pct']:.1f}%), "
            f"cabin fill {_fmt_pct(row['cabin_fill_rate'])}, "
            f"flights {row['flights_flown']}\n"
        )
    console.print()


def render_full_dashboard(console: Console) -> None:
    console.print("\n[bold magenta]══ KPI Dashboard (Phase 6) ══[/bold magenta]\n")
    render_kpi_summary(console)
    render_fleet_utilization(console)


def run_dashboard_cli(console: Console) -> None:
    console.print(
        "\n[dim]Analytics — commands: dashboard | stats | route <ID> | "
        "compare_routes <A> <B> | compare_fleet <T1> <T2> | fleet | help | back[/dim]\n"
    )
    while True:
        line = input("analytics> ").strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        if cmd in ("back", "q", "quit", "exit"):
            break
        if cmd == "help":
            console.print(
                "[cyan]dashboard[/cyan] — KPI + fleet\n"
                "[cyan]stats[/cyan] — KPI summary only\n"
                "[cyan]fleet[/cyan] — fleet table\n"
                "[cyan]route TPA-JFK[/cyan] — route analytics\n"
                "[cyan]compare_routes TPA-JFK JFK-BOS[/cyan]\n"
                "[cyan]compare_fleet SJT-001 SJT-002[/cyan]\n"
                "[cyan]back[/cyan] — return\n"
            )
            continue
        if cmd == "dashboard":
            render_full_dashboard(console)
            continue
        if cmd == "stats":
            render_kpi_summary(console)
            continue
        if cmd == "fleet":
            render_fleet_utilization(console)
            continue
        if cmd == "route":
            if len(parts) < 2:
                console.print("[yellow]Usage: route <route_id>[/yellow]\n")
                continue
            render_route_card(console, parts[1])
            continue
        if cmd == "compare_routes":
            if len(parts) < 3:
                console.print("[yellow]Usage: compare_routes <r1> <r2>[/yellow]\n")
                continue
            render_compare_routes(console, parts[1], parts[2])
            continue
        if cmd == "compare_fleet":
            if len(parts) < 3:
                console.print("[yellow]Usage: compare_fleet <t1> <t2>[/yellow]\n")
                continue
            render_compare_fleet(console, parts[1], parts[2])
            continue
        console.print(f"[yellow]Unknown command: {line}[/yellow]\n")
