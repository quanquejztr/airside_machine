"""
Weekly tail schedule grid: days of week as columns, hour of day as rows.

`rich` is imported lazily inside the print_* helpers: server/game_api.py calls
json_tail_schedule_view() for the overlay dock, and the web path must not depend
on the CLI renderer.
"""

from __future__ import annotations

from engine.scheduling import (
    calendar_game_week_from_state,
    get_tail_flight_segments_for_week,
    week_base_hours,
)

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _day_hour_time_label(game_week, dep_abs):
    """Map absolute dep time to day index 0..6, hour row 0..23, and HH:MM label."""
    w_base = week_base_hours(game_week)
    offset = float(dep_abs) - w_base
    offset %= 168.0
    if offset < 0:
        offset += 168.0
    d_idx = int(offset // 24.0)
    d_idx = max(0, min(6, d_idx))
    hod = offset - 24.0 * d_idx
    if hod >= 24.0:
        hod = 23.99
    hh = int(hod)
    mm = int(round((hod - hh) * 60.0)) % 60
    return d_idx, hh, f"{hh:02d}:{mm:02d}"


def _segment_dep_arr_labels(game_week, s):
    """
    Dep / arr labels from absolute game hours (never raw HH:MM strings alone).

    DB strings can hide a next-day arrival (e.g. \"05:00\" / \"03:27\" looks like arr < dep).
    If departure and arrival fall on different week-local days, prefix with MON..SUN.
    """
    dep_abs = float(s["scheduled_dep_game_hour"])
    arr_abs = float(s["scheduled_arr_game_hour"])
    d0, _, t0 = _day_hour_time_label(game_week, dep_abs)
    d1, _, t1 = _day_hour_time_label(game_week, arr_abs)
    if d0 != d1:
        return f"{DAYS[d0]} {t0}", f"{DAYS[d1]} {t1}"
    return t0, t1


def _segment_planned_labels(game_week, s):
    """
    Planned (template-like) dep/arr labels for schedule views.

    Operational delays can push absolute game-hour timestamps across days and make the
    weekly grid look "mixed". For the planned schedule, prefer stored HH:MM labels.
    """
    dep_txt = str(s.get("scheduled_dep_time") or "").strip()
    arr_txt = str(s.get("scheduled_arr_time") or "").strip()
    if dep_txt and arr_txt:
        return dep_txt, arr_txt
    return _segment_dep_arr_labels(game_week, s)


def _format_route_line(s: dict) -> str:
    """Planned route; if diverted to another field, show filed dest + [alternate]."""
    o = (s.get("origin_iata") or "").strip()
    filed = (s.get("route_dest_iata") or s.get("dest_iata") or "").strip()
    alt = (s.get("divert_airport_iata") or "").strip().upper()
    if alt and filed and alt != filed.upper():
        return f"{o}-{filed} [{alt}]"
    return f"{o}-{filed}" if filed else f"{o}-?"


def json_tail_schedule_view(tail_number: str, game_week=None) -> dict:
    """
    Same content as the CLI planned list + Mon–Sun hour grid, as JSON for the map UI.
    """
    if game_week is None:
        game_week = calendar_game_week_from_state()
    tail_number = str(tail_number or "").strip().upper()
    segments = get_tail_flight_segments_for_week(tail_number, game_week)
    planned = []
    for s in segments:
        dep_l, arr_l = _segment_planned_labels(game_week, s)
        route_txt = _format_route_line(s)
        if int(s.get("is_ferry") or 0):
            route_txt = f"{route_txt} (ferry)"
        planned.append(
            {
                "day": str(s.get("day_of_week") or "—"),
                "dep": dep_l,
                "arr": arr_l,
                "flight": str(s.get("flight_number") or "—"),
                "route": route_txt,
                "status": str(s.get("status") or "—"),
                "is_ferry": bool(int(s.get("is_ferry") or 0)),
            }
        )
    grid = [[[] for _ in range(7)] for _ in range(24)]
    for s in segments:
        dep = float(s["scheduled_dep_game_hour"])
        d_idx, hh, _ = _day_hour_time_label(game_week, dep)
        grid[hh][d_idx].append((dep, s))
    hours = []
    for hour in range(24):
        cells = []
        for d in range(7):
            cell = sorted(grid[hour][d], key=lambda t: (t[0], str(t[1].get("segment_id", ""))))
            lines = []
            prev_seg = None
            for _dep_abs, seg in cell:
                warn = False
                if prev_seg is not None:
                    if float(seg["scheduled_dep_game_hour"]) < float(prev_seg["scheduled_arr_game_hour"]) - 1e-6:
                        warn = True
                dep_l, arr_l = _segment_planned_labels(game_week, seg)
                fn = seg.get("flight_number") or "?"
                rte = _format_route_line(seg)
                if int(seg.get("is_ferry") or 0):
                    rte = f"{rte} (ferry)"
                lines.append(
                    {
                        "text": f"{dep_l} - {arr_l}  {fn}  {rte}",
                        "overlap": warn,
                    }
                )
                prev_seg = seg
            cells.append(lines)
        hours.append({"hour": f"{hour:02d}:00", "cells": cells})
    return {
        "tail_number": tail_number,
        "game_week": int(game_week),
        "days": list(DAYS),
        "planned": planned,
        "hours": hours,
    }


def print_tail_schedule_detail(console: Console, tail_number: str, game_week=None) -> None:
    """
    Chronological list of this tail's segments for the week, then the week×hour grid.
    """
    from rich.panel import Panel
    from rich.table import Table
    if game_week is None:
        game_week = calendar_game_week_from_state()

    tail_number = tail_number.strip().upper()
    segments = get_tail_flight_segments_for_week(tail_number, game_week)

    console.print()
    console.print(
        f"[bold cyan]Aircraft schedule — {tail_number}[/bold cyan] "
        f"[dim](game week {game_week})[/dim]\n"
    )

    if not segments:
        console.print(
            f"[yellow]No flights scheduled for this tail in week {game_week}.[/yellow]\n"
        )
        return

    tbl = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    tbl.add_column("Day", style="dim", width=4)
    tbl.add_column("Dep", width=6)
    tbl.add_column("Arr", width=6)
    tbl.add_column("Flight", style="cyan", width=10)
    tbl.add_column("Route", width=14)
    tbl.add_column("Status", width=12)

    for s in segments:
        dep_l, arr_l = _segment_planned_labels(game_week, s)
        route = _format_route_line(s)
        tbl.add_row(
            str(s.get("day_of_week") or "—"),
            dep_l,
            arr_l,
            str(s.get("flight_number") or "—"),
            route,
            str(s.get("status") or "—"),
        )

    console.print(
        Panel(
            tbl,
            title="[dim]Planned schedule (departure order)[/dim]",
            border_style="blue",
        )
    )
    disrupted = []
    for s in segments:
        st = str(s.get("status") or "")
        dm = int(s.get("delay_minutes") or 0)
        if st in ("DELAYED", "HOLDING", "DIVERTED") or dm > 0:
            disrupted.append(s)
    if disrupted:
        ops = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        ops.add_column("Flight", style="cyan", width=10)
        ops.add_column("Planned", width=16)
        ops.add_column("Operational", width=14)
        ops.add_column("Delay", width=9)
        for s in disrupted:
            dep_l, arr_l = _segment_planned_labels(game_week, s)
            st = str(s.get("status") or "—")
            dm = int(s.get("delay_minutes") or 0)
            ops.add_row(
                str(s.get("flight_number") or "—"),
                f"{dep_l}-{arr_l}",
                st,
                f"+{dm}m" if dm > 0 else "—",
            )
        console.print(
            Panel(
                ops,
                title="[dim]Operational delays / disruptions[/dim]",
                border_style="yellow",
            )
        )
    console.print()
    print_tail_weekly_grid(console, tail_number, game_week=game_week)


def print_tail_weekly_grid(console, tail_number, game_week=None):
    """
    Print a Rich table: columns = Mon–Sun, rows = local hours 00:00–23:00.
    Each cell lists departures in that hour (with exact time + flight + route).
    """
    from rich.panel import Panel
    from rich.table import Table
    if game_week is None:
        game_week = calendar_game_week_from_state()

    segments = get_tail_flight_segments_for_week(tail_number, game_week)

    if not segments:
        console.print(
            f"[dim]No flights scheduled for {tail_number} in week {game_week} (nothing to show on the grid).[/dim]\n"
        )
        return

    # Each cell: list of (dep_abs, segment) for sort + overlap check
    grid = [[[] for _ in range(7)] for _ in range(24)]
    for s in segments:
        dep = float(s["scheduled_dep_game_hour"])
        d_idx, hh, _ = _day_hour_time_label(game_week, dep)
        grid[hh][d_idx].append((dep, s))

    def _cell_lines(cell: list) -> str:
        if not cell:
            return ""
        cell.sort(key=lambda t: (t[0], str(t[1].get("segment_id", ""))))
        lines_out = []
        prev_seg = None
        for dep_abs, seg in cell:
            warn = ""
            if prev_seg is not None:
                if float(seg["scheduled_dep_game_hour"]) < float(prev_seg["scheduled_arr_game_hour"]) - 1e-6:
                    warn = "[yellow]⚠ [/yellow]"
            dep_l, arr_l = _segment_planned_labels(game_week, seg)
            fn = seg.get("flight_number") or "?"
            rte = _format_route_line(seg)
            lines_out.append(
                f"{warn}[dim]{dep_l} - {arr_l}[/dim]  {fn}  [dim]{rte}[/dim]"
            )
            prev_seg = seg
        return "\n".join(lines_out)

    table = Table(
        title=f"[dim]Week {game_week} · {tail_number}[/dim]",
        caption=(
            "[dim]Planned schedule only (delay markers moved to operational panel above) · "
            "rows = local hour of departure · each day column is separate · "
            "stacked lines = multiple flights that hour · "
            "⚠ = next dep before previous arr (overlap / invalid for one aircraft)[/dim]"
        ),
        show_header=True,
        header_style="dim",
        box=None,
        pad_edge=False,
    )
    table.add_column("Time", style="dim", justify="right", width=6, no_wrap=True)
    for d in DAYS:
        table.add_column(d, min_width=14, overflow="fold")

    for hour in range(24):
        row_cells = [f"{hour:02d}:00"]
        for d in range(7):
            txt = _cell_lines(grid[hour][d])
            if txt:
                row_cells.append(txt)
            else:
                row_cells.append("[dim]·[/dim]")
        table.add_row(*row_cells)

    console.print()
    console.print(
        Panel(
            table,
            border_style="dim",
            title="[dim]This aircraft · weekly schedule[/dim]",
            title_align="left",
        ),
        highlight=False,
    )
    console.print()
