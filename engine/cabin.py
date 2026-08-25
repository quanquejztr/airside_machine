"""
Cabin configuration module for Phase 1.
Handles seat configuration, validation, reconfiguration, and fare calculation.
"""

import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db
from engine.setup import get_airline
from engine.aircraft import get_aircraft_type


def compute_eec_used(eco: int, prem_eco: int, biz: int, first: int) -> int:
    """
    Compute EEC usage for a cabin layout using the per-seat multipliers.
    Returned as an integer for storage (table column is INTEGER).
    """
    eec_cost_eco = float(
        db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_cost_economy'")[
            "value"
        ]
    )
    eec_cost_prem = float(
        db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_cost_prem_eco'")[
            "value"
        ]
    )
    eec_cost_biz = float(
        db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_cost_business'")[
            "value"
        ]
    )
    eec_cost_first = float(
        db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_cost_first'")[
            "value"
        ]
    )
    used = (
        float(eco) * eec_cost_eco
        + float(prem_eco) * eec_cost_prem
        + float(biz) * eec_cost_biz
        + float(first) * eec_cost_first
    )
    return int(round(used))


def default_premium_first_ticket_fares(price_leisure: float, price_business: float) -> tuple[float, float]:
    """
    Default W and F one-way ticket prices from leisure/business *base* prices,
    using the same eec_yield multipliers as legacy fare_for_class.
    """
    r_pe = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_yield_prem_eco'")
    r_fi = db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_yield_first'")
    m_pe = float(r_pe["value"]) if r_pe else 1.5
    m_fi = float(r_fi["value"]) if r_fi else 4.0
    return (
        round(float(price_leisure) * m_pe, 2),
        round(float(price_business) * m_fi, 2),
    )


def ensure_cabin_config_exists(tail_number: str, *, type_id: str | None = None) -> dict | None:
    """
    Ensure there is a `fleet_cabin_config` row for this tail.
    If missing (legacy data / incomplete seed), create one from `aircraft_default_config`,
    or fall back to an all-economy layout sized to the aircraft's EEC limit.
    """
    tail = (tail_number or "").strip().upper()
    if not tail:
        return None
    existing = get_cabin_config(tail)
    if existing:
        return existing

    # Resolve type_id from fleet if not provided
    tid = (type_id or "").strip().upper() if type_id else None
    if not tid:
        fr = db.fetch_one("SELECT type_id FROM fleet WHERE tail_number = ?", (tail,))
        tid = str(fr["type_id"]).strip().upper() if fr and fr["type_id"] else None
    if not tid:
        return None

    default_cfg = get_default_config(tid)
    if default_cfg:
        eco = int(default_cfg["seats_economy"] or 0)
        prem = int(default_cfg["seats_premium_economy"] or 0)
        biz = int(default_cfg["seats_business"] or 0)
        first = int(default_cfg["seats_first"] or 0)
    else:
        # Fallback: all-economy sized to EEC limit (economy multiplier is usually 1.0).
        at = get_aircraft_type(tid)
        eec_limit = int(at["eec"] or 0) if at else 0
        eec_cost_eco = float(
            db.fetch_one("SELECT value FROM financial_constants WHERE key = 'eec_cost_economy'")[
                "value"
            ]
        )
        eco = int(eec_limit / max(0.1, eec_cost_eco)) if eec_limit > 0 else 0
        prem = 0
        biz = 0
        first = 0

    at2 = get_aircraft_type(tid)
    eec_limit2 = int(at2["eec"] or 0) if at2 else 0
    ok, _msg = validate_config(eco, prem, biz, first, eec_limit2)
    if not ok:
        # If even the fallback doesn't validate (weird constants), clamp to something safe.
        eco = max(1, min(eco, eec_limit2 or eco))
        prem = biz = first = 0

    eec_used = compute_eec_used(eco, prem, biz, first)
    db.execute(
        """
        INSERT OR IGNORE INTO fleet_cabin_config (
            tail_number, seats_economy, seats_premium_economy,
            seats_business, seats_first, eec_used,
            last_reconfig_week, reconfig_cost_paid
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0.0)
        """,
        (tail, eco, prem, biz, first, eec_used),
    )
    return get_cabin_config(tail)


def get_cabin_config(tail_number):
    """
    Get current cabin configuration for a specific aircraft.
    
    Args:
        tail_number: Aircraft tail number (e.g., 'SJT-001')
    
    Returns:
        dict: {
            'tail_number': str,
            'seats_economy': int,
            'seats_premium_economy': int,
            'seats_business': int,
            'seats_first': int,
            'eec_used': int,
            'last_reconfig_week': int,
            'reconfig_cost_paid': float
        } or None if not found
    """
    config = db.fetch_one(
        "SELECT * FROM fleet_cabin_config WHERE tail_number = ?",
        (tail_number,),
    )
    
    return dict(config) if config else None


def get_default_config(type_id):
    """
    Get default cabin configuration for an aircraft type.
    
    Args:
        type_id: Aircraft type identifier (e.g., 'B737')
    
    Returns:
        dict: Default cabin configuration or None if not found
    """
    config = db.fetch_one(
        "SELECT * FROM aircraft_default_config WHERE type_id = ?",
        (type_id,)
    )
    
    return dict(config) if config else None


def validate_config(eco, prem_eco, biz, first, eec_limit):
    """
    Validate that a proposed cabin configuration fits within EEC limit.
    
    EEC formula:
    eec_used = (eco × 1.0) + (prem_eco × 1.5) + (biz × 2.0) + (first × 4.0)
    
    Args:
        eco: Number of economy seats
        prem_eco: Number of premium economy seats
        biz: Number of business seats
        first: Number of first class seats
        eec_limit: Maximum EEC capacity for the aircraft
    
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    # Get EEC cost multipliers from financial_constants
    eec_cost_eco = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_economy'"
    )['value'])
    
    eec_cost_prem = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_prem_eco'"
    )['value'])
    
    eec_cost_biz = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_business'"
    )['value'])
    
    eec_cost_first = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_first'"
    )['value'])
    
    # Calculate EEC used
    eec_used = (
        eco * eec_cost_eco +
        prem_eco * eec_cost_prem +
        biz * eec_cost_biz +
        first * eec_cost_first
    )
    
    # Validate
    if eec_used > eec_limit:
        excess = eec_used - eec_limit
        return False, f"Configuration exceeds capacity by {excess:.1f} EEC units"
    
    # Check for negative seats
    if eco < 0 or prem_eco < 0 or biz < 0 or first < 0:
        return False, "Seat counts cannot be negative"
    
    # Check for zero total seats
    total_seats = eco + prem_eco + biz + first
    if total_seats == 0:
        return False, "Configuration must have at least one seat"
    
    # Success
    utilization = (eec_used / eec_limit * 100) if eec_limit > 0 else 0
    remaining = eec_limit - eec_used
    
    return True, f"Valid configuration. {utilization:.1f}% utilized, {remaining:.1f} EEC remaining"


def fare_for_class(seat_class, route_id, route_row=None):
    """
    Calculate fare for a specific seat class on a route.
    
    Uses yield multipliers from financial_constants:
    - Economy: price_leisure × 1.0
    - Premium Economy: stored price_premium_economy, or price_leisure × eec_yield_prem_eco
    - Business: price_business × eec_yield_business
    - First: stored price_first, or price_business × eec_yield_first
    
    Args:
        seat_class: 'economy', 'premium_economy', 'business', or 'first'
        route_id: Route identifier (e.g., 'TPA-JFK')
        route_row: optional route dict (e.g. what-if prices without writing DB)
    
    Returns:
        float: Calculated fare for the class
    
    Raises:
        ValueError: If route or seat class is invalid
    """
    # Get route
    if route_row is not None:
        route = dict(route_row) if not isinstance(route_row, dict) else route_row
    else:
        r = db.fetch_one("SELECT * FROM routes WHERE route_id = ?", (route_id,))
        route = dict(r) if r else None
    if not route:
        raise ValueError(f"Route '{route_id}' not found")
    
    def _num(key: str) -> float | None:
        v = route.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Get yield multipliers from financial_constants
    if seat_class == 'economy':
        multiplier = float(db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'eec_yield_economy'"
        )['value'])
        base_fare = float(route['price_leisure'])
    
    elif seat_class == 'premium_economy':
        pv = _num("price_premium_economy")
        if pv is not None and pv > 0:
            return pv
        multiplier = float(db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'eec_yield_prem_eco'"
        )['value'])
        base_fare = float(route['price_leisure'])
        return base_fare * multiplier
    
    elif seat_class == 'business':
        multiplier = float(db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'eec_yield_business'"
        )['value'])
        base_fare = float(route['price_business'])
        return base_fare * multiplier
    
    elif seat_class == 'first':
        fv = _num("price_first")
        if fv is not None and fv > 0:
            return fv
        multiplier = float(db.fetch_one(
            "SELECT value FROM financial_constants WHERE key = 'eec_yield_first'"
        )['value'])
        base_fare = float(route['price_business'])
        return base_fare * multiplier
    
    else:
        raise ValueError(f"Invalid seat class: '{seat_class}'")
    
    return float(base_fare) * float(multiplier)


def eec_delta(old_config, new_config):
    """
    Calculate absolute EEC change between two configurations.
    Used to determine reconfiguration cost.
    
    Args:
        old_config: dict with seats_economy, seats_premium_economy, seats_business, seats_first
        new_config: dict with seats_economy, seats_premium_economy, seats_business, seats_first
    
    Returns:
        float: Absolute EEC change
    """
    # Get EEC cost multipliers
    eec_cost_eco = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_economy'"
    )['value'])
    
    eec_cost_prem = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_prem_eco'"
    )['value'])
    
    eec_cost_biz = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_business'"
    )['value'])
    
    eec_cost_first = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_first'"
    )['value'])
    
    # Calculate old EEC
    old_eec = (
        old_config['seats_economy'] * eec_cost_eco +
        old_config['seats_premium_economy'] * eec_cost_prem +
        old_config['seats_business'] * eec_cost_biz +
        old_config['seats_first'] * eec_cost_first
    )
    
    # Calculate new EEC
    new_eec = (
        new_config['seats_economy'] * eec_cost_eco +
        new_config['seats_premium_economy'] * eec_cost_prem +
        new_config['seats_business'] * eec_cost_biz +
        new_config['seats_first'] * eec_cost_first
    )
    
    # Return absolute difference
    return abs(new_eec - old_eec)


def reconfigure(tail_number, eco, prem_eco, biz, first):
    """
    Reconfigure an aircraft's cabin layout.
    
    Requirements:
    - Aircraft must be IDLE
    - Aircraft must be at home hub
    - New configuration must be valid (within EEC limit)
    - Player must have enough cash for reconfiguration fee
    
    Cost formula: abs(eec_delta) × reconfig_cost_per_eec
    
    Args:
        tail_number: Aircraft to reconfigure
        eco: New economy seats
        prem_eco: New premium economy seats
        biz: New business seats
        first: New first class seats
    
    Returns:
        tuple: (success: bool, cost: float, message: str)
    
    Raises:
        ValueError: If aircraft doesn't exist or requirements not met
    """
    # Get aircraft
    fleet_aircraft = db.fetch_one("SELECT * FROM fleet WHERE tail_number = ?", (tail_number,))
    if not fleet_aircraft:
        raise ValueError(f"Aircraft '{tail_number}' not found in fleet")
    
    # Get airline
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found")
    
    # Check status
    if fleet_aircraft['status'] != 'IDLE':
        return False, 0.0, f"Aircraft must be IDLE (currently {fleet_aircraft['status']})"
    
    # Check location
    if fleet_aircraft['current_airport_iata'] != airline['home_hub_iata']:
        return False, 0.0, f"Aircraft must be at home hub {airline['home_hub_iata']} (currently at {fleet_aircraft['current_airport_iata']})"
    
    # Get aircraft type and EEC limit
    aircraft_type = get_aircraft_type(fleet_aircraft['type_id'])
    if not aircraft_type:
        raise ValueError(f"Aircraft type '{fleet_aircraft['type_id']}' not found")
    # sqlite3.Row does not have .get(); convert to dict or use bracket access
    aircraft_type = dict(aircraft_type) if aircraft_type else {}
    eec_limit = int(aircraft_type.get('eec', 0))
    if eec_limit == 0:
        return False, 0.0, "Aircraft type missing EEC capacity data"
    
    # Validate new configuration
    is_valid, validation_msg = validate_config(eco, prem_eco, biz, first, eec_limit)
    if not is_valid:
        return False, 0.0, validation_msg
    
    # Get old configuration
    old_config = get_cabin_config(tail_number) or ensure_cabin_config_exists(
        tail_number, type_id=str(fleet_aircraft["type_id"])
    )
    if not old_config:
        return False, 0.0, "Current cabin configuration not found"
    
    # Calculate cost
    new_config = {
        'seats_economy': eco,
        'seats_premium_economy': prem_eco,
        'seats_business': biz,
        'seats_first': first
    }
    
    delta = eec_delta(old_config, new_config)
    cost_per_eec = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'reconfig_cost_per_eec'"
    )['value'])
    
    total_cost = delta * cost_per_eec
    
    # Check cash
    if airline['cash'] < total_cost:
        return False, total_cost, f"Insufficient funds. Cost: ${total_cost:,.2f}, Available: ${airline['cash']:,.2f}"
    
    # Calculate new EEC used
    eec_cost_eco = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_economy'"
    )['value'])
    eec_cost_prem = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_prem_eco'"
    )['value'])
    eec_cost_biz = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_business'"
    )['value'])
    eec_cost_first = float(db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'eec_cost_first'"
    )['value'])
    
    new_eec_used = int(
        eco * eec_cost_eco +
        prem_eco * eec_cost_prem +
        biz * eec_cost_biz +
        first * eec_cost_first
    )
    
    # Get current game week
    game_state = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
    current_week = game_state['game_week'] if game_state else 1
    
    # Update cabin configuration
    db.execute("""
        UPDATE fleet_cabin_config
        SET seats_economy = ?,
            seats_premium_economy = ?,
            seats_business = ?,
            seats_first = ?,
            eec_used = ?,
            last_reconfig_week = ?,
            reconfig_cost_paid = ?
        WHERE tail_number = ?
    """, (eco, prem_eco, biz, first, new_eec_used, current_week, total_cost, tail_number))
    
    # Deduct cash
    from engine.setup import update_cash

    try:
        update_cash(-total_cost)
    except ValueError as e:
        return False, total_cost, str(e)
    
    success_msg = f"✓ Reconfiguration complete. Cost: ${total_cost:,.2f}. New configuration: {eco}Y/{prem_eco}PE/{biz}J/{first}F"
    
    return True, total_cost, success_msg


if __name__ == "__main__":
    # Test the module
    print("Testing cabin module...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        # Test fare calculation
        print("\nTesting fare_for_class():")
        routes = db.fetch_all("SELECT route_id FROM routes LIMIT 1")
        if routes:
            route_id = routes[0]['route_id']
            try:
                eco_fare = fare_for_class('economy', route_id)
                prem_fare = fare_for_class('premium_economy', route_id)
                biz_fare = fare_for_class('business', route_id)
                first_fare = fare_for_class('first', route_id)
                
                print(f"Route: {route_id}")
                print(f"  Economy: ${eco_fare:.2f}")
                print(f"  Premium Economy: ${prem_fare:.2f}")
                print(f"  Business: ${biz_fare:.2f}")
                print(f"  First: ${first_fare:.2f}")
            except Exception as e:
                print(f"  Error: {e}")
        
        # Test validation
        print("\nTesting validate_config():")
        valid, msg = validate_config(150, 20, 10, 0, 200)
        print(f"  Config (150Y/20PE/10J/0F, limit 200): {valid}")
        print(f"  Message: {msg}")
