"""
Airport management module for Phase 1.
Handles airport queries with resolved fee rates.
"""

import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import db


def get_airport(iata):
    """
    Get airport by IATA code with resolved fee rates.
    
    Resolves fees using the two-layer system:
    1. Check airport's override fields first
    2. Fall back to category defaults if no override
    
    Args:
        iata: Airport IATA code (e.g., 'TPA', 'JFK')
    
    Returns:
        dict: Airport data with resolved fees, or None if not found
    """
    airport = db.get_airport(iata.upper())
    if not airport:
        return None
    
    airport = dict(airport)
    
    # Get category defaults
    category = db.fetch_one(
        "SELECT * FROM airport_categories WHERE category = ?",
        (airport['category'],)
    )
    
    if category:
        category = dict(category)
        
        # Resolve landing fee (airport override or category default)
        if airport['landing_fee_override'] is not None:
            airport['landing_fee_per_1000'] = airport['landing_fee_override']
        else:
            airport['landing_fee_per_1000'] = category['landing_fee_per_1000']
        
        # Resolve gate fee (airport override or category default)
        if airport['gate_fee_override'] is not None:
            airport['gate_fee'] = airport['gate_fee_override']
        else:
            airport['gate_fee'] = category['gate_fee']
        
        # Resolve curfew settings
        if airport['has_curfew'] is not None:
            airport['has_curfew_resolved'] = bool(airport['has_curfew'])
            airport['curfew_start_resolved'] = airport['curfew_start']
            airport['curfew_end_resolved'] = airport['curfew_end']
        else:
            airport['has_curfew_resolved'] = bool(category['curfew_active'])
            airport['curfew_start_resolved'] = category['curfew_start']
            airport['curfew_end_resolved'] = category['curfew_end']
        
        # Add category info
        airport['slot_tier'] = category['slot_tier']
    
    return airport


def search_airports(query):
    """
    Search airports by IATA code, name, or city.
    
    Args:
        query: Search term (matches IATA, name, or city)
    
    Returns:
        list: Matching airports with resolved fees
    """
    query_upper = query.upper().strip()
    query_pattern = f"%{query_upper}%"
    prefix = f"{query_upper}%"

    # Partial IATA as well as name/city, and rank by relevance then airport size so a
    # typeahead surfaces LAX before a similarly named airfield.
    airports = db.fetch_all("""
        SELECT * FROM airports
        WHERE iata LIKE ?
           OR UPPER(name) LIKE ?
           OR UPPER(city) LIKE ?
        ORDER BY
            CASE
                WHEN iata = ?             THEN 0
                WHEN iata LIKE ?          THEN 1
                WHEN UPPER(city) LIKE ?   THEN 2
                WHEN UPPER(name) LIKE ?   THEN 3
                ELSE 4
            END,
            score DESC,
            iata
        LIMIT 50
    """, (prefix, query_pattern, query_pattern,
          query_upper, prefix, prefix, prefix))
    
    # Resolve fees for each airport
    return [get_airport(airport['iata']) for airport in airports]


def list_airports_by_category(category):
    """
    List all airports in a category.
    
    Args:
        category: Airport category (small_airport, medium_airport, large_airport)
    
    Returns:
        list: Airports with resolved fees
    """
    airports = db.fetch_all(
        "SELECT * FROM airports WHERE category = ? ORDER BY iata",
        (category.upper(),)
    )
    
    return [get_airport(airport['iata']) for airport in airports]


_map_catalog_cache = None


def list_airports_for_map(force_refresh: bool = False):
    """
    Full airport catalog for the live map (lat/lon + size fields).

    Cached in-process — airport seed data does not change during a save.
    """
    global _map_catalog_cache
    if _map_catalog_cache is not None and not force_refresh:
        return _map_catalog_cache
    rows = db.fetch_all(
        """
        SELECT iata, icao, name, city, country, lat, lon,
               runway_length_ft, gate_count, timezone, score, category
        FROM airports
        ORDER BY score DESC, iata
        """
    ) or []
    out = []
    for row in rows:
        out.append(
            {
                "iata": row["iata"],
                "icao": row["icao"],
                "name": row["name"],
                "city": row["city"],
                "country": row["country"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "runway_length_ft": int(row["runway_length_ft"])
                if row["runway_length_ft"] is not None
                else None,
                "gate_count": int(row["gate_count"]) if row["gate_count"] is not None else None,
                "timezone": row["timezone"],
                "score": int(row["score"] or 0),
                "category": row["category"],
            }
        )
    _map_catalog_cache = out
    return out


def display_airport_info(iata):
    """Display detailed airport information."""
    airport = get_airport(iata.upper())
    
    if not airport:
        print(f"\n✗ Airport '{iata}' not found.\n")
        return
    
    print("\n" + "=" * 80)
    print(f"{airport['name']}")
    print("=" * 80)
    
    print(f"\nIdentifiers:")
    print(f"  IATA: {airport['iata']}")
    print(f"  ICAO: {airport['icao']}")
    
    print(f"\nLocation:")
    print(f"  City: {airport['city']}, {airport['country']}")
    print(f"  Coordinates: {airport['lat']:.4f}, {airport['lon']:.4f}")
    print(f"  Timezone: {airport['timezone']}")
    
    print(f"\nCategory & Facilities:")
    print(f"  Category: {airport['category']}")
    print(f"  Slot Tier: Level {airport['slot_tier']}")
    print(f"  Gates: {airport['gate_count']}")
    if airport['runway_length_ft']:
        print(f"  Runway: {airport['runway_length_ft']:,} ft")
    
    print(f"\nFees (Resolved):")
    print(f"  Landing Fee: ${airport['landing_fee_per_1000']:.2f} per 1,000 lbs MTOW")
    print(f"  Gate Fee: ${airport['gate_fee']:.2f} per use")
    
    if airport['has_curfew_resolved']:
        print(f"\nCurfew:")
        print(f"  Active: YES")
        print(f"  Hours: {airport['curfew_start_resolved']} - {airport['curfew_end_resolved']}")
    else:
        print(f"\nCurfew: NO")
    
    print("=" * 80 + "\n")


def display_airport_search_results(query):
    """Display search results for airports."""
    airports = search_airports(query)
    
    if not airports:
        print(f"\n⚠ No airports found matching '{query}'.\n")
        return
    
    print("\n" + "=" * 120)
    print(f"AIRPORT SEARCH RESULTS: '{query}'")
    print("=" * 120)
    print(f"{'IATA':<6} {'Name':<40} {'City':<20} {'Cat':<15} {'Gates':>6} {'Runway':>8}")
    print("-" * 120)
    
    for airport in airports:
        runway_str = f"{airport['runway_length_ft']:,}" if airport['runway_length_ft'] else "N/A"
        print(
            f"{airport['iata']:<6} "
            f"{airport['name'][:38]:<40} "
            f"{airport['city'][:18]:<20} "
            f"{airport['category']:<15} "
            f"{airport['gate_count']:>6} "
            f"{runway_str:>8}"
        )
    
    print("=" * 120)
    print(f"Found {len(airports)} airport(s)")
    print("=" * 120 + "\n")


if __name__ == "__main__":
    # Test the module
    print("Testing airports module...")
    
    if not db.db_exists():
        print("⚠ Database not found. Please run main.py first to initialize.")
    else:
        # Test getting a specific airport
        print("\nTesting get_airport('TPA'):")
        display_airport_info('TPA')
        
        # Test search
        print("\nTesting search_airports('Tampa'):")
        display_airport_search_results('Tampa')
