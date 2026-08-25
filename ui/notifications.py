"""
Player notifications (Phase 11+ PDF): auction results, licence alerts, competitor entries.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from db import db


def show_notifications_cli(console: Console) -> None:
    rows = db.fetch_all(
        """
        SELECT notification_id, game_week, type, route_pair_id, body
        FROM player_notifications
        WHERE read = 0
        ORDER BY game_week DESC
        """
    )
    if not rows:
        console.print("[dim]No unread notifications.[/dim]\n")
        return
    t = Table(title="NOTIFICATIONS", show_lines=False)
    t.add_column("Week", justify="right", width=6)
    t.add_column("Type", width=14)
    t.add_column("Route", width=10)
    t.add_column("Message", width=74)
    ids = []
    for r in rows:
        ids.append(str(r["notification_id"]))
        t.add_row(
            str(r["game_week"]),
            str(r["type"]),
            str(r["route_pair_id"] or "—"),
            str(r["body"]),
        )
    console.print(t)
    console.print()
    try:
        db.execute(
            f"UPDATE player_notifications SET read = 1 WHERE notification_id IN ({','.join(['?']*len(ids))})",
            tuple(ids),
        )
    except Exception:
        pass

