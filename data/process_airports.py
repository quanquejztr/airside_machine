"""
Airport Data Processing Pipeline
=================================

This script combines all airport data processing steps into a single pipeline.
Run each function in order to process raw airport data into the final schema.

Pipeline Steps:
1. clean_and_filter_airports() - Filter top 500 airports and categorize them
2. add_runway_lengths() - Add maximum runway length from runways data
3. add_gate_and_timezone() - Add gate counts and timezone information
4. restructure_to_final_schema() - Convert to final database schema

Input Files:
- us-airports.csv (raw airport data - 32k+ airports)
- runways_length.csv (runway lengths for each airport)

Output Files:
- top-500-airports.csv (after step 1)
- us-airports-with-runways.csv (after step 2)
- us-airports-with-gates.csv (after step 3)
- airports.csv (final output - ready for database)

Final Schema:
- iata (TEXT PK): 3-letter IATA code
- icao (TEXT): 4-letter ICAO code  
- name (TEXT): Display name
- city (TEXT): City name
- country (TEXT): ISO 2-letter country code
- lat (REAL): Latitude
- lon (REAL): Longitude
- category (TEXT FK): REGIONAL/NATIONAL/INTERNATIONAL
- runway_length_ft (INTEGER): Longest runway
- gate_count (INTEGER): Number of gates
- timezone (TEXT): IANA timezone string
"""

import csv
import random
from collections import defaultdict


# ============================================================================
# STEP 1: CLEAN AND FILTER AIRPORTS
# ============================================================================

def get_score(row):
    """Extract score from row (second to last column)."""
    try:
        score_str = row[-2]
        return int(score_str)
    except (ValueError, IndexError):
        return None


def categorize_airport(row):
    """
    Categorize airport based on type, name, and scheduled service.
    
    Rules:
    - INTERNATIONAL: large_airport with "International" in name
    - NATIONAL: large_airport without "International" OR medium_airport with scheduled service
    - REGIONAL: small_airport OR medium_airport without scheduled service
    """
    airport_type = row[2]  # type column
    name = row[3]  # name column
    scheduled_service = row[14]  # scheduled_service column (1 or 0)
    
    if airport_type == 'large_airport':
        if 'International' in name or 'Intl' in name:
            return 'INTERNATIONAL'
        else:
            return 'NATIONAL'
    elif airport_type == 'medium_airport':
        if scheduled_service == '1':
            return 'NATIONAL'
        else:
            return 'REGIONAL'
    else:
        return 'REGIONAL'


def clean_and_filter_airports(input_file='us-airports-raw.csv', output_file='top-500-airports.csv', top_n=500):
    """
    Step 1: Clean and filter to top 500 busiest airports.
    
    - Filters to only small/medium/large airports
    - Sorts by traffic score
    - Keeps top 500 busiest
    - Reclassifies to REGIONAL/NATIONAL/INTERNATIONAL
    """
    valid_types = {'small_airport', 'medium_airport', 'large_airport'}
    valid_airports = []
    
    print("=" * 60)
    print("STEP 1: Cleaning and Filtering Airports")
    print("=" * 60)
    
    with open(input_file, 'r', encoding='utf-8') as infile:
        reader = csv.reader(infile)
        header = next(reader)
        
        for row in reader:
            airport_type = row[2]
            
            if airport_type not in valid_types:
                continue
            
            score = get_score(row)
            if score is None:
                continue
            
            valid_airports.append((score, row))
    
    # Sort by score and take top N
    valid_airports.sort(key=lambda x: x[0], reverse=True)
    top_airports = valid_airports[:top_n]
    
    stats = {
        'total_valid': len(valid_airports),
        'kept': len(top_airports),
        'INTERNATIONAL': 0,
        'NATIONAL': 0,
        'REGIONAL': 0
    }
    
    with open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        writer = csv.writer(outfile)
        writer.writerow(header)
        
        for score, row in top_airports:
            new_category = categorize_airport(row)
            row[2] = new_category
            writer.writerow(row)
            stats[new_category] += 1
    
    print(f"✓ Filtered {stats['total_valid']:,} valid airports to top {top_n}")
    print(f"  - INTERNATIONAL: {stats['INTERNATIONAL']:,}")
    print(f"  - NATIONAL: {stats['NATIONAL']:,}")
    print(f"  - REGIONAL: {stats['REGIONAL']:,}")
    print(f"✓ Output: {output_file}\n")
    
    return output_file


# ============================================================================
# STEP 2: ADD RUNWAY LENGTHS
# ============================================================================

def get_max_runway_lengths(runways_file='runways_length.csv'):
    """Read runways file and get maximum runway length for each airport."""
    max_runways = defaultdict(int)
    
    with open(runways_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        
        for row in reader:
            try:
                airport_id = row[2].strip('"')
                runway_length = int(row[3])
                
                if runway_length > max_runways[airport_id]:
                    max_runways[airport_id] = runway_length
                    
            except (ValueError, IndexError):
                continue
    
    return dict(max_runways)


def add_runway_lengths(input_file='top-500-airports.csv', output_file='us-airports-with-runways.csv'):
    """
    Step 2: Add maximum runway length for each airport.
    
    Reads runways_length.csv and finds the longest runway at each airport.
    """
    print("=" * 60)
    print("STEP 2: Adding Runway Lengths")
    print("=" * 60)
    
    max_runways = get_max_runway_lengths()
    print(f"✓ Loaded runway data for {len(max_runways):,} airports")
    
    stats = {'total': 0, 'with_runway': 0, 'without_runway': 0}
    
    with open(input_file, 'r', encoding='utf-8') as infile, \
         open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        
        reader = csv.reader(infile)
        writer = csv.writer(outfile)
        
        header = next(reader)
        header.append('runway_length_ft')
        writer.writerow(header)
        
        for row in reader:
            stats['total'] += 1
            airport_ident = row[1]
            runway_length = max_runways.get(airport_ident, None)
            
            if runway_length is not None:
                row.append(runway_length)
                stats['with_runway'] += 1
            else:
                row.append('')
                stats['without_runway'] += 1
            
            writer.writerow(row)
    
    print(f"✓ Added runway lengths to {stats['with_runway']}/{stats['total']} airports")
    print(f"✓ Output: {output_file}\n")
    
    return output_file


# ============================================================================
# STEP 3: ADD GATE COUNTS AND TIMEZONE
# ============================================================================

US_TIMEZONES = [
    (-180, -130, -10, 'Pacific/Honolulu'),
    (-130, -117, -8, 'America/Anchorage'),
    (-125, -114, -8, 'America/Los_Angeles'),
    (-114, -104, -7, 'America/Denver'),
    (-104, -90, -6, 'America/Chicago'),
    (-90, -67, -5, 'America/New_York'),
]


def get_timezone(longitude):
    """Determine timezone based on longitude."""
    for west, east, offset, tz_name in US_TIMEZONES:
        if west <= longitude <= east:
            return offset, tz_name
    return -5, 'America/New_York'


def load_gate_counts_from_file(gates_file='runways_length.csv'):
    """Load gate counts from CSV file."""
    gates = {}
    
    with open(gates_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ident = row['ident']
            gate_count = int(row['gate_count'])
            gates[ident] = gate_count
    
    return gates


def add_gate_and_timezone(input_file='us-airports-with-runways.csv', 
                          output_file='us-airports-with-gates.csv',
                          gates_file='runways_length.csv'):
    """
    Step 3: Add gate counts and timezone information.
    
    - Reads gate counts from runways_length.csv
    - Calculates timezone based on longitude
    """
    print("=" * 60)
    print("STEP 3: Adding Gate Counts and Timezones")
    print("=" * 60)
    
    gates = load_gate_counts_from_file(gates_file)
    print(f"✓ Loaded gate counts for {len(gates):,} airports")
    
    stats = {'total': 0, 'with_gates': 0}
    
    with open(input_file, 'r', encoding='utf-8') as infile, \
         open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        
        reader = csv.reader(infile)
        writer = csv.writer(outfile)
        
        header = next(reader)
        header.extend(['gate_count', 'timezone_offset', 'timezone_name'])
        writer.writerow(header)
        
        for row in reader:
            stats['total'] += 1
            
            ident = row[1]
            longitude = float(row[5])
            
            # Get timezone
            tz_offset, tz_name = get_timezone(longitude)
            
            # Get gate count
            gate_count = gates.get(ident, '')
            if gate_count:
                stats['with_gates'] += 1
            
            row.extend([gate_count, tz_offset, tz_name])
            writer.writerow(row)
    
    print(f"✓ Added gate counts to {stats['with_gates']}/{stats['total']} airports")
    print(f"✓ Added timezones to all airports")
    print(f"✓ Output: {output_file}\n")
    
    return output_file


# ============================================================================
# STEP 4: RESTRUCTURE TO FINAL SCHEMA
# ============================================================================

def restructure_to_final_schema(input_file='us-airports-with-gates.csv', 
                                output_file='airports.csv'):
    """
    Step 4: Restructure to final database schema.
    
    Creates final airports.csv with only the columns needed for the game:
    iata, icao, name, city, country, lat, lon, category, 
    runway_length_ft, gate_count, timezone
    """
    print("=" * 60)
    print("STEP 4: Restructuring to Final Schema")
    print("=" * 60)
    
    stats = {'total': 0, 'skipped_no_iata': 0, 'success': 0}
    
    with open(input_file, 'r', encoding='utf-8') as infile, \
         open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        
        reader = csv.DictReader(infile)
        
        fieldnames = ['iata', 'icao', 'name', 'city', 'country', 'lat', 'lon', 
                      'category', 'runway_length_ft', 'gate_count', 'timezone']
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for row in reader:
            stats['total'] += 1
            
            iata = row['iata_code'].strip()
            
            if not iata:
                stats['skipped_no_iata'] += 1
                continue
            
            new_row = {
                'iata': iata,
                'icao': row['ident'],
                'name': row['name'],
                'city': row['municipality'],
                'country': row['iso_country'],
                'lat': row['latitude_deg'],
                'lon': row['longitude_deg'],
                'category': row['type'],
                'runway_length_ft': row.get('runway_length_ft', ''),
                'gate_count': row.get('gate_count', ''),
                'timezone': row.get('timezone_name', '')
            }
            
            writer.writerow(new_row)
            stats['success'] += 1
    
    print(f"✓ Restructured {stats['success']:,} airports to final schema")
    print(f"✓ Output: {output_file}\n")
    
    return output_file


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def run_full_pipeline():
    """
    Run the complete airport data processing pipeline.
    
    This will:
    1. Filter to top 500 busiest airports
    2. Add runway lengths
    3. Add gate counts and timezones
    4. Restructure to final database schema
    
    Final output: airports.csv (ready for database import)
    """
    print("\n" + "=" * 60)
    print("AIRPORT DATA PROCESSING PIPELINE")
    print("=" * 60 + "\n")
    
    # Step 1: Clean and filter
    file1 = clean_and_filter_airports(
        input_file='us-airports-raw.csv',
        output_file='top-500-airports.csv'
    )
    
    # Step 2: Add runways
    file2 = add_runway_lengths(
        input_file=file1,
        output_file='us-airports-with-runways.csv'
    )
    
    # Step 3: Add gates and timezone
    file3 = add_gate_and_timezone(
        input_file=file2,
        output_file='us-airports-with-gates.csv'
    )
    
    # Step 4: Final restructure
    final_file = restructure_to_final_schema(
        input_file=file3,
        output_file='airports.csv'
    )
    
    print("=" * 60)
    print("PIPELINE COMPLETE!")
    print("=" * 60)
    print(f"\n✓ Final output: {final_file}")
    print("✓ Ready for database import\n")


if __name__ == '__main__':
    # Run individual steps or the full pipeline
    
    # Option 1: Run full pipeline
    run_full_pipeline()
    
    # Option 2: Run individual steps (uncomment as needed)
    # clean_and_filter_airports()
    # add_runway_lengths()
    # add_gate_and_timezone()
    # restructure_to_final_schema()
