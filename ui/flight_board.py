"""
Flight Board UI - Phase 3

Uses a vertical Rich Group (not Layout) so the board renders in Cursor/IDE terminals.
Optional Rich Live refresh; set AIRLINE_SIM_NO_LIVE=1 for a single static snapshot only.

IDE terminals (VS Code / Cursor) often break cursor-based Live redraws: each tick appends a
full copy instead of replacing. We default to alternate-screen Live there (``screen=True``);
set AIRLINE_SIM_NO_LIVE_SCREEN=1 to force the old behavior, or AIRLINE_SIM_LIVE_SCREEN=1
to always use alternate screen.
"""

from rich.console import Console, Group
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
import os
import sys
import threading
import time

# Match engine.time_utils.DAYS_LIST (avoid importing engine at module load).
_DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

console = Console()

# Dashboard: a few latest arrivals, then up to N−3 non-landed legs in schedule order.
FLIGHT_BOARD_LANDED_HEAD = 1
FLIGHT_BOARD_NEAREST_LIMIT = 30


def board_weekday_time_label(absolute_game_hour: float, hhmm: str | None) -> str:
    """MON..SUN from position in the 168h week + HH:MM (from row or derived)."""
    h_in_w = float(absolute_game_hour) % 168.0
    di = min(6, max(0, int(h_in_w // 24)))
    day = _DOW[di]
    t = (hhmm or "").strip()
    if not t:
        sod = float(absolute_game_hour) % 24.0
        h = int(sod)
        m = int(round((sod - h) * 60.0)) % 60
        t = f"{h:02d}:{m:02d}"
    return f"{day} {t}"


def dashboard_flights_for_board(
    flights: list,
    current_game_hour: float,
    limit: int = FLIGHT_BOARD_NEAREST_LIMIT,
    landed_head: int = FLIGHT_BOARD_LANDED_HEAD,
) -> list:
    """
    Flight operations board row order:
    1) Up to `landed_head` most recently landed legs (highest scheduled arrival first).
    2) Remaining rows: non-landed legs only, in chronological order from the first segment
       not yet arrived (scheduled / in air / departing / delayed / etc.).
    """
    if not flights or limit <= 0:
        return []
    now = float(current_game_hour)
    landed_cap = max(0, min(landed_head, limit))

    landed = [f for f in flights if f.get("status") == "LANDED"]
    landed.sort(key=lambda f: float(f["scheduled_arr_game_hour"]), reverse=True)
    head = landed[:landed_cap]

    rest_limit = limit - len(head)
    if rest_limit <= 0:
        return head

    rest_pool = [f for f in flights if f.get("status") != "LANDED"]
    rest_sorted = sorted(
        rest_pool,
        key=lambda f: (float(f["scheduled_dep_game_hour"]), str(f.get("segment_id", ""))),
    )
    idx = 0
    for i, f in enumerate(rest_sorted):
        if float(f["scheduled_arr_game_hour"]) >= now:
            idx = i
            break
    else:
        tail = rest_sorted[max(0, len(rest_sorted) - rest_limit) :]
        return head + tail

    tail = rest_sorted[idx : idx + rest_limit]
    return head + tail


def chronological_flights_for_board(
    flights: list, current_game_hour: float, limit: int = FLIGHT_BOARD_NEAREST_LIMIT
) -> list:
    """Backward-compatible name; delegates to :func:`dashboard_flights_for_board`."""
    return dashboard_flights_for_board(flights, current_game_hour, limit=limit)


def format_time_remaining(seconds):
    """
    Format seconds as time remaining string.
    
    Args:
        seconds: Seconds (can be negative for overdue)
    
    Returns:
        str: Formatted time (e.g., "5m 30s", "DEPARTING", "IN AIR")
    """
    if seconds <= 0:
        return "NOW"
    elif seconds < 60:
        return f"{seconds}s"
    else:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"


def format_session_time(seconds):
    """Format session seconds as MM:SS (legacy)."""
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes:02d}:{secs:02d}"


def game_time_status_line(game_hours_elapsed: float, speed_mult: int) -> str:
    """Human-readable game date from absolute game hours."""
    week = int(game_hours_elapsed // 168) + 1
    h_in_w = game_hours_elapsed % 168
    day = int(h_in_w // 24) + 1
    hod = h_in_w % 24
    hh = int(hod)
    mm = int(round((hod - hh) * 60)) % 60
    spd = "paused" if speed_mult == 0 else f"{speed_mult}×"
    return f"Week {week} · Day {day} · {hh:02d}:{mm:02d} · {spd}"


def render_flight_status(flight, current_game_hour: float):
    """
    Get status text and color for a flight.
    
    Args:
        flight: Flight segment dict
        current_game_hour: Current absolute game time (hours since game start)
    
    Returns:
        tuple: (status_text, color)
    """
    status = flight['status']
    dep_h = float(flight['scheduled_dep_game_hour'])
    arr_h = float(flight['scheduled_arr_game_hour'])
    act_dep = flight.get('actual_dep_game_hour')
    dep_ref = float(act_dep) if act_dep is not None else dep_h
    
    if status == 'SCHEDULED':
        time_to_dep_h = dep_h - current_game_hour
        time_to_dep_sec = time_to_dep_h * 3600.0
        if time_to_dep_sec <= 0:
            return "DEPARTING", "yellow bold"
        elif time_to_dep_sec <= 300:
            return f"BOARDING ({format_time_remaining(int(time_to_dep_sec))})", "yellow"
        else:
            return f"Scheduled ({format_time_remaining(int(time_to_dep_sec))})", "white"
    
    elif status == 'IN_AIR':
        try:
            from engine.environment import is_airport_closed_at

            planned_dest = flight.get("route_dest_iata") or flight.get("dest_iata")
            if (
                planned_dest
                and not (flight.get("divert_airport_iata") or "").strip()
                and is_airport_closed_at(str(planned_dest).upper(), float(current_game_hour))
            ):
                return "Dest closed — diverting", "magenta"
        except Exception:
            pass
        time_to_arr_h = arr_h - current_game_hour
        time_to_arr_sec = time_to_arr_h * 3600.0
        if time_to_arr_sec <= 0:
            return "LANDING", "green bold"
        else:
            elapsed = current_game_hour - dep_ref
            total_duration = arr_h - dep_ref
            progress_pct = (elapsed / total_duration * 100) if total_duration > 0 else 0
            return f"In Air {progress_pct:.1f}% ({format_time_remaining(int(time_to_arr_sec))})", "cyan"
    
    elif status == 'LANDED':
        return "LANDED", "green"
    
    elif status == 'CANCELLED':
        return "CANCELLED", "red"
    
    elif status == 'DELAYED':
        # Same timeline as SCHEDULED; segment was marked delayed by schedule normalization / strikes
        time_to_dep_h = dep_h - current_game_hour
        time_to_dep_sec = time_to_dep_h * 3600.0
        dm = flight.get("delay_minutes")
        slip = f" · +{int(dm)}m vs plan" if dm else ""
        if time_to_dep_sec <= 0:
            return f"Delayed · DEPARTING{slip}", "yellow bold"
        if time_to_dep_sec <= 300:
            return (
                f"Delayed · BOARDING ({format_time_remaining(int(time_to_dep_sec))}){slip}",
                "yellow",
            )
        return (
            f"Delayed ({format_time_remaining(int(time_to_dep_sec))}){slip}",
            "red",
        )

    elif status == 'DIVERTED':
        alt = flight.get("divert_airport_iata") or flight.get("dest_iata")
        return f"DIVERTED ({alt})", "magenta"

    elif status == 'HOLDING':
        return "HOLDING (weather)", "cyan"
    
    else:
        return status, "white"


def create_flight_board_table(flights, current_game_hour: float, flights_total: int | None = None):
    """
    Create Rich table for flight board display.
    
    Args:
        flights: List of flight segment dicts (already filtered for the table)
        current_game_hour: Current absolute game time (hours)
        flights_total: If more flights existed before filtering, mention in title
    
    Returns:
        Table: Rich table
    """
    title = "✈️  Flight Operations Board"
    if flights_total is not None and flights_total > len(flights):
        title += f" — {len(flights)} of {flights_total} (latest landed + ops, this week)"
    elif flights_total is not None and flights_total > 0:
        title += f" — {len(flights)} flight(s)"

    table = Table(
        title=title,
        show_header=True,
        header_style="bold magenta",
        border_style="blue",
        show_lines=False,
    )

    table.add_column("Flight", style="cyan", width=10)
    table.add_column("Tail", style="yellow", width=8)
    table.add_column("Route", style="white", width=12)
    table.add_column("Departure", justify="center", width=13)
    table.add_column("Arrival", justify="center", width=13)
    table.add_column("Pax (actual)", justify="right", width=16)
    table.add_column("Status", width=25)
    table.add_column("Revenue", justify="right", width=12)
    
    for flight in flights:
        dep_time = board_weekday_time_label(
            float(flight["scheduled_dep_game_hour"]),
            flight.get("scheduled_dep_time"),
        )
        arr_time = board_weekday_time_label(
            float(flight["scheduled_arr_game_hour"]),
            flight.get("scheduled_arr_time"),
        )
        
        oi = flight.get("origin_iata") or flight.get("route_origin_iata")
        di = flight.get("dest_iata") or flight.get("route_dest_iata")
        # Route display (diverted legs show alternate in brackets)
        if flight.get("status") == "DIVERTED" and flight.get("divert_airport_iata"):
            route_display = f"{oi}→{di} [{flight['divert_airport_iata']}]"
        else:
            route_display = f"{oi}→{di}"
        
        # Passenger count (by cabin if available; fallback to legacy B/L).
        y = int(flight.get("pax_economy") or 0)
        w = int(flight.get("pax_premium_economy") or 0)
        j = int(flight.get("pax_business_cabin") or 0)
        f = int(flight.get("pax_first") or 0)
        if (y + w + j + f) > 0:
            pax_display = f"{y+w+j+f} Y{y}/W{w}/J{j}/F{f}"
        else:
            pb = int(flight.get("pax_business") or 0)
            pl = int(flight.get("pax_leisure") or 0)
            total_pax = pb + pl
            pax_display = f"{total_pax} B{pb}/L{pl}" if total_pax > 0 else "-"
        
        # Revenue
        if flight['revenue_gross'] > 0:
            revenue_display = f"${flight['revenue_gross']:,.0f}"
        else:
            revenue_display = "-"
        
        # Status
        status_text, status_color = render_flight_status(flight, current_game_hour)
        
        table.add_row(
            flight['flight_number'],
            flight['tail_number'],
            route_display,
            dep_time,
            arr_time,
            pax_display,
            Text(status_text, style=status_color),
            revenue_display
        )
    
    return table


def create_game_clock_panel(clock_status: dict):
    """
    Panel for continuous game clock (game hours + speed).
    
    clock_status: needs game_hours_elapsed, speed_multiplier, is_paused, time_display (optional)
    """
    info = Text()
    if clock_status.get("is_paused"):
        info.append("⏸ PAUSED", style="yellow bold")
    else:
        info.append("▶ RUNNING", style="green bold")
    info.append("\n\n", style="white")
    info.append(clock_status.get("time_display") or "—", style="cyan")
    info.append("\n", style="white")
    sp = clock_status.get("speed_multiplier", 0)
    info.append(f"Speed: {'paused' if sp == 0 else f'{sp}×'}", style="white")
    return Panel(info, title="Game Clock", border_style="blue")


def create_summary_panel(flights, flights_total: int | None = None):
    """
    Create summary statistics panel.
    
    Args:
        flights: List of flight segments (same subset as the main table)
        flights_total: Total flights in the week before row cap (optional)
    
    Returns:
        Panel: Rich panel
    """
    # Calculate stats
    total_flights = len(flights)
    scheduled = sum(1 for f in flights if f['status'] == 'SCHEDULED')
    in_air = sum(1 for f in flights if f['status'] == 'IN_AIR')
    landed = sum(1 for f in flights if f['status'] == 'LANDED')
    cancelled = sum(1 for f in flights if f['status'] == 'CANCELLED')
    
    total_pax = sum(f['pax_business'] + f['pax_leisure'] for f in flights)
    total_revenue = sum(f['revenue_gross'] for f in flights if f['revenue_gross'])
    
    info = Text()
    info.append(f"Total Flights: {total_flights}\n", style="bold white")
    info.append(f"  Scheduled:   {scheduled}\n", style="white")
    info.append(f"  In Air:      {in_air}\n", style="cyan")
    info.append(f"  Landed:      {landed}\n", style="green")
    if cancelled > 0:
        info.append(f"  Cancelled:   {cancelled}\n", style="red")
    
    info.append("\n")
    info.append(f"Total Pax:     {total_pax:,}\n", style="yellow")
    info.append(f"Total Revenue: ${total_revenue:,.0f}\n", style="green bold")
    if flights_total is not None and flights_total > total_flights:
        info.append(
            f"\nDashboard: up to {FLIGHT_BOARD_LANDED_HEAD} latest landed, "
            f"then operational legs ({total_flights} shown of {flights_total} this week)\n",
            style="dim",
        )
    
    panel_title = "Summary"
    if flights_total is not None and flights_total > total_flights:
        panel_title = f"Summary (board cap {FLIGHT_BOARD_NEAREST_LIMIT})"
    return Panel(info, title=panel_title, border_style="green")


def render_flight_board(flights, clock_status, current_game_hour: float):
    """
    Render complete flight board with clock and summary.
    
    Args:
        flights: All flight segments for the week (unfiltered); board shows 3 latest landed then operational legs
        clock_status: Dict with time_display, speed_multiplier, is_paused
        current_game_hour: Absolute game time for status column
    """
    flights_total = len(flights) if flights else 0
    flights_view = dashboard_flights_for_board(
        flights, current_game_hour, FLIGHT_BOARD_NEAREST_LIMIT
    )

    try:
        from engine.events import is_ui_blackout

        if is_ui_blackout():
            line = "SYSTEM DISRUPTION — DATA UNAVAILABLE"
        else:
            from engine.news_feed import ticker_plain_text

            tw = getattr(console, "width", None) or 96  # module-level Rich console
            line = ticker_plain_text(max(48, tw - 8))
    except Exception:
        line = (
            "30 real sec = 1 game hour at 1× · Game Clock: pause / speed · Enter to exit"
        )
    footer_text = Text()
    footer_text.append(line, style="dim")
    news_panel = Panel(footer_text, border_style="white", title="News")

    # Rich Layout often paints nothing in embedded terminals (e.g. Cursor). Stack with Group.
    return Group(
        create_game_clock_panel(clock_status),
        create_summary_panel(flights_view, flights_total=flights_total),
        create_flight_board_table(flights_view, current_game_hour, flights_total=flights_total),
        news_panel,
    )


def _clock_status_for_live(ghe: float) -> dict:
    """Header panel dict using interpolated game time + speed from DB."""
    from engine.clock import get_game_time
    
    gt = get_game_time()
    sp = int(gt["speed_multiplier"]) if gt else 0
    return {
        "time_display": game_time_status_line(ghe, sp),
        "speed_multiplier": sp,
        "is_paused": sp == 0,
    }


def _use_live_alternate_screen() -> bool:
    """
    Use Rich Live ``screen=True`` (alternate buffer) so each refresh replaces the view.

    With ``screen=False``, Rich moves the cursor up and repaints; many IDE-embedded
    terminals ignore that and append instead, producing a scrolling stack of boards.
    """
    if os.environ.get("AIRLINE_SIM_NO_LIVE_SCREEN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if os.environ.get("AIRLINE_SIM_LIVE_SCREEN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    tp = (os.environ.get("TERM_PROGRAM") or "").strip().lower()
    return tp in ("vscode", "cursor")


def display_flight_board_live(flights=None, refresh_per_second: float = 12.0):
    """
    Show the flight board. Uses Rich Live to refresh when stdout is a TTY.

    Set ``AIRLINE_SIM_NO_LIVE=1`` for one static snapshot (no animation, lower CPU).

    In VS Code / Cursor, Live uses the alternate screen by default so the clock
    updates in place instead of stacking. ``AIRLINE_SIM_NO_LIVE_SCREEN=1`` forces
    cursor-based redraw; ``AIRLINE_SIM_LIVE_SCREEN=1`` always uses alternate screen.
    """
    from engine.scheduling import get_all_flights
    from engine.clock import get_display_game_hours

    if flights is None:
        flights = get_all_flights()
    if not flights:
        console.print("\n[yellow]No flights scheduled for this week.[/yellow]\n")
        return

    no_live = os.environ.get("AIRLINE_SIM_NO_LIVE", "").strip().lower() in ("1", "true", "yes")
    want_live = sys.stdout.isatty() and not no_live

    if not want_live:
        console.print(
            "[dim]Static flight board (AIRLINE_SIM_NO_LIVE=1 or non-TTY). "
            "[bold]Enter[/bold] to return.[/dim]\n"
        )
        display_flight_board_static(flights)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        console.print()
        return

    interval = max(1.0 / refresh_per_second, 0.02)
    console.print(
        "[dim]Live board — updates in real time. "
        "[bold]Press Enter[/bold] on this line to return (stdin must be on the main thread in IDE terminals).[/dim]\n"
    )

    ghe = get_display_game_hours()
    initial = render_flight_board(
        flights, _clock_status_for_live(ghe), current_game_hour=ghe
    )
    use_alt_screen = _use_live_alternate_screen()

    try:
        if not use_alt_screen:
            console.print(initial)
            console.print()
    except Exception as ex:
        console.print(f"[yellow]Could not render board ({ex}); showing minimal static.[/yellow]\n")
        display_flight_board_static(flights)
        try:
            input("\nPress Enter to return to menu… ")
        except (EOFError, KeyboardInterrupt):
            pass
        console.print()
        return

    stop = threading.Event()

    def live_refresh_loop() -> None:
        # Live + updates run here; main thread handles input() (required for Cursor/VS Code terminals).
        live_kw = {
            "console": console,
            "transient": False,
            "auto_refresh": False,
            "screen": use_alt_screen,
            "redirect_stdout": False,
            "redirect_stderr": False,
            "vertical_overflow": "visible",
        }
        if use_alt_screen:
            live_kw.pop("vertical_overflow", None)
        try:
            live_ctx = Live(initial, **live_kw)
        except TypeError:
            live_kw.pop("vertical_overflow", None)
            live_ctx = Live(initial, **live_kw)
        try:
            with live_ctx as live:
                while not stop.is_set():
                    ghe = get_display_game_hours()
                    flights_now = get_all_flights()
                    if not flights_now:
                        break
                    live.update(
                        render_flight_board(
                            flights_now,
                            _clock_status_for_live(ghe),
                            current_game_hour=ghe,
                        ),
                        refresh=True,
                    )
                    time.sleep(interval)
        except Exception:
            pass

    refresh_thread = threading.Thread(target=live_refresh_loop, daemon=True)
    refresh_thread.start()

    try:
        input("\nPress Enter to return to menu… ")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        stop.set()
    refresh_thread.join(timeout=3.0)
    console.print()


def display_flight_board_static(flights=None, current_game_hour: float = None):
    """
    Display flight board using current game time from DB or running GameClock.
    """
    from engine.scheduling import get_all_flights
    from engine.clock import get_global_clock, get_game_time
    
    if flights is None:
        flights = get_all_flights()
    
    if not flights:
        console.print("\n[yellow]No flights scheduled for this week.[/yellow]\n")
        return
    
    clk = get_global_clock()
    if clk is not None and clk.is_alive():
        st = clk.get_status()
        ghe = st["game_hours_elapsed"]
        clock_status = {
            "time_display": st["time_display"],
            "speed_multiplier": st["speed_multiplier"],
            "is_paused": st["is_paused"],
        }
    else:
        gt = get_game_time()
        ghe = float(gt["game_hours_elapsed"]) if gt else 0.0
        sp = int(gt["speed_multiplier"]) if gt else 0
        clock_status = {
            "time_display": game_time_status_line(ghe, sp),
            "speed_multiplier": sp,
            "is_paused": sp == 0,
        }
    
    if current_game_hour is not None:
        ghe = float(current_game_hour)
    
    layout = render_flight_board(flights, clock_status, current_game_hour=ghe)
    console.print(layout)
    console.print()


# Example usage for testing
if __name__ == "__main__":
    # Create some test flights
    test_flights = [
        {
            'segment_id': 'TEST-001',
            'flight_number': 'AA001',
            'tail_number': 'N123AA',
            'route_id': 'TPA-JFK',
            'origin_iata': 'TPA',
            'dest_iata': 'JFK',
            'scheduled_dep_time': '08:00',
            'scheduled_arr_time': '11:00',
            'scheduled_dep_game_hour': 8.0,
            'scheduled_arr_game_hour': 11.0,
            'actual_dep_game_hour': 8.0,
            'status': 'IN_AIR',
            'pax_business': 12,
            'pax_leisure': 138,
            'revenue_gross': 15250.00
        },
        {
            'segment_id': 'TEST-002',
            'flight_number': 'AA002',
            'tail_number': 'N456BB',
            'route_id': 'ATL-LAX',
            'origin_iata': 'ATL',
            'dest_iata': 'LAX',
            'scheduled_dep_time': '09:00',
            'scheduled_arr_time': '12:30',
            'scheduled_dep_game_hour': 9.0,
            'scheduled_arr_game_hour': 12.5,
            'actual_dep_game_hour': None,
            'status': 'SCHEDULED',
            'pax_business': 0,
            'pax_leisure': 0,
            'revenue_gross': 0
        },
        {
            'segment_id': 'TEST-003',
            'flight_number': 'AA003',
            'tail_number': 'N789CC',
            'route_id': 'MIA-BOS',
            'origin_iata': 'MIA',
            'dest_iata': 'BOS',
            'scheduled_dep_time': '10:00',
            'scheduled_arr_time': '13:00',
            'scheduled_dep_game_hour': 10.0,
            'scheduled_arr_game_hour': 13.0,
            'actual_dep_game_hour': None,
            'status': 'SCHEDULED',
            'pax_business': 0,
            'pax_leisure': 0,
            'revenue_gross': 0
        }
    ]
    
    print("\n=== Flight Board Test ===\n")
    display_flight_board_static(test_flights, current_game_hour=9.5)
    
    print("\n✓ Flight board test complete")
