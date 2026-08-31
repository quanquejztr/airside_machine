"""
Airside Machine - Main Entry Point
Phase 3: Continuous game clock, scheduling, weekly settlement
"""

import argparse
import os
import sys
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console

from db import db
from db.seed import run_seed
from engine import setup, aircraft, airports, routes
from ui import panels
from ui import commands
from ui.dashboard import run_dashboard_cli
from ui.flight_board import game_time_status_line

console = Console()


def print_header():
    """Print game header with current game time when available."""
    console.print("\n" + "=" * 60)
    console.print("[bold cyan]AIRSIDE MACHINE[/bold cyan]")
    console.print("Continuous clock · 30 real sec = 1 game hour at 1×")
    console.print("=" * 60)
    try:
        from engine.clock import get_game_time
        gt = get_game_time()
        if gt:
            ghe = float(gt["game_hours_elapsed"])
            sp = int(gt["speed_multiplier"])
            console.print(f"[dim]{game_time_status_line(ghe, sp)}[/dim]")
    except Exception:
        pass
    try:
        if setup.airline_exists():
            from engine.fuel import sanitize_fuel_price_in_db
            from ui.fuel_ticker import format_fuel_ticker_line

            sanitize_fuel_price_in_db()
            console.print(f"[yellow]{format_fuel_ticker_line()}[/yellow]")
    except Exception:
        pass
    console.print()


def init_database():
    """Initialize database and seed data."""
    print("Initializing database...")
    
    # Check if database already exists
    if db.db_exists():
        print("⚠ Database already exists.")
        response = input("Do you want to recreate it? (yes/no): ").lower()
        if response != 'yes':
            print("Keeping existing database.")
            return
        print("\nRecreating database...")
    
    # Initialize schema
    db.init_db()
    
    # Seed data
    print()
    run_seed()


def ensure_game_clock():
    """
    Start the background GameClock thread (daemon) if an airline exists.
    Paused (0×) until the player chooses a speed in Game Clock menu.
    """
    from engine.clock import start_game_clock, get_global_clock
    from engine.scheduling import on_departure, on_arrival, spawn_rotation_segments_for_week
    from engine.ai_flights import ai_on_departure as ai_on_departure_cb, ai_on_arrival as ai_on_arrival_cb
    from engine.settlement import enqueue_settlement_after_week_boundary
    
    if not setup.airline_exists():
        return None

    try:
        from engine.gates import ensure_weekly_airport_auctions
        from engine.slots import (
            ensure_weekly_slot_auctions,
            grandfather_historic_slot_holdings,
            seed_slot_controlled_airports,
        )

        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        seed_slot_controlled_airports()
        grandfather_historic_slot_holdings(cur_week)
        ensure_weekly_slot_auctions(cur_week)
        ensure_weekly_airport_auctions(cur_week)
    except Exception:
        pass

    # Ensure the current week's segments exist from saved templates.
    # This prevents "new week, no flights" cases if week-roll settlement didn't run for any reason.
    try:
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        spawn_rotation_segments_for_week(cur_week)
    except Exception:
        pass

    # Ensure AI competitors are seeded and the current week's AI segments exist.
    # Without this, week 1 can show zero AI flights until the first rollover.
    try:
        from engine.ai import ai_bootstrap_if_needed, ensure_competitors_seeded
        from engine.ai_flights import spawn_ai_segments_for_week

        ensure_competitors_seeded()
        gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        cur_week = int(gs["game_week"]) if gs and gs["game_week"] is not None else 1
        ai_bootstrap_if_needed(cur_week)
        spawn_ai_segments_for_week(cur_week)
    except Exception:
        pass
    
    existing = get_global_clock()
    if existing is not None and existing.is_alive():
        try:
            from engine.settlement import catch_up_missing_settlements

            catch_up_missing_settlements()
        except Exception:
            pass
        return existing
    
    def on_week_roll(new_week):
        if new_week <= 1:
            return
        enqueue_settlement_after_week_boundary(new_week)

    def on_fuel_tick(game_hours_elapsed, speed_multiplier):
        try:
            from engine import fuel as fuel_mod

            fuel_mod.tick_fuel_price(game_hours_elapsed, speed_multiplier)
        except Exception:
            pass
        try:
            from engine.map_bridge import maybe_push_map_update

            maybe_push_map_update(game_hours_elapsed)
        except Exception:
            pass

    clock = start_game_clock(
        on_week=on_week_roll,
        on_tick=on_fuel_tick,
        on_departure=on_departure,
        on_arrival=on_arrival,
        on_ai_departure=ai_on_departure_cb,
        on_ai_arrival=ai_on_arrival_cb,
    )
    try:
        from engine.settlement import catch_up_missing_settlements

        catch_up_missing_settlements()
    except Exception:
        pass
    return clock


def show_stats():
    """Show database statistics."""
    print("\n" + "=" * 60)
    print("DATABASE STATISTICS")
    print("=" * 60)
    
    try:
        # Count airports by category
        airports = db.list_airports()
        regional = len([a for a in airports if a['category'] == 'REGIONAL'])
        national = len([a for a in airports if a['category'] == 'NATIONAL'])
        international = len([a for a in airports if a['category'] == 'INTERNATIONAL'])
        
        print(f"\nAirports: {len(airports):,} total")
        print(f"  - REGIONAL: {regional:,}")
        print(f"  - NATIONAL: {national:,}")
        print(f"  - INTERNATIONAL: {international:,}")
        
        # Count aircraft by category
        aircraft = db.list_aircraft_types()
        turboprop = len([a for a in aircraft if a['category'] == 'TURBOPROP'])
        regional_jet = len([a for a in aircraft if a['category'] == 'REGIONAL_JET'])
        narrow = len([a for a in aircraft if a['category'] == 'NARROW'])
        wide = len([a for a in aircraft if a['category'] == 'WIDE'])
        
        print(f"\nAircraft Types: {len(aircraft):,} total")
        print(f"  - TURBOPROP: {turboprop:,}")
        print(f"  - REGIONAL_JET: {regional_jet:,}")
        print(f"  - NARROW: {narrow:,}")
        print(f"  - WIDE: {wide:,}")
        
        # Show sample airports
        print(f"\nSample Airports:")
        sample_airports = db.fetch_all("SELECT iata, name, category, gate_count FROM airports LIMIT 5")
        for airport in sample_airports:
            print(f"  {airport['iata']}: {airport['name']} ({airport['category']}, {airport['gate_count']} gates)")
        
        # Show sample aircraft
        print(f"\nSample Aircraft:")
        sample_aircraft = db.fetch_all("SELECT type_id, display_name, category, range_nm FROM aircraft_types LIMIT 5")
        for aircraft in sample_aircraft:
            print(f"  {aircraft['type_id']}: {aircraft['display_name']} ({aircraft['category']}, {aircraft['range_nm']:,} nm)")
        
        print()
        
    except Exception as e:
        print(f"\n✗ Error reading database: {e}\n")


def reset_database():
    """Reset the entire database to start fresh."""
    console.print("\n[bold red]⚠ WARNING: This will delete EVERYTHING![/bold red]")
    console.print("[yellow]All airlines, fleet, routes, flights, and progress will be lost.[/yellow]\n")
    
    confirm = input("Type 'RESET' to confirm: ").strip()
    
    if confirm != 'RESET':
        console.print("[cyan]Reset cancelled.[/cyan]\n")
        return
    
    try:
        console.print("\n[cyan]Resetting database...[/cyan]")
        
        from engine.clock import stop_game_clock
        stop_game_clock()
        
        # Delete the database file completely
        import os
        db_path = Path(__file__).parent / "db" / "airline_sim.db"
        if db_path.exists():
            console.print("[dim]Deleting old database...[/dim]")
            os.remove(db_path)
        
        # Recreate database from scratch
        db.init_db()
        run_seed()
        
        console.print("[bold green]✓ Database reset complete![/bold green]")
        console.print("[dim]You can now create a new airline with $1B starting cash.[/dim]\n")
        
    except Exception as e:
        console.print(f"[bold red]✗ Error resetting database: {e}[/bold red]\n")


def print_command_help():
    """One-line reference for all main-menu commands."""
    console.print("\n[bold cyan]Commands[/bold cyan] [dim](type at main prompt)[/dim]")
    console.print(
        "  [cyan]help[/cyan] · list commands  ·  [cyan]1[/cyan] Create/view airline  ·  "
        "[cyan]2[/cyan] Aircraft catalog  ·  [cyan]3[/cyan] Fleet  ·  [cyan]4[/cyan] Buy/lease aircraft"
    )
    console.print(
        "  [cyan]5[/cyan] Airports  ·  [cyan]6[/cyan] Open route  ·  [cyan]7[/cyan] View routes  ·  "
        "[cyan]8[/cyan] Route pricing & demand"
    )
    console.print(
        "  [cyan]9[/cyan] Assign flight rotation  ·  [cyan]10[/cyan] Flight board (live)  ·  "
        "[cyan]11[/cyan] DB stats  ·  [cyan]12[/cyan] Reset database"
    )
    console.print(
        "  [cyan]13[/cyan] Game clock  ·  [cyan]14[/cyan] Week summary  ·  "
        "[cyan]15[/cyan] KPI & analytics  ·  [cyan]16[/cyan] Aircraft schedule (detail)"
    )
    console.print(
        "  [cyan]17[/cyan] Fuel desk (hedge / reserve / alerts)  ·  [cyan]18[/cyan] Exit  ·  "
        "[cyan]19[/cyan] Market intel (competitors / routes / contested markets)"
    )
    console.print(
        "  [dim]Fuel commands:[/dim] [cyan]hedge <weeks>[/cyan] · [cyan]cancel_hedge[/cyan] · "
        "[cyan]buy_fuel <gal>[/cyan] · [cyan]set_dip_alert <$/bbl>[/cyan] · [cyan]clear_dip_alert[/cyan] · "
        "[cyan]ack_fuel[/cyan]"
    )
    console.print(
        "  [dim]Gate auctions & ops:[/dim] [cyan]gate_auctions[/cyan] · [cyan]gate_bid <auction_id> <units> <$/unit>[/cyan] · "
        "[cyan]gate_bids[/cyan] · [cyan]gates[/cyan] · [cyan]slots[/cyan] · "
        "[cyan]slot_bid <auction_id|IATA> <units> <$/unit>[/cyan] · [cyan]board [IATA][/cyan] · [cyan]notifications[/cyan]"
    )
    console.print(
        "  [dim]Bank:[/dim] [cyan]bank[/cyan] · [cyan]loan_offers <amount>[/cyan] · "
        "[cyan]loan_take <weeks|index> <amount>[/cyan] · [cyan]loan_payoff <id>[/cyan] · [cyan]loans[/cyan]"
    )
    console.print(
        "  [dim]AI debug:[/dim] [cyan]ai_debug <AI_ID>[/cyan] · [cyan]ai_candidates <AI_ID>[/cyan] · [cyan]ai_log <AI_ID>[/cyan]"
    )
    console.print()


def show_menu():
    """Show main menu with updated session status."""
    try:
        from ui.week_summary import try_show_pending_week_summary

        try_show_pending_week_summary(console)
    except Exception:
        pass
    print_header()  # Show updated session timer
    try:
        from engine.clock import get_global_clock

        clk = get_global_clock()
        if clk is not None and clk.is_alive():
            st = clk.get_status()
            alert = st.get("auto_pause_alert")
            if alert:
                console.print(f"[bold yellow]⚠ AUTO-PAUSE:[/bold yellow] {alert}")
                console.print("[dim]Open menu 13 and set speed > 0 to resume.[/dim]\n")
    except Exception:
        pass
    print("=" * 60)
    print("MAIN MENU")
    print("=" * 60)
    print("1. Create/View Airline")
    print("2. Aircraft Catalog")
    print("3. View Fleet")
    print("4. Buy/Lease Aircraft")
    print("5. View Airports")
    print("6. Open Route")
    print("7. View Routes")
    print("8. Manage Route Pricing & Demand")
    print("9. Assign Flight Rotation")
    print("10. View Flight Board (Monitor Flights)")
    print("11. Database Stats")
    print("12. Reset Database (Start Over)")
    print("13. Game Clock (pause / speed / Phase 9 tests — 13 = clear weather & alerts)")
    print("14. Week Summary (on-demand, up to date)")
    print("15. KPI & Route Analytics (Phase 6 — dashboard / stats / route / compare)")
    print("16. View Aircraft Schedule (detail — list + weekly grid; clear schedule)")
    print("17. Fuel desk — hedge, reserve, dip alerts (Phase 8)")
    print("18. Exit")
    print("19. Market / competitors (Phase 10 — market, routes, competitors, …)")
    print("=" * 60)


def run_main_menu_choice(raw_choice: str):
    """
    Execute one main-menu selection (same mapping as the interactive loop).
    Returns None to keep playing, or 'exit' after menu 18 (quit).
    """
    choice = raw_choice.strip().lower()
    if setup.airline_exists():
        try:
            from engine import fuel as fin

            if choice.startswith("hedge "):
                w = int(choice.split()[1])
                fin.hedge_fuel(w)
                console.print("[green]Hedge recorded. Use menu 17 for details.[/green]")
                return None
            if choice == "cancel_hedge":
                fin.cancel_hedge()
                console.print("[green]Hedge cancelled.[/green]")
                return None
            if choice.startswith("buy_fuel "):
                g = float(choice.split()[1].replace(",", ""))
                fin.buy_reserve(g)
                console.print("[green]Reserve purchase recorded.[/green]")
                return None
            if choice.startswith("set_dip_alert "):
                p = float(choice.split()[1].replace(",", ""))
                fin.set_dip_alert(p)
                console.print("[green]Dip alert set.[/green]")
                return None
            if choice == "clear_dip_alert":
                fin.set_dip_alert(None)
                console.print("[green]Dip alert cleared.[/green]")
                return None
            if choice in ("ack_fuel", "ack_fuel_shock"):
                fin.acknowledge_fuel_shock()
                console.print("[green]Fuel shock acknowledged.[/green]")
                return None
            # Phase 11 revised: gate auctions replace route-licence auctions.
            if choice in ("notifications", "n"):
                from ui.notifications import show_notifications_cli

                show_notifications_cli(console)
                return None
            if choice in ("slots", "slot") or choice.startswith("slot_bid"):
                from ui.slots import handle_slots_cli

                handle_slots_cli(console, choice)
                return None
            if choice in ("bank", "loans") or choice.startswith("loan_"):
                from ui.banking import handle_bank_cli

                handle_bank_cli(console, choice)
                return None
            if choice in ("gate_auctions", "gates", "gate_bids") or choice.startswith("gate_bid "):
                from ui.gate_auctions import handle_gate_cli

                handle_gate_cli(console, choice)
                return None
            if choice.startswith("board"):
                parts = choice.split()
                from ui.airport_board import airport_board

                gs = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
                gw = int(gs["game_week"] or 1) if gs else 1
                if len(parts) >= 2:
                    airport_board(console, parts[1], gw)
                else:
                    hub = db.fetch_one("SELECT home_hub_iata FROM airline WHERE id = 1")
                    airport_board(console, str(hub["home_hub_iata"]) if hub else "JFK", gw)
                return None
            if choice.startswith("ai_debug") or choice.startswith("ai_candidates") or choice.startswith("ai_log"):
                from ui.ai_debug import handle_ai_debug_cli

                handle_ai_debug_cli(console, choice)
                return None
        except Exception as e:
            console.print(f"[red]{e}[/red]")
            return None
    if choice in ("help", "h", "?"):
        print_command_help()
        return None
    if choice == "1":
        create_or_view_airline()
        return None
    if choice == "2":
        browse_aircraft_catalog()
        return None
    if choice == "3":
        view_fleet()
        return None
    if choice == "4":
        commands.buy_or_lease_aircraft()
        return None
    if choice == "5":
        view_airports()
        return None
    if choice == "6":
        commands.open_new_route()
        return None
    if choice == "7":
        commands.view_routes()
        return None
    if choice == "8":
        commands.manage_route_pricing()
        return None
    if choice == "9":
        commands.assign_flight_rotation()
        return None
    if choice == "10":
        view_flight_board()
        return None
    if choice == "11":
        show_stats()
        return None
    if choice == "12":
        reset_database()
        return None
    if choice == "13":
        game_clock_menu()
        return None
    if choice == "14":
        view_week_summary()
        return None
    if choice == "15":
        open_kpi_dashboard()
        return None
    if choice == "16":
        view_aircraft_schedule_detail()
        return None
    if choice == "17":
        fuel_desk_menu()
        return None
    if choice == "18":
        from engine.clock import stop_game_clock

        stop_game_clock()
        print("\nExiting. Thanks for playing!\n")
        return "exit"
    if choice == "19":
        from ui.market_intel import market_intel_cli

        market_intel_cli(console)
        return None
    print("\n✗ Invalid choice. Enter 1-19 or help.")
    return None


def create_or_view_airline():
    """Create airline or view existing airline."""
    if setup.airline_exists():
        panels.render_airline_info()
    else:
        print("\nNo airline exists. Let's create one!")
        setup.create_airline()
        # Seed competitors + spawn their week-1 flight segments immediately.
        try:
            from engine.ai import ai_bootstrap_if_needed, ensure_competitors_seeded
            from engine.ai_flights import spawn_ai_segments_for_week
            from engine.scheduling import calendar_game_week_from_state

            ensure_competitors_seeded()
            gw = calendar_game_week_from_state()
            ai_bootstrap_if_needed(gw)
            spawn_ai_segments_for_week(gw)
        except Exception:
            pass
        ensure_game_clock()


def browse_aircraft_catalog():
    """Browse aircraft catalog with filters."""
    print("\nFilter by category? (TURBOPROP/REGIONAL_JET/NARROW/WIDE or press Enter for all)")
    category = input("Category: ").strip().upper()
    
    if category and category not in ['TURBOPROP', 'REGIONAL_JET', 'NARROW', 'WIDE']:
        print("Invalid category. Showing all aircraft.")
        category = None
    
    panels.render_aircraft_catalog(category, limit=30)


def view_fleet():
    """View current fleet with actions: renew lease, reconfigure cabin."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return

    while True:
        panels.render_fleet()
        console.print("[bold cyan]═══ FLEET ACTIONS ═══[/bold cyan]")
        console.print("  [cyan]1.[/cyan] Renew aircraft lease")
        console.print("  [cyan]2.[/cyan] Reconfigure cabin (seat layout)")
        console.print("  [cyan]3.[/cyan] Back to main menu\n")
        sub = input("Choice (1-3, Enter = back): ").strip()
        if sub not in ("1", "2"):
            break

        if sub == "1":
            commands._fleet_renew_lease()
        elif sub == "2":
            commands._fleet_reconfigure_cabin()


def view_airports():
    """View airports menu."""
    while True:
        print("\n" + "=" * 60)
        print("AIRPORT LOOKUP")
        print("=" * 60)
        print("1. Search by IATA code")
        print("2. Search by name/city")
        print("3. List by category")
        print("4. Back to Main Menu")

        choice = input("\nChoice: ").strip()
        if not choice or choice == "4":
            return

        if choice == '1':
            iata = input("Enter IATA code: ").strip().upper()
            if not iata:
                console.print("[yellow]Cancelled.[/yellow]\n")
                continue
            panels.render_airport_info(iata)
            continue

        if choice == '2':
            query = input("Enter search term: ").strip()
            if not query:
                console.print("[yellow]Cancelled.[/yellow]\n")
                continue
            from engine.airports import display_airport_search_results

            display_airport_search_results(query)
            continue

        if choice == '3':
            print("\nCategories: REGIONAL, NATIONAL, INTERNATIONAL")
            category = input("Enter category: ").strip().upper()
            if category not in ['REGIONAL', 'NATIONAL', 'INTERNATIONAL']:
                print("Invalid category.")
                continue
            from engine.airports import list_airports_by_category

            results = list_airports_by_category(category)
            print(f"\nFound {len(results)} {category} airports")
            for ap in results[:20]:
                print(f"  {ap['iata']}: {ap['name']}")
            if len(results) > 20:
                print(f"  ... and {len(results) - 20} more")
            continue

        print("Invalid choice.")


        # stay in this menu after attempt (success or failure)


def view_flight_board():
    """View flight board showing all scheduled, in-air, and landed flights."""
    if not setup.airline_exists():
        print("\n✗ Create an airline first!\n")
        return
    
    from engine.scheduling import get_all_flights
    from ui.flight_board import display_flight_board_live
    
    flights = get_all_flights()
    
    if not flights:
        console.print("\n[yellow]⚠ No flights scheduled for this week![/yellow]")
        console.print("[dim]Use option 9 'Assign Flight Rotation' to schedule flights first.[/dim]\n")
        return
    
    console.print("\n[bold cyan]═══ FLIGHT BOARD ═══[/bold cyan]")
    try:
        from engine.clock import get_game_time

        gt = get_game_time()
        sp = int(gt["speed_multiplier"]) if gt else 0
        if sp == 0:
            console.print(
                "[dim]Game clock is paused — open menu 13 and set speed > 0 so game time (and statuses) advance.[/dim]\n"
            )
        else:
            console.print(
                f"[dim]Game clock at {sp}× — flight board refreshes live below "
                f"(AIRLINE_SIM_NO_LIVE=1 for one snapshot only).[/dim]\n"
            )
    except Exception:
        console.print("[dim]Flight list shown below.[/dim]\n")

    display_flight_board_live(flights)


def view_week_summary():
    """Show P/L-style week summary for any week (settled or live snapshot)."""
    if not setup.airline_exists():
        console.print("\n[yellow]Create an airline first.[/yellow]\n")
        return
    try:
        from ui.week_summary import show_week_summary_interactive

        show_week_summary_interactive(console)
    except Exception as e:
        console.print(f"[red]{e}[/red]\n")


def view_aircraft_schedule_detail():
    """List all legs for one tail this game week, then the Mon–Sun × hour grid."""
    if not setup.airline_exists():
        console.print("\n[yellow]Create an airline first.[/yellow]\n")
        return
    tail = input("\nTail number (e.g. N123AB): ").strip().upper()
    if not tail:
        console.print("[yellow]Cancelled.[/yellow]\n")
        return
    ac = aircraft.get_fleet_aircraft(tail)
    if not ac:
        console.print(f"[red]No aircraft '{tail}' in your fleet.[/red]\n")
        return
    try:
        from ui.tail_schedule_grid import print_tail_schedule_detail

        print_tail_schedule_detail(console, tail)
    except Exception as e:
        console.print(f"[red]{e}[/red]\n")
        return

    while True:
        console.print("\n[bold cyan]═══ SCHEDULE ACTIONS ═══[/bold cyan]")
        console.print("  [cyan]1.[/cyan] Back to main menu")
        console.print(
            "  [cyan]2.[/cyan] Clear this aircraft's weekly schedule "
            "[dim](saved rotation + SCHEDULED legs — same as menu 9 → option 3)[/dim]\n"
        )
        sub = input("Choice (1-2, Enter = back): ").strip()
        if sub != "2":
            break

        gs = db.fetch_one("SELECT game_hours_elapsed FROM game_state WHERE id = 1")
        ghe = float(gs["game_hours_elapsed"] or 0.0) if gs else 0.0
        row = db.fetch_one(
            """
            SELECT COUNT(*) AS c FROM flight_segments
            WHERE tail_number = ? AND status = 'IN_AIR'
              AND scheduled_arr_game_hour > ?
            """,
            (tail, ghe),
        )
        in_air = int(row["c"] or 0) if row else 0
        if in_air:
            console.print(
                f"\n[yellow]This tail has {in_air} flight(s) in the air. "
                "Those legs are kept until they land; everything else for this week is cleared.[/yellow]"
            )
        console.print(
            f"\n[bold]Clear[/bold] this aircraft's rotation: templates, future legs, and completed "
            f"legs this calendar week for [cyan]{tail}[/cyan] (cash is not reversed). "
            "Set aircraft to IDLE if nothing is in flight?"
        )
        ok = input("Type CLEAR to confirm (or Enter to cancel): ").strip().upper()
        if ok != "CLEAR":
            console.print("[dim]Cancelled.[/dim]")
            continue

        try:
            from engine.scheduling import cancel_rotation
            from ui.tail_schedule_grid import print_tail_schedule_detail

            cancel_rotation(tail, wipe_completed_this_week=True)
            console.print(
                f"\n[green]✓ Cleared rotation, templates, and week schedule rows for {tail}. "
                "Stuck \"in flight\" legs past their ETA were removed; at most one active leg may remain.[/green]\n"
            )
            console.print("[dim]Updated grid:[/dim]\n")
            print_tail_schedule_detail(console, tail)
        except Exception as e:
            console.print(f"\n[red]✗ Could not clear schedule: {e}[/red]\n")
        break


def game_clock_menu():
    """Pause, resume, set speed, view game time."""
    if not setup.airline_exists():
        console.print("\n[yellow]Create an airline first.[/yellow]\n")
        return
    
    clk = ensure_game_clock()
    if clk is None:
        console.print("\n[red]Could not start game clock.[/red]\n")
        return

    from ui.week_summary import (
        get_pause_on_week_summary,
        set_pause_on_week_summary,
        try_show_pending_week_summary,
    )
    
    while True:
        try:
            try_show_pending_week_summary(console)
        except Exception:
            pass
        console.print("\n[bold cyan]═══ GAME CLOCK ═══[/bold cyan]")
        console.print(
            "  [dim]Phase 9: 11 = resolve AOG · 12 = trigger_weather · "
            "13 = clear weather / blackout / alert (skip)[/dim]"
        )
        try:
            st = clk.get_status()
            console.print(f"  {st['time_display']}")
            console.print(
                f"  Game hours (total): {st['game_hours_elapsed']:.2f} · "
                f"Speed: {st['speed_multiplier']}× · "
                f"{'Paused' if st['is_paused'] else 'Running'}"
            )
            alert = st.get("auto_pause_alert")
            if alert:
                console.print(f"  [bold yellow]Alert:[/bold yellow] {alert}")
        except Exception as e:
            console.print(f"  [dim]{e}[/dim]")
        
        console.print("\n  [cyan]1.[/cyan] Resume / run at 1×")
        console.print("  [cyan]2.[/cyan] Pause")
        console.print("  [cyan]3.[/cyan] Set speed 2×")
        console.print("  [cyan]4.[/cyan] Set speed 4×")
        console.print("  [cyan]4s.[/cyan] Speedrun: set speed 20×")
        console.print("  [cyan]6s.[/cyan] Turbo: set speed 60×")
        console.print("  [cyan]5.[/cyan] Back to main menu")
        p = get_pause_on_week_summary()
        console.print(
            f"  [cyan]6.[/cyan] Toggle pause when week summary pops "
            f"[dim](now: {'ON' if p else 'OFF'})[/dim]"
        )
        console.print(
            "  [cyan]7.[/cyan] Simulate AOG on a tail [dim](auto-pause test)[/dim]"
        )
        console.print(
            "  [cyan]8.[/cyan] Simulate weather closure [dim](IATA; auto-pause if flights active)[/dim]"
        )
        console.print(
            "  [cyan]9.[/cyan] Acknowledge fuel shock [dim](Phase 8 banner)[/dim]"
        )
        console.print("  [cyan]10.[/cyan] Debug: force fuel shock [dim](test)[/dim]")
        console.print(
            "  [cyan]11.[/cyan] Resolve AOG on a tail [dim](maintenance fee)[/dim]"
        )
        console.print(
            "  [cyan]12.[/cyan] Phase 9 weather: trigger_weather [dim](timed closure; does not auto-pause from here)[/dim]"
        )
        console.print(
            "  [cyan]13.[/cyan] Clear disruptions [dim](all weather closures + flight-board blackout + dismiss alert)[/dim]"
        )
        console.print(
            "  [cyan]14.[/cyan] Swap aircraft onto a cancelled leg [dim](IDLE tail at origin)[/dim]"
        )
        console.print("  [dim]5 = back to main menu · clock may still be paused — use 1 to resume after clear[/dim]\n")

        sub = input("Choice (1-14, or 4s): ").strip().lower()
        try:
            if sub == "1":
                ok, msg = clk.resume(1, player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub == "2":
                ok, msg = clk.pause(player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub == "3":
                ok, msg = clk.set_speed(2, player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub == "4":
                ok, msg = clk.set_speed(4, player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub in ("4s", "s", "20", "20x", "20×"):
                ok, msg = clk.set_speed(20, player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub in ("6s", "60", "60x", "60×"):
                ok, msg = clk.set_speed(60, player_initiated=True)
                console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            elif sub == "5":
                break
            elif sub == "6":
                cur = get_pause_on_week_summary()
                set_pause_on_week_summary(not cur)
                console.print(
                    f"[green]Pause on week summary: {'ON' if not cur else 'OFF'}[/green]"
                )
            elif sub == "7":
                tail = input("Tail number to ground (AOG): ").strip().upper()
                if not tail:
                    console.print("[yellow]Cancelled.[/yellow]")
                else:
                    from engine.events import trigger_aog

                    try:
                        trigger_aog(tail, reason="Simulated AOG (test)")
                        console.print("[green]AOG applied — clock should auto-pause.[/green]")
                    except Exception as ex:
                        console.print(f"[red]{ex}[/red]")
            elif sub == "8":
                iata = input("Airport IATA to close for weather: ").strip().upper()
                if len(iata) != 3:
                    console.print("[yellow]Need a 3-letter IATA code.[/yellow]")
                else:
                    from engine.environment import (
                        register_weather_closure,
                        clear_weather_closure,
                    )

                    register_weather_closure(iata)
                    console.print(
                        f"[green]Registered closure at {iata}.[/green] "
                        "If you have active flights there, unpause to trigger auto-pause."
                    )
                    clr = input("Clear this closure now? (y/N): ").strip().lower()
                    if clr == "y":
                        clear_weather_closure(iata)
                        console.print("[dim]Closure cleared.[/dim]")
            elif sub == "9":
                from engine.fuel import acknowledge_fuel_shock

                acknowledge_fuel_shock()
                console.print("[green]Fuel shock acknowledged.[/green]")
            elif sub == "10":
                from engine.fuel import debug_force_shock

                debug_force_shock()
                console.print("[yellow]Debug fuel shock applied (auto-pause).[/yellow]")
            elif sub == "11":
                tail = input("Tail number to clear AOG: ").strip().upper()
                if not tail:
                    console.print("[yellow]Cancelled.[/yellow]")
                else:
                    from engine.events import resolve_aog

                    try:
                        fee = resolve_aog(tail)
                        console.print(f"[green]AOG resolved — maintenance ${fee:,.0f} charged.[/green]")
                    except Exception as ex:
                        console.print(f"[red]{ex}[/red]")
            elif sub == "12":
                iata = input("Airport IATA: ").strip().upper()
                dh = input("Duration (game hours, default 2): ").strip()
                try:
                    dur = float(dh) if dh else 2.0
                except ValueError:
                    dur = 2.0
                if len(iata) != 3:
                    console.print("[yellow]Need a 3-letter IATA code.[/yellow]")
                else:
                    import sys

                    from engine.events import trigger_weather

                    console.print("[dim]Applying weather closure…[/dim]")
                    sys.stdout.flush()
                    try:
                        # Do not auto-pause from this test menu — keeps the clock at your
                        # chosen speed so the next prompt is obvious. Random weekly weather
                        # still uses full auto-pause behavior.
                        msg = trigger_weather(
                            iata, dur, request_pause_if_affecting=False
                        )
                    except Exception as ex:
                        console.print(f"[red]Weather failed: {ex}[/red]")
                    else:
                        console.print(f"[green]{msg}[/green]")
                        try:
                            from engine.environment import flight_board_hint_for_closed_airport

                            console.print(flight_board_hint_for_closed_airport(iata))
                        except Exception:
                            pass
                        console.print(
                            "[dim]You can keep using this menu (1–13) or 5 = main menu.[/dim]"
                        )
                    sys.stdout.flush()
            elif sub == "13":
                from engine.events import clear_player_disruptions

                clear_player_disruptions()
                console.print(
                    "[green]Cleared weather closures, UI blackout, and auto-pause alert.[/green]\n"
                    "[dim]If the game is still paused, choose 1 to resume.[/dim]"
                )
            elif sub == "14":
                sid = input("Cancelled segment_id: ").strip()
                tail = input("Replacement tail (IDLE, at origin): ").strip().upper()
                if not sid or not tail:
                    console.print("[yellow]Cancelled.[/yellow]")
                else:
                    from engine.events import swap_aircraft

                    try:
                        new_id = swap_aircraft(sid, tail)
                        console.print(f"[green]Swap scheduled as {new_id}[/green]")
                    except Exception as ex:
                        console.print(f"[red]{ex}[/red]")
            elif sub in ("help", "h", "?"):
                console.print(
                    "[dim]Speed uses game clock; week boundary runs settlement in the background. "
                    "Use 7/8 only for testing auto-pause. KPI & week summary are on the main menu.[/dim]"
                )
            else:
                console.print("[yellow]Invalid choice.[/yellow]")
        except Exception as ex:
            console.print(f"[red]{ex}[/red]")


def open_kpi_dashboard():
    """Phase 6 analytics CLI — does not pause the game clock."""
    if not setup.airline_exists():
        console.print("\n[yellow]Create an airline first.[/yellow]\n")
        return
    run_dashboard_cli(console)


def query_airports():
    """Interactive airport query."""
    print("\n" + "=" * 60)
    print("AIRPORT QUERY")
    print("=" * 60)
    
    query = input("\nEnter airport IATA code (or 'list' to see all): ").strip().upper()
    
    if query == 'LIST':
        airports_list = db.list_airports()
        print(f"\nFound {len(airports_list):,} airports:\n")
        for airport in airports_list[:20]:  # Show first 20
            print(f"{airport['iata']}: {airport['name']} ({airport['city']}, {airport['category']})")
        if len(airports_list) > 20:
            print(f"... and {len(airports_list) - 20:,} more")
    else:
        airport = db.get_airport(query)
        if airport:
            print(f"\n{airport['name']}")
            print(f"  IATA: {airport['iata']}")
            print(f"  ICAO: {airport['icao']}")
            print(f"  City: {airport['city']}, {airport['country']}")
            print(f"  Category: {airport['category']}")
            print(f"  Gates: {airport['gate_count']}")
            print(f"  Runway: {airport['runway_length_ft']:,} ft" if airport['runway_length_ft'] else "  Runway: N/A")
            print(f"  Location: {airport['lat']:.4f}, {airport['lon']:.4f}")
            print(f"  Timezone: {airport['timezone']}")
        else:
            print(f"\n✗ Airport '{query}' not found")


def query_aircraft():
    """Interactive aircraft query."""
    print("\n" + "=" * 60)
    print("AIRCRAFT QUERY")
    print("=" * 60)
    
    query = input("\nEnter aircraft type_id (or 'list' to see all): ").strip().upper()
    
    if query == 'LIST':
        aircraft_list = db.list_aircraft_types()
        print(f"\nFound {len(aircraft_list):,} aircraft types:\n")
        for ac in aircraft_list[:20]:  # Show first 20
            print(f"{ac['type_id']}: {ac['display_name']} ({ac['category']})")
        if len(aircraft_list) > 20:
            print(f"... and {len(aircraft_list) - 20:,} more")
    else:
        aircraft_data = db.get_aircraft_type(query)
        if aircraft_data:
            print(f"\n{aircraft_data['display_name']}")
            print(f"  Type ID: {aircraft_data['type_id']}")
            print(f"  Category: {aircraft_data['category']}")
            print(f"  Range: {aircraft_data['range_nm']:,} nm")
            print(f"  Cruise Speed: {aircraft_data['cruise_speed_kts']:,} kts")
            print(f"  Fuel Burn: {aircraft_data['fuel_burn_gph']:,.0f} gal/hr")
            print(f"  MTOW: {aircraft_data['mtow_lbs']:,} lbs")
            print(f"  Runway Required: {aircraft_data['runway_req_ft']:,} ft")
            print(f"  Purchase Price: ${aircraft_data['purchase_price']:,}")
            print(f"  Weekly Lease: ${aircraft_data['weekly_lease_cost']:,}")
        else:
            print(f"\n✗ Aircraft '{query}' not found")


def fuel_desk_menu():
    """Hedge, reserve, dip alerts — Phase 8 fuel market."""
    if not setup.airline_exists():
        console.print("\n[yellow]Create an airline first.[/yellow]\n")
        return
    db.ensure_schema_migrations()
    from engine import fuel as fin
    from ui.fuel_ticker import fuel_sparkline

    while True:
        al = setup.get_airline()
        gs = db.fetch_one("SELECT fuel_price_current FROM game_state WHERE id = 1")
        barrel = float(gs["fuel_price_current"] or 195.0) if gs else 195.0
        cash = float(al["cash"] or 0)
        res = float(al.get("fuel_reserve_gallons") or 0)
        avg = al.get("fuel_reserve_avg_price")
        dip = al.get("fuel_dip_alert_price")
        hp = al.get("fuel_hedged_price")
        hw = al.get("fuel_hedged_weeks_remaining")
        burn = fin.estimated_weekly_burn_gallons()
        spot_gal = fin.all_in_spot_from_state()
        prem_rate = float(db.get_financial_constant("fuel_hedge_premium_rate") or 0.05)

        console.print("\n[bold cyan]═══ FUEL DESK ═══[/bold cyan]")
        console.print(f"  Spot [yellow]${barrel:.2f}/bbl[/yellow]  ·  All-in ~[dim]${spot_gal:.3f}/gal[/dim]")
        console.print(f"  Sparkline (weekly closes): [green]{fuel_sparkline(20)}[/green]")
        console.print(
            f"  Est. weekly burn [dim]{burn:,.0f} gal[/dim]  ·  Cash [green]${cash:,.0f}[/green]"
        )
        console.print(
            f"  Reserve [cyan]{res:,.0f} gal[/cyan] avg [dim]${float(avg) if avg is not None else 0:.3f}/gal[/dim]"
        )
        hed = f"${float(hp):.2f}/bbl, {int(hw)} wk left" if hp is not None and hw else "none"
        console.print(f"  Hedge: [yellow]{hed}[/yellow]")
        console.print(
            f"  Dip alert: [dim]{float(dip) if dip is not None else 'off'}[/dim]  ·  "
            f"Premiums: 2wk ~${burn * spot_gal * 2 * prem_rate:,.0f} · "
            f"4wk ~${burn * spot_gal * 4 * prem_rate:,.0f} · "
            f"8wk ~${burn * spot_gal * 8 * prem_rate:,.0f}"
        )
        gs2 = db.fetch_one("SELECT fuel_shock_pending, fuel_shock_message FROM game_state WHERE id = 1")
        if gs2 and int(gs2["fuel_shock_pending"] or 0):
            console.print(
                f"  [bold red]SHOCK PENDING:[/bold red] {gs2['fuel_shock_message'] or ''} "
                "(type [cyan]ack_fuel[/cyan] after reviewing)"
            )
        console.print("\n  [cyan]1[/cyan] Hedge 2wk   [cyan]2[/cyan] Hedge 4wk   [cyan]3[/cyan] Hedge 8wk")
        console.print("  [cyan]4[/cyan] Cancel hedge   [cyan]5[/cyan] Buy reserve   [cyan]6[/cyan] Set dip alert")
        console.print("  [cyan]7[/cyan] Clear dip   [cyan]8[/cyan] Ack fuel shock   [cyan]9[/cyan] Back\n")
        sub = input("Choice (1-9): ").strip()
        try:
            if sub == "1":
                fin.hedge_fuel(2)
                console.print("[green]Hedge active.[/green]")
            elif sub == "2":
                fin.hedge_fuel(4)
                console.print("[green]Hedge active.[/green]")
            elif sub == "3":
                fin.hedge_fuel(8)
                console.print("[green]Hedge active.[/green]")
            elif sub == "4":
                fin.cancel_hedge()
                console.print("[green]Hedge cancelled.[/green]")
            elif sub == "5":
                raw = input("Gallons to add to reserve: ").strip().replace(",", "")
                fin.buy_reserve(float(raw))
                console.print("[green]Reserve updated.[/green]")
            elif sub == "6":
                raw = input("Alert when barrel price falls below ($): ").strip().replace(",", "")
                fin.set_dip_alert(float(raw))
                console.print("[green]Dip alert set.[/green]")
            elif sub == "7":
                fin.set_dip_alert(None)
                console.print("[green]Dip alert cleared.[/green]")
            elif sub == "8":
                fin.acknowledge_fuel_shock()
                console.print("[green]Acknowledged — you can resume speed in menu 13.[/green]")
            elif sub == "9":
                break
            else:
                console.print("[yellow]Invalid choice.[/yellow]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")


def main(map_port=None, ui_port=None):
    """Main CLI loop. Optional map_port: serve flight map. ui_port: full overlay UI."""
    if ui_port is not None:
        from server.game_http import start_game_ui_server

        if not db.db_exists():
            print("Database not found. Initializing...")
            init_database()
        else:
            db.ensure_schema_migrations()
        start_game_ui_server(int(ui_port))
        url = f"http://127.0.0.1:{int(ui_port)}/"
        print("=" * 60)
        print("Airside Machine — map UI")
        print(f"Open in your browser: {url}")
        print("Clock starts paused. Overlay buttons open windows on the map.")
        print("Ctrl+C to quit. CLI is not used in this mode.")
        print("=" * 60)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nExiting.\n")
        return 0
    if map_port is not None:
        try:
            from server.map_http import start_flight_map_server

            start_flight_map_server(int(map_port))
            console.print(
                f"[dim]Flight map (browser): http://127.0.0.1:{int(map_port)}/[/dim]\n"
            )
        except OSError as e:
            console.print(f"[yellow]Flight map server not started: {e}[/yellow]\n")
    # Always start the interactive MapLibre server if Node is available.
    try:
        from engine.map_bridge import start_local_map_server

        start_local_map_server(3000)
    except Exception:
        pass

    print_header()
    
    # Check if database exists
    if not db.db_exists():
        print("Database not found. Initializing...")
        init_database()
    else:
        print("✓ Database found")
        db.ensure_schema_migrations()
        
        # Check if airline exists
        if setup.airline_exists():
            print("✓ Airline loaded")
            panels.render_airline_info()
        else:
            print("\n⚠ No airline found. Create one to get started!")
    
    if setup.airline_exists():
        ensure_game_clock()
        try:
            from ui.week_summary import try_show_pending_week_summary

            try_show_pending_week_summary(console)
        except Exception:
            pass
    
    # Main menu loop
    while True:
        show_menu()
        raw = input("\nEnter choice (1-19) or help: ").strip()

        try:
            outcome = run_main_menu_choice(raw)
            if outcome == "exit":
                break
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}\n")
    
    return 0


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="Airside Machine — terminal game.")
    _parser.add_argument(
        "--map",
        nargs="?",
        const=8775,
        type=int,
        metavar="PORT",
        help="Serve read-only flight map on 127.0.0.1:PORT (default 8775). Tiles load from the internet.",
    )
    _parser.add_argument(
        "--ui",
        nargs="?",
        const=8765,
        type=int,
        metavar="PORT",
        help="Full-screen map UI with overlay windows on 127.0.0.1:PORT (default 8765).",
    )
    _args, _unknown = _parser.parse_known_args()
    try:
        sys.exit(main(map_port=_args.map, ui_port=_args.ui))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Exiting...\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}\n")
        sys.exit(1)
