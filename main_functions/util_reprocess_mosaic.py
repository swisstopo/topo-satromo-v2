"""
util_reprocess_mosaic.py -- Batch-run cloud-free monthly mosaics.

Runs main_cloudfree_mosaic.py once per calendar month from START_YEAR/
START_MONTH to END_YEAR/END_MONTH (inclusive), using the last day of each
month as --date and --days 31 so the whole month is covered. Output
GeoTIFFs are written to the temp/ folder, one file per month.

Usage:
    python main_functions/util_reprocess_mosaic.py
"""

import subprocess
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = ROOT / "temp"

START_YEAR  = 2015
START_MONTH = 1
END_YEAR    = 2026
END_MONTH   = 8

DAYS           = 31
SORT_METHOD    = "valid_data"
MOSAIC_METHOD  = "first"
EDGE_MARGIN_PX = 200


def month_range(start_year, start_month, end_year, end_month):
    """Yield (year, month) tuples from start to end, inclusive."""
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def last_day_of_month(year, month):
    last_day = monthrange(year, month)[1]
    return date(year, month, last_day)


def main():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    script_path = ROOT / "main_functions" / "main_cloudfree_mosaic.py"

    months = list(month_range(START_YEAR, START_MONTH, END_YEAR, END_MONTH))
    print(
        f"Running cloud-free mosaic for {len(months)} months "
        f"({START_YEAR}-{START_MONTH:02d} -> {END_YEAR}-{END_MONTH:02d})"
    )
    print(f"Output folder: {TEMP_DIR}\n")

    for idx, (year, month) in enumerate(months, 1):
        end_date = last_day_of_month(year, month)
        date_str = end_date.strftime("%Y-%m-%d")
        output_path = TEMP_DIR / f"mosaic_{year:04d}-{month:02d}.tif"

        print(f"[{idx}/{len(months)}] {date_str}")

        cmd = [
            sys.executable, str(script_path),
            "--date", date_str,
            "--days", str(DAYS),
            "--sort", SORT_METHOD,
            "--method", MOSAIC_METHOD,
            "--edge-margin-px", str(EDGE_MARGIN_PX),
            "--output", str(output_path),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"  [error] mosaic failed for {date_str} (exit code {result.returncode})")

    print("\nDone.")


if __name__ == "__main__":
    main()
