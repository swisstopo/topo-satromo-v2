"""
util_reprocess_mosaic_thumbnails.py -- A4 year-overview PNGs from monthly mosaics.

Scans a directory for mosaic GeoTIFFs named with a YYYY-MM date (as produced
by util_reprocess_mosaic.py, e.g. mosaic_2018-03.tif) and renders one A4
portrait PNG per year, laid out as a 3x4 grid of the twelve monthly
thumbnails.

No-data pixels within a month's mosaic (clouds, missing coverage) are drawn
pink so gaps are easy to spot against the true-color imagery.

Usage:
    python main_functions/util_reprocess_mosaic_thumbnails.py temp
    python main_functions/util_reprocess_mosaic_thumbnails.py temp --output-dir temp/overview
    python main_functions/util_reprocess_mosaic_thumbnails.py temp --title-suffix "edge-margin-px=200"

    Options:
      --output-dir PATH   Directory for the PNG pages. Default: input_dir
      --title-suffix TEXT Text appended to each page's title, e.g. to record
                          which run parameters produced the mosaics.
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.enums import Resampling

MONTH_NAMES = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

FILENAME_RE = re.compile(r"(\d{4})-(\d{2})")

THUMB_LONG_SIDE = 800  # px, longer side of the downsampled read


def find_year_month_files(input_dir: Path) -> dict:
    """Map year -> {month: tif_path} from filenames containing YYYY-MM."""
    years = defaultdict(dict)
    for tif_path in sorted(input_dir.glob("*.tif")):
        match = FILENAME_RE.search(tif_path.stem)
        if not match:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        years[year][month] = tif_path
    return years


def read_thumbnail(tif_path: Path) -> np.ndarray:
    """Read a downsampled RGBA thumbnail (0-1 float) from a mosaic GeoTIFF.

    The mosaic's no-data areas are stored as a per-dataset mask band
    (dataset_mask), not as a 4th RGBA band, so alpha is read separately.
    """
    with rasterio.open(tif_path) as ds:
        scale = THUMB_LONG_SIDE / max(ds.width, ds.height)
        out_h = max(1, int(round(ds.height * scale)))
        out_w = max(1, int(round(ds.width * scale)))
        rgb = ds.read(
            out_shape=(min(ds.count, 3), out_h, out_w),
            resampling=Resampling.average,
        ).astype(np.float32) / 255.0
        alpha = ds.dataset_mask(
            out_shape=(out_h, out_w),
            resampling=Resampling.average,
        ).astype(np.float32) / 255.0

    return np.dstack([rgb[0], rgb[1], rgb[2], alpha])


def build_year_page(year: int, month_files: dict, output_path: Path, title_suffix: str = "") -> None:
    """Render one A4 portrait PNG with a 3x4 grid of monthly mosaics."""
    fig, axes = plt.subplots(
        4, 3,
        figsize=(8.27, 11.69),  # A4 portrait, inches
        dpi=200,
    )
    title = f"Cloud-free Sentinel-2 mosaics {year}"
    if title_suffix:
        title += f" {title_suffix}"
    fig.suptitle(title, fontsize=16, y=0.98)

    for month in range(1, 13):
        row, col = divmod(month - 1, 3)
        ax = axes[row][col]
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(MONTH_NAMES[month - 1], fontsize=10)

        tif_path = month_files.get(month)
        if tif_path is None:
            ax.set_facecolor("#f0f0f0")
            ax.text(
                0.5, 0.5, "keine Daten",
                ha="center", va="center", fontsize=8, color="gray",
                transform=ax.transAxes,
            )
            continue

        try:
            thumb = read_thumbnail(tif_path)
        except Exception as exc:
            ax.set_facecolor("#f0f0f0")
            ax.text(
                0.5, 0.5, f"Fehler:\n{exc}",
                ha="center", va="center", fontsize=7, color="red",
                transform=ax.transAxes,
            )
            continue

        ax.set_facecolor("pink")
        ax.imshow(thumb)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)
    print(f"  Written: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build one A4 PNG per year, showing all 12 monthly cloud-free "
            "mosaics as thumbnails."
        ),
    )
    parser.add_argument(
        "input_dir",
        help="Directory containing mosaic_YYYY-MM.tif files.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for the PNG pages. Defaults to input_dir.",
    )
    parser.add_argument(
        "--title-suffix",
        default="",
        help="Text appended to each page's title, e.g. to record run parameters.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else input_dir

    years = find_year_month_files(input_dir)
    if not years:
        print(f"No mosaic_YYYY-MM.tif files found in {input_dir}")
        sys.exit(1)

    print(f"Found {len(years)} year(s): {sorted(years)}\n")

    for year in sorted(years):
        print(f"Year {year}")
        output_path = output_dir / f"mosaic_overview_{year}.png"
        build_year_page(year, years[year], output_path, title_suffix=args.title_suffix)

    print("\nDone.")


if __name__ == "__main__":
    main()
