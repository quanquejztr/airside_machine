"""
Airline setup module for Phase 1.
Handles airline creation with user input.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db


# Constants for airline creation
STARTING_CASH = 1_000_000_000  # $1B
DEFAULT_CREDIT_SCORE = 720
DEFAULT_REPUTATION = 50.0
DEFAULT_BRAND_POWER = 1.0
DEFAULT_XP = 0
# Hub bootstrap when the home airport is capacity-controlled.
PLAYER_STARTER_HUB_GATES = 2
# Weekly runway movements at a slot-controlled hub (2 movements per round-trip).
# 12 ≈ six weekly cycles — enough to open a small hub network without skipping auctions.
PLAYER_STARTER_HUB_SLOTS = 12


def airline_exists():
    """Check if airline already exists."""
    airline = db.fetch_one("SELECT id FROM airline WHERE id = 1")
    return airline is not None


def grant_player_hub_starter_capacity(home_hub_iata: str, *, from_game_week: int = 1) -> dict:
    """
    Give the new player free hub capacity so week-1 ops are possible.

    - 2 gate stands if the hub is an auctioned airport
    - 12 weekly runway movements if the hub is slot-controlled (seeded across
      the same horizon AI uses for slot bootstrap)

    `from_game_week` is where the grant starts. A hub opened mid-game needs its stands
    effective now and its slots seeded from the current week forward; seeding week 1
    onward would write allocations for weeks already settled and leave the player with
    nothing usable in the week they actually opened it.
    """
    hub = str(home_hub_iata or "").upper().strip()
    out = {"hub": hub, "gates": 0, "slots": 0, "slot_weeks": 0}
    if not hub:
        return out

    try:
        from engine.gates import _upsert_allocation, is_auctioned_airport

        if is_auctioned_airport(hub):
            _upsert_allocation(
                hub, "PLAYER", PLAYER_STARTER_HUB_GATES,
                effective_week=max(1, int(from_game_week)),
            )
            out["gates"] = PLAYER_STARTER_HUB_GATES
    except Exception:
        pass

    try:
        from engine.slots import (
            ensure_min_slots_held,
            is_slot_controlled,
            seed_slot_controlled_airports,
        )

        seed_slot_controlled_airports()
        if is_slot_controlled(hub):
            weeks = 24
            try:
                weeks = max(1, int(float(db.get_financial_constant("ai_slot_seed_weeks") or 24)))
            except Exception:
                weeks = 24
            start = max(1, int(from_game_week))
            for w in range(start, start + weeks):
                ensure_min_slots_held(hub, "PLAYER", w, PLAYER_STARTER_HUB_SLOTS)
            out["slots"] = PLAYER_STARTER_HUB_SLOTS
            out["slot_weeks"] = weeks
    except Exception:
        pass

    return out


def _airport_dict(iata: str) -> Optional[Dict[str, Any]]:
    """SQLite Row is a sequence to the type checker; use a mapping for field access."""
    row = db.get_airport(str(iata).upper())
    if row is None:
        return None
    keys = [str(k) for k in row.keys()]
    return dict(zip(keys, tuple(row)))


def validate_home_hub(iata: str) -> Tuple[bool, Union[str, Dict[str, Any]]]:
    """Validate that the home hub airport exists."""
    airport = _airport_dict(iata)
    if not airport:
        return False, f"Airport '{iata}' not found in database"
    return True, airport


def create_airline(name=None, callsign=None, home_hub_iata=None):
    """
    Create a new airline with user-provided or prompted information.
    
    Args:
        name: Airline name (if None, will prompt user)
        callsign: 3-letter ICAO callsign (if None, will prompt user)
        home_hub_iata: Home airport IATA code (if None, will prompt user)
    
    Returns:
        dict: Airline data if successful, None if failed
    
    Raises:
        ValueError: If airline already exists
    """
    # Check if airline already exists
    if airline_exists():
        raise ValueError("Airline already exists! Only one airline per game.")
    
    try:
        from engine.clock import stop_game_clock
        stop_game_clock()
    except Exception:
        pass
    db.purge_all_player_game_data()
    _reseed_competitors_after_purge()
    print(
        "(Cleared any leftover flights, fleet, routes, ledgers, and AI competitors from this save file.)"
    )
    
    # Get airline name
    if name is None:
        print("\n" + "=" * 60)
        print("CREATE YOUR AIRLINE")
        print("=" * 60)
        name = input("\nEnter your airline name: ").strip()
        while not name:
            print("⚠ Airline name cannot be empty.")
            name = input("Enter your airline name: ").strip()
    
    # Get callsign
    if callsign is None:
        callsign = input("Enter 3-letter callsign (e.g., SJT, AAL): ").strip().upper()
        while len(callsign) != 3 or not callsign.isalpha():
            print("⚠ Callsign must be exactly 3 letters.")
            callsign = input("Enter 3-letter callsign: ").strip().upper()
    
    # Get home hub
    if home_hub_iata is None:
        print("\nPopular hub options: ATL, ORD, LAX, DFW, DEN, JFK, SFO")
        home_hub_iata = input("Enter home hub airport (IATA code): ").strip().upper()
        
        airport = _airport_dict(home_hub_iata)
        while airport is None:
            print(f"⚠ Airport '{home_hub_iata}' not found in database")
            home_hub_iata = input("Enter home hub airport (IATA code): ").strip().upper()
            airport = _airport_dict(home_hub_iata)

        print(f"\n✓ Selected: {airport['name']} ({airport['city']}, {airport['country']})")
    else:
        airport = _airport_dict(home_hub_iata)
        if airport is None:
            raise ValueError(f"Airport '{home_hub_iata}' not found in database")
    
    # Create airline in database
    try:
        db.execute("""
            INSERT INTO airline (
                id, name, callsign, home_hub_iata, cash,
                total_debt, credit_score, reputation_score,
                brand_power, xp, fuel_reserve_gallons
            ) VALUES (1, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, 0.0)
        """, (
            name,
            callsign,
            home_hub_iata,
            STARTING_CASH,
            DEFAULT_CREDIT_SCORE,
            DEFAULT_REPUTATION,
            DEFAULT_BRAND_POWER,
            DEFAULT_XP
        ))
        
        # Initialize game_state
        fuel0 = 195.0
        row = db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'fuel_base_price_bbl'"
        )
        if row:
            try:
                fuel0 = float(row["value"])
            except (TypeError, ValueError):
                fuel0 = 195.0
        
        db.execute("""
            INSERT OR REPLACE INTO game_state (
                id, game_week, schema_version,
                game_hours_elapsed, speed_multiplier,
                current_month, fuel_price_current, fuel_price_trend,
                demand_noise_seed, pause_on_week_summary
            ) VALUES (1, 1, 1, 0.0, 0, 1, ?, 0.0, 1, 0)
        """, (fuel0,))

        starter = grant_player_hub_starter_capacity(home_hub_iata)
        
        # Return created airline data
        airline = {
            'id': 1,
            'name': name,
            'callsign': callsign,
            'home_hub_iata': home_hub_iata,
            'cash': STARTING_CASH,
            'credit_score': DEFAULT_CREDIT_SCORE,
            'reputation_score': DEFAULT_REPUTATION,
            'brand_power': DEFAULT_BRAND_POWER,
            'xp': DEFAULT_XP,
            'starter_hub_gates': int(starter.get("gates") or 0),
            'starter_hub_slots': int(starter.get("slots") or 0),
        }
        
        print("\n" + "=" * 60)
        print("✓ AIRLINE CREATED SUCCESSFULLY!")
        print("=" * 60)
        print(f"Name: {name}")
        print(f"Callsign: {callsign}")
        print(f"Home Hub: {home_hub_iata} - {airport['name']}")
        print(f"Starting Cash: ${STARTING_CASH:,}")
        print(f"Credit Score: {DEFAULT_CREDIT_SCORE}")
        print(f"Reputation: {DEFAULT_REPUTATION:.1f}/100")
        if starter.get("gates"):
            print(f"Starter hub gates: {starter['gates']} (free at {home_hub_iata})")
        if starter.get("slots"):
            print(
                f"Starter hub slots: {starter['slots']} movements/week "
                f"for {starter.get('slot_weeks', 0)} weeks at {home_hub_iata}"
            )
        print("=" * 60 + "\n")
        
        return airline
        
    except Exception as e:
        print(f"\n✗ Failed to create airline: {e}\n")
        raise


def _reseed_competitors_after_purge() -> None:
    """Wipe left competitor rows in purge; rebuild roster from data/competitors.json."""
    try:
        from engine.ai import ensure_competitors_seeded, reset_competitor_seed_cache

        reset_competitor_seed_cache()
        ensure_competitors_seeded()
    except Exception:
        pass


def _stop_clock_and_news() -> None:
    try:
        from engine.clock import stop_game_clock

        stop_game_clock()
    except Exception:
        pass
    try:
        from engine.news_feed import clear_feed

        clear_feed()
    except Exception:
        pass


def reset_airline():
    """
    Keep name, callsign, and original hub; wipe progress back to a new-game start
    (starting cash, week 1, no fleet/routes).
    """
    al = get_airline()
    if not al:
        raise ValueError("No airline to reset.")
    name = str(al["name"])
    callsign = str(al["callsign"])
    hub = str(al["home_hub_iata"])
    _stop_clock_and_news()
    db.purge_all_player_game_data()
    _reseed_competitors_after_purge()
    return create_airline(name=name, callsign=callsign, home_hub_iata=hub)


def delete_airline():
    """Remove the player airline and all progress. Catalog/airports stay."""
    if not airline_exists():
        raise ValueError("No airline to delete.")
    _stop_clock_and_news()
    db.purge_all_player_game_data()
    _reseed_competitors_after_purge()


def get_airline():
    """
    Get the current airline data.
    
    Returns:
        dict: Airline data if exists, None otherwise
    """
    airline = db.fetch_one("SELECT * FROM airline WHERE id = 1")
    return dict(airline) if airline else None


def update_cash(amount):
    """
    Update airline cash balance.
    
    Args:
        amount: Amount to add (positive) or deduct (negative)
    
    Returns:
        float: New cash balance
    
    Raises:
        ValueError: If insufficient funds
    """
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found. Create an airline first.")
    
    new_cash = airline['cash'] + amount
    
    if new_cash < 0:
        raise ValueError(f"Insufficient funds. Current: ${airline['cash']:,}, Required: ${abs(amount):,}")
    
    db.execute("UPDATE airline SET cash = ? WHERE id = 1", (new_cash,))
    
    return new_cash


def apply_settlement_cash(amount: float) -> float:
    """Add/subtract cash during week-end settlement. May go negative (ops + debt)."""
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found. Create an airline first.")
    new_cash = float(airline["cash"] or 0.0) + float(amount)
    db.execute("UPDATE airline SET cash = ? WHERE id = 1", (new_cash,))
    return new_cash


def display_airline_info():
    """Display current airline information."""
    airline = get_airline()
    
    if not airline:
        print("\n⚠ No airline created yet.\n")
        return
    
    airport = _airport_dict(str(airline["home_hub_iata"]))

    print("\n" + "=" * 60)
    print("YOUR AIRLINE")
    print("=" * 60)
    print(f"Name: {airline['name']}")
    print(f"Callsign: {airline['callsign']}")
    print(f"Home Hub: {airline['home_hub_iata']} - {airport['name'] if airport else 'Unknown'}")
    print(f"\nFinancials:")
    print(f"  Cash: ${airline['cash']:,}")
    print(f"  Debt: ${airline['total_debt']:,}")
    print(f"  Credit Score: {airline['credit_score']}")
    print(f"\nReputation:")
    print(f"  Score: {airline['reputation_score']:.1f}/100")
    print(f"  Brand Power: {airline['brand_power']:.2f}x")
    print(f"  XP: {airline['xp']:,}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    # Test the module
    print("Testing airline creation...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        if airline_exists():
            print("Airline already exists:")
            display_airline_info()
        else:
            airline = create_airline()
            print("\nAirline created:", airline)
