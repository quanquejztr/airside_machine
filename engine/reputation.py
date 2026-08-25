"""
Reputation and brand power — Phase 9 (on-time performance + AOG penalties).
"""

from __future__ import annotations

from db import db


def update_reputation(game_week: int) -> tuple[float, float]:
    """
    Settlement hook: adjust airline.reputation_score from OTP and AOG events this week.

    Returns:
        (delta_applied, new_score)
    """
    airline = db.fetch_one("SELECT reputation_score FROM airline WHERE id = 1")
    prev = float(airline["reputation_score"] or 50.0) if airline else 50.0

    landed = db.fetch_all(
        """
        SELECT delay_minutes
        FROM flight_segments
        WHERE game_week = ? AND status IN ('LANDED', 'DIVERTED')
        """,
        (game_week,),
    )
    n = len(landed)
    if n <= 0:
        on_time_rate = 1.0
    else:
        on_time = sum(1 for r in landed if int(r["delay_minutes"] or 0) == 0)
        on_time_rate = on_time / float(n)

    if on_time_rate >= 0.90:
        base_delta = 2.0
    elif on_time_rate >= 0.75:
        base_delta = 0.0
    elif on_time_rate >= 0.60:
        base_delta = -2.0
    else:
        base_delta = -5.0

    aog_row = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM event_log
        WHERE game_week = ? AND event_type = 'AOG'
          AND (description IS NULL OR description NOT LIKE '%Resolved maintenance%')
        """,
        (game_week,),
    )
    aog_n = int(aog_row["c"] or 0) if aog_row else 0
    delta = base_delta - 0.5 * float(aog_n)

    new_score = max(0.0, min(100.0, prev + delta))
    db.execute("UPDATE airline SET reputation_score = ? WHERE id = 1", (new_score,))
    return (delta, new_score)


def reputation_to_brand_power(score: float) -> float:
    """Map 0–100 reputation linearly to brand multiplier 0.85–1.20."""
    s = max(0.0, min(100.0, float(score)))
    return 0.85 + (s / 100.0) * (1.20 - 0.85)


def reputation_to_interest_modifier(score: float) -> float:
    """
    Interest rate modifier for loan origination (Phase 12 wiring — banking.py).

    Maps 0–100 to roughly -0.5% … +1.5% expressed as a fraction (e.g. -0.005 … +0.015).
    """
    s = max(0.0, min(100.0, float(score)))
    return -0.005 + (s / 100.0) * (0.015 - (-0.005))


def apply_brand_power_from_reputation() -> float:
    """
    Write airline.brand_power from reputation plus permanent marketing bumps.

    brand_power = reputation_to_brand_power(score) + marketing_brand_bonus

    Phase 13 campaigns should increment marketing_brand_bonus (not brand_power
    directly) so weekly settlement does not wipe purchased awareness.
    """
    row = db.fetch_one(
        """
        SELECT reputation_score,
               COALESCE(marketing_brand_bonus, 0) AS marketing_brand_bonus
        FROM airline WHERE id = 1
        """
    )
    score = float(row["reputation_score"] or 50.0) if row else 50.0
    bonus = float(row["marketing_brand_bonus"] or 0.0) if row else 0.0
    bp = reputation_to_brand_power(score) + bonus
    db.execute("UPDATE airline SET brand_power = ? WHERE id = 1", (bp,))
    return bp
