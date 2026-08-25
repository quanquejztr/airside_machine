# Airport Data Processing

This folder contains the complete pipeline for processing raw airport data into the final database schema.

## Quick Start

Run the entire pipeline:
```bash
python3 process_airports.py
```

## Pipeline Overview

The `process_airports.py` script combines all processing steps into a single pipeline:

### Step 1: Clean and Filter Airports
**Function:** `clean_and_filter_airports()`
- **Input:** `us-airports-raw.csv` (32,000+ airports)
- **Output:** `top-500-airports.csv` (500 busiest)
- **Process:**
  - Filters to only small/medium/large airports
  - Sorts by traffic score (busiest first)
  - Keeps top 500 airports
  - Reclassifies to REGIONAL/NATIONAL/INTERNATIONAL based on:
    - Large + "International" in name → INTERNATIONAL
    - Large without "International" → NATIONAL
    - Medium with scheduled service → NATIONAL
    - Small or medium without service → REGIONAL

### Step 2: Add Runway Lengths
**Function:** `add_runway_lengths()`
- **Input:** `top-500-airports.csv` + `runways_length.csv`
- **Output:** `us-airports-with-runways.csv`
- **Process:**
  - Reads runway data from `runways_length.csv`
  - Finds the maximum runway length for each airport
  - Adds `runway_length_ft` column

### Step 3: Add Gate Counts and Timezones
**Function:** `add_gate_and_timezone()`
- **Input:** `us-airports-with-runways.csv` + `runways_length.csv`
- **Output:** `us-airports-with-gates.csv`
- **Process:**
  - Reads gate counts from `runways_length.csv`
  - Calculates timezone based on longitude:
    - UTC-10: Hawaii
    - UTC-8: Pacific/Alaska
    - UTC-7: Mountain
    - UTC-6: Central
    - UTC-5: Eastern
  - Adds `gate_count`, `timezone_offset`, `timezone_name` columns

### Step 4: Restructure to Final Schema
**Function:** `restructure_to_final_schema()`
- **Input:** `us-airports-with-gates.csv`
- **Output:** `airports.csv` (FINAL - ready for database)
- **Process:**
  - Removes unnecessary columns
  - Renames columns to match database schema
  - Uses IATA code as primary key

## Final Schema

The output file `airports.csv` has these columns:

| Column | Type | Example | Description |
|--------|------|---------|-------------|
| iata | TEXT PK | ATL | 3-letter IATA code (Primary key) |
| icao | TEXT | KATL | 4-letter ICAO code |
| name | TEXT | Hartsfield Jackson Atlanta Intl | Display name |
| city | TEXT | Atlanta | City name |
| country | TEXT | US | ISO 2-letter country code |
| lat | REAL | 33.6367 | Latitude for distance calculations |
| lon | REAL | -84.428 | Longitude for distance calculations |
| category | TEXT FK | INTERNATIONAL | REGIONAL/NATIONAL/INTERNATIONAL |
| runway_length_ft | INTEGER | 11890 | Longest runway (for aircraft validation) |
| gate_count | INTEGER | 192 | Number of gates at airport |
| timezone | TEXT | America/New_York | IANA timezone (for curfew windows) |

## Input Files Required

1. **us-airports-raw.csv** - Raw airport data with all US airports
2. **runways_length.csv** - Runway and gate data with columns:
   - `ident` (ICAO code like KATL)
   - `iata_code` (3-letter code like ATL)
   - `gate_count` (number of gates)

## Running Individual Steps

You can also run individual steps by uncommenting them in `process_airports.py`:

```python
# Run only step 1
clean_and_filter_airports()

# Run only step 2
add_runway_lengths()

# Run only step 3
add_gate_and_timezone()

# Run only step 4
restructure_to_final_schema()
```

## Legacy Scripts

The following individual scripts are preserved for reference but are now combined in `process_airports.py`:

- `cleaning_data.py` - Original step 1 implementation
- `add_runways.py` - Original step 2 implementation
- `add_gate_timezone.py` - Original step 3 implementation (timezone only)
- `add_gates.py` - Original gate count addition
- `restructure_airports.py` - Original step 4 implementation

## Next Steps

After running this pipeline:

1. ✅ `airports.csv` is ready for database import
2. Create other seed data files:
   - `aircraft_types.csv`
   - `airport_categories.csv`
   - `seasonality.csv`
   - `financial_constants.csv`
