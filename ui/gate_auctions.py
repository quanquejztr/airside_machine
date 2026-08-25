"""
Airport gate-use auctions (Phase 11 revised).

Commands:
- gate_auctions
- gate_bid <auction_id> <units> <price_per_unit>
- gate_bids
- gates
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db
from engine.gates import list_open_gate_auctions, submit_gate_bid, current_game_week
from engine.scheduling import hhmm_from_absolute_game_hour


def gate_auctions_panel(console: Console) -> None:
    rows = list_open_gate_auctions()
    if not rows:
        console.print("[yellow]No open gate auctions.[/yellow]\n")
        return
    t = Table(title="OPEN GATE AUCTIONS", show_lines=False)
    t.add_column("Auction ID", style="dim", width=38)
    t.add_column("Airport", style="cyan", width=6)
    t.add_column("Opens", justify="right", width=6)
    t.add_column("Closes", justify="right", width=7)
    t.add_column("Avail", justify="right", width=6)
    t.add_column("Current $/unit", justify="right", width=12)
    for a in rows:
        t.add_row(
            str(a["auction_id"]),
            str(a["airport_iata"]),
            str(a["opens_week"]),
            str(a["closes_week"]),
            str(a["units_available"]),
            f"${float(a['current_price_per_unit']):,.0f}",
        )
    console.print(t)
    console.print(
        f"[dim]Week {current_game_week()} · auctions resolve at end-of-week settlement · bid is ascending via price_per_unit. "
        f"Command: [cyan]gate_bid <auction_id> <units> <price_per_unit>[/cyan][/dim]\n"
    )


def gates_panel(console: Console) -> None:
    rows = db.fetch_all(
        """
        SELECT airport_iata, gate_units, used_this_week, scheduled_this_week, effective_week
        FROM airport_gate_allocations
        WHERE holder_id = 'PLAYER' AND status = 'ACTIVE'
        ORDER BY airport_iata
        """
    )
    if not rows:
        console.print("[yellow]You have no gate allocations at auctioned airports.[/yellow]\n")
        return
    t = Table(title="YOUR GATE ALLOCATIONS", show_lines=False)
    t.add_column("Airport", style="cyan", width=6)
    t.add_column("Units", justify="right", width=6)
    t.add_column("Touches", justify="right", width=8)
    t.add_column("Busy h", justify="right", width=7)
    t.add_column("Util", justify="right", width=6)
    t.add_column("Gate gaps (this week)", width=34)
    # Concurrent-gates model: show utilization in gate-hours
    try:
        from engine.gates import _mtt_hours
        mtt = float(_mtt_hours())
    except Exception:
        mtt = 0.5

    def _dow_hhmm(abs_hour: float) -> str:
        # "Mon 08:07" within the current 168h week
        h_in_w = float(abs_hour) % 168.0
        di = int(h_in_w // 24.0)
        dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow = dows[max(0, min(6, di))]
        hhmm = hhmm_from_absolute_game_hour(float(abs_hour))
        return f"{dow} {hhmm}"

    for r in rows:
        u = int(r["gate_units"] or 0)
        ap = str(r["airport_iata"]).upper()
        eff = int(r["effective_week"] or 1)
        gw = current_game_week()
        cnt = db.fetch_one(
            """
            SELECT
              SUM(CASE WHEN COALESCE(fs.origin_iata, ro.origin_iata) = ? THEN 1 ELSE 0 END) +
              SUM(CASE WHEN COALESCE(fs.dest_iata,   ro.dest_iata)   = ? THEN 1 ELSE 0 END) AS n
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
        units_label = str(u)
        if eff > int(gw):
            units_label = f"{u} (wk{eff})"
        frees_cell = "—"
        try:
            from engine.gates import player_gate_gap_starts_by_unit

            # Compute more gaps than we display; we'll present at most one per day (Mon–Sun) per gate
            # so players can plan across the whole week.
            gaps = player_gate_gap_starts_by_unit(ap, int(gw), gate_units=u, max_gaps_per_gate=80)
            if gaps:
                lines = []
                for i, gs in enumerate(gaps, 1):
                    if not gs:
                        continue
                    # Pick earliest gap per day-of-week to avoid only showing Monday.
                    by_day: dict[int, tuple[float, float]] = {}
                    for a, b in gs:
                        h_in_w = float(a) % 168.0
                        di = int(h_in_w // 24.0)  # 0..6
                        cur = by_day.get(di)
                        if cur is None or float(a) < float(cur[0]):
                            by_day[di] = (float(a), float(b))
                    parts = []
                    for di in range(7):
                        if di in by_day:
                            a, b = by_day[di]
                            parts.append(f"{_dow_hhmm(a)}→{_dow_hhmm(b)}")
                    lines.append(f"G{i}: " + ", ".join(parts))
                frees_cell = "\n".join(lines) if lines else "—"
        except Exception:
            frees_cell = "—"

        t.add_row(str(r["airport_iata"]), units_label, str(touches), f"{busy:.1f}", f"{util:.0f}%", frees_cell)
    console.print(t)
    console.print("[dim]Concurrent gates: capacity is simultaneous gate windows; utilization uses busy gate-hours.[/dim]\n")


def gate_bids_panel(console: Console) -> None:
    rows = db.fetch_all(
        """
        SELECT a.auction_id, a.airport_iata, a.opens_week, a.closes_week,
               b.units_requested, b.price_per_unit
        FROM airport_gate_bids b
        JOIN airport_gate_auctions a ON a.auction_id = b.auction_id
        WHERE b.bidder_id = 'PLAYER'
        ORDER BY a.closes_week DESC, a.airport_iata
        """
    )
    if not rows:
        console.print("[dim]You have no active gate bids.[/dim]\n")
        return
    t = Table(title="YOUR GATE BIDS", show_lines=False)
    t.add_column("Airport", style="cyan", width=6)
    t.add_column("Units", justify="right", width=6)
    t.add_column("$/unit", justify="right", width=10)
    t.add_column("Closes", justify="right", width=7)
    t.add_column("Auction ID", style="dim", width=38)
    for r in rows[:200]:
        t.add_row(
            str(r["airport_iata"]),
            str(int(r["units_requested"] or 0)),
            f"${float(r['price_per_unit'] or 0):,.0f}",
            str(r["closes_week"]),
            str(r["auction_id"]),
        )
    console.print(t)
    console.print()


def handle_gate_cli(console: Console, raw: str) -> None:
    parts = raw.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    if cmd in ("gate_auctions", "gates_auctions"):
        gate_auctions_panel(console)
        return
    if cmd in ("gate_bids", "gate_bid_status"):
        gate_bids_panel(console)
        return
    if cmd == "gates":
        gates_panel(console)
        return
    if cmd == "gate_bid" and len(parts) >= 4:
        aid = parts[1]
        units = int(parts[2].replace(",", ""))
        price = float(parts[3].replace(",", ""))
        submit_gate_bid(aid, units, price, bidder_id="PLAYER")
        console.print(f"[green]Bid recorded: {units} units @ ${price:,.0f} for {aid}.[/green]\n")
        return
    console.print("[yellow]Usage:[/yellow] gate_auctions · gate_bid <id> <units> <price_per_unit> · gate_bids · gates\n")

