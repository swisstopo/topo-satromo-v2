"""
util_reprocess_mosaic_csde.py -- Batch-run seasonal cloud-free mosaics.

Runs main_cloudfree_mosaic_csde.py once per two-month window for every year
from START_YEAR to END_YEAR. Three overlapping windows per year cover the
vegetation season:

    June/July         YYYY-06-01 .. YYYY-07-31
    July/August       YYYY-07-01 .. YYYY-08-31
    August/September  YYYY-08-01 .. YYYY-09-30

Each window is named after its end date, following the same convention as
step1_processor_s2_sr.py / step1_processor_vhi.py
(swisseo_s2-sr_v200_mosaic_<end-date>t235959_<band>_10m.tif), so the
per-band outputs parse with the step1 filename parser.

Outputs go to OUTPUT_DIR (created if missing). That is deliberately on the
data drive rather than the project's temp/: each window is about 3.8 GB and
the full batch roughly 125 GB.

A window whose TCI already exists is skipped -- the TCI is written last, so
its presence means that window finished. The batch can therefore be
interrupted and resumed without redoing completed windows.

Usage:
    python main_functions/util_reprocess_mosaic_csde.py
"""

import subprocess
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Not the project's temp/: the full batch writes ~3.8 GB per window
# (~125 GB over all of them), which does not fit on the system disk.
OUTPUT_DIR = Path("/mnt/d/temp/mosaic_csde")

# Product prefix, matching COLLECTION_ID in main_cloudfree_mosaic_csde.py
# with the "ch.swisstopo." organisation prefix stripped.
PRODUCT_STEM = "swisseo_s2-sr_v200"

START_YEAR = 2016
END_YEAR   = 2026

# Two-month windows, as (first month, last month)
WINDOWS = [
    (6, 7),   # June / July
    (7, 8),   # July / August
    (8, 9),   # August / September
]

BLOCK_ROWS = 2500
WORKERS    = 16


def window_dates(year, start_month, end_month):
    """First day of start_month through last day of end_month."""
    start = date(year, start_month, 1)
    end = date(year, end_month, monthrange(year, end_month)[1])
    return start, end


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    script_path = ROOT / "main_functions" / "main_cloudfree_mosaic_csde.py"

    runs = [
        (year, start_month, end_month)
        for year in range(START_YEAR, END_YEAR + 1)
        for start_month, end_month in WINDOWS
    ]

    print(
        f"Running {len(runs)} two-month mosaics "
        f"({START_YEAR}-{END_YEAR}, {len(WINDOWS)} windows per year)"
    )
    print(f"Output folder: {OUTPUT_DIR}\n")

    for idx, (year, start_month, end_month) in enumerate(runs, 1):
        start, end = window_dates(year, start_month, end_month)
        output_base = OUTPUT_DIR / f"{PRODUCT_STEM}_mosaic_{end}t235959.tif"
        tci_path = output_base.with_name(output_base.stem + "_tci_10m.tif")

        if tci_path.exists():
            print(f"[{idx}/{len(runs)}] {start} → {end}  already done, skipping")
            continue

        print(f"[{idx}/{len(runs)}] {start} → {end}")

        cmd = [
            sys.executable, str(script_path),
            "--start-date", start.isoformat(),
            "--end-date", end.isoformat(),
            "--block-rows", str(BLOCK_ROWS),
            "--workers", str(WORKERS),
            "--output", str(output_base),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"  [error] mosaic failed for {start} → {end} (exit code {result.returncode})")

    print("\nDone.")


if __name__ == "__main__":
    main()
