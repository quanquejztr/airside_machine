"""
CLI command workflows extracted from main.py (prompts + menus that call engine).
No game rules live here — only interaction flows.
"""

from __future__ import annotations

from rich.console import Console

from db import db
from engine import aircraft, airports, routes
from ui import panels
from ui.flight_board import game_time_status_line

console = Console()


def _ask_yes_no(prompt: str, *, default: bool | None = None) -> bool | None:
    """
    Yes/no prompt.
    - Returns True/False for explicit answers.
    - Returns default when user presses Enter and default is provided.
    - Returns None when user presses Enter and default is None (treated as cancel/back).
    """
    while True:
        raw = input(prompt).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        console.print("[yellow]Please answer yes or no.[/yellow]")


def _ask_int(prompt: str, *, default: int | None = None, min_value: int | None = None) -> int | None:
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            v = int(raw)
        except ValueError:
            console.print("[yellow]Invalid number.[/yellow]")
            continue
        if min_value is not None and v < min_value:
            console.print(f"[yellow]Must be at least {min_value}.[/yellow]")
            continue
        return v


def _ask_float(prompt: str, *, default: float | None = None, min_value: float | None = None) -> float | None:
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            console.print("[yellow]Invalid number.[/yellow]")
            continue
        if min_value is not None and v < min_value:
            console.print(f"[yellow]Must be at least {min_value}.[/yellow]")
            continue
        return v


def prompt_optional_cabin_seats(type_id):
    """
    Prompt for Y/PE/J/F seat counts at aircraft delivery.
    Returns None to use aircraft_default_config, or (eco, prem_eco, biz, first).
    """
    from engine.cabin import validate_config

    type_id = type_id.strip().upper()
    row = db.fetch_one(
        "SELECT * FROM aircraft_default_config WHERE type_id = ?",
        (type_id,),
    )
    if not row:
        return None
    at = aircraft.get_aircraft_type(type_id)
    # sqlite3.Row has no .get(); use subscript
    eec_limit = int(at["eec"] or 0) if at else 0
    if eec_limit <= 0:
        print(
            "\n  Using catalog default cabin (EEC limit missing; customization disabled)."
        )
        return None
    eco = int(row["seats_economy"])
    prem = int(row["seats_premium_economy"])
    biz = int(row["seats_business"])
    first = int(row["seats_first"])
    print(
        f"\n  Default cabin for {type_id}: {eco} Y / {prem} PE / {biz} J / {first} F "
        f"(EEC cap: {eec_limit})"
    )
    ans = input("  Customize seat counts before delivery? (y/N): ").strip().lower()
    if ans != "y":
        return None
    while True:
        try:
            print("  Enter seat counts (integers):")
            eco = int(input("    Economy: ").strip())
            prem = int(input("    Premium economy: ").strip())
            biz = int(input("    Business: ").strip())
            first = int(input("    First: ").strip())
        except ValueError:
            print("  Please enter whole numbers.\n")
            continue
        ok, msg = validate_config(eco, prem, biz, first, eec_limit)
        if ok:
            return (eco, prem, biz, first)
        print(f"  {msg}\n")


def _print_tail_schedule_grid(tail_number):
    """Show week×time grid for this tail (after tail+routes entry, or after booking). Non-fatal if render fails."""
    try:
        from ui.tail_schedule_grid import print_tail_weekly_grid

        print_tail_weekly_grid(console, tail_number)
    except Exception:
        pass


def _fleet_renew_lease():
    """Extend an aircraft's lease by N additional weeks."""
    tail = input("\nTail number to renew (e.g. VNA-001): ").strip().upper()
    if not tail:
        console.print("[yellow]Cancelled.[/yellow]\n")
        return
    ac = aircraft.get_fleet_aircraft(tail)
    if not ac:
        console.print(f"[red]No aircraft '{tail}' in your fleet.[/red]\n")
        return
    if ac["ownership"] != "LEASED":
        console.print(f"[yellow]{tail} is OWNED, not leased — no lease to renew.[/yellow]\n")
        return

    rem = int(ac.get("lease_weeks_remaining") or 0)
    weekly = float(ac.get("lease_weekly_cost") or 0)
    console.print(
        f"\n  [cyan]{tail}[/cyan] — lease rate [yellow]${weekly:,.0f}/wk[/yellow], "
        f"[dim]{rem} weeks remaining[/dim]"
    )
    # Future: price negotiation based on fleet size, loyalty, market conditions
    raw = input("  Additional weeks to add (e.g. 52): ").strip()
    if not raw:
        console.print("[yellow]Cancelled.[/yellow]\n")
        return
    try:
        extra = int(raw)
    except ValueError:
        console.print("[red]Invalid number.[/red]\n")
        return
    if extra <= 0:
        console.print("[red]Must be at least 1 week.[/red]\n")
        return

    new_rem = rem + extra
    db.execute(
        "UPDATE fleet SET lease_weeks_remaining = ? WHERE tail_number = ?",
        (new_rem, tail),
    )
    console.print(
        f"[green]✓ Lease renewed — {tail} now has {new_rem} weeks remaining "
        f"(+{extra} weeks at ${weekly:,.0f}/wk).[/green]\n"
    )


def _fleet_reconfigure_cabin():
    """Change seat layout of an aircraft (must be IDLE at home hub)."""
    from engine.cabin import reconfigure, get_cabin_config

    tail = input("\nTail number to reconfigure (e.g. VNA-001): ").strip().upper()
    if not tail:
        console.print("[yellow]Cancelled.[/yellow]\n")
        return
    ac = aircraft.get_fleet_aircraft(tail)
    if not ac:
        console.print(f"[red]No aircraft '{tail}' in your fleet.[/red]\n")
        return

    cfg = get_cabin_config(tail)
    if cfg:
        console.print(
            f"\n  Current cabin: Y{cfg['seats_economy']} / W{cfg['seats_premium_economy']} "
            f"/ J{cfg['seats_business']} / F{cfg['seats_first']}"
        )

    at = aircraft.get_aircraft_type(ac["type_id"])
    eec_limit = int(at["eec"] or 0) if at else 0
    if eec_limit > 0:
        console.print(f"  EEC capacity: {eec_limit}")

    console.print("  [dim]Aircraft must be IDLE and at your home hub.[/dim]\n")
    try:
        eco = int(input("  New economy seats: ").strip())
        prem = int(input("  New premium economy seats: ").strip())
        biz = int(input("  New business seats: ").strip())
        first = int(input("  New first class seats: ").strip())
    except (ValueError, EOFError):
        console.print("[yellow]Cancelled.[/yellow]\n")
        return

    try:
        ok, cost, msg = reconfigure(tail, eco, prem, biz, first)
        if ok:
            console.print(f"[green]{msg}[/green]\n")
        else:
            console.print(f"[red]{msg}[/red]\n")
    except Exception as e:
        console.print(f"[red]{e}[/red]\n")


def buy_or_lease_aircraft():
    """Buy or lease aircraft menu — supports ordering multiple units at once."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return

    print("\n" + "=" * 60)
    print("AVAILABLE AIRCRAFT")
    print("=" * 60)
    panels.render_aircraft_catalog(limit=999)

    print("=" * 60)
    print("BUY OR LEASE AIRCRAFT")
    print("=" * 60)
    while True:
        print("1. Buy Aircraft (pay full price, own forever)")
        print("2. Lease Aircraft (pay weekly, return at end)")
        print("3. Back to Main Menu")

        choice = input("\nChoice: ").strip()
        if not choice or choice == "3":
            return

        if choice == '1':
            type_id = input("Enter aircraft type_id to buy: ").strip().upper()
            if not type_id:
                console.print("[yellow]Cancelled.[/yellow]\n")
                continue
            qty = _prompt_quantity()
            if qty <= 0:
                continue
            try:
                cabin = prompt_optional_cabin_seats(type_id)
                for _ in range(qty):
                    aircraft.buy_aircraft(type_id, cabin_seats=cabin)
                if qty > 1:
                    console.print(f"[green]✓ Purchased {qty}× {type_id}.[/green]\n")
            except Exception as e:
                print(f"\n✗ Purchase failed: {e}\n")
            continue

        if choice == '2':
            type_id = input("Enter aircraft type_id to lease: ").strip().upper()
            if not type_id:
                console.print("[yellow]Cancelled.[/yellow]\n")
                continue
            qty = _prompt_quantity()
            if qty <= 0:
                continue
            weeks_int = _ask_int("Lease duration (weeks): ", min_value=1)
            if weeks_int is None:
                console.print("[yellow]Cancelled.[/yellow]\n")
                continue
            try:
                cabin = prompt_optional_cabin_seats(type_id)
                for _ in range(qty):
                    aircraft.lease_aircraft(type_id, weeks_int, cabin_seats=cabin)
                if qty > 1:
                    console.print(f"[green]✓ Leased {qty}× {type_id} for {weeks_int} weeks.[/green]\n")
            except Exception as e:
                print(f"\n✗ Error: {e}\n")
            continue

        print("\n✗ Invalid choice.\n")


def _prompt_quantity() -> int:
    """Ask for how many aircraft to order; returns 0 on cancel."""
    raw = input("How many? (Enter = 1): ").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        print("✗ Invalid number.\n")
        return 0
    if n <= 0:
        print("✗ Cancelled.\n")
        return 0
    return n


def open_new_route():
    """Open a new route."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return

    while True:
        print("\n" + "=" * 60)
        print("OPEN NEW ROUTE")
        print("=" * 60)

        origin = input("Origin airport (IATA): ").strip().upper()
        if not origin:
            return
        dest = input("Destination airport (IATA): ").strip().upper()
        if not dest:
            return

        try:
            from engine.airports import get_airport
            from engine.routes import preview_route_opening, execute_route_opens

            origin_airport = get_airport(origin)
            dest_airport = get_airport(dest)
            if not origin_airport:
                print(f"\n✗ Origin airport '{origin}' not found.\n")
                continue
            if not dest_airport:
                print(f"\n✗ Destination airport '{dest}' not found.\n")
                continue

            airline = setup.get_airline()
            hub = (airline.get("home_hub_iata") or "").strip().upper() or None

            preview = preview_route_opening(origin, dest, hub)
            distance_nm = preview["distance_nm"]
            pf = preview["forward"]
            pr = preview["reverse"]

            print(f"\n{origin_airport['name']} ({origin}) → {dest_airport['name']} ({dest})")
            print(f"  Great-circle distance: {distance_nm:,.0f} nm")

            avg_row = db.fetch_one(
                "SELECT AVG(cruise_speed_kts) AS avg_kts FROM aircraft_types WHERE cruise_speed_kts > 0"
            )
            avg_kts = float(avg_row["avg_kts"]) if avg_row and avg_row["avg_kts"] else 450.0
            one_way_minutes = int(round((float(distance_nm) / max(1.0, avg_kts)) * 60.0))
            turnaround_minutes = int(float(db.get_financial_constant("mtt_minutes") or 30))
            round_trip_minutes = one_way_minutes * 2 + turnaround_minutes

            def _fmt_minutes(total_minutes: int) -> str:
                h, m = divmod(max(0, int(total_minutes)), 60)
                return f"{h}h{m:02d}m" if h else f"{m}m"

            print(
                f"  Time estimate: {_fmt_minutes(one_way_minutes)} flight + "
                f"{_fmt_minutes(turnaround_minutes)} turnaround (minimum)"
            )
            print(
                f"  Round-trip estimate ({origin}→{dest}→{origin}): "
                f"{_fmt_minutes(round_trip_minutes)}"
            )
            if hub:
                print(f"  Home hub: {hub} — ", end="")
                if preview["hub_involved"]:
                    print(
                        "hub touches this pair (both directions share one acquisition fee when both are new)."
                    )
                else:
                    print("neither airport is your hub (only the requested leg can be opened).")

            def _leg_line(p: dict) -> str:
                if p.get("player_has"):
                    return "in your network"
                if p.get("catalog_has"):
                    return "market catalog only — add to your network to operate"
                return "not in database yet"

            print(f"\n  {pf['route_id']}: {_leg_line(pf)}")
            print(f"  {pr['route_id']}: {_leg_line(pr)}")

            if not preview["opens"]:
                print("\n  Nothing to add — route network already covers this request.\n")
                continue

            from engine.demand import preview_weekly_demand_before_open
            from engine.demand_display import format_cli_block, summary_from_preview

            print("\n  --- Estimated weekly market demand (default fares) ---")
            print(
                "  Hero number = this week's market total. Cabin letters are a split of that "
                "market. Template base is internal.\n"
            )
            for spec in preview["opens"]:
                oa = get_airport(spec["origin"])
                da = get_airport(spec["dest"])
                if not oa or not da:
                    continue
                dem = preview_weekly_demand_before_open(oa, da, distance_nm)
                rid = f"{spec['origin']}-{spec['dest']}"
                summary = summary_from_preview(dem)
                print(f"  {rid}")
                for line in format_cli_block(summary):
                    print(f"    {line}")
                print(
                    f"    Default fares: ${dem['price_business_default']:,.2f} business, "
                    f"${dem['price_leisure_default']:,.2f} leisure"
                )
                print(
                    "    AI competition can reduce your share of this market once rivals fly the OD."
                )

            total_new = float(preview["total_new_cost"] or 0.0)
            print(
                f"\n  Acquisition due now: ${total_new:,.2f} "
                f"(reference formula for {origin}→{dest}: ${preview['cost_reference']:,.2f})"
            )
            print(f"  Current Cash: ${airline['cash']:,.2f}")
            if float(airline["cash"] or 0.0) < total_new:
                print(f"\n✗ Need ${total_new - float(airline['cash'] or 0.0):,.2f} more cash.\n")
                continue
            print(f"  Cash After Purchase: ${float(airline['cash'] or 0.0) - total_new:,.2f}")

            if total_new > 0:
                ok = _ask_yes_no(
                    f"\nConfirm opening for ${total_new:,.2f} acquisition? (yes/no): ",
                    default=None,
                )
            else:
                ok = _ask_yes_no(
                    "\nAdd the complementary route(s) at no acquisition charge? (yes/no): ",
                    default=None,
                )
            if ok is not True:
                print("\n✗ Route opening cancelled.\n")
                continue

            result = execute_route_opens(preview["opens"])
            if result.get("opened"):
                last = result["opened"][-1]
                panels.render_route_details(last["route_id"])
        except Exception as e:
            print(f"\n✗ Failed to open route: {e}\n")


def view_routes():
    """View all active routes."""
    panels.render_route_list()


def manage_route_pricing():
    """Manage route pricing with demand analysis."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return
    
    while True:
        # Show all routes first
        panels.render_route_list()

        print("\n" + "=" * 60)
        print("ROUTE PRICING MANAGEMENT")
        print("=" * 60)
        print("1. View Route Details & Demand")
        print("2. Update Route Prices")
        print("3. Test Price Scenario (What-If Analysis)")
        print("4. Back to Main Menu")

        choice = input("\nChoice: ").strip()
        if not choice or choice == "4":
            return

        if choice == '1':
            route_id = input("Enter route ID (e.g., TPA-JFK): ").strip().upper()
            if not route_id:
                print("\n✗ Cancelled.\n")
                continue
            panels.render_route_details(route_id)
            continue

        if choice == '2':
            route_id = input("Enter route ID (e.g., TPA-JFK): ").strip().upper()
            if not route_id:
                print("\n✗ Cancelled.\n")
                continue
            try:
                rt = routes.get_route(route_id)
                if not rt:
                    print(f"\n✗ Route '{route_id}' not found.\n")
                    continue
                panels.render_route_details(route_id)
                pl, pb, pw, pf = (
                    float(rt["price_leisure"]),
                    float(rt["price_business"]),
                    float(rt["price_premium_economy"]),
                    float(rt["price_first"]),
                )
                print("\nEnter new fares (Enter keeps the value in brackets):")
                price_leisure = _ask_float(
                    f"  Leisure / economy base (Y) [${pl:,.2f}]: $",
                    default=pl,
                    min_value=0.01,
                )
                price_premium_economy = _ask_float(
                    f"  Premium economy (W) [${pw:,.2f}]: $",
                    default=pw,
                    min_value=0.01,
                )
                price_business = _ask_float(
                    f"  Business / J base [${pb:,.2f}]: $",
                    default=pb,
                    min_value=0.01,
                )
                price_first = _ask_float(
                    f"  First class (F) [${pf:,.2f}]: $",
                    default=pf,
                    min_value=0.01,
                )
                if any(
                    x is None
                    for x in (
                        price_business,
                        price_leisure,
                        price_premium_economy,
                        price_first,
                    )
                ):
                    print("\n✗ Cancelled.\n")
                    continue
                routes.update_route_prices(
                    route_id,
                    price_business,
                    price_leisure,
                    price_premium_economy,
                    price_first,
                )
                print("\nUpdated route performance:")
                panels.render_route_details(route_id)
            except Exception as e:
                print(f"\n✗ Error: {e}\n")
            continue

        if choice == '3':
            route_id = input("Enter route ID (e.g., TPA-JFK): ").strip().upper()
            if not route_id:
                print("\n✗ Cancelled.\n")
                continue
            try:
                rt = routes.get_route(route_id)
                if not rt:
                    print(f"\n✗ Route '{route_id}' not found.\n")
                    continue
                panels.render_route_details(route_id)
                pl, pb, pw, pf = (
                    float(rt["price_leisure"]),
                    float(rt["price_business"]),
                    float(rt["price_premium_economy"]),
                    float(rt["price_first"]),
                )
                print("\nTest scenario — enter proposed fares (Enter keeps the value in brackets):")
                price_leisure = _ask_float(
                    f"  Leisure / economy base (Y) [${pl:,.2f}]: $",
                    default=pl,
                    min_value=0.01,
                )
                price_premium_economy = _ask_float(
                    f"  Premium economy (W) [${pw:,.2f}]: $",
                    default=pw,
                    min_value=0.01,
                )
                price_business = _ask_float(
                    f"  Business / J base [${pb:,.2f}]: $",
                    default=pb,
                    min_value=0.01,
                )
                price_first = _ask_float(
                    f"  First class (F) [${pf:,.2f}]: $",
                    default=pf,
                    min_value=0.01,
                )
                if any(
                    x is None
                    for x in (
                        price_business,
                        price_leisure,
                        price_premium_economy,
                        price_first,
                    )
                ):
                    print("\n✗ Cancelled.\n")
                    continue
                panels.render_route_pricing_analysis(
                    route_id,
                    price_business,
                    price_leisure,
                    price_premium_economy,
                    price_first,
                )
                apply = _ask_yes_no("Apply these prices? (yes/no): ", default=None)
                if apply is True:
                    routes.update_route_prices(
                        route_id,
                        price_business,
                        price_leisure,
                        price_premium_economy,
                        price_first,
                    )
                    print("\n✓ Prices updated!\n")
            except Exception as e:
                print(f"\n✗ Error: {e}\n")
            continue

        print("\n✗ Invalid choice.\n")


def assign_flight_rotation():
    """Assign a flight rotation to an aircraft for the week."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return
    
    from engine.scheduling import (
        assign_rotation,
        create_chained_detailed_rotation,
        create_flight_schedule,
        merge_detailed_weekly_template,
        format_rotation_airport_chain,
        route_ids_from_airport_chain,
    )
    
    # Get fleet and routes
    fleet = aircraft.get_fleet()
    routes_list = routes.get_player_routes()
    
    if not fleet:
        console.print("\n[yellow]⚠ No aircraft in fleet. Purchase or lease aircraft first.[/yellow]\n")
        return
    
    if not routes_list:
        console.print("\n[yellow]⚠ No routes opened yet. Open routes first.[/yellow]\n")
        return
    
    # Show fleet
    panels.render_fleet()
    
    # Show routes
    panels.render_route_list()
    
    console.print("\n[bold cyan]═══ ASSIGN FLIGHT ROTATION ═══[/bold cyan]")
    console.print("[dim]Assign an aircraft to fly a sequence of routes[/dim]")
    console.print(
        "[dim]The same rotation is saved for every in-game week; each new week gets flights "
        "from this template automatically. Use Assign again only when you want to change the plan.[/dim]"
    )
    try:
        from engine.scheduling import max_weekly_airborne_hours_cap
        cap_h = max_weekly_airborne_hours_cap()
        console.print(
            f"[dim]Weekly airborne cap per aircraft: {cap_h:.0f} h (sum of flight times). "
            f"New flights are added if they do not overlap and respect turnaround time vs existing flights.[/dim]"
        )
    except Exception:
        pass
    console.print(
        "[dim]Tips: Enter a hyphen chain of airports in order (e.g. [cyan]sfo-san-sfo-sea-sfo-den-sfo[/cyan]) "
        "or legacy comma-separated route IDs (TPA-JFK,JFK-BOS). Routes must exist under menu 6. "
        "Quick Schedule chains in order. For Detailed with [bold]two or more[/bold] routes, the sim "
        "runs the full rotation each operating day (first-leg time you enter, then MTT + flight times). "
        "Single-route Detailed still cannot repeat the same one-way on multiple days without a return. "
        "Quick-vs-detailed conflict: option 3.[/dim]\n"
    )
    
    # Get aircraft
    tail_number = input("Enter tail number: ").strip().upper()
    if not tail_number:
        print("\n✗ Tail number required\n")
        return
    
    # Check if aircraft exists
    ac = aircraft.get_fleet_aircraft(tail_number)
    if not ac:
        console.print(f"\n[red]✗ Aircraft '{tail_number}' not found in fleet[/red]\n")
        return
    
    routes_input = input(
        "Route plan (e.g. sfo-san-sfo-sea-sfo) or comma-separated route IDs: "
    ).strip()
    if not routes_input:
        print("\n✗ At least one route required\n")
        return

    try:
        if "," in routes_input:
            route_ids = [r.strip().upper() for r in routes_input.split(",") if r.strip()]
        else:
            route_ids = route_ids_from_airport_chain(routes_input)
    except ValueError as e:
        console.print(f"\n[red]✗ {e}[/red]\n")
        return
    if not route_ids:
        print("\n✗ At least one route required\n")
        return

    chain_disp = format_rotation_airport_chain(route_ids)
    console.print(
        f"\n[bold]{tail_number}[/bold] · plan: [cyan]{chain_disp}[/cyan]"
    )
    console.print(
        "[dim]Weekly schedule for this aircraft right now (updates after you save below):[/dim]"
    )
    _print_tail_schedule_grid(tail_number)
    
    # Ask if user wants to customize schedule or use auto-schedule
    console.print("\n[bold cyan]═══ SCHEDULING OPTIONS ═══[/bold cyan]")
    console.print("  [cyan]1.[/cyan] Quick Schedule (auto-schedule immediately)")
    console.print("  [cyan]2.[/cyan] Detailed Schedule (customize times, days, flight numbers)")
    console.print("  [cyan]3.[/cyan] Clear this aircraft's weekly schedule (start over)\n")
    
    sched_choice = input("Choice (1-3): ").strip()
    
    if sched_choice == '3':
        from engine.scheduling import cancel_rotation
        try:
            cancel_rotation(tail_number, wipe_completed_this_week=True)
            console.print(
                f"\n[green]✓ Cleared rotation, templates, and week schedule rows for {tail_number}. "
                "Stuck IN_AIR legs past ETA are removed; at most one active in-flight leg may remain.[/green]\n"
            )
        except Exception as e:
            console.print(f"\n[red]✗ Could not clear schedule: {e}[/red]\n")
        return
    
    if sched_choice == '1':
        # Quick auto-schedule
        try:
            rotation = assign_rotation(tail_number, route_ids)
            
            console.print(f"\n[green]✓ Rotation assigned successfully![/green]")
            console.print(f"  Tail: {rotation['tail_number']}")
            console.print(
                f"  Plan: {format_rotation_airport_chain(rotation['route_ids'])}"
            )
            console.print(f"  Total Distance: {rotation['total_distance']:,.0f} nm")
            console.print(f"  Estimated Duration: {rotation['estimated_duration'] // 60} minutes")
            console.print(f"  Flights Created: {len(rotation['segments'])}")
            try:
                from engine.scheduling import get_tail_weekly_utilization
                u = get_tail_weekly_utilization(rotation["tail_number"])
                console.print(
                    f"  Week airborne: {u['airborne_hours']:.1f} / {u['cap_hours']:.0f} h "
                    f"({u['utilization_pct']:.0f}% of weekly cap)"
                )
            except Exception:
                pass
            console.print("")
            _print_tail_schedule_grid(rotation["tail_number"])
            console.print("[dim]View flights with option 10 'View Flight Board'[/dim]\n")
            
        except Exception as e:
            console.print(f"\n[red]✗ Failed to assign rotation: {e}[/red]\n")
        
    elif sched_choice == '2':
        # Detailed scheduling
        try:
            console.print("\n[bold cyan]═══ DETAILED FLIGHT SCHEDULING ═══[/bold cyan]")
            console.print(
                "[dim]This pattern is saved for every in-game week. "
                "Two or more routes: same operating days for all legs; each day runs the full rotation in order, "
                "starting at the first-leg time you enter (later legs follow after flight time + turnaround).[/dim]\n"
            )
            
            gs_week = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
            game_week_sched = int(gs_week["game_week"]) if gs_week else 1
            
            if len(route_ids) >= 2:
                console.print(
                    "[yellow]Routes must connect end-to-end and the last leg must land where the first leg departs "
                    "(closed loop each day), e.g. … → ATL so the next day can start at ATL.[/yellow]\n"
                )
                airline_info = db.fetch_one("SELECT callsign FROM airline WHERE id = 1")
                console.print("\n[bold]Operating days (shared by every leg)[/bold]")
                console.print("  [cyan]1.[/cyan] Daily (7 days/week)")
                console.print("  [cyan]2.[/cyan] Weekdays only (Mon-Fri)")
                console.print("  [cyan]3.[/cyan] Weekends only (Sat-Sun)")
                console.print("  [cyan]4.[/cyan] Custom days\n")
                days_choice = input("Choice (1-4): ").strip()
                if days_choice == '1':
                    days_of_week = "DAILY"
                elif days_choice == '2':
                    days_of_week = '["MON","TUE","WED","THU","FRI"]'
                elif days_choice == '3':
                    days_of_week = '["SAT","SUN"]'
                elif days_choice == '4':
                    console.print("\n[dim]Select days (comma-separated):[/dim]")
                    console.print("1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun")
                    days_input = input("Days: ").strip()
                    day_map = {'1': 'MON', '2': 'TUE', '3': 'WED', '4': 'THU', '5': 'FRI', '6': 'SAT', '7': 'SUN'}
                    selected_days = [day_map[d.strip()] for d in days_input.split(',') if d.strip() in day_map]
                    days_of_week = "[" + ",".join(f'"{d}"' for d in selected_days) + "]"
                else:
                    console.print("[red]✗ Invalid choice, using Daily[/red]")
                    days_of_week = "DAILY"
                
                console.print("\n[bold]First leg of each day (HH:MM)[/bold]")
                console.print("[dim]Local time when the rotation starts (first route only).[/dim]")
                departure_time = input("Time (HH:MM): ").strip()
                if not departure_time or ':' not in departure_time:
                    console.print("[red]✗ Invalid time format, using 08:00[/red]")
                    departure_time = "08:00"
                
                flight_numbers = []
                for i, route_id in enumerate(route_ids, 1):
                    console.print(f"\n[bold]Leg {i}/{len(route_ids)}: {route_id}[/bold]")
                    console.print("[dim]Flight number (Enter for auto: sticky/random, overlap-checked)[/dim]")
                    fn = input("Flight number: ").strip().upper()
                    flight_numbers.append(fn)
                
                try:
                    result = create_chained_detailed_rotation(
                        tail_number, route_ids, flight_numbers, days_of_week, departure_time
                    )
                    console.print(
                        f"\n[green]✓ Chained rotation saved: {result['segments_planned']} segment(s) this week.[/green]"
                    )
                    console.print(
                        f"  Plan: {format_rotation_airport_chain(result['route_ids'])}"
                    )
                    try:
                        from engine.scheduling import get_tail_weekly_utilization
                        u = get_tail_weekly_utilization(tail_number)
                        console.print(
                            f"  Week airborne: {u['airborne_hours']:.1f} / {u['cap_hours']:.0f} h "
                            f"({u['utilization_pct']:.0f}% of weekly cap)"
                        )
                    except Exception:
                        pass
                    _print_tail_schedule_grid(tail_number)
                    console.print("[dim]View flights with option 10 'View Flight Board'[/dim]\n")
                except Exception as e:
                    console.print(f"\n[red]✗ Failed to schedule chained rotation: {e}[/red]\n")
            else:
                flights_created = 0
                template_items = []
                
                for i, route_id in enumerate(route_ids, 1):
                    console.print(f"\n[bold]Flight {i}/{len(route_ids)}: {route_id}[/bold]")
                    
                    console.print("[dim]Flight number (press Enter for auto: sticky/random)[/dim]")
                    flight_number = input("Flight number: ").strip().upper()
                    
                    console.print("\n[bold]Operating Days:[/bold]")
                    console.print("  [cyan]1.[/cyan] Daily (7 days/week)")
                    console.print("  [cyan]2.[/cyan] Weekdays only (Mon-Fri)")
                    console.print("  [cyan]3.[/cyan] Weekends only (Sat-Sun)")
                    console.print("  [cyan]4.[/cyan] Custom days\n")
                    
                    days_choice = input("Choice (1-4): ").strip()
                    
                    if days_choice == '1':
                        days_of_week = "DAILY"
                    elif days_choice == '2':
                        days_of_week = '["MON","TUE","WED","THU","FRI"]'
                    elif days_choice == '3':
                        days_of_week = '["SAT","SUN"]'
                    elif days_choice == '4':
                        console.print("\n[dim]Select days (comma-separated):[/dim]")
                        console.print("1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun")
                        days_input = input("Days: ").strip()
                        day_map = {'1': 'MON', '2': 'TUE', '3': 'WED', '4': 'THU', '5': 'FRI', '6': 'SAT', '7': 'SUN'}
                        selected_days = [day_map[d.strip()] for d in days_input.split(',') if d.strip() in day_map]
                        days_of_week = "[" + ",".join(f'"{d}"' for d in selected_days) + "]"
                    else:
                        console.print("[red]✗ Invalid choice, using Daily[/red]")
                        days_of_week = "DAILY"
                    
                    console.print("\n[bold]Departure Time:[/bold]")
                    console.print("[dim]Enter in 24-hour format (HH:MM)[/dim]")
                    departure_time = input("Time (HH:MM): ").strip()
                    
                    if not departure_time or ':' not in departure_time:
                        console.print("[red]✗ Invalid time format, using 08:00[/red]")
                        departure_time = "08:00"
                    
                    try:
                        sched_result = create_flight_schedule(
                            tail_number, route_id, flight_number,
                            days_of_week, departure_time
                        )
                        template_items.append(sched_result["template_item"])
                        console.print(f"[green]✓ Flight {flight_number} scheduled[/green]")
                        flights_created += 1
                    except Exception as e:
                        console.print(f"[red]✗ Failed to schedule flight: {e}[/red]")
                
                if flights_created > 0:
                    merge_detailed_weekly_template(tail_number, template_items, game_week_sched)
                    console.print(f"\n[green]✓ Successfully scheduled {flights_created} flight(s)![/green]")
                    try:
                        from engine.scheduling import get_tail_weekly_utilization
                        u = get_tail_weekly_utilization(tail_number)
                        console.print(
                            f"  Week airborne: {u['airborne_hours']:.1f} / {u['cap_hours']:.0f} h "
                            f"({u['utilization_pct']:.0f}% of weekly cap)"
                        )
                    except Exception:
                        pass
                    _print_tail_schedule_grid(tail_number)
                    console.print("[dim]View flights with option 10 'View Flight Board'[/dim]\n")
                else:
                    console.print("\n[red]✗ No flights were scheduled[/red]\n")
                
        except Exception as e:
            console.print(f"\n[red]✗ Failed to create detailed schedule: {e}[/red]\n")
    
    else:
        console.print("\n[red]✗ Invalid choice[/red]\n")


