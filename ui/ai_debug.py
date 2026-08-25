"""
AI observability commands (PDF Part 9):
- ai_debug <id>
- ai_candidates <id>
- ai_log <id> [N]
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db


def _ai_debug_one(console: Console, cid: str) -> None:
    c = db.fetch_one("SELECT * FROM competitors WHERE competitor_id = ?", (cid,))
    if not c:
        console.print(f"[red]No competitor '{cid}'.[/red]\n")
        return
    console.print(
        f"\n[bold]{c['name']}[/bold] ({cid}) — strategy {c['strategy']} · stance {c['stance'] if 'stance' in c.keys() else 'GROW'} · "
        f"cash ${float(c['cash'] or 0):,.0f} · "
        f"fleet {int(c['fleet_size'] or 0)}/{int(c['max_fleet_size'] or 0)} · rep {float(c['reputation'] or 0):.0f}\n"
    )
    gates = db.fetch_all(
        """
        SELECT airport_iata, gate_units, effective_week, below_threshold_weeks
        FROM airport_gate_allocations
        WHERE holder_id = ? AND status = 'ACTIVE'
        ORDER BY airport_iata
        """,
        (cid,),
    )
    gt = Table(title="Gate holdings", show_lines=False)
    gt.add_column("Airport", style="cyan", width=8)
    gt.add_column("Units", justify="right", width=6)
    gt.add_column("Effective", justify="right", width=10)
    gt.add_column("Below wks", justify="right", width=10)
    if not gates:
        console.print("[dim]No gate allocations.[/dim]\n")
    else:
        for row in gates:
            gt.add_row(
                str(row["airport_iata"]),
                str(int(row["gate_units"] or 0)),
                str(int(row["effective_week"] or 1)),
                str(int(row["below_threshold_weeks"] or 0)),
            )
        console.print(gt)
        console.print()

    try:
        from engine.gates import current_game_week
        from engine.slots import clock_hour_label, list_slot_airport_rows, seed_slot_controlled_airports

        seed_slot_controlled_airports()
        gw = current_game_week()
        st = Table(title="Slot movements (this week)", show_lines=False)
        st.add_column("Airport", style="cyan", width=8)
        st.add_column("This AI", justify="right", width=8)
        st.add_column("Airport peak", justify="right", width=14)
        st.add_column("Cap/hr", justify="right", width=7)
        shown = False
        for row in list_slot_airport_rows(gw, holder_id=cid):
            if int(row["used"] or 0) <= 0:
                continue
            shown = True
            peak = "—"
            if int(row["peak_hour"]) >= 0:
                peak = f"{int(row['peak_movements'])}/{int(row['cap'])} {clock_hour_label(int(row['peak_hour']))}"
            st.add_row(str(row["iata"]), str(int(row["used"])), peak, str(int(row["cap"])))
        if shown:
            console.print(st)
            console.print()
    except Exception:
        pass

    r = db.fetch_all(
        """
        SELECT route_pair_id, status, frequency_per_week, fare_leisure, fare_business,
               estimated_weekly_profit, actual_weekly_net_avg, actual_lf_avg,
               market_share, contested, fare_war_weeks
        FROM competitor_routes
        WHERE competitor_id = ?
        ORDER BY estimated_weekly_profit DESC
        """,
        (cid,),
    )
    t = Table(title="Routes", show_lines=False)
    t.add_column("Pair", width=10, style="cyan")
    t.add_column("Status", width=10)
    t.add_column("Freq", justify="right", width=4)
    t.add_column("Fare L/B", justify="right", width=14)
    t.add_column("Paper $", justify="right", width=10)
    t.add_column("Flown $", justify="right", width=10)
    t.add_column("LF", justify="right", width=5)
    t.add_column("Share", justify="right", width=6)
    t.add_column("War", justify="right", width=4)
    t.add_column("Cont", justify="right", width=4)
    for row in r[:25]:
        t.add_row(
            str(row["route_pair_id"]),
            str(row["status"]),
            str(row["frequency_per_week"]),
            f"{float(row['fare_leisure']):.0f}/{float(row['fare_business']):.0f}",
            f"{float(row['estimated_weekly_profit']):,.0f}",
            f"{float(row['actual_weekly_net_avg'] or 0):,.0f}",
            f"{float(row['actual_lf_avg'] or 0):.2f}",
            f"{float(row['market_share'] or 0):.2f}",
            str(int(row["fare_war_weeks"] or 0)),
            str(int(row["contested"] or 0)),
        )
    console.print(t)
    skipped = db.fetch_one(
        """
        SELECT route_pair_id, score, rejection_reason
        FROM ai_route_candidates
        WHERE competitor_id = ? AND COALESCE(rejection_reason, '') != ''
          AND decision != 'OPENED'
        ORDER BY score DESC
        LIMIT 1
        """,
        (cid,),
    )
    if skipped:
        console.print(
            f"[dim]Top rejected:[/dim] {skipped['route_pair_id']} "
            f"score {float(skipped['score']):.2f} — {skipped['rejection_reason']}\n"
        )
    console.print()


def _ai_candidates(console: Console, cid: str) -> None:
    rows = db.fetch_all(
        """
        SELECT route_pair_id, score, estimated_weekly_profit, estimated_market_share,
               decision, rejection_reason
        FROM ai_route_candidates
        WHERE competitor_id = ?
        ORDER BY score DESC
        LIMIT 40
        """,
        (cid,),
    )
    if not rows:
        console.print("[dim]No candidates yet.[/dim]\n")
        return
    t = Table(title=f"AI Candidates — {cid}", show_lines=False)
    t.add_column("Pair", style="cyan", width=10)
    t.add_column("Score", justify="right", width=6)
    t.add_column("Profit", justify="right", width=10)
    t.add_column("Share", justify="right", width=6)
    t.add_column("Decision", width=14)
    t.add_column("Why", width=16)
    for r in rows:
        t.add_row(
            str(r["route_pair_id"]),
            f"{float(r['score']):.2f}",
            f"{float(r['estimated_weekly_profit']):,.0f}",
            f"{float(r['estimated_market_share']):.2f}",
            str(r["decision"]),
            str(r["rejection_reason"] or "—"),
        )
    console.print(t)
    console.print()


def _ai_log(console: Console, cid: str, n: int = 8) -> None:
    rows = db.fetch_all(
        """
        SELECT game_week, duration_ms, error, narrative
        FROM ai_turn_log
        WHERE competitor_id = ?
        ORDER BY game_week DESC
        LIMIT ?
        """,
        (cid, int(n)),
    )
    t = Table(title=f"AI Turn Log — {cid}", show_lines=False)
    t.add_column("Week", justify="right", width=6)
    t.add_column("ms", justify="right", width=8)
    t.add_column("Error", width=28)
    t.add_column("Narrative", width=70)
    for r in rows:
        t.add_row(
            str(r["game_week"]),
            f"{float(r['duration_ms'] or 0):.0f}",
            str(r["error"] or ""),
            str(r["narrative"] or ""),
        )
    console.print(t)
    console.print()


def handle_ai_debug_cli(console: Console, raw: str) -> None:
    parts = raw.strip().split()
    cmd = parts[0].lower() if parts else ""
    if cmd == "ai_debug":
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] ai_debug <AI_MERIDIAN|AI_SOLARA|AI_VANTAGE|AI_CRESTLINE>\n")
            return
        _ai_debug_one(console, parts[1].upper())
        return
    if cmd == "ai_candidates":
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] ai_candidates <AI_ID>\n")
            return
        _ai_candidates(console, parts[1].upper())
        return
    if cmd == "ai_log":
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] ai_log <AI_ID> [N]\n")
            return
        n = int(parts[2]) if len(parts) >= 3 else 8
        _ai_log(console, parts[1].upper(), n)
        return
    console.print("[yellow]Unknown AI command.[/yellow]\n")

