"""
Shared demand labels for CLI / API / web (Phase 5 — UI honesty).

Hero number = this week's market total (business + leisure pools).
Cabin Y/W/J/F is a split of that market — never the only figure.
Template base B/L is an internal input, shown as a footnote.
"""

from __future__ import annotations

from typing import Any, Optional


SOURCE_SHORT = {
    "BTS": "BTS",
    "LEGACY": "LEGACY",
    "GRAVITY": "GRAVITY",
}

SOURCE_BLURB = {
    "BTS": "Real market traffic (US DOT)",
    "LEGACY": "Category × distance estimate",
    "GRAVITY": "Modelled from airport size & distance",
}


def normalize_demand_source(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in SOURCE_SHORT:
        return s
    return ""


def source_badge(raw: Any) -> str:
    s = normalize_demand_source(raw)
    return SOURCE_SHORT.get(s, "—")


def source_blurb(raw: Any) -> str:
    s = normalize_demand_source(raw)
    return SOURCE_BLURB.get(s, "Unknown source")


def resolve_weekly_market_total(
    *,
    business_pax: Any = 0,
    leisure_pax: Any = 0,
    weekly_market_total: Any = None,
    total_pax: Any = None,
) -> int:
    """Prefer explicit weekly_market_total; else B+L; never aircraft-fill total_pax alone."""
    if weekly_market_total is not None:
        try:
            return max(0, int(weekly_market_total))
        except (TypeError, ValueError):
            pass
    try:
        b = int(business_pax or 0)
        l = int(leisure_pax or 0)
        if b or l:
            return max(0, b + l)
    except (TypeError, ValueError):
        pass
    try:
        return max(0, int(total_pax or 0))
    except (TypeError, ValueError):
        return 0


def cabin_line(payload: dict[str, Any]) -> str:
    e = int(payload.get("economy_pax") or 0)
    w = int(payload.get("premium_economy_pax") or 0)
    j = int(payload.get("business_cabin_pax") or 0)
    f = int(payload.get("first_pax") or 0)
    return f"Y{e:,} / W{w:,} / J{j:,} / F{f:,}"


def build_demand_summary(
    *,
    business_pax: Any = 0,
    leisure_pax: Any = 0,
    demand_source: Any = "",
    market_floor_applied: Any = False,
    base_demand_business: Any = 0,
    base_demand_leisure: Any = 0,
    economy_pax: Any = 0,
    premium_economy_pax: Any = 0,
    business_cabin_pax: Any = 0,
    first_pax: Any = 0,
    game_week: Any = None,
    current_month: Any = None,
    weekly_market_total: Any = None,
    aircraft_fill_pax: Any = None,
) -> dict[str, Any]:
    """Structured summary + plain labels for UIs."""
    market = resolve_weekly_market_total(
        business_pax=business_pax,
        leisure_pax=leisure_pax,
        weekly_market_total=weekly_market_total,
    )
    src = normalize_demand_source(demand_source)
    floored = bool(market_floor_applied)
    try:
        bb = int(base_demand_business or 0)
        bl = int(base_demand_leisure or 0)
    except (TypeError, ValueError):
        bb, bl = 0, 0

    cabin = {
        "economy_pax": int(economy_pax or 0),
        "premium_economy_pax": int(premium_economy_pax or 0),
        "business_cabin_pax": int(business_cabin_pax or 0),
        "first_pax": int(first_pax or 0),
    }

    week_bit = ""
    if game_week is not None:
        week_bit = f" (game week {int(game_week)}"
        if current_month is not None:
            week_bit += f", month {int(current_month)}"
        week_bit += ")"

    source_line = f"Source: {source_badge(src) or '—'} — {source_blurb(src)}"
    if floored:
        source_line += " · minimum playable market applied"

    labels = {
        "hero": f"This week's market: {market:,} pax{week_bit}",
        "source": source_line,
        "segment": (
            f"{int(business_pax or 0):,} business · {int(leisure_pax or 0):,} leisure"
        ),
        "cabin": f"Cabin split (not the full market alone): {cabin_line(cabin)}",
        "template": (
            f"Template base (internal): {bb} B / {bl} L — input to the demand formula, "
            "not weekly passengers"
        ),
    }
    if aircraft_fill_pax is not None:
        try:
            labels["aircraft"] = (
                f"One-aircraft seat fill (projection): {int(aircraft_fill_pax):,} pax"
            )
        except (TypeError, ValueError):
            pass

    return {
        "weekly_market_total": market,
        "business_pax": int(business_pax or 0),
        "leisure_pax": int(leisure_pax or 0),
        "demand_source": src,
        "demand_source_badge": source_badge(src),
        "demand_source_blurb": source_blurb(src),
        "market_floor_applied": floored,
        "base_demand_business": bb,
        "base_demand_leisure": bl,
        "game_week": int(game_week) if game_week is not None else None,
        "current_month": int(current_month) if current_month is not None else None,
        **cabin,
        "aircraft_fill_pax": (
            int(aircraft_fill_pax) if aircraft_fill_pax is not None else None
        ),
        "labels": labels,
    }


def summary_from_preview(dem: dict[str, Any]) -> dict[str, Any]:
    return build_demand_summary(
        business_pax=dem.get("business_pax"),
        leisure_pax=dem.get("leisure_pax"),
        demand_source=dem.get("demand_source"),
        market_floor_applied=dem.get("market_floor_applied"),
        base_demand_business=dem.get("base_demand_business"),
        base_demand_leisure=dem.get("base_demand_leisure"),
        economy_pax=dem.get("economy_pax"),
        premium_economy_pax=dem.get("premium_economy_pax"),
        business_cabin_pax=dem.get("business_cabin_pax"),
        first_pax=dem.get("first_pax"),
        game_week=dem.get("game_week"),
        current_month=dem.get("current_month"),
        weekly_market_total=dem.get("weekly_market_total", dem.get("total_pax")),
    )


def floor_flag_for_route(
    origin_iata: str,
    dest_iata: str,
    distance_nm: float,
    origin_airport: Optional[dict] = None,
    dest_airport: Optional[dict] = None,
) -> tuple[str, bool]:
    """Recompute source + floor badge for an opened route (bases are sticky)."""
    from engine.airports import get_airport
    from engine.route_demand import compute_base_demand

    ao = origin_airport or get_airport(str(origin_iata).upper())
    ad = dest_airport or get_airport(str(dest_iata).upper())
    if not ao or not ad:
        return "", False
    info = compute_base_demand(float(distance_nm), dict(ao), dict(ad))
    return (
        str(info.get("demand_source") or ""),
        bool(info.get("market_floor_applied")),
    )


def format_cli_block(summary: dict[str, Any]) -> list[str]:
    """Plain text lines for terminal UIs."""
    labels = summary.get("labels") or {}
    lines = [
        labels.get("hero", ""),
        labels.get("source", ""),
        labels.get("segment", ""),
        labels.get("cabin", ""),
        labels.get("template", ""),
    ]
    if labels.get("aircraft"):
        lines.append(labels["aircraft"])
    return [ln for ln in lines if ln]
