"""Runway slot occupancy (hourly movement caps) plus weekly quota auctions."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db
from engine.slots import (
    clock_hour_label,
    ensure_weekly_slot_auctions,
    grandfather_historic_slot_holdings,
    list_open_slot_auctions,
    list_slot_airport_rows,
    seed_slot_controlled_airports,
    slots_held_vs_used,
    submit_slot_bid,
)


def slots_panel(console: Console) -> None:
    seed_slot_controlled_airports()
    gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    gw = int(gs["game_week"] or 1) if gs else 1
    grandfather_historic_slot_holdings(gw)
    ensure_weekly_slot_auctions(gw)
    rows = list_slot_airport_rows(gw, holder_id="PLAYER")
    t = Table(title=f"SLOT-CONTROLLED AIRPORTS — WEEK {gw}", show_lines=False)
    t.add_column("Airport", style="cyan", width=8)
    t.add_column("Cap/hr", justify="right", width=7)
    t.add_column("Held", justify="right", width=6)
    t.add_column("Used", justify="right", width=6)
    t.add_column("Peak hour", width=28)
    t.add_column("Airport peak", justify="right", width=14)
    t.add_column("Status", width=10)
    operated = 0
    for r in rows:
        summ = slots_held_vs_used(str(r["iata"]), "PLAYER", gw)
        used = int(summ["used"] or r["used"])
        held = int(summ["held"] or r.get("held") or 0)
        if used > 0:
            operated += 1
        peak_h = int(r["peak_hour"])
        peak_n = int(r["peak_movements"])
        cap = int(r["cap"])
        peak_lbl = clock_hour_label(peak_h) if peak_h >= 0 else "—"
        status = "FULL" if r["full"] else ("busy" if peak_n >= max(1, cap - 1) else "ok")
        t.add_row(
            str(r["iata"]),
            str(cap),
            str(held),
            str(used),
            peak_lbl,
            f"{peak_n}/{cap}" if peak_h >= 0 else "0",
            status,
        )
    console.print(t)
    auctions = list_open_slot_auctions()
    if auctions:
        at = Table(title="OPEN SLOT AUCTIONS", show_lines=False)
        at.add_column("Auction ID", style="dim", width=38)
        at.add_column("Airport", style="cyan", width=6)
        at.add_column("Avail", justify="right", width=6)
        at.add_column("Current $/unit", justify="right", width=14)
        at.add_column("Closes", justify="right", width=7)
        for a in auctions:
            at.add_row(
                str(a["auction_id"]),
                str(a["airport_iata"]),
                str(a["units_available"]),
                f"${float(a['current_price_per_unit'] or 0):,.0f}",
                str(a["closes_week"]),
            )
        console.print(at)
    if operated == 0:
        console.print(
            "[dim]You have no movements at these airports this week. "
            "Hourly caps apply to everyone. Weekly quota is extra — bid this week, awards start next week.[/dim]\n"
        )
    else:
        console.print(
            "[dim]Need a gate and a runway slot. FULL hours refuse new schedules. "
            "Held is your weekly movement quota. Command: [cyan]slot_bid <auction_id|IATA> <units> <price>[/cyan][/dim]\n"
        )


def handle_slots_cli(console: Console, raw: str) -> None:
    parts = raw.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    if cmd in ("slots", "slot"):
        slots_panel(console)
        return
    if cmd == "slot_bid" and len(parts) >= 4:
        token = parts[1]
        units = int(parts[2].replace(",", ""))
        price = float(parts[3].replace(",", ""))
        aid = token
        if len(token) <= 4 and token.isalpha():
            iata = token.upper()
            match = next(
                (a for a in list_open_slot_auctions() if str(a.get("airport_iata") or "").upper() == iata),
                None,
            )
            if not match:
                console.print(f"[red]No open slot auction for {iata}.[/red]\n")
                return
            aid = str(match["auction_id"])
        submit_slot_bid(aid, units, price, bidder_id="PLAYER")
        console.print(f"[green]Slot bid recorded: {units} units @ ${price:,.0f} for {aid}.[/green]\n")
        return
    console.print("[yellow]Usage:[/yellow] slots · slot_bid <auction_id|IATA> <units> <price_per_unit>\n")
