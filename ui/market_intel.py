from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db


def _print_competitors_overview(console: Console) -> None:
    from engine.ai import ensure_competitors_seeded, estimate_competitor_weekly_revenue

    ensure_competitors_seeded()
    comps = db.fetch_all(
        """
        SELECT competitor_id, name, home_hub_iata, reputation
        FROM competitors
        ORDER BY competitor_id
        """
    )
    t = Table(title="Competitors", show_lines=True)
    t.add_column("ID", style="cyan")
    t.add_column("Name")
    t.add_column("Hub", justify="center")
    t.add_column("Routes", justify="right")
    t.add_column("Est. weekly revenue", justify="right")
    t.add_column("Reputation", justify="right")
    for c in comps:
        cid = str(c["competitor_id"])
        rc = db.fetch_one(
            "SELECT COUNT(*) AS n FROM competitor_routes WHERE competitor_id = ?",
            (cid,),
        )
        nrt = int(rc["n"] or 0) if rc else 0
        rev = estimate_competitor_weekly_revenue(cid)
        t.add_row(
            cid,
            str(c["name"]),
            str(c["home_hub_iata"]),
            str(nrt),
            f"${rev:,.0f}",
            f"{float(c['reputation'] or 0):.1f}",
        )
    console.print()
    console.print(t)
    console.print()


def _print_contested_routes(console: Console) -> None:
    from engine.ai import ensure_competitors_seeded
    from engine.demand import compute_route_contested_intel
    from engine.scheduling import calendar_game_week_from_state

    ensure_competitors_seeded()
    gw = calendar_game_week_from_state()
    gs = db.fetch_one("SELECT current_month FROM game_state WHERE id = 1")
    month = int(gs["current_month"] or 1) if gs else 1

    rows = db.fetch_all(
        """
        SELECT DISTINCT r.route_id
        FROM routes r
        WHERE EXISTS (
            SELECT 1
            FROM competitor_routes cr
            WHERE cr.outbound_route_id = r.route_id OR cr.inbound_route_id = r.route_id
        )
        ORDER BY r.route_id
        LIMIT 40
        """
    )
    if not rows:
        console.print("[yellow]No contested routes (no AI on your route list).[/yellow]\n")
        return
    t = Table(title="Contested routes — fares & logit market share", show_lines=True)
    t.add_column("Route", style="cyan", width=14)
    t.add_column("Player biz / lei", width=18)
    t.add_column("AI (biz / lei)", width=22)
    t.add_column("Share biz (P | AI)", width=22)
    t.add_column("Share lei (P | AI)", width=22)
    for r in rows:
        rid = str(r["route_id"])
        intel = compute_route_contested_intel(rid, game_week=gw, current_month=month)
        if not intel:
            continue
        pfb = intel["player_fare_business"]
        pfl = intel["player_fare_leisure"]
        psb = intel["player_share_business"]
        psl = intel["player_share_leisure"]
        lines_ai = []
        lines_sb = []
        lines_sl = []
        for comp in intel["competitors"]:
            cb = comp["fare_business"]
            cl = comp["fare_leisure"]
            sb = comp["share_business"]
            sl = comp["share_leisure"]
            lines_ai.append(f"{comp['competitor_id'][:8]} ${cb:.0f}/${cl:.0f}")
            lines_sb.append(f"{psb*100:.0f}% | {sb*100:.0f}%")
            lines_sl.append(f"{psl*100:.0f}% | {sl*100:.0f}%")
        t.add_row(
            rid,
            f"${pfb:.0f} / ${pfl:.0f}",
            "\n".join(lines_ai) if lines_ai else "—",
            "\n".join(lines_sb) if lines_sb else "—",
            "\n".join(lines_sl) if lines_sl else "—",
        )
    console.print()
    console.print(t)
    console.print()


def _print_route_market(console: Console, route_id: str) -> None:
    from engine.ai import ensure_competitors_seeded
    from engine.demand import compute_route_contested_intel
    from engine.scheduling import calendar_game_week_from_state
    from engine.routes import get_route

    ensure_competitors_seeded()
    rid = route_id.strip().upper()
    route = get_route(rid)
    if not route:
        console.print(f"[red]Unknown route '{rid}'.[/red]\n")
        return
    gw = calendar_game_week_from_state()
    gs = db.fetch_one("SELECT current_month FROM game_state WHERE id = 1")
    month = int(gs["current_month"] or 1) if gs else 1
    intel = compute_route_contested_intel(rid, game_week=gw, current_month=month)
    console.print(f"\n[bold cyan]Market — {rid}[/bold cyan]\n")
    console.print(
        f"  Player fares: business ${float(route['price_business']):.0f}, "
        f"leisure ${float(route['price_leisure']):.0f}\n"
    )
    if not intel:
        console.print("  [dim]No AI competition on this route.[/dim]\n")
        return
    console.print(
        f"  Player share (business / leisure): "
        f"{intel['player_share_business']*100:.1f}% / {intel['player_share_leisure']*100:.1f}%\n"
    )
    for comp in intel["competitors"]:
        console.print(
            f"  [cyan]{comp['competitor_id']}[/cyan]  fares biz/lei "
            f"${comp['fare_business']:.0f} / ${comp['fare_leisure']:.0f}  "
            f"share biz/lei {comp['share_business']*100:.1f}% / {comp['share_leisure']*100:.1f}%\n"
        )
    console.print()


def show_market_intel(console: Console) -> None:
    _print_competitors_overview(console)
    _print_contested_routes(console)
    _print_slot_pressure(console)


def _print_slot_pressure(console: Console) -> None:
    try:
        from engine.gates import current_game_week
        from engine.slots import clock_hour_label, list_slot_airport_rows, seed_slot_controlled_airports

        seed_slot_controlled_airports()
        gw = current_game_week()
        t = Table(title="Slot-controlled airports", show_lines=False)
        t.add_column("Airport", style="cyan", width=8)
        t.add_column("Cap/hr", justify="right", width=7)
        t.add_column("Peak", justify="right", width=10)
        t.add_column("When", width=28)
        t.add_column("Status", width=8)
        any_row = False
        for r in list_slot_airport_rows(gw):
            peak_n = int(r["peak_movements"] or 0)
            if peak_n <= 0:
                continue
            any_row = True
            cap = int(r["cap"])
            t.add_row(
                str(r["iata"]),
                str(cap),
                f"{peak_n}/{cap}",
                clock_hour_label(int(r["peak_hour"])),
                "FULL" if r["full"] else "ok",
            )
        if any_row:
            console.print(t)
            console.print()
    except Exception:
        pass


def _print_all_ai_competitor_routes(console: Console, competitor_id: str | None = None) -> None:
    """List every route currently operated by AI competitors (optional filter by competitor_id)."""
    from engine.ai import ensure_competitors_seeded

    ensure_competitors_seeded()
    cid_filter = competitor_id.strip().upper() if competitor_id else None
    if cid_filter:
        rows = db.fetch_all(
            """
            SELECT cr.competitor_id, c.name, cr.route_pair_id, cr.outbound_route_id, cr.inbound_route_id,
                   cr.fare_business, cr.fare_leisure,
                   cr.frequency_per_week
            FROM competitor_routes cr
            JOIN competitors c ON c.competitor_id = cr.competitor_id
            WHERE cr.competitor_id = ?
            ORDER BY cr.route_pair_id
            """,
            (cid_filter,),
        )
        title = f"AI routes — {cid_filter}"
    else:
        rows = db.fetch_all(
            """
            SELECT cr.competitor_id, c.name, cr.route_pair_id, cr.outbound_route_id, cr.inbound_route_id,
                   cr.fare_business, cr.fare_leisure,
                   cr.frequency_per_week
            FROM competitor_routes cr
            JOIN competitors c ON c.competitor_id = cr.competitor_id
            ORDER BY cr.competitor_id, cr.route_pair_id
            """
        )
        title = "All routes operated by AI competitors"
    if not rows:
        console.print(
            "[yellow]No AI-operated routes"
            + (f" for {cid_filter}." if cid_filter else ".")
            + "[/yellow]\n"
        )
        return
    t = Table(title=title, show_lines=False)
    t.add_column("AI", style="cyan", width=12)
    t.add_column("Name", max_width=18)
    t.add_column("Route", style="white", width=17)
    t.add_column("Biz", justify="right")
    t.add_column("Lei", justify="right")
    t.add_column("Freq/wk", justify="right")
    for r in rows[:300]:
        pair = str(r["route_pair_id"])
        out_id = str(r["outbound_route_id"])
        in_id = str(r["inbound_route_id"])
        t.add_row(
            str(r["competitor_id"]),
            str(r["name"]),
            f"{pair}\n({out_id}/{in_id})",
            f"${float(r['fare_business'] or 0):.0f}",
            f"${float(r['fare_leisure'] or 0):.0f}",
            str(int(r["frequency_per_week"] or 0)),
        )
    console.print()
    console.print(t)
    if len(rows) > 300:
        console.print(f"[dim]… {len(rows) - 300} more rows[/dim]")
    console.print()


def market_intel_cli(console: Console) -> None:
    """
    Commands: market · market <route_id> · competitors · routes [ai_id] · back
    """
    console.print(
        "\n[bold cyan]Market intelligence[/bold cyan] "
        "[dim](market · market <route> · competitors · routes [ai_id] · back)[/dim]\n"
    )
    while True:
        cmd = input("market> ").strip()
        if not cmd or cmd.lower() in ("back", "exit", "q"):
            return
        parts = cmd.split(maxsplit=1)
        head = parts[0].lower()

        if head == "competitors":
            _print_competitors_overview(console)
            continue

        if head == "routes":
            filt = parts[1].strip() if len(parts) >= 2 else None
            _print_all_ai_competitor_routes(console, filt)
            continue

        if head == "market":
            if len(parts) >= 2:
                _print_route_market(console, parts[1])
            else:
                show_market_intel(console)
            continue

        console.print(
            "[yellow]Unknown command. Try: market, market ATL-JFK, competitors, routes, routes AI_MERIDIAN, back[/yellow]\n"
        )
