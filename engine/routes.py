"""
Route management module for Phase 1.
Handles route opening with Haversine distance calculation.
"""

import sys
import math
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db
from engine.airports import get_airport
from engine.cabin import default_premium_first_ticket_fares


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate great circle distance between two points using Haversine formula.
    
    Args:
        lat1, lon1: First point coordinates (degrees)
        lat2, lon2: Second point coordinates (degrees)
    
    Returns:
        float: Distance in nautical miles
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) ** 2)
    
    c = 2 * math.asin(math.sqrt(a))
    
    # Earth's radius in nautical miles
    earth_radius_nm = 3440.065
    
    distance = earth_radius_nm * c
    return round(distance, 2)


def route_exists(origin_iata, dest_iata):
    """True if a row exists in the global ``routes`` catalog (may include AI-prepared markets)."""
    route_id = f"{origin_iata.upper()}-{dest_iata.upper()}"
    route = db.fetch_one("SELECT route_id FROM routes WHERE route_id = ?", (route_id,))
    return route is not None


def player_route_exists(origin_iata, dest_iata) -> bool:
    """True if the player airline has added this directional route to their network (``player_routes``)."""
    route_id = f"{origin_iata.upper()}-{dest_iata.upper()}"
    row = db.fetch_one("SELECT 1 FROM player_routes WHERE route_id = ?", (route_id,))
    return row is not None


def _large_airport_gated_route(origin: dict, dest: dict) -> bool:
    return origin.get("category") == "large_airport" or dest.get("category") == "large_airport"


def _enrich_route_dict(route: dict) -> dict:
    """Normalize sqlite row dict: defaults for is_active and optional cabin fares."""
    d = dict(route)
    d.setdefault("is_active", 1)
    if d.get("price_premium_economy") is None or d.get("price_first") is None:
        pl = float(d.get("price_leisure") or 0)
        pb = float(d.get("price_business") or 0)
        pp, pf = default_premium_first_ticket_fares(pl, pb)
        if d.get("price_premium_economy") is None:
            d["price_premium_economy"] = pp
        if d.get("price_first") is None:
            d["price_first"] = pf
    return d


def get_route(route_id):
    """Get route by route_id."""
    route = db.fetch_one("SELECT * FROM routes WHERE route_id = ?", (route_id,))
    if not route:
        return None
    return _enrich_route_dict(route)


def get_all_routes():
    """Get all active routes."""
    routes = db.fetch_all("SELECT * FROM routes ORDER BY route_id")
    out = []
    for route in routes:
        out.append(_enrich_route_dict(route))
    return out


def get_player_routes() -> list[dict]:
    """
    Routes the PLAYER has opened.

    We track these explicitly in `player_routes` because the `routes` table is also used as the
    global market route catalog (AI may insert routes for simulation even if the player never opened them).
    """
    rows = db.fetch_all(
        """
        SELECT r.*
        FROM routes r
        JOIN player_routes pr ON pr.route_id = r.route_id
        ORDER BY r.route_id
        """
    )
    out = []
    for route in rows:
        out.append(_enrich_route_dict(route))
    return out


def estimate_base_demand(distance_nm, origin_airport, dest_airport):
    """
    Base weekly business/leisure demand for a new route.

    Prefers BTS market anchors (US), then gravity, then legacy category buckets.
    See engine.route_demand.compute_base_demand for details.
    """
    from engine.route_demand import compute_base_demand

    out = compute_base_demand(distance_nm, origin_airport, dest_airport)
    return int(out["base_demand_business"]), int(out["base_demand_leisure"])


def calculate_route_acquisition_cost(origin, dest, distance_nm):
    """
    Calculate the cost to acquire/open a new route.
    
    Formula: (origin_score * origin_multiplier + destination_score * destination_multiplier + distance * 0.3) * (1 + excise_tax_rate)
    
    Multipliers by airport category:
    - large_airport: 0.015
    - medium_airport: 0.01
    - small_airport: 0.0075
    
    Args:
        origin: Origin airport dict with 'score' and 'category'
        dest: Destination airport dict with 'score' and 'category'
        distance_nm: Route distance in nautical miles
    
    Returns:
        float: Route acquisition cost in USD
    """
    # Get excise tax rate from financial constants
    excise_tax = db.fetch_one(
        "SELECT value FROM financial_constants WHERE key = 'excise_tax_rate'"
    )
    excise_tax_rate = excise_tax['value'] if excise_tax else 0.075
    
    # Determine multipliers based on airport category
    category_multipliers = {
        'large_airport': 0.015,
        'medium_airport': 0.01,
        'small_airport': 0.0075
    }
    
    origin_multiplier = category_multipliers.get(origin['category'], 0.0075)
    dest_multiplier = category_multipliers.get(dest['category'], 0.0075)
    
    # Get airport scores (default to 0 if not present)
    origin_score = origin.get('score', 0)
    dest_score = dest.get('score', 0)
    
    # Calculate base cost
    base_cost = (
        (origin_score * origin_multiplier) +
        (dest_score * dest_multiplier) +
        (distance_nm * 0.3)
    )
    
    # Apply excise tax
    total_cost = base_cost * (1 + excise_tax_rate)
    
    return round(total_cost, 2)


def preview_route_opening(origin_iata, dest_iata, hub_iata=None):
    """
    Plan which route rows to add and the cash required.

    Rules:
    - If the airline home hub touches this city pair (hub is origin or destination), missing
      legs are opened as a pair: **one** acquisition fee (for the user-requested direction’s
      formula) covers both directions; any second leg is free at acquisition time.
    - If neither airport is the hub, only the requested origin→destination leg is opened,
      at normal acquisition cost.

    Returns:
        dict with mode, hub_involved, total_new_cost, opens (list of dicts with origin, dest,
        charge_acquisition), forward/reverse existence, distance_nm, reference acquisition
        for the requested direction (cost_reference).
    """
    origin_iata = origin_iata.upper()
    dest_iata = dest_iata.upper()
    hub_u = hub_iata.strip().upper() if hub_iata else None

    if origin_iata == dest_iata:
        raise ValueError("Origin and destination must be different airports.")
    origin = get_airport(origin_iata)
    dest = get_airport(dest_iata)
    if not origin:
        raise ValueError(f"Origin airport '{origin_iata}' not found.")
    if not dest:
        raise ValueError(f"Destination airport '{dest_iata}' not found.")

    distance_nm = haversine_distance(
        origin["lat"], origin["lon"],
        dest["lat"], dest["lon"],
    )
    cost_reference = calculate_route_acquisition_cost(origin, dest, distance_nm)
    large_fwd = _large_airport_gated_route(origin, dest)

    fwd_id = f"{origin_iata}-{dest_iata}"
    rev_id = f"{dest_iata}-{origin_iata}"
    cat_fwd = route_exists(origin_iata, dest_iata)
    cat_rev = route_exists(dest_iata, origin_iata)
    player_fwd = player_route_exists(origin_iata, dest_iata)
    player_rev = player_route_exists(dest_iata, origin_iata)

    hub_involved = bool(hub_u) and (origin_iata == hub_u or dest_iata == hub_u)
    opens = []

    if not hub_involved:
        if player_fwd:
            total_new = 0.0
            mode = "single"
        else:
            total_new = cost_reference
            mode = "single"
            opens.append(
                {
                    "origin": origin_iata,
                    "dest": dest_iata,
                    "charge_acquisition": True,
                }
            )
    else:
        mode = "hub_pair"
        if player_fwd and player_rev:
            total_new = 0.0
        elif not player_fwd and not player_rev:
            rev_origin = get_airport(dest_iata)
            rev_dest = get_airport(origin_iata)
            large_rev = (
                _large_airport_gated_route(rev_origin, rev_dest) if rev_origin and rev_dest else False
            )
            total_new = cost_reference
            opens.append(
                {
                    "origin": origin_iata,
                    "dest": dest_iata,
                    "charge_acquisition": True,
                }
            )
            opens.append(
                {
                    "origin": dest_iata,
                    "dest": origin_iata,
                    "charge_acquisition": False,
                }
            )
        elif player_fwd and not player_rev:
            total_new = 0.0
            opens.append(
                {
                    "origin": dest_iata,
                    "dest": origin_iata,
                    "charge_acquisition": False,
                }
            )
        else:
            # player has reverse only; add forward
            total_new = 0.0
            opens.append(
                {
                    "origin": origin_iata,
                    "dest": dest_iata,
                    "charge_acquisition": False,
                }
            )

    return {
        "mode": mode,
        "hub_involved": hub_involved,
        "total_new_cost": total_new,
        "opens": opens,
        "forward": {
            "route_id": fwd_id,
            "player_has": player_fwd,
            "catalog_has": cat_fwd,
        },
        "reverse": {
            "route_id": rev_id,
            "player_has": player_rev,
            "catalog_has": cat_rev,
        },
        "distance_nm": distance_nm,
        "cost_reference": cost_reference,
    }


def execute_route_opens(opens, price_business=None, price_leisure=None):
    """
    opens: list of dicts with keys origin, dest, charge_acquisition (bool).
    """
    opened = []
    skipped = []
    total_paid = 0.0

    for spec in opens:
        o = spec["origin"].upper()
        d = spec["dest"].upper()
        charge = bool(spec["charge_acquisition"])
        if player_route_exists(o, d):
            skipped.append(f"{o}-{d}")
            continue
        r = open_route(o, d, price_business, price_leisure, silent=True, charge_acquisition=charge)
        opened.append(r)
        total_paid += float(r.get("acquisition_cost") or 0.0)

    print("\n" + "=" * 60)
    print("ROUTE OPENING SUMMARY")
    print("=" * 60)
    for r in opened:
        if r.get("auction_message"):
            print(f"  ✓ {r['route_id']}: {r['auction_message']}")
            continue
        tag = "" if r.get("acquisition_cost") else " (no acquisition charge)"
        print(f"  ✓ Opened {r['route_id']}{tag}")
        if r.get("acquisition_cost"):
            print(f"      Paid ${r['acquisition_cost']:,.2f}")
    for sid in skipped:
        print(f"  · Already active: {sid}")
    if opened:
        last = opened[-1]
        print(f"\n  Total acquisition paid: ${total_paid:,.2f}")
        print(f"  Cash balance: ${last['new_cash_balance']:,.2f}\n")
    else:
        print("\n  No new routes added.\n")
    return {"opened": opened, "skipped": skipped, "total_paid": total_paid}


def open_route(origin_iata, dest_iata, price_business=None, price_leisure=None, silent=False, charge_acquisition=True):
    """
    Open a new route between two airports.
    
    Requires payment of route acquisition cost based on airport scores,
    categories, and distance.
    
    Args:
        origin_iata: Origin airport IATA code
        dest_iata: Destination airport IATA code
        price_business: Business class fare (optional, will use default)
        price_leisure: Leisure class fare (optional, will use default)
        charge_acquisition: If False, insert the route without deducting acquisition cash
            (hub-pair companion leg).
    
    Returns:
        dict: Route data including acquisition_cost
    
    Raises:
        ValueError: If airports don't exist, route already exists, insufficient funds, or origin == destination
    """
    origin_iata = origin_iata.upper()
    dest_iata = dest_iata.upper()
    
    # Validate airports are different
    if origin_iata == dest_iata:
        raise ValueError("Origin and destination must be different airports.")
    
    # Validate both airports exist
    origin = get_airport(origin_iata)
    if not origin:
        raise ValueError(f"Origin airport '{origin_iata}' not found.")
    
    dest = get_airport(dest_iata)
    if not dest:
        raise ValueError(f"Destination airport '{dest_iata}' not found.")
    
    route_id = f"{origin_iata}-{dest_iata}"

    if player_route_exists(origin_iata, dest_iata):
        raise ValueError(f"You already operate route '{route_id}'.")

    existing_row = get_route(route_id)
    if existing_row:
        distance_nm = float(existing_row["distance_nm"])
        acquisition_cost = calculate_route_acquisition_cost(origin, dest, distance_nm)
        large_gate = False

        from engine.setup import get_airline, update_cash

        airline = get_airline()
        if not airline:
            raise ValueError("No airline found. Create an airline first.")

        if large_gate:
            charge_acquisition = False
            acquisition_paid = 0.0
        elif charge_acquisition:
            if airline["cash"] < acquisition_cost:
                raise ValueError(
                    f"Insufficient funds to add route to your network. "
                    f"Cost: ${acquisition_cost:,.2f}, Available: ${airline['cash']:,.2f}"
                )
            acquisition_paid = acquisition_cost
        else:
            acquisition_paid = 0.0

        try:
            gw = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
            opened_week = int(gw["game_week"] or 1) if gw else 1
            db.execute(
                "INSERT INTO player_routes (route_id, opened_week) VALUES (?, ?)",
                (route_id, opened_week),
            )
        except Exception:
            pass

        if charge_acquisition:
            new_cash = update_cash(-acquisition_cost)
            paid = acquisition_cost
        else:
            new_cash = airline["cash"]
            paid = 0.0

        er = dict(existing_row)
        return {
            "route_id": route_id,
            "origin_iata": origin_iata,
            "dest_iata": dest_iata,
            "distance_nm": distance_nm,
            "base_demand_business": er.get("base_demand_business"),
            "base_demand_leisure": er.get("base_demand_leisure"),
            "demand_source": er.get("demand_source"),
            "price_business": er.get("price_business"),
            "price_leisure": er.get("price_leisure"),
            "price_premium_economy": er.get("price_premium_economy"),
            "price_first": er.get("price_first"),
            "origin_name": origin["name"],
            "dest_name": dest["name"],
            "acquisition_cost": paid,
            "new_cash_balance": new_cash,
            "auction_opened": False,
            "auction_message": None,
            "claimed_existing_catalog": True,
        }

    # New catalog row: calculate distance using Haversine formula
    distance_nm = haversine_distance(
        origin['lat'], origin['lon'],
        dest['lat'], dest['lon']
    )

    # Calculate route acquisition cost (waived for hub-pair companion legs)
    acquisition_cost = calculate_route_acquisition_cost(origin, dest, distance_nm)
    # Phase 11 revised: routes are never licence-gated; gates are auctioned at airports instead.
    large_gate = False

    from engine.setup import get_airline, update_cash
    airline = get_airline()
    if not airline:
        raise ValueError("No airline found. Create an airline first.")

    if large_gate:
        charge_acquisition = False
        acquisition_paid = 0.0
    elif charge_acquisition:
        if airline['cash'] < acquisition_cost:
            raise ValueError(
                f"Insufficient funds to open route. "
                f"Cost: ${acquisition_cost:,.2f}, Available: ${airline['cash']:,.2f}"
            )
        acquisition_paid = acquisition_cost
    else:
        acquisition_paid = 0.0
    
    # Estimate base demand (BTS / gravity / legacy)
    from engine.route_demand import compute_base_demand

    demand_info = compute_base_demand(distance_nm, origin, dest)
    base_demand_business = int(demand_info["base_demand_business"])
    base_demand_leisure = int(demand_info["base_demand_leisure"])
    demand_source = str(demand_info.get("demand_source") or "LEGACY")
    
    # Set default prices if not provided
    # Simple pricing: $0.15-0.25 per mile for business, $0.08-0.12 for leisure
    if price_business is None:
        price_business = round(distance_nm * 0.20, 2)
    
    if price_leisure is None:
        price_leisure = round(distance_nm * 0.10, 2)

    price_premium_economy, price_first = default_premium_first_ticket_fares(price_leisure, price_business)
    
    is_active = 1
    # Create route in database
    db.execute("""
        INSERT INTO routes (
            route_id, origin_iata, dest_iata, distance_nm,
            base_demand_business, base_demand_leisure,
            price_business, price_leisure, price_premium_economy, price_first, is_active,
            demand_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        route_id, origin_iata, dest_iata, distance_nm,
        base_demand_business, base_demand_leisure,
        price_business, price_leisure, price_premium_economy, price_first, is_active,
        demand_source,
    ))

    # Mark as a PLAYER-opened route for UI / ownership filtering.
    try:
        gw = db.fetch_one("SELECT game_week FROM game_state WHERE id = 1")
        opened_week = int(gw["game_week"] or 1) if gw else 1
        db.execute(
            "INSERT OR IGNORE INTO player_routes (route_id, opened_week) VALUES (?, ?)",
            (route_id, opened_week),
        )
    except Exception:
        pass

    auction_msg = None
    if charge_acquisition:
        new_cash = update_cash(-acquisition_cost)
        paid = acquisition_cost
    else:
        new_cash = airline['cash']
        paid = 0.0

    route = {
        'route_id': route_id,
        'origin_iata': origin_iata,
        'dest_iata': dest_iata,
        'distance_nm': distance_nm,
        'base_demand_business': base_demand_business,
        'base_demand_leisure': base_demand_leisure,
        'demand_source': demand_source,
        'price_business': price_business,
        'price_leisure': price_leisure,
        'price_premium_economy': price_premium_economy,
        'price_first': price_first,
        'origin_name': origin['name'],
        'dest_name': dest['name'],
        'acquisition_cost': paid,
        'new_cash_balance': new_cash,
        'auction_opened': False,
        'auction_message': auction_msg,
    }

    if not silent:
        print(f"\n✓ Route Opened: {route_id}")
        print(f"  {origin['city']} ({origin_iata}) → {dest['city']} ({dest_iata})")
        print(f"  Distance: {distance_nm:,.0f} nm")
        print(f"  Base Demand: {base_demand_business} business, {base_demand_leisure} leisure "
              f"(template · {demand_source}"
              f"{' · min market' if demand_info.get('market_floor_applied') else ''})")
        print(
            f"  Default Fares: ${price_leisure:.2f} leisure (Y base), ${price_premium_economy:.2f} premium (W), "
            f"${price_business:.2f} business (J base), ${price_first:.2f} first (F)\n"
        )

    return route


def update_route_prices(
    route_id,
    price_business,
    price_leisure,
    price_premium_economy,
    price_first,
):
    """
    Update fares for an existing route.
    
    Args:
        route_id: Route identifier (e.g., 'TPA-JFK')
        price_business: Business (J) base — multiplied by eec_yield_business for the J ticket
        price_leisure: Leisure / economy (Y) base — multiplied by eec_yield_economy for the Y ticket
        price_premium_economy: One-way W ticket (premium economy) in dollars
        price_first: One-way F ticket (first) in dollars
    
    Raises:
        ValueError: If route doesn't exist or prices are invalid
    """
    if min(price_business, price_leisure, price_premium_economy, price_first) <= 0:
        raise ValueError("Prices must be positive.")
    
    route = get_route(route_id)
    if not route:
        raise ValueError(f"Route '{route_id}' not found.")
    
    db.execute("""
        UPDATE routes 
        SET price_business = ?, price_leisure = ?,
            price_premium_economy = ?, price_first = ?
        WHERE route_id = ?
    """, (price_business, price_leisure, price_premium_economy, price_first, route_id))
    
    print(f"\n✓ Updated prices for {route_id}")
    print(f"  Leisure (Y base): ${price_leisure:.2f}")
    print(f"  Premium economy (W): ${price_premium_economy:.2f}")
    print(f"  Business (J base): ${price_business:.2f}")
    print(f"  First (F): ${price_first:.2f}\n")


def display_routes():
    """Display all active routes."""
    routes = get_all_routes()
    
    if not routes:
        print("\n⚠ No routes opened yet.\n")
        return
    
    print("\n" + "=" * 110)
    print("ACTIVE ROUTES")
    print("=" * 110)
    print(f"{'Route ID':<12} {'Origin':<6} {'Dest':<6} {'Distance':>10} {'Bus Fare':>10} {'Lei Fare':>10} {'Base Bus':>8} {'Base Lei':>8}")
    print("-" * 110)
    
    for route in routes:
        print(
            f"{route['route_id']:<12} "
            f"{route['origin_iata']:<6} "
            f"{route['dest_iata']:<6} "
            f"{route['distance_nm']:>9,.0f}nm "
            f"${route['price_business']:>9,.2f} "
            f"${route['price_leisure']:>9,.2f} "
            f"{route['base_demand_business']:>8} "
            f"{route['base_demand_leisure']:>8}"
        )
    
    print("=" * 110)
    print(f"Total Routes: {len(routes)}")
    print("=" * 110 + "\n")


if __name__ == "__main__":
    # Test the module
    print("Testing routes module...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        print("\nCurrent routes:")
        display_routes()
