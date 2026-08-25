from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from db import db

_node_proc: Optional[subprocess.Popen] = None
_last_post_wall: float = 0.0


def start_local_map_server(port: int = 3000) -> None:
    global _node_proc
    if _node_proc is not None and _node_proc.poll() is None:
        return
    root = Path(__file__).resolve().parent.parent
    srv_dir = root / "server" / "map_node"
    js = srv_dir / "server.js"
    if not js.exists():
        return
    _node_proc = subprocess.Popen(
        ["node", str(js)],
        cwd=str(srv_dir),
        env={**dict(**__import__("os").environ), "MAP_PORT": str(int(port))},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _build_live_flights_payload(game_hours_elapsed: float) -> Dict[str, Any]:
    ghe = float(game_hours_elapsed)
    boarding_lead_h = 5.0 / 60.0
    rows = db.fetch_all(
        """
        SELECT fs.segment_id, fs.flight_number, fs.tail_number, fs.route_id,
               fs.status, fs.scheduled_dep_game_hour, fs.scheduled_arr_game_hour,
               fs.actual_dep_game_hour,
               r.origin_iata, r.dest_iata,
               ao.lat AS o_lat, ao.lon AS o_lon,
               ad.lat AS d_lat, ad.lon AS d_lon,
               t.cruise_speed_kts AS speed_kts
        FROM flight_segments fs
        JOIN routes r ON r.route_id = fs.route_id
        JOIN airports ao ON ao.iata = r.origin_iata
        JOIN airports ad ON ad.iata = r.dest_iata
        JOIN fleet f ON f.tail_number = fs.tail_number
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE fs.status IN ('SCHEDULED','DELAYED','HOLDING','IN_AIR','DIVERTED')
        ORDER BY fs.scheduled_dep_game_hour ASC
        """
    )
    flights = []
    in_air = 0
    for r in rows:
        dep = float(r["scheduled_dep_game_hour"] or 0.0)
        arr = float(r["scheduled_arr_game_hour"] or dep)
        dur = max(1e-6, arr - dep)
        st = str(r["status"] or "")

        # Only show legs that are relevant "now":
        # - In air (or diverted/holding): must not be past ETA (filters stale IN_AIR rows).
        # - Scheduled/delayed/holding: show from boarding window until ETA.
        if st in ("IN_AIR", "DIVERTED", "HOLDING"):
            if arr <= ghe:
                continue
            in_air += 1
        else:
            if dep > ghe + boarding_lead_h:
                continue
            if arr <= ghe:
                continue

        # Progress uses actual_dep_game_hour when available (better for delayed legs).
        a_dep = r["actual_dep_game_hour"]
        dep0 = float(a_dep) if a_dep is not None else dep
        dur0 = max(1e-6, arr - dep0)
        prog = (ghe - dep0) / dur0
        prog = max(0.0, min(1.0, float(prog)))
        flights.append(
            {
                "id": str(r["segment_id"]),
                "label": f"{r['flight_number']} {r['tail_number']}",
                "from": {"lat": float(r["o_lat"]), "lng": float(r["o_lon"])},
                "to": {"lat": float(r["d_lat"]), "lng": float(r["d_lon"])},
                "progress": float(prog),
                "speed": float(r["speed_kts"] or 0.0),
                "status": st,
            }
        )
        # Soft cap for UI performance
        if len(flights) >= 200:
            break
    return {"flights": flights}


def maybe_push_map_update(game_hours_elapsed: float, *, port: int = 3000, min_period_ms: int = 250) -> None:
    global _last_post_wall
    now = time.time()
    if (now - _last_post_wall) * 1000.0 < float(min_period_ms):
        return
    _last_post_wall = now

    payload = _build_live_flights_payload(float(game_hours_elapsed))
    raw = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/api/update",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=0.2):
            pass
    except Exception:
        pass

