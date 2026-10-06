"""
util_reprocess_mosaic_thumbnails.py -- A4 year-overview PNGs from mosaics.

Renders one A4 portrait PNG per year, laid out as a 3x4 grid of the twelve
months. The input directory is inspected and the naming scheme detected
automatically; both mosaic generations are supported:

  csde    swisseo_s2-sr_v200_mosaic_<YYYY-MM-DD>t235959_tci_10m.tif
          (util_reprocess_mosaic_csde.py / main_cloudfree_mosaic_csde.py)
          Only the TCI is used; the per-band and observation files are
          ignored. These are two-month windows named after their END date,
          so a June/July composite is placed on the July slot and June
          shows as no-data.

  legacy  mosaic_<YYYY-MM>.tif
          (util_reprocess_mosaic.py) -- one file per calendar month.

If any csde TCI is present the directory is treated as csde, otherwise the
legacy pattern is used.

Pink marks missing data throughout: no-data pixels inside a mosaic (clouds,
missing coverage) and whole months for which no mosaic exists.

Usage:
    python main_functions/util_reprocess_mosaic_thumbnails.py temp
    python main_functions/util_reprocess_mosaic_thumbnails.py /mnt/d/temp/mosaic_csde
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

# csde: ..._mosaic_<YYYY-MM-DD>t235959_tci_10m.tif -- anchored on the TCI
# suffix so the per-band/observation files of the same window are ignored.
# It also excludes create_enhanced_rgb's "..._tci_10m.temp.tif" leftovers,
# whose stem ends in ".temp" and therefore does not match.
CSDE_TCI_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})t\d{6}_tci_10m$")

# legacy: mosaic_<YYYY-MM>.tif -- one file per calendar month
LEGACY_RE = re.compile(r"(\d{4})-(\d{2})")

THUMB_LONG_SIDE = 800  # px, longer side of the downsampled read


def find_year_month_files(input_dir: Path) -> tuple:
    """
    Map year -> {month: tif_path}, detecting the naming scheme in use.

    Returns (years, scheme). csde wins when both are present, since its
    band files would otherwise also match the looser legacy pattern.
    """
    tifs = sorted(input_dir.glob("*.tif"))

    csde = defaultdict(dict)
    for tif_path in tifs:
        match = CSDE_TCI_RE.search(tif_path.stem)
        if match:
            # Two-month window named after its end date -> end month slot
            csde[int(match.group(1))][int(match.group(2))] = tif_path
    if csde:
        return csde, "csde"

    legacy = defaultdict(dict)
    for tif_path in tifs:
        match = LEGACY_RE.search(tif_path.stem)
        if match:
            legacy[int(match.group(1))][int(match.group(2))] = tif_path
    return legacy, "legacy"


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
            # Pink for a missing month, same as no-data inside a mosaic, so
            # every gap on the page reads the same way.
            ax.set_facecolor("pink")
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
        help=(
            "Directory containing the mosaics. Either csde TCIs "
            "(..._<YYYY-MM-DD>t235959_tci_10m.tif) or legacy "
            "mosaic_<YYYY-MM>.tif files; the scheme is detected automatically."
        ),
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

    years, scheme = find_year_month_files(input_dir)
    if not years:
        print(
            f"No mosaics found in {input_dir} — expected either csde TCIs "
            f"(..._<YYYY-MM-DD>t235959_tci_10m.tif) or legacy mosaic_<YYYY-MM>.tif"
        )
        sys.exit(1)

    n_files = sum(len(m) for m in years.values())
    print(f"Detected naming scheme: {scheme}  ({n_files} mosaic(s))")
    print(f"Found {len(years)} year(s): {sorted(years)}\n")

    for year in sorted(years):
        print(f"Year {year}")
        output_path = output_dir / f"mosaic_overview_{year}.png"
        build_year_page(year, years[year], output_path, title_suffix=args.title_suffix)

    print("\nDone.")


if __name__ == "__main__":
    main()
