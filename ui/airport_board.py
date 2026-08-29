"""
Airport Flight Board (Phase 11+ PDF): unified view of player + AI flights through one airport.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db
from engine.scheduling import hhmm_from_absolute_game_hour

# Cabin-level counts after departure; legacy B/L columns otherwise.
_PLAYER_PAX_SQL = """
(CASE
  WHEN (COALESCE(fs.pax_economy, 0) + COALESCE(fs.pax_premium_economy, 0)
        + COALESCE(fs.pax_business_cabin, 0) + COALESCE(fs.pax_first, 0)) > 0
  THEN COALESCE(fs.pax_economy, 0) + COALESCE(fs.pax_premium_economy, 0)
       + COALESCE(fs.pax_business_cabin, 0) + COALESCE(fs.pax_first, 0)
  ELSE COALESCE(fs.pax_business, 0) + COALESCE(fs.pax_leisure, 0)
END) AS pax
"""


def format_board_pax_label(pax, operator_id: str) -> str:
    """Player flights show actual boarded pax; AI shows simulated estimate."""
    if pax is None:
        return "—"
    n = int(pax)
    if n <= 0:
        return "—"
    return str(n) if str(operator_id) == "PLAYER" else f"~{n}"


def _time_label(game_hour: float) -> str:
    # display "W{week} {dow} HH:MM"
    week = int(game_hour // 168.0) + 1
    h_in_week = game_hour % 168.0
    dow_idx = int(h_in_week // 24.0)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow = dows[max(0, min(6, dow_idx))]
    return f"W{week} {dow} {hhmm_from_absolute_game_hour(game_hour)}"


def get_airport_board_rows(airport_iata: str, game_week: int) -> list[dict]:
    iata = airport_iata.strip().upper()
    gw = int(game_week)
    out: list[dict] = []

    # Player departures
    rows = db.fetch_all(
        f"""
        SELECT segment_id, 'PLAYER' AS operator_id, fs.flight_number AS flight_number,
               fs.tail_number AS tail_number,
               COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
               COALESCE(fs.dest_iata, r.dest_iata) AS dest_iata,
               fs.scheduled_dep_game_hour AS game_hour,
               'DEP' AS direction, fs.status AS status,
               (COALESCE(fs.revenue_economy,0)+COALESCE(fs.revenue_premium_economy,0)+COALESCE(fs.revenue_business_cabin,0)+COALESCE(fs.revenue_first,0)) AS revenue,
               {_PLAYER_PAX_SQL}
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE COALESCE(fs.origin_iata, r.origin_iata) = ? AND fs.game_week = ?
        """,
        (iata, gw),
    )
    for r in rows:
        out.append(dict(r))

    # Player arrivals
    rows = db.fetch_all(
        f"""
        SELECT segment_id, 'PLAYER' AS operator_id, fs.flight_number AS flight_number,
               fs.tail_number AS tail_number,
               COALESCE(fs.origin_iata, r.origin_iata) AS origin_iata,
               COALESCE(fs.dest_iata, r.dest_iata) AS dest_iata,
               fs.scheduled_arr_game_hour AS game_hour,
               'ARR' AS direction, fs.status AS status,
               (COALESCE(fs.revenue_economy,0)+COALESCE(fs.revenue_premium_economy,0)+COALESCE(fs.revenue_business_cabin,0)+COALESCE(fs.revenue_first,0)) AS revenue,
               {_PLAYER_PAX_SQL}
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE COALESCE(fs.dest_iata, r.dest_iata) = ? AND fs.game_week = ?
        """,
        (iata, gw),
    )
    for r in rows:
        out.append(dict(r))

    # AI departures
    rows = db.fetch_all(
        """
        SELECT s.segment_id, s.competitor_id AS operator_id, s.flight_number,
               NULL AS tail_number,
               s.origin_iata, s.dest_iata,
               s.scheduled_dep_game_hour AS game_hour,
               'DEP' AS direction, s.status AS status,
               s.simulated_revenue AS revenue,
               (s.simulated_pax_leisure + s.simulated_pax_business) AS pax
        FROM ai_flight_segments s
        WHERE s.origin_iata = ? AND s.game_week = ?
        """,
        (iata, gw),
    )
    for r in rows:
        out.append(dict(r))

    # AI arrivals
    rows = db.fetch_all(
        """
        SELECT s.segment_id, s.competitor_id AS operator_id, s.flight_number,
               NULL AS tail_number,
               s.origin_iata, s.dest_iata,
               s.scheduled_arr_game_hour AS game_hour,
               'ARR' AS direction, s.status AS status,
               s.simulated_revenue AS revenue,
               (s.simulated_pax_leisure + s.simulated_pax_business) AS pax
        FROM ai_flight_segments s
        WHERE s.dest_iata = ? AND s.game_week = ?
        """,
        (iata, gw),
    )
    for r in rows:
        out.append(dict(r))

    out.sort(key=lambda x: float(x.get("game_hour") or 0.0))
    return out


def airport_board(console: Console, airport_iata: str, game_week: int) -> None:
    rows = get_airport_board_rows(airport_iata, game_week)
    iata = airport_iata.strip().upper()
    if not rows:
        console.print(f"[yellow]No flights for {iata} in week {game_week}.[/yellow]\n")
        return
    t = Table(title=f"AIRPORT BOARD — {iata} (W{game_week})", show_lines=False)
    t.add_column("Time", width=14)
    t.add_column("Dir", width=3)
    t.add_column("Flight#", width=8)
    t.add_column("Tail", width=9)
    t.add_column("Airline", width=10)
    t.add_column("Route", width=13)
    t.add_column("Status", width=10)
    t.add_column("Pax", justify="right", width=6)
    for r in rows:
        gh = float(r.get("game_hour") or 0.0)
        pax = r.get("pax")
        pax_s = format_board_pax_label(pax, str(r.get("operator_id") or ""))
        t.add_row(
            _time_label(gh),
            str(r.get("direction") or ""),
            str(r.get("flight_number") or ""),
            str(r.get("tail_number") or "—"),
            str(r.get("operator_id") or ""),
            f"{r.get('origin_iata')}→{r.get('dest_iata')}",
            str(r.get("status") or ""),
            pax_s,
        )
    console.print(t)
    console.print()

