"""
Aircraft management module for Phase 1.
Handles aircraft catalog browsing, purchasing, and leasing.
"""

import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db
from engine.setup import get_airline, update_cash


def list_catalog(category=None, sort_by='type_id'):
    """
    List all aircraft types from the catalog.
    
    Args:
        category: Filter by category (TURBOPROP, REGIONAL_JET, NARROW, WIDE)
        sort_by: Sort field (type_id, purchase_price, range_nm, etc.)
    
    Returns:
        list: Aircraft types formatted for display
    """
    aircraft_types = db.list_aircraft_types(category)
    
    # Sort by requested field
    if sort_by and sort_by in ['type_id', 'purchase_price', 'weekly_lease_cost', 'range_nm', 'category']:
        aircraft_types = sorted(aircraft_types, key=lambda x: x[sort_by])
    
    return aircraft_types


def get_aircraft_type(type_id):
    """Get aircraft type details."""
    return db.get_aircraft_type(type_id)


def generate_tail_number(airline_callsign, aircraft_count):
    """
    Generate a tail number for a new aircraft.
    Format: CALLSIGN-NNN (e.g., SJT-001, SJT-002)
    
    Args:
        airline_callsign: Airline's 3-letter callsign
        aircraft_count: Current number of aircraft in fleet
    
    Returns:
        str: Tail number
    """
    return f"{airline_callsign}-{aircraft_count + 1:03d}"


def get_fleet():
    """
    Get all aircraft in the player's fleet.
    
    Returns:
        list: Fleet aircraft with details
    """
    fleet = db.fetch_all("SELECT * FROM fleet ORDER BY tail_number")
    return [dict(aircraft) for aircraft in fleet]


def get_fleet_count():
    """Get total number of aircraft in fleet."""
    result = db.fetch_one("SELECT COUNT(*) as count FROM fleet")
    return result['count'] if result else 0


def _insert_initial_fleet_cabin(tail_number, type_id, eco, prem_eco, biz, first):
    """Create fleet_cabin_config at delivery (no reconfiguration fee)."""
    from engine.cabin import compute_eec_used, validate_config

    aircraft_row = get_aircraft_type(type_id)
    if not aircraft_row:
        raise ValueError(f"Aircraft type '{type_id}' not found")
    eec_limit = int(aircraft_row["eec"] or 0)
    if eec_limit <= 0:
        raise ValueError("Aircraft type missing EEC capacity data")
    is_valid, validation_msg = validate_config(eco, prem_eco, biz, first, eec_limit)
    if not is_valid:
        raise ValueError(validation_msg)
    eec_used = compute_eec_used(eco, prem_eco, biz, first)
    db.execute(
        """
        INSERT INTO fleet_cabin_config (
            tail_number, seats_economy, seats_premium_economy,
            seats_business, seats_first, eec_used,
            last_reconfig_week, reconfig_cost_paid
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0.0)
        """,
        (tail_number, eco, prem_eco, biz, first, eec_used),
    )


def _cabin_for_delivery(type_id, cabin_seats):
    """
    Resolve Y/W/J/F at delivery and validate EEC before a fleet row is written.
    Returns (eco, prem, biz, first) or None when there is no cabin data to store.
    """
    from engine.cabin import validate_config

    aircraft_row = get_aircraft_type(type_id)
    if not aircraft_row:
        raise ValueError(f"Aircraft type '{type_id}' not found in catalog.")
    if cabin_seats is not None:
        eco, prem_eco, biz, first = (int(x) for x in cabin_seats)
    else:
        default_config = db.fetch_one(
            "SELECT * FROM aircraft_default_config WHERE type_id = ?",
            (type_id,),
        )
        if not default_config:
            return None
        eco = int(default_config["seats_economy"])
        prem_eco = int(default_config["seats_premium_economy"])
        biz = int(default_config["seats_business"])
        first = int(default_config["seats_first"])
    eec_limit = int(aircraft_row["eec"] or 0)
    if eec_limit <= 0:
        raise ValueError("Aircraft type missing EEC capacity data")
    is_valid, validation_msg = validate_config(eco, prem_eco, biz, first, eec_limit)
    if not is_valid:
        raise ValueError(validation_msg)
    return (eco, prem_eco, biz, first)


def get_fleet_aircraft(tail_number):
    """
    Get a specific aircraft from the fleet by tail number.
    
    Args:
        tail_number: Aircraft tail number
    
    Returns:
        dict: Aircraft data or None if not found
    """
    aircraft = db.fetch_one(
        "SELECT * FROM fleet WHERE tail_number = ?",
        (tail_number,)
    )
    return dict(aircraft) if aircraft else None


def buy_aircraft(type_id, tail_number=None, cabin_seats=None):
    """
    Purchase an aircraft outright.
    
    Args:
        type_id: Aircraft type to purchase (e.g., 'B737-800')
        tail_number: Custom tail number (optional, will auto-generate if None)
        cabin_seats: Optional (economy, premium_economy, business, first) seat counts.
            If None, uses aircraft_default_config for the type.
    
    Returns:
        dict: Fleet entry for purchased aircraft
    
    Raises:
        ValueError: If insufficient funds, invalid aircraft type, or airline doesn't exist
    """
    # Get airline
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found. Create an airline first.")
    
    # Get aircraft type
    aircraft = get_aircraft_type(type_id)
    if not aircraft:
        raise ValueError(f"Aircraft type '{type_id}' not found in catalog.")
    
    # Check if player has enough cash
    purchase_price = aircraft['purchase_price']
    if airline['cash'] < purchase_price:
        raise ValueError(
            f"Insufficient funds. "
            f"Purchase price: ${purchase_price:,}, "
            f"Available cash: ${airline['cash']:,}"
        )
    
    # Generate tail number if not provided
    if tail_number is None:
        tail_number = generate_tail_number(airline['callsign'], get_fleet_count())
    
    # Check if tail number already exists
    existing = db.fetch_one("SELECT tail_number FROM fleet WHERE tail_number = ?", (tail_number,))
    if existing:
        raise ValueError(f"Tail number '{tail_number}' already exists in fleet.")

    cabin = _cabin_for_delivery(type_id, cabin_seats)

    # Add aircraft to fleet
    db.execute("""
        INSERT INTO fleet (
            tail_number, type_id, ownership, status,
            current_airport_iata, lease_weekly_cost,
            lease_weeks_remaining, weeks_since_maintenance, aog_reason
        ) VALUES (?, ?, 'OWNED', 'IDLE', ?, 0.0, NULL, 0, NULL)
    """, (tail_number, type_id, airline['home_hub_iata']))

    if cabin is not None:
        _insert_initial_fleet_cabin(tail_number, type_id, *cabin)
    
    # Deduct cash
    new_cash = update_cash(-purchase_price)
    
    # Return fleet entry
    fleet_aircraft = {
        'tail_number': tail_number,
        'type_id': type_id,
        'ownership': 'OWNED',
        'status': 'IDLE',
        'current_airport_iata': airline['home_hub_iata'],
        'purchase_price': purchase_price,
        'new_cash_balance': new_cash
    }
    
    print(f"\n✓ Purchased {aircraft['display_name']}")
    print(f"  Tail Number: {tail_number}")
    print(f"  Cost: ${purchase_price:,}")
    print(f"  New Cash Balance: ${new_cash:,}\n")
    
    return fleet_aircraft


def lease_aircraft(type_id, weeks, tail_number=None, cabin_seats=None):
    """
    Lease an aircraft for a specified number of weeks.
    
    Args:
        type_id: Aircraft type to lease (e.g., 'B737-800')
        weeks: Number of weeks to lease (must be positive)
        tail_number: Custom tail number (optional, will auto-generate if None)
        cabin_seats: Optional (economy, premium_economy, business, first) seat counts.
            If None, uses aircraft_default_config for the type.
    
    Returns:
        dict: Fleet entry for leased aircraft
    
    Raises:
        ValueError: If invalid parameters or airline doesn't exist
    """
    # Validate weeks
    if weeks <= 0:
        raise ValueError("Lease duration must be at least 1 week.")
    
    # Get airline
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found. Create an airline first.")
    
    # Get aircraft type
    aircraft = get_aircraft_type(type_id)
    if not aircraft:
        raise ValueError(f"Aircraft type '{type_id}' not found in catalog.")
    
    # Check cash and charge the first week now so $0 cash cannot lease.
    weekly_cost = float(aircraft['weekly_lease_cost'] or 0)
    if airline['cash'] < weekly_cost:
        raise ValueError(
            f"Insufficient funds. "
            f"First-week lease: ${weekly_cost:,.0f}, "
            f"Available cash: ${airline['cash']:,.0f}"
        )
    
    # Generate tail number if not provided
    if tail_number is None:
        tail_number = generate_tail_number(airline['callsign'], get_fleet_count())
    
    # Check if tail number already exists
    existing = db.fetch_one("SELECT tail_number FROM fleet WHERE tail_number = ?", (tail_number,))
    if existing:
        raise ValueError(f"Tail number '{tail_number}' already exists in fleet.")

    cabin = _cabin_for_delivery(type_id, cabin_seats)

    # Add aircraft to fleet
    db.execute("""
        INSERT INTO fleet (
            tail_number, type_id, ownership, status,
            current_airport_iata, lease_weekly_cost,
            lease_weeks_remaining, lease_prepaid, weeks_since_maintenance, aog_reason
        ) VALUES (?, ?, 'LEASED', 'IDLE', ?, ?, ?, 1, 0, NULL)
    """, (tail_number, type_id, airline['home_hub_iata'], weekly_cost, weeks))

    if cabin is not None:
        _insert_initial_fleet_cabin(tail_number, type_id, *cabin)

    new_cash = update_cash(-weekly_cost)

    fleet_aircraft = {
        'tail_number': tail_number,
        'type_id': type_id,
        'ownership': 'LEASED',
        'status': 'IDLE',
        'current_airport_iata': airline['home_hub_iata'],
        'weekly_lease_cost': weekly_cost,
        'lease_weeks_remaining': weeks,
        'total_lease_cost': weekly_cost * weeks,
        'new_cash_balance': new_cash,
    }

    return fleet_aircraft

def get_aircraft_seat_config(tail_number, type_id: str) -> str:
    """
    Compact Y/W/J/F string. For catalog rows (no tail), use type default from aircraft_default_config.

    Lazy-imports engine.cabin to avoid circular import (cabin imports this module for get_aircraft_type).
    """
    from engine.cabin import get_cabin_config, get_default_config

    if tail_number:
        cfg = get_cabin_config(tail_number) or get_default_config(type_id)
    else:
        cfg = get_default_config(type_id)
    if not cfg:
        return "-"
    y = int(cfg.get("seats_economy") or 0)
    w = int(cfg.get("seats_premium_economy") or 0)
    j = int(cfg.get("seats_business") or 0)
    f = int(cfg.get("seats_first") or 0)
    parts = [f"Y{y}"]
    if w:
        parts.append(f"W{w}")
    parts.append(f"J{j}")
    if f:
        parts.append(f"F{f}")
    total = y + w + j + f
    return " ".join(parts) + f"  ({total})"

def display_catalog(category=None, limit=20):
    """Display aircraft catalog in a formatted view."""
    aircraft_list = list_catalog(category)
    
    if not aircraft_list:
        print("\n⚠ No aircraft found in catalog.\n")
        return
    
    print("\n" + "=" * 145)
    print("AIRCRAFT CATALOG")
    if category:
        print(f"Category: {category}")
    print("=" * 145)
    print(
        f"{'Type ID':<12} {'Name':<35} {'Cat':<12} {'Range':>8} {'Speed':>7} "
        f"{'Price':>15} {'Lease/wk':>15} {'Default cabin (Y/W/J/F)':>24}"
    )
    print("-" * 145)
    
    for i, aircraft in enumerate(aircraft_list[:limit]):
        print(
            f"{aircraft['type_id']:<12} "
            f"{aircraft['display_name']:<35} "
            f"{aircraft['category']:<12} "
            f"{aircraft['range_nm']:>7,}nm "
            f"{aircraft['cruise_speed_kts']:>6}kt "
            f"${aircraft['purchase_price']:>13,} "
            f"${aircraft['weekly_lease_cost']:>13,}"
            f"{get_aircraft_seat_config(None, str(aircraft['type_id'])):>22}"
        )
    
    if len(aircraft_list) > limit:
        print(f"\n... and {len(aircraft_list) - limit} more aircraft")
    
    print("=" * 145 + "\n")


def display_fleet():
    """Display current fleet in a formatted view."""
    fleet = get_fleet()
    
    if not fleet:
        print("\n⚠ No aircraft in fleet. Purchase or lease aircraft to get started.\n")
        return
    
    print("\n" + "=" * 100)
    print("YOUR FLEET")
    print("=" * 100)
    print(f"{'Tail #':<12} {'Type':<12} {'Model':<30} {'Owner':<8} {'Status':<12} {'Location':<8}")
    print("-" * 100)
    
    for aircraft in fleet:
        ac_type = get_aircraft_type(aircraft['type_id'])
        display_name = ac_type['display_name'] if ac_type else aircraft['type_id']
        
        print(
            f"{aircraft['tail_number']:<12} "
            f"{aircraft['type_id']:<12} "
            f"{display_name:<30} "
            f"{aircraft['ownership']:<8} "
            f"{aircraft['status']:<12} "
            f"{aircraft['current_airport_iata']:<8}"
        )
    
    print("=" * 100)
    print(f"Total Aircraft: {len(fleet)}")
    print("=" * 100 + "\n")


# Seat Configuration System

# Seat type space equivalents (in "economy seat units")
SEAT_SPACE_UNITS = {
    'economy': 1.0,      # 1 economy seat = 1 unit
    'premium_economy': 1.5,  # 1 premium economy = 1.5 units
    'business': 2.0,     # 1 business = 2 units
    'first': 4.0         # 1 first class = 4 units
}


def get_aircraft_max_seats(type_id):
    """
    Get maximum seat capacity for an aircraft type.
    
    This is estimated based on aircraft category:
    - TURBOPROP: 10-20 seats
    - REGIONAL_JET: 50-100 seats
    - NARROW: 120-200 seats
    - WIDE: 250-400 seats
    
    Args:
        type_id: Aircraft type identifier
    
    Returns:
        int: Maximum economy-equivalent seats
    """
    aircraft_type = get_aircraft_type(type_id)
    if not aircraft_type:
        return 0
    
    # If max_seats is in database, use it
    if 'eec' in aircraft_type and aircraft_type['eec']:
        return aircraft_type['eec']


def calculate_seat_units(economy=0, premium_economy=0, business=0, first=0):
    """
    Calculate total seat space units for a given configuration.
    
    Uses the conversion:
    - 1 economy = 1 unit
    - 1 premium economy = 1.5 units
    - 1 business = 2 units
    - 1 first = 4 units
    
    Args:
        economy: Number of economy seats
        premium_economy: Number of premium economy seats
        business: Number of business seats
        first: Number of first class seats
    
    Returns:
        float: Total seat space units
    """
    total_units = (
        economy * SEAT_SPACE_UNITS['economy'] +
        premium_economy * SEAT_SPACE_UNITS['premium_economy'] +
        business * SEAT_SPACE_UNITS['business'] +
        first * SEAT_SPACE_UNITS['first']
    )
    return total_units


def validate_seat_configuration(type_id, economy=0, premium_economy=0, business=0, first=0):
    """
    Validate that a proposed seat configuration fits in the aircraft.
    
    Args:
        type_id: Aircraft type identifier
        economy: Number of economy seats
        premium_economy: Number of premium economy seats
        business: Number of business seats
        first: Number of first class seats
    
    Returns:
        dict: {
            'valid': bool,
            'total_seats': int,
            'total_units': float,
            'max_seats': int,
            'units_used': float,
            'units_available': float,
            'utilization_pct': float,
            'message': str
        }
    
    Raises:
        ValueError: If aircraft type doesn't exist
    """
    aircraft_type = get_aircraft_type(type_id)
    if not aircraft_type:
        raise ValueError(f"Aircraft type '{type_id}' not found.")
    
    # Get maximum capacity
    max_seats = get_aircraft_max_seats(type_id)
    
    # Calculate proposed configuration
    total_seats = economy + premium_economy + business + first
    total_units = calculate_seat_units(economy, premium_economy, business, first)
    
    # Check if it fits
    valid = total_units <= max_seats
    units_available = max_seats - total_units
    utilization_pct = (total_units / max_seats * 100) if max_seats > 0 else 0
    
    if valid:
        if utilization_pct > 90:
            message = f"✓ Configuration valid. Excellent utilization ({utilization_pct:.1f}%)."
        elif utilization_pct > 70:
            message = f"✓ Configuration valid. Good utilization ({utilization_pct:.1f}%)."
        else:
            message = f"✓ Configuration valid. Room for {units_available:.1f} more seat units ({utilization_pct:.1f}% used)."
    else:
        excess = total_units - max_seats
        message = f"✗ Configuration exceeds capacity by {excess:.1f} seat units."
    
    return {
        'valid': valid,
        'total_seats': total_seats,
        'total_units': total_units,
        'max_seats': max_seats,
        'units_used': total_units,
        'units_available': units_available if valid else -abs(units_available),
        'utilization_pct': utilization_pct,
        'message': message
    }


def suggest_seat_configurations(type_id):
    """
    Suggest common seat configurations for an aircraft type.
    
    Args:
        type_id: Aircraft type identifier
    
    Returns:
        list: List of suggested configurations with descriptions
    """
    aircraft_type = get_aircraft_type(type_id)
    if not aircraft_type:
        return []
    
    max_seats = get_aircraft_max_seats(type_id)
    category = aircraft_type['category']
    
    suggestions = []
    
    if category == 'TURBOPROP':
        # All economy for small aircraft
        suggestions.append({
            'name': 'All Economy',
            'economy': max_seats,
            'premium_economy': 0,
            'business': 0,
            'first': 0,
            'description': 'Maximum capacity, budget carrier'
        })
    
    elif category == 'REGIONAL_JET':
        # All economy
        suggestions.append({
            'name': 'All Economy',
            'economy': max_seats,
            'premium_economy': 0,
            'business': 0,
            'first': 0,
            'description': 'Maximum capacity'
        })
        # Premium split
        suggestions.append({
            'name': 'Economy + Business',
            'economy': int(max_seats * 0.7),
            'premium_economy': 0,
            'business': int(max_seats * 0.15),
            'first': 0,
            'description': 'Regional business service'
        })
    
    elif category == 'NARROW':
        # All economy
        suggestions.append({
            'name': 'All Economy',
            'economy': max_seats,
            'premium_economy': 0,
            'business': 0,
            'first': 0,
            'description': 'Low-cost carrier configuration'
        })
        # Two-class
        suggestions.append({
            'name': 'Economy + Premium Economy',
            'economy': int(max_seats * 0.75),
            'premium_economy': int(max_seats * 0.15),
            'business': 0,
            'first': 0,
            'description': 'Standard two-class service'
        })
        # Three-class
        suggestions.append({
            'name': 'Economy + Premium + Business',
            'economy': int(max_seats * 0.65),
            'premium_economy': int(max_seats * 0.12),
            'business': int(max_seats * 0.10),
            'first': 0,
            'description': 'Premium mainline service'
        })
    
    elif category == 'WIDE':
        # All economy
        suggestions.append({
            'name': 'All Economy',
            'economy': max_seats,
            'premium_economy': 0,
            'business': 0,
            'first': 0,
            'description': 'High-density charter configuration'
        })
        # Three-class
        suggestions.append({
            'name': 'Economy + Premium + Business',
            'economy': int(max_seats * 0.60),
            'premium_economy': int(max_seats * 0.15),
            'business': int(max_seats * 0.12),
            'first': 0,
            'description': 'Standard international configuration'
        })
        # Four-class
        suggestions.append({
            'name': 'Four-Class Premium',
            'economy': int(max_seats * 0.50),
            'premium_economy': int(max_seats * 0.15),
            'business': int(max_seats * 0.10),
            'first': int(max_seats * 0.03),
            'description': 'Luxury international service'
        })
    
    # Validate all suggestions
    valid_suggestions = []
    for config in suggestions:
        validation = validate_seat_configuration(
            type_id,
            config['economy'],
            config['premium_economy'],
            config['business'],
            config['first']
        )
        if validation['valid']:
            config['validation'] = validation
            valid_suggestions.append(config)
    
    return valid_suggestions


def display_seat_configuration_test(type_id):
    """Display seat configuration validation for testing."""
    print(f"\n{'=' * 80}")
    print(f"SEAT CONFIGURATION VALIDATOR: {type_id}")
    print(f"{'=' * 80}")
    
    aircraft_type = get_aircraft_type(type_id)
    if not aircraft_type:
        print(f"✗ Aircraft '{type_id}' not found.")
        return
    
    print(f"Aircraft: {aircraft_type['display_name']}")
    print(f"Category: {aircraft_type['category']}")
    
    max_seats = get_aircraft_max_seats(type_id)
    print(f"Max Capacity: {max_seats} economy seats (or equivalent)\n")
    
    print("Seat Space Conversion Rates:")
    for seat_type, units in SEAT_SPACE_UNITS.items():
        print(f"  {seat_type.replace('_', ' ').title():<20} = {units} units")
    
    print(f"\n{'=' * 80}")
    print("SUGGESTED CONFIGURATIONS")
    print(f"{'=' * 80}")
    
    suggestions = suggest_seat_configurations(type_id)
    for i, config in enumerate(suggestions, 1):
        print(f"\n{i}. {config['name']}")
        print(f"   {config['description']}")
        print(f"   Economy: {config['economy']}, Premium Economy: {config['premium_economy']}, " 
              f"Business: {config['business']}, First: {config['first']}")
        print(f"   Total Seats: {config['validation']['total_seats']}")
        print(f"   Space Units: {config['validation']['total_units']:.1f} / {max_seats}")
        print(f"   Utilization: {config['validation']['utilization_pct']:.1f}%")
    
    print(f"\n{'=' * 80}\n")


if __name__ == "__main__":
    # Test the module
    print("Testing aircraft module...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        print("\nAircraft Catalog (first 10):")
        display_catalog(limit=10)
        
        print("\nCurrent Fleet:")
        display_fleet()
        
        # Test seat configuration validator
        print("\n" + "=" * 80)
        print("TESTING SEAT CONFIGURATION VALIDATOR")
        print("=" * 80)
        
        # Test with different aircraft types
        test_aircraft = ['B737-800', 'A350-900', 'CRJ-900', 'PC12']
        for ac_type in test_aircraft:
            ac = get_aircraft_type(ac_type)
            if ac:
                display_seat_configuration_test(ac_type)
