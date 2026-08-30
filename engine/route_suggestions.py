"""Rank destination airports from a chosen origin (route-opening suggestions)."""

from __future__ import annotations

import math
from typing import Optional

from db import db
from engine.airports import get_airport
from engine.demand import preview_weekly_demand_before_open
from engine.routes import (
    haversine_distance,
    player_route_exists,
    preview_route_opening,
)

DEFAULT_MAX_RADIUS_NM = 4200.0
CANDIDATE_POOL = 60


def _default_cruise_kts() -> float:
    row = db.fetch_one(
        """
        SELECT AVG(t.cruise_speed_kts) AS kts
        FROM fleet f
        JOIN aircraft_types t ON t.type_id = f.type_id
        WHERE t.cruise_speed_kts > 0
        """
    )
    if row and row["kts"]:
        return float(row["kts"])
    row = db.fetch_one(
        "SELECT AVG(cruise_speed_kts) AS kts FROM aircraft_types WHERE cruise_speed_kts > 0"
    )
    return float(row["kts"] or 450.0) if row else 450.0


def _quick_pair_score(origin: dict, other: dict, distance_nm: float) -> float:
    """Cheap pre-rank before full weekly demand preview."""
    o = str(origin["iata"]).upper()
    d = str(other["iata"]).upper()
    bts_out = db.lookup_bts_anchor_weekly(o, d) or 0.0
    bts_in = db.lookup_bts_anchor_weekly(d, o) or 0.0
    if bts_out or bts_in:
        return max(bts_out, bts_in)
    s_o = float(origin.get("score") or 0)
    s_d = float(other.get("score") or 0)
    if s_o <= 0 or s_d <= 0:
        return 0.0
    return math.sqrt(s_o * s_d) / max(1.0, distance_nm / 800.0)


def popular_destinations_from_origin(
    origin_iata: str,
    *,
    limit: int = 25,
    max_radius_nm: float = DEFAULT_MAX_RADIUS_NM,
    hub_iata: Optional[str] = None,
) -> list[dict]:
    """
    Bidirectional market suggestions from ``origin_iata`` to other airports.

    Each row includes outbound (origin→other) and inbound (other→origin) weekly demand,
    distance, estimated block time, and open cost if legs are not yet in the network.
    """
    origin_iata = str(origin_iata or "").strip().upper()
    if not origin_iata:
        raise ValueError("Origin airport is required.")
    origin = get_airport(origin_iata)
    if not origin:
        raise ValueError(f"Origin airport '{origin_iata}' not found.")

    limit = max(1, min(int(limit or 25), 50))
    hub_u = str(hub_iata).strip().upper() if hub_iata else None
    cruise_kts = _default_cruise_kts()

    lat_o = float(origin["lat"])
    lon_o = float(origin["lon"])
    candidates: list[tuple[float, dict, float]] = []

    for row in db.fetch_all("SELECT iata, name, city, lat, lon, score, category FROM airports"):
        other = dict(row)
        iata = str(other["iata"]).upper()
        if iata == origin_iata:
            continue
        dist = haversine_distance(lat_o, lon_o, float(other["lat"]), float(other["lon"]))
        if dist > max_radius_nm:
            continue
        score = _quick_pair_score(origin, other, dist)
        if score <= 0:
            score = float(other.get("score") or 0) / max(1.0, dist / 500.0)
        candidates.append((score, other, dist))

    candidates.sort(key=lambda x: x[0], reverse=True)
    pool_size = max(CANDIDATE_POOL, limit * 3)
    pool = candidates[:pool_size]

    results: list[dict] = []
    for _quick, other, dist in pool:
        other_iata = str(other["iata"]).upper()
        try:
            dem_out = preview_weekly_demand_before_open(origin, other, dist)
            dem_in = preview_weekly_demand_before_open(other, origin, dist)
        except Exception:
            continue

        out_pax = int(dem_out.get("weekly_market_total") or dem_out.get("total_pax") or 0)
        in_pax = int(dem_in.get("weekly_market_total") or dem_in.get("total_pax") or 0)
        rank = max(out_pax, in_pax)

        open_cost = 0.0
        hub_pair = False
        try:
            prev = preview_route_opening(origin_iata, other_iata, hub_iata=hub_u)
            open_cost = float(prev.get("total_new_cost") or 0)
            hub_pair = bool(prev.get("hub_involved"))
        except Exception:
            pass

        flight_hours = (dist / cruise_kts) if cruise_kts > 0 else 0.0
        has_out = player_route_exists(origin_iata, other_iata)
        has_in = player_route_exists(other_iata, origin_iata)
        if has_out and has_in:
            status = "open"
        elif has_out or has_in:
            status = "partial"
        else:
            status = "new"

        results.append(
            {
                "other_iata": other_iata,
                "other_name": str(other.get("name") or ""),
                "other_city": str(other.get("city") or ""),
                "distance_nm": round(dist, 1),
                "flight_hours": round(flight_hours, 2),
                "outbound": {
                    "from": origin_iata,
                    "to": other_iata,
                    "weekly_demand": out_pax,
                    "player_has": has_out,
                },
                "inbound": {
                    "from": other_iata,
                    "to": origin_iata,
                    "weekly_demand": in_pax,
                    "player_has": has_in,
                },
                "rank_demand": rank,
                "open_cost": open_cost,
                "hub_pair": hub_pair,
                "network_status": status,
                "demand_source": dem_out.get("demand_source") or dem_in.get("demand_source"),
            }
        )

    results.sort(key=lambda r: int(r["rank_demand"]), reverse=True)
    return results[:limit]
