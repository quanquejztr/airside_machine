"""
Seed script for loading static reference data into the database.
Loads 5 static reference tables from CSV files:
- airport_categories
- airports
- aircraft_types
- seasonality
- financial_constants
"""

import csv
import sqlite3
from pathlib import Path


def get_db_path():
    """Get the path to the database file."""
    return Path(__file__).parent / "airline_sim.db"


def get_data_path(filename):
    """Get the path to a data file."""
    return Path(__file__).parent.parent / "data" / filename


def load_csv(filename):
    """Load CSV data from the data directory. Rejects rows whose field count != header."""
    filepath = get_data_path(filename)
    rows = []
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        n = len(header)
        for lineno, fields in enumerate(reader, start=2):
            if not fields or all(not str(c).strip() for c in fields):
                continue
            first = str(fields[0]).strip()
            if first.startswith("#"):
                continue
            if len(fields) != n:
                raise ValueError(
                    f"{filename} line {lineno}: expected {n} fields, got {len(fields)}: {fields}"
                )
            rows.append(dict(zip(header, fields)))
    return rows


def seed_airport_categories(conn):
    """Seed airport_categories table."""
    print("Seeding airport_categories...")
    data = load_csv("airport_categories.csv")
    cursor = conn.cursor()
    
    for row in data:
        cursor.execute("""
            INSERT OR REPLACE INTO airport_categories (
                category, slot_tier, landing_fee_per_1000, gate_fee,
                runway_min_ft, curfew_active, curfew_start, curfew_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row['category'],
            int(row['slot_tier']),
            float(row['landing_fee_per_1000']),
            float(row['gate_fee']),
            int(row['runway_min_ft']),
            int(row['curfew_active']),
            row.get('curfew_start') if row.get('curfew_start') != 'NULL' else None,
            row.get('curfew_end') if row.get('curfew_end') != 'NULL' else None
        ))
    
    conn.commit()
    print(f"✓ Seeded {len(data)} airport categories")


def determine_category(gate_count, runway_length):
    """Determine airport category based on gates and runway."""
    if gate_count >= 40 or (runway_length and runway_length >= 10000):
        return 'INTERNATIONAL'
    elif gate_count >= 10 or (runway_length and runway_length >= 6000):
        return 'NATIONAL'
    else:
        return 'REGIONAL'


def seed_airports(conn):
    """Seed airports table."""
    print("Seeding airports...")
    data = load_csv("airports.csv")
    cursor = conn.cursor()
    
    for row in data:
        # Use category from CSV directly
        category = row['category'] if row.get('category') else 'medium_airport'
        
        # Get score from CSV
        score = int(row['score']) if row.get('score') else 0
        
        cursor.execute("""
            INSERT OR REPLACE INTO airports (
                iata, icao, name, city, country, lat, lon, 
                runway_length_ft, gate_count, timezone, score, category,
                slot_level, runway_count,
                landing_fee_override, gate_fee_override,
                has_curfew, curfew_start, curfew_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row['iata'],
            row['icao'],
            row['name'],
            row['city'],
            row['country'],
            float(row['lat']),
            float(row['lon']),
            int(row['runway_length_ft']) if row.get('runway_length_ft') else None,
            int(row['gate_count']),
            row['timezone'],
            score,
            category,
            int(row.get('slot_level') or 1),
            int(row.get('runway_count') or 0),
            None,  # landing_fee_override (not in CSV, use category default)
            None,  # gate_fee_override (not in CSV, use category default)
            None,  # has_curfew (use category default)
            None,  # curfew_start (use category default)
            None   # curfew_end (use category default)
        ))
    
    conn.commit()
    print(f"✓ Seeded {len(data)} airports")


def seed_aircraft_types(conn):
    """Seed aircraft_types table."""
    print("Seeding aircraft_types...")
    data = load_csv("aircraft_types.csv")
    cursor = conn.cursor()
    
    for row in data:
        # Handle null values
        eec = int(row['eec']) if row.get('eec') and row['eec'] not in ('null', 'NULL', '') else None
        lease_cost = int(row['weekly_lease_cost']) if row.get('weekly_lease_cost') and row['weekly_lease_cost'] not in ('null', 'NULL', '') else 0
        cat = row["category"]
        mi = None
        raw_mi = row.get("maintenance_interval_weeks")
        if raw_mi and str(raw_mi).strip().lower() not in ("", "null", "none"):
            try:
                mi = int(float(raw_mi))
            except (TypeError, ValueError):
                mi = None
        if mi is None or mi <= 0:
            mi = {"TURBOPROP": 8, "REGIONAL_JET": 10, "NARROW": 12, "WIDE": 16}.get(cat, 12)

        cursor.execute("""
            INSERT OR REPLACE INTO aircraft_types (
                type_id, display_name, category, range_nm, cruise_speed_kts,
                fuel_burn_gph, mtow_lbs, runway_req_ft, purchase_price,
                weekly_lease_cost, eec, maintenance_interval_weeks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row['type_id'],
            row['display_name'],
            cat,
            int(row['range_nm']),
            int(row['cruise_speed_kts']),
            float(row['fuel_burn_gph']),
            int(row['mtow_lbs']),
            int(row['runway_req_ft']),
            int(row['purchase_price']),
            lease_cost,
            eec,
            mi,
        ))
    
    conn.commit()
    print(f"✓ Seeded {len(data)} aircraft types")


def seed_aircraft_default_config(conn):
    """Seed aircraft_default_config table."""
    print("Seeding aircraft_default_config...")
    data = load_csv("aircraft_default_config.csv")
    cursor = conn.cursor()
    
    for row in data:
        # Skip rows without proper type_id (comments, empty lines)
        if not row.get('type_id') or row['type_id'].startswith('#'):
            continue
            
        cursor.execute("""
            INSERT OR REPLACE INTO aircraft_default_config (
                type_id, seats_economy, seats_premium_economy,
                seats_business, seats_first, eec_used
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            row['type_id'],
            int(row['seats_economy']),
            int(row['seats_premium_economy']),
            int(row['seats_business']),
            int(row['seats_first']),
            int(row['eec_used'])
        ))
    
    conn.commit()
    print(f"✓ Seeded {cursor.rowcount} aircraft default configs")


def seed_seasonality(conn):
    """Seed seasonality table."""
    print("Seeding seasonality...")
    data = load_csv("seasonality.csv")
    cursor = conn.cursor()
    
    for row in data:
        cursor.execute("""
            INSERT OR REPLACE INTO seasonality (
                month, business_multiplier, leisure_multiplier
            ) VALUES (?, ?, ?)
        """, (
            int(row['month']),
            float(row['business_multiplier']),
            float(row['leisure_multiplier'])
        ))
    
    conn.commit()
    print(f"✓ Seeded {len(data)} seasonality months")


def seed_financial_constants(conn):
    """Seed financial_constants table."""
    print("Seeding financial_constants...")
    data = load_csv("financial_constants.csv")
    cursor = conn.cursor()
    
    for row in data:
        cursor.execute("""
            INSERT OR REPLACE INTO financial_constants (key, value)
            VALUES (?, ?)
        """, (row['key'], float(row['value'])))
    
    conn.commit()
    print(f"✓ Seeded {len(data)} financial constants")


def run_seed(db_path=None):
    """
    Run all seed functions.
    
    Args:
        db_path: Path to the SQLite database file (optional)
    """
    if db_path is None:
        db_path = get_db_path()
    
    print("=" * 60)
    print("SEEDING DATABASE")
    print("=" * 60)
    print(f"Database: {db_path}\n")
    
    conn = sqlite3.connect(db_path)
    
    try:
        # Seed all static reference tables
        seed_airport_categories(conn)
        seed_airports(conn)
        seed_aircraft_types(conn)
        seed_seasonality(conn)
        seed_financial_constants(conn)
        seed_aircraft_default_config(conn)
        
        print("\n" + "=" * 60)
        print("✓ All static reference data seeded successfully")
        print("=" * 60)
        
        # Verify data was loaded
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM airports")
        airport_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM aircraft_types")
        aircraft_count = cursor.fetchone()[0]
        
        print(f"\nData verification:")
        print(f"  - Airports: {airport_count:,}")
        print(f"  - Aircraft types: {aircraft_count:,}")
        print(f"  - Database ready for gameplay!\n")
        
    except Exception as e:
        conn.rollback()
        print(f"\n✗ Seed failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    # Run seed with default database path
    run_seed()
