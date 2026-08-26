"""
UI panels module for Phase 1 & 2.
Rich table renderers for game data display including demand analytics.
"""

import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from db import db
from engine.setup import get_airline
from engine.aircraft import list_catalog, get_fleet, get_aircraft_type, get_aircraft_seat_config
from engine.cabin import get_cabin_config, get_default_config
from engine.scheduling import (
    get_tail_weekly_utilization,
    max_weekly_airborne_hours_cap,
    route_current_schedule_summary,
    route_weekly_passenger_accounting,
)
from engine.airports import get_airport
from engine.routes import get_all_routes, get_route


console = Console()


def render_airline_info():
    """Render airline information panel."""
    airline = get_airline()
    
    if not airline:
        console.print("[yellow]⚠ No airline created yet.[/yellow]\n")
        return
    
    airport = get_airport(airline['home_hub_iata'])
    
    # Create table for airline info
    table = Table(title="YOUR AIRLINE", show_header=False, box=None)
    table.add_column("Field", style="cyan", width=20)
    table.add_column("Value", style="white")
    
    table.add_row("Name", airline['name'])
    table.add_row("Callsign", airline['callsign'])
    table.add_row("Home Hub", f"{airline['home_hub_iata']} - {airport['name'] if airport else 'Unknown'}")
    table.add_row("", "")
    table.add_row("[bold]Financials", "")
    table.add_row("  Cash", f"${airline['cash']:,}")
    table.add_row("  Debt", f"${airline['total_debt']:,}")
    table.add_row("  Credit Score", str(airline['credit_score']))
    table.add_row("", "")
    table.add_row("[bold]Reputation", "")
    table.add_row("  Score", f"{airline['reputation_score']:.1f}/100")
    table.add_row("  Brand Power", f"{airline['brand_power']:.2f}x")
    table.add_row("  XP", f"{airline['xp']:,}")
    
    console.print(table)
    console.print()


def render_aircraft_catalog(category=None, limit=20):
    """Render aircraft catalog as a Rich table."""
    aircraft_list = list_catalog(category)
    
    if not aircraft_list:
        console.print("[yellow]⚠ No aircraft found in catalog.[/yellow]\n")
        return
    
    # Create table
    title = "AIRCRAFT CATALOG"
    if category:
        title += f" - {category}"
    
    table = Table(title=title, show_lines=False)
    table.add_column("Type ID", style="cyan", width=12)
    table.add_column("Name", style="white", width=35)
    table.add_column("Category", style="yellow", width=12)
    table.add_column("Range", justify="right", style="green", width=10)
    table.add_column("Speed", justify="right", style="blue", width=8)
    table.add_column("Purchase Price", justify="right", style="magenta", width=16)
    table.add_column("Lease/week", justify="right", style="magenta", width=16)
    table.add_column("Default cabin", style="dim", width=24, overflow="fold")

    for aircraft in aircraft_list[:limit]:
        cabin_txt = get_aircraft_seat_config(None, str(aircraft["type_id"]))
        table.add_row(
            aircraft['type_id'],
            aircraft['display_name'],
            aircraft['category'],
            f"{aircraft['range_nm']:,} nm",
            f"{aircraft['cruise_speed_kts']} kt",
            f"${aircraft['purchase_price']:,}",
            f"${aircraft['weekly_lease_cost']:,}",
            cabin_txt,
        )
    
    console.print(table)
    
    if len(aircraft_list) > limit:
        console.print(f"[dim]... and {len(aircraft_list) - limit} more aircraft[/dim]\n")
    else:
        console.print()


def _fleet_cabin_display(tail_number: str, type_id: str) -> str:
    """Compact Y/W/J/F seat counts for fleet table (IATA-style class letters)."""
    cfg = get_cabin_config(tail_number) or get_default_config(type_id)
    if not cfg:
        return "—"
    y = int(cfg.get("seats_economy") or 0)
    w = int(cfg.get("seats_premium_economy") or 0)
    j = int(cfg.get("seats_business") or 0)
    f = int(cfg.get("seats_first") or 0)
    parts = [f"Y{y}"]
    if w:
        parts.append(f"W{w}")
    parts.append(f"J{j}")
    if f:
        parts.append(f"F{f}")
    total = y + w + j + f
    return " ".join(parts) + f"  [dim]({total})[/dim]"
    

def render_fleet():
    """Render player's fleet as a Rich table."""
    fleet = get_fleet()
    
    if not fleet:
        console.print("[yellow]⚠ No aircraft in fleet. Purchase or lease aircraft to get started.[/yellow]\n")
        return
    
    # Create table
    table = Table(title="YOUR FLEET", show_lines=False)
    table.add_column("Tail #", style="cyan", width=12)
    table.add_column("Type", style="white", width=10)
    table.add_column("Model", style="white", width=26)
    table.add_column("Cabin", style="dim", width=22, overflow="fold")
    table.add_column("Ownership", style="yellow", width=10)
    table.add_column("Status", style="green", width=12)
    table.add_column("Location", style="blue", width=8)
    table.add_column("Lease Cost", justify="right", style="magenta", width=14)
    table.add_column("Week airborne", justify="right", width=14)
    
    cap_note = max_weekly_airborne_hours_cap()
    for aircraft in fleet:
        ac_type = get_aircraft_type(aircraft['type_id'])
        display_name = ac_type['display_name'] if ac_type else aircraft['type_id']
        
        u = get_tail_weekly_utilization(aircraft['tail_number'])
        util_txt = f"{u['airborne_hours']:.0f}/{u['cap_hours']:.0f}h"
        if u["at_or_over_limit"]:
            util_txt = f"[bold red]{util_txt}[/bold red]"
        elif u["airborne_hours"] >= u["cap_hours"] * 0.85:
            util_txt = f"[yellow]{util_txt}[/yellow]"
        
        # Color status
        status_color = "green" if aircraft['status'] == 'IDLE' else "yellow"
        
        # Show lease cost or "Owned"
        if aircraft['ownership'] == 'LEASED':
            lease_info = f"${aircraft['lease_weekly_cost']:,}/wk"
            if aircraft['lease_weeks_remaining']:
                lease_info += f" ({aircraft['lease_weeks_remaining']}w)"
        else:
            lease_info = "OWNED"
        
        cabin_txt = _fleet_cabin_display(aircraft["tail_number"], aircraft["type_id"])

        table.add_row(
            aircraft['tail_number'],
            aircraft['type_id'],
            display_name,
            cabin_txt,
            aircraft['ownership'],
            f"[{status_color}]{aircraft['status']}[/{status_color}]",
            aircraft['current_airport_iata'] or "N/A",
            lease_info,
            util_txt,
        )
    
    console.print(table)
    console.print(
        "[dim]Cabin: Y=economy, W=premium economy, J=business, F=first; number in parentheses = total seats.[/dim]"
    )
    console.print(
        f"[dim]Week airborne = sum of block times (arr−dep) for legs departing in the current "
        f"168h clock week (by scheduled dep time), vs cap ({cap_note:.0f} h). "
        f"Multi-day patterns add each day’s flying. New flights merge when they fit (no overlap, MTT).[/dim]"
    )
    console.print(f"[bold]Total Aircraft:[/bold] {len(fleet)}\n")


def render_airport_info(iata):
    """Render detailed airport information as a Rich panel."""
    from engine.airports import get_airport
    
    airport = get_airport(iata.upper())
    
    if not airport:
        console.print(f"[red]✗ Airport '{iata}' not found.[/red]\n")
        return
    
    # Build info text
    info = Text()
    info.append(f"{airport['name']}\n", style="bold cyan")
    info.append(f"\nIdentifiers:\n", style="bold")
    info.append(f"  IATA: {airport['iata']}  |  ICAO: {airport['icao']}\n")
    info.append(f"\nLocation:\n", style="bold")
    info.append(f"  City: {airport['city']}, {airport['country']}\n")
    info.append(f"  Coordinates: {airport['lat']:.4f}, {airport['lon']:.4f}\n")
    info.append(f"  Timezone: {airport['timezone']}\n")
    info.append(f"\nCategory & Facilities:\n", style="bold")
    info.append(f"  Category: {airport['category']}\n")
    info.append(f"  Slot Tier: Level {airport['slot_tier']}\n")
    info.append(f"  Gates: {airport['gate_count']}\n")
    if airport['runway_length_ft']:
        info.append(f"  Runway: {airport['runway_length_ft']:,} ft\n")
    info.append(f"\nFees:\n", style="bold")
    info.append(f"  Landing: ${airport['landing_fee_per_1000']:.2f} per 1,000 lbs MTOW\n")
    info.append(f"  Gate: ${airport['gate_fee']:.2f} per use\n")
    
    if airport['has_curfew_resolved']:
        info.append(f"\nCurfew: ", style="bold")
        info.append(f"{airport['curfew_start_resolved']} - {airport['curfew_end_resolved']}\n", style="red")
    
    panel = Panel(info, border_style="blue", expand=False)
    console.print(panel)
    console.print()


def render_route_list():
    """Render player-opened routes as a Rich table."""
    from engine.routes import get_player_routes

    routes = get_player_routes()
    
    if not routes:
        console.print("[yellow]⚠ No routes opened yet.[/yellow]\n")
        return
    
    # Create table
    table = Table(title="ACTIVE ROUTES", show_lines=False)
    table.add_column("Route ID", style="cyan", width=12)
    table.add_column("Origin", style="white", width=6)
    table.add_column("Dest", style="white", width=6)
    table.add_column("Distance", justify="right", style="green", width=12)
    table.add_column("Trip time", justify="right", style="green", width=9)
    table.add_column("Bus Base", justify="right", style="magenta", width=10)
    table.add_column("Lei Base", justify="right", style="magenta", width=10)
    table.add_column("W Fare", justify="right", style="magenta", width=10)
    table.add_column("F Fare", justify="right", style="magenta", width=10)
    table.add_column("Rem F", justify="right", style="blue", width=7)
    table.add_column("Rem J", justify="right", style="blue", width=7)
    table.add_column("Rem W", justify="right", style="blue", width=7)
    table.add_column("Rem Y", justify="right", style="blue", width=7)
    table.add_column("Current schedule", style="yellow", width=36)
    table.add_column("Gate", style="dim", width=10)

    avg_row = db.fetch_one(
        "SELECT AVG(cruise_speed_kts) AS avg_kts FROM aircraft_types WHERE cruise_speed_kts > 0"
    )
    avg_kts = float(avg_row["avg_kts"]) if avg_row and avg_row["avg_kts"] else 450.0
    turnaround_minutes = int(float(db.get_financial_constant("mtt_minutes") or 30))

    def _fmt_minutes(total_minutes: int) -> str:
        h, m = divmod(max(0, int(total_minutes)), 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"

    for route in routes:
        ops = route_weekly_passenger_accounting(route["route_id"])
        rem_f = rem_j = rem_w = rem_y = "—"
        if ops:
            rc = ops.get("remaining_cabin_demand") or {}
            try:
                rem_y = str(int(rc.get("Y", 0)))
                rem_w = str(int(rc.get("W", 0)))
                rem_j = str(int(rc.get("J", 0)))
                rem_f = str(int(rc.get("F", 0)))
            except Exception:
                rem_f = rem_j = rem_w = rem_y = "—"

        sched = route_current_schedule_summary(route["route_id"])
        sched_cell = sched if sched else "—"
        ia = int(route.get("is_active", 1) or 1)
        gate = "active" if ia else "auction"

        distance_nm = float(route.get("distance_nm") or 0.0)
        one_way_minutes = int(round((distance_nm / max(1.0, avg_kts)) * 60.0))
        trip_minutes = one_way_minutes + turnaround_minutes

        table.add_row(
            route["route_id"],
            route["origin_iata"],
            route["dest_iata"],
            f"{route['distance_nm']:,.0f} nm",
            _fmt_minutes(trip_minutes),
            f"${route['price_business']:,.2f}",
            f"${route['price_leisure']:,.2f}",
            f"${float(route.get('price_premium_economy') or 0):,.2f}",
            f"${float(route.get('price_first') or 0):,.2f}",
            rem_f,
            rem_j,
            rem_w,
            rem_y,
            sched_cell,
            gate,
        )
    
    console.print(table)
    console.print(f"[bold]Total Routes:[/bold] {len(routes)}")
    console.print(
        "[dim]Rem F/J/W/Y = remaining weekly cabin demand after assuming your scheduled legs fill up to "
        "their cabin seat capacity (and subtracting actual pax for in-air/landed legs). "
        "Current schedule = distinct flight number + aircraft type_id this clock week.[/dim]\n"
    )


def render_route_details(route_id):
    """Render detailed route information with projected fees and demand estimate."""
    route = get_route(route_id)
    if not route:
        console.print(f"[red]✗ Route '{route_id}' not found.[/red]\n")
        return
    
    origin = get_airport(route['origin_iata'])
    dest = get_airport(route['dest_iata'])
    
    # Get demand estimate
    try:
        from engine.demand import estimate_route_performance
        performance = estimate_route_performance(route_id)
        has_demand_data = True
    except Exception as e:
        has_demand_data = False
        performance = None
    
    # Build info text
    info = Text()
    info.append(f"Route: {route['route_id']}\n", style="bold cyan")
    info.append(f"{origin['city']} → {dest['city']}\n", style="white")
    info.append(
        "\n[dim]Forecast demand uses your leisure/business base fares vs a distance reference "
        "($0.10/nm leisure, $0.20/nm business): changing price moves this forecast. Premium/first ticket "
        "levels affect revenue per seat, not the pool sizes here. Uses the same game week as ops below.[/dim]\n",
        style="dim",
    )
    info.append(f"\nDistance: {route['distance_nm']:,.0f} nm\n", style="bold")
    
    info.append(f"\nCurrent Fares:\n", style="bold")
    info.append(f"  Leisure / economy base (Y): ${route['price_leisure']:,.2f}\n")
    info.append(
        f"  Premium economy (W): ${float(route.get('price_premium_economy') or 0):,.2f}\n"
    )
    info.append(f"  Business base (J): ${route['price_business']:,.2f}\n")
    info.append(f"  First class (F): ${float(route.get('price_first') or 0):,.2f}\n")

    sched = route_current_schedule_summary(route_id)
    info.append(f"\nCurrent schedule (this week):\n", style="bold")
    info.append(
        f"  {sched if sched else '— (no flights scheduled for this route in the current week)'}\n",
        style="yellow",
    )

    if has_demand_data and performance:
        from engine.demand_display import build_demand_summary, floor_flag_for_route, format_cli_block

        src = str(route.get("demand_source") or "")
        floored = False
        try:
            src2, floored = floor_flag_for_route(
                str(route["origin_iata"]),
                str(route["dest_iata"]),
                float(route.get("distance_nm") or 0),
            )
            if src2:
                src = src2
        except Exception:
            pass
        summary = build_demand_summary(
            business_pax=performance.get("business_pax"),
            leisure_pax=performance.get("leisure_pax"),
            demand_source=src,
            market_floor_applied=floored,
            base_demand_business=route.get("base_demand_business"),
            base_demand_leisure=route.get("base_demand_leisure"),
            economy_pax=performance.get("economy_pax"),
            premium_economy_pax=performance.get("premium_economy_pax"),
            business_cabin_pax=performance.get("business_cabin_pax"),
            first_pax=performance.get("first_pax"),
            weekly_market_total=performance.get("weekly_market_total"),
            aircraft_fill_pax=performance.get("aircraft_fill_pax", performance.get("total_pax")),
        )

        info.append(f"\nEstimated Weekly Demand:\n", style="bold green")
        for line in format_cli_block(summary):
            style = "bold green" if line.startswith("This week's market") else (
                "green" if line.startswith("Source:") or "business ·" in line or line.startswith("Cabin") else "dim"
            )
            info.append(f"  {line}\n", style=style)
        al = get_airline()
        rep = float(al["reputation_score"]) if al else 50.0
        rm = float(performance.get("reputation_multiplier") or 1.0)
        info.append(
            f"  Reputation {rep:.0f}/100 · demand ×{rm:.2f} (weekly score from ops, pax, network, revenue & finances)\n",
            style="dim",
        )

        info.append(f"\nProjected Weekly Revenue (by cabin):\n", style="bold magenta")
        if performance.get('pax_economy', 0) > 0:
            info.append(f"  Economy: {performance['pax_economy']} pax → ${performance['revenue_economy']:,.2f}\n", style="magenta")
        if performance.get('pax_premium_economy', 0) > 0:
            info.append(f"  Premium Economy: {performance['pax_premium_economy']} pax → ${performance['revenue_premium_economy']:,.2f}\n", style="magenta")
        if performance.get('pax_business', 0) > 0:
            info.append(f"  Business: {performance['pax_business']} pax → ${performance['revenue_business']:,.2f}\n", style="magenta")
        if performance.get('pax_first', 0) > 0:
            info.append(f"  First: {performance['pax_first']} pax → ${performance['revenue_first']:,.2f}\n", style="magenta")
        info.append(f"  Total Gross: ${performance['gross_revenue']:,.2f}\n", style="bold magenta")
        info.append(f"  Avg Fare: ${performance['avg_fare']:.2f}\n", style="magenta")
    else:
        from engine.demand_display import source_badge, source_blurb, floor_flag_for_route

        src = str(route.get("demand_source") or "")
        floored = False
        try:
            src2, floored = floor_flag_for_route(
                str(route["origin_iata"]),
                str(route["dest_iata"]),
                float(route.get("distance_nm") or 0),
            )
            if src2:
                src = src2
        except Exception:
            pass
        info.append(f"\nDemand (template only — open detail after schedule for weekly market):\n", style="bold")
        info.append(
            f"  Source: {source_badge(src) or '—'} — {source_blurb(src)}"
            f"{' · minimum playable market applied' if floored else ''}\n",
            style="green",
        )
        info.append(
            f"  Template base (internal): {route['base_demand_business']} B / "
            f"{route['base_demand_leisure']} L — not weekly passengers\n",
            style="dim",
        )

    ops = route_weekly_passenger_accounting(route["route_id"])
    if ops:
        info.append(f"\nThis week (game week {ops['game_week']}) — demand vs actual:\n", style="bold")
        info.append(
            f"  Modeled weekly market: {ops['weekly_business']} business / "
            f"{ops['weekly_leisure']} leisure (forecast)\n",
            style="white",
        )
        info.append(
            f"  Pax carried (in air + landed): {ops['carried_business']} business / "
            f"{ops['carried_leisure']} leisure (actual)\n",
            style="cyan",
        )
        rs_b = int(ops.get("reserved_scheduled_business") or 0)
        rs_l = int(ops.get("reserved_scheduled_leisure") or 0)
        info.append(
            f"  Scheduled legs — demand absorbed (min of pool share & cabin seats each dep): "
            f"{rs_b} business / {rs_l} leisure\n",
            style="dim",
        )
        info.append(
            f"  Committed to your flights (scheduled share + actual carried): "
            f"{ops['committed_business']} business / "
            f"{ops['committed_leisure']} leisure\n",
            style="white",
        )
        info.append(
            f"  Remaining market: "
            f"{ops['remaining_business']} business / {ops['remaining_leisure']} leisure\n",
            style="yellow",
        )
        rc = ops.get("remaining_cabin_demand") or {}
        try:
            info.append(
                f"  Remaining market by cabin: "
                f"F{int(rc.get('F', 0))} / J{int(rc.get('J', 0))} / "
                f"W{int(rc.get('W', 0))} / Y{int(rc.get('Y', 0))}\n",
                style="yellow",
            )
        except Exception:
            pass
        sd = int(ops.get("segments_delayed") or 0)
        info.append(
            f"  Flights on this route: {ops['segments_total']} "
            f"({ops['segments_scheduled']} scheduled, {sd} delayed, "
            f"{ops['segments_in_air']} in air, {ops['segments_landed']} landed)\n",
            style="dim",
        )

    info.append(f"\nProjected Fees (per flight):\n", style="bold")
    info.append(f"  Origin Landing: ${origin['landing_fee_per_1000']:.2f}/1000 lbs\n")
    info.append(f"  Origin Gate: ${origin['gate_fee']:.2f}\n")
    info.append(f"  Dest Landing: ${dest['landing_fee_per_1000']:.2f}/1000 lbs\n")
    info.append(f"  Dest Gate: ${dest['gate_fee']:.2f}\n")
    
    panel = Panel(info, border_style="blue", expand=False)
    console.print(panel)
    console.print()


def render_route_pricing_analysis(
    route_id,
    price_business,
    price_leisure,
    price_premium_economy,
    price_first,
):
    """
    Render demand and revenue analysis for hypothetical price points.
    Used for "what if" pricing scenarios.
    """
    from engine.cabin import fare_for_class
    from engine.demand import compute_demand_with_price, compute_revenue, estimate_route_performance

    route = get_route(route_id)
    if not route:
        console.print(f"[red]✗ Route '{route_id}' not found.[/red]\n")
        return
    
    try:
        import copy

        gs = db.fetch_one("SELECT game_week, current_month FROM game_state WHERE id = 1")
        gweek = int(gs["game_week"] or 1) if gs else 1
        gmon = int(gs["current_month"] or 1) if gs else 1

        temp_route = copy.copy(dict(route))
        temp_route["price_business"] = price_business
        temp_route["price_leisure"] = price_leisure
        temp_route["price_premium_economy"] = price_premium_economy
        temp_route["price_first"] = price_first

        # Same game week as main menu / route details
        current_performance = estimate_route_performance(
            route_id, game_week=gweek, current_month=gmon
        )

        demand = compute_demand_with_price(
            route_id, price_business, price_leisure, game_week=gweek, current_month=gmon
        )
        
        # Default cabin config (same as in estimate_route_performance)
        cabin_config = {
            "seats_economy": 138,
            "seats_premium_economy": 0,
            "seats_business": 12,
            "seats_first": 0,
        }

        proposed_revenue = compute_revenue(
            route_id,
            cabin_config,
            demand["leisure_pax"],
            demand["business_pax"],
            route_row=temp_route,
        )

        cur_market = int(current_performance["business_pax"]) + int(
            current_performance["leisure_pax"]
        )
        prop_market = int(demand["business_pax"]) + int(demand["leisure_pax"])
        
        # Show comparison
        table = Table(title=f"Pricing Analysis: {route_id}", show_lines=True)
        table.add_column("Metric", style="cyan", width=25)
        table.add_column("Current", style="white", justify="right", width=15)
        table.add_column("Proposed", style="yellow", justify="right", width=15)
        table.add_column("Change", style="green", justify="right", width=15)
        
        dcur = dict(route)
        y_list_cur = fare_for_class("economy", route_id, route_row=dcur)
        j_list_cur = fare_for_class("business", route_id, route_row=dcur)
        y_list_pr = fare_for_class("economy", route_id, route_row=temp_route)
        j_list_pr = fare_for_class("business", route_id, route_row=temp_route)
        y_pct = ((y_list_pr / y_list_cur) - 1) * 100 if y_list_cur and y_list_pr else 0.0
        j_pct = ((j_list_pr / j_list_cur) - 1) * 100 if j_list_cur and j_list_pr else 0.0

        table.add_row(
            "Leisure base (Y)",
            f"${route['price_leisure']:.2f}",
            f"${price_leisure:.2f}",
            f"{((price_leisure / route['price_leisure'] - 1) * 100):+.1f}%",
        )
        table.add_row(
            "Y list fare (est.)",
            f"${y_list_cur:.2f}",
            f"${y_list_pr:.2f}",
            f"{y_pct:+.1f}%",
        )

        table.add_row(
            "Business base (B)",
            f"${route['price_business']:.2f}",
            f"${price_business:.2f}",
            f"{((price_business / route['price_business'] - 1) * 100):+.1f}%",
        )
        table.add_row(
            "J list fare (est.)",
            f"${j_list_cur:.2f}",
            f"${j_list_pr:.2f}",
            f"{j_pct:+.1f}%",
        )

        table.add_row(
            "Premium (W) ticket",
            f"${float(route.get('price_premium_economy') or 0):.2f}",
            f"${price_premium_economy:.2f}",
            f"{((price_premium_economy / max(0.01, float(route.get('price_premium_economy') or 0.01)) - 1) * 100):+.1f}%",
        )

        table.add_row(
            "First (F) ticket",
            f"${float(route.get('price_first') or 0):.2f}",
            f"${price_first:.2f}",
            f"{((price_first / max(0.01, float(route.get('price_first') or 0.01)) - 1) * 100):+.1f}%",
        )
        
        table.add_row("", "", "", "")  # Separator
        
        # Business demand
        bus_demand_change = demand['business_pax'] - current_performance['business_pax']
        table.add_row(
            "Business pool (B seg.)",
            f"{current_performance['business_pax']} pax",
            f"{demand['business_pax']} pax",
            f"{bus_demand_change:+d} pax"
        )
        
        # Leisure demand
        lei_demand_change = demand['leisure_pax'] - current_performance['leisure_pax']
        table.add_row(
            "Leisure pool (L seg.)",
            f"{current_performance['leisure_pax']} pax",
            f"{demand['leisure_pax']} pax",
            f"{lei_demand_change:+d} pax"
        )

        # Premium economy demand
        pe_cur = int(current_performance.get("premium_economy_pax", 0))
        pe_new = int(demand.get("premium_economy_pax", 0))
        table.add_row(
            "Premium Econ Demand",
            f"{pe_cur} pax",
            f"{pe_new} pax",
            f"{(pe_new - pe_cur):+d} pax",
        )

        # First demand
        f_cur = int(current_performance.get("first_pax", 0))
        f_new = int(demand.get("first_pax", 0))
        table.add_row(
            "First Demand",
            f"{f_cur} pax",
            f"{f_new} pax",
            f"{(f_new - f_cur):+d} pax",
        )
        
        # Total market demand (B+L segments), not cabin seats filled (total_pax from revenue)
        total_demand_change = prop_market - cur_market
        table.add_row(
            "Total market demand",
            f"{cur_market} pax",
            f"{prop_market} pax",
            f"{total_demand_change:+d} pax",
        )
        
        table.add_row("", "", "", "")  # Separator
        
        # Revenue
        revenue_change = proposed_revenue['gross_revenue'] - current_performance['gross_revenue']
        revenue_pct = (revenue_change / current_performance['gross_revenue'] * 100) if current_performance['gross_revenue'] > 0 else 0
        table.add_row(
            "Weekly Revenue",
            f"${current_performance['gross_revenue']:,.2f}",
            f"${proposed_revenue['gross_revenue']:,.2f}",
            f"${revenue_change:+,.2f} ({revenue_pct:+.1f}%)"
        )
        
        console.print(table)
        console.print(
            "[dim]Pool elasticity vs a distance reference fare (same as Route details): compare your Y/B *bases* to "
            "$0.10/nm and $0.20/nm on this route length. Competitors compare list fares via logit. Cabin rows = split of pools.[/dim]\n"
        )
        
    except Exception as e:
        console.print(f"[red]✗ Error calculating demand: {e}[/red]\n")


if __name__ == "__main__":
    # Test the module
    print("Testing UI panels...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        render_airline_info()
        render_aircraft_catalog(limit=10)
        render_fleet()
        render_route_list()
