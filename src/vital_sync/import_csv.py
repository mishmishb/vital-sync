#!/usr/bin/env python3
"""
Import Renpho or MyNetDiary CSV exports into the health data cache.

Usage:
    python import_csv.py renpho /path/to/renpho_export.csv
    python import_csv.py mynetdiary /path/to/mynetdiary_export.csv
"""

import sys
from pathlib import Path

from vital_sync.analytics import (
    load_cache,
    merge_records,
    parse_mynetdiary_csv,
    parse_renpho_csv,
    save_cache,
)

CACHE_PATH = Path(__file__).parent.parent / "data" / "cache.json"


def main():
    if len(sys.argv) != 3:
        print("Usage: import_csv.py <renpho|mynetdiary> <path_to_csv>")
        sys.exit(1)

    source = sys.argv[1].lower()
    csv_path = Path(sys.argv[2])

    if not csv_path.exists():
        print(f"Error: File not found: {csv_path}")
        sys.exit(1)

    existing = load_cache(CACHE_PATH)

    if source == "renpho":
        new_records = parse_renpho_csv(csv_path)
    elif source == "mynetdiary":
        new_records = parse_mynetdiary_csv(csv_path)
    else:
        print(f"Error: Unknown source '{source}'. Use 'renpho' or 'mynetdiary'.")
        sys.exit(1)

    print(f"Parsed {len(new_records)} records from {source} CSV")

    merged = merge_records(existing, new_records)
    save_cache(merged, CACHE_PATH)

    print(f"Cache updated: {len(existing)} → {len(merged)} total records")

    # Show what was added
    existing_dates = {r.date for r in existing}
    added = [r for r in new_records if r.date not in existing_dates]
    if added:
        print(f"New dates added: {', '.join(str(r.date) for r in added)}")
    else:
        print("No new dates (all imported dates already existed)")


if __name__ == "__main__":
    main()
