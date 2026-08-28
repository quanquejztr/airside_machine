import csv
import glob
import os
from collections import defaultdict

OUTPUT_FILE = "US_CARRIER_MARKET.csv"

# (YEAR, ORIGIN, DEST) -> total passengers
market_data = defaultdict(float)

input_files = sorted(glob.glob("*.csv"))

print("=" * 70)
print("Starting demand merger")
print("=" * 70)

total_files = 0
total_rows = 0
total_nonzero = 0

for file in input_files:
    filename = os.path.basename(file)

    # Don't process the output file
    if filename == OUTPUT_FILE:
        continue

    # Get year from filename
    year = os.path.splitext(filename)[0]

    # Only process files named as years
    if not year.isdigit():
        continue

    total_files += 1

    print(f"\nProcessing: {filename}")
    print("-" * 70)

    file_rows = 0
    file_nonzero = 0
    file_markets = set()

    with open(file, "r", newline="", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)

        for row in reader:
            file_rows += 1
            total_rows += 1

            passengers = float(row["PASSENGERS"])

            # Ignore 0 passengers
            if passengers < 1:
                continue

            file_nonzero += 1
            total_nonzero += 1

            origin = row["ORIGIN"]
            dest = row["DEST"]

            # Track unique routes within this year
            file_markets.add((origin, dest))

            # Merge only same YEAR + ORIGIN + DEST
            market_data[(year, origin, dest)] += passengers

    print(f"  Rows read:              {file_rows:,}")
    print(f"  Non-zero passengers:    {file_nonzero:,}")
    print(f"  Unique routes:          {len(file_markets):,}")

print("\n" + "=" * 70)
print("Writing output")
print("=" * 70)

print(f"Output file: {OUTPUT_FILE}")
print(f"Total input files: {total_files}")
print(f"Total rows read: {total_rows:,}")
print(f"Total non-zero rows: {total_nonzero:,}")
print(f"Final unique year/routes: {len(market_data):,}")

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as outfile:
    writer = csv.writer(outfile)

    writer.writerow([
        "PASSENGERS",
        "ORIGIN",
        "DEST",
        "YEAR"
    ])

    for i, ((year, origin, dest), passengers) in enumerate(market_data.items(), 1):
        writer.writerow([
            passengers,
            origin,
            dest,
            year
        ])

        # Show progress every 50,000 rows
        if i % 50_000 == 0:
            print(f"  Written: {i:,} rows")

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
print(f"Files processed:        {total_files}")
print(f"Input rows:             {total_rows:,}")
print(f"Non-zero input rows:    {total_nonzero:,}")
print(f"Output rows:            {len(market_data):,}")
print("=" * 70)

print("\n" + "=" * 70)
print("Writing output")
print("=" * 70)

print(f"Output file: {OUTPUT_FILE}")
print(f"Total input files: {total_files}")
print(f"Total rows read: {total_rows:,}")
print(f"Total non-zero rows: {total_nonzero:,}")
print(f"Final unique year/routes: {len(market_data):,}")

# Sort by passengers, largest first
sorted_data = sorted(
    market_data.items(),
    key=lambda x: x[1],
    reverse=True
)

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as outfile:
    writer = csv.writer(outfile)

    writer.writerow([
        "PASSENGERS",
        "ORIGIN",
        "DEST",
        "YEAR"
    ])

    for i, ((year, origin, dest), passengers) in enumerate(sorted_data, 1):
        writer.writerow([
            passengers,
            origin,
            dest,
            year
        ])

        if i % 50_000 == 0:
            print(f"  Written: {i:,} rows")

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
print(f"Files processed:        {total_files}")
print(f"Input rows:             {total_rows:,}")
print(f"Non-zero input rows:    {total_nonzero:,}")
print(f"Output rows:            {len(sorted_data):,}")
print("=" * 70)