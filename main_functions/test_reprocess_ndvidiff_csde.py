"""
test_reprocess_ndvidiff_csde.py -- NDVI and year-on-year NDVI difference.

Works on the output of util_reprocess_mosaic_csde.py, i.e. a directory of
per-band mosaics named <stem>_mosaic_<YYYY-MM-DD>t235959_<band>_10m.tif.

Step 1 -- NDVI per window, from that window's B04 and B08:

    ..._2024-07-31t235959_b04_10m.tif  (Red)
    ..._2024-07-31t235959_b08_10m.tif  (NIR)   ->  ..._2024-07-31t235959_ndvi_10m.tif

The source bands are uint16 digital numbers, so they are converted to
reflectance first (reflectance = dn * 0.0001 - 0.1) and NDVI is computed on
the reflectance values:

    ndvi = (nir - red) / (nir + red)

Step 2 -- NDVI difference against the same window of the previous year:

    ..._2024-07-31t235959_ndvi_10m.tif
  - ..._2023-07-31t235959_ndvi_10m.tif   ->  ..._2024-07-31t235959_ndvidiff_10m.tif

Windows are paired on their month and day, so July/August 2024 is only ever
compared with July/August 2023. The earliest year has no predecessor and is
skipped.

Encoding -- both outputs are Int16 with no-data 32701 and a scale of 1000:

    ndvi      = dn / 1000        (dn -1000 .. 1000)
    ndvidiff  = dn / 1000        (dn -2000 .. 2000)

The scale is also written to the band as a GDAL scale tag. Note that GDAL
expresses it the other way round (value = dn * scale), so the tag holds
0.001 while the encoding factor here is 1000.

No-data propagates: an NDVI pixel is no-data where either source band is
no-data (0) or where nir + red == 0; an NDVIdiff pixel is no-data where
either year's NDVI is no-data.

Usage:
    python main_functions/test_reprocess_ndvidiff_csde.py /mnt/d/temp/mosaic_csde
    python main_functions/test_reprocess_ndvidiff_csde.py /mnt/d/temp/mosaic_csde --overwrite
    python main_functions/test_reprocess_ndvidiff_csde.py /mnt/d/temp/mosaic_csde --ndvi-only

    Options:
      --output-dir PATH  Where to write. Default: input_dir
      --overwrite        Recompute outputs that already exist (default: skip)
      --ndvi-only        Run step 1 only, no differences
      --block-rows N     Rows per processing block (memory/speed). Default: 2048
"""

import argparse
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from math import ceil
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

# Encoding of the swissEO S2-SR band mosaics this script reads
SRC_DN_SCALE  = 0.0001
SRC_DN_OFFSET = -0.1
SRC_NODATA    = 0

# Encoding of the NDVI / NDVIdiff rasters this script writes
NDVI_FACTOR     = 1000           # dn = round(ndvi * NDVI_FACTOR)
NDVI_GDAL_SCALE = 1.0 / NDVI_FACTOR   # GDAL tag: value = dn * scale
NDVI_NODATA     = 32701

# Matches the B04 of a csde window; the rest of the filenames are derived
# from it, so a window is only processed when its Red band is present.
B04_RE = re.compile(r"_mosaic_(\d{4})-(\d{2})-(\d{2})t\d{6}_b04_10m$")


def find_windows(input_dir: Path) -> list:
    """Return [(year, month_day, base_path)] for every window with a B04."""
    windows = []
    for b04 in sorted(input_dir.glob("*_b04_10m.tif")):
        match = B04_RE.search(b04.stem)
        if not match:
            continue
        year, month, day = (int(g) for g in match.groups())
        base = b04.with_name(b04.stem[: -len("_b04_10m")])
        windows.append((year, (month, day), base))
    return windows


def _write_cog(tmp_path: str, out_path: Path, nodata_value: int) -> bool:
    """Convert the temp GeoTIFF to a COG, as main_cloudfree_mosaic_csde.py does."""
    cmd = [
        "gdalwarp", "-of", "COG", "-co", "BIGTIFF=YES",
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2", "-co", "NUM_THREADS=ALL_CPUS",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
        "-srcnodata", str(nodata_value), "-dstnodata", str(nodata_value),
        str(tmp_path), str(out_path), "-overwrite",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] gdalwarp failed:\n{result.stderr}")
        return False
    return True


def _out_profile(ref: rasterio.DatasetReader) -> dict:
    return dict(
        driver="GTiff", width=ref.width, height=ref.height, count=1, dtype="int16",
        crs=ref.crs, transform=ref.transform, nodata=NDVI_NODATA,
        tiled=True, blockxsize=512, blockysize=512,
        compress="deflate", predictor=2, bigtiff="YES",
    )


def _check_same_grid(a: rasterio.DatasetReader, b: rasterio.DatasetReader, what: str) -> None:
    if (a.crs, a.width, a.height) != (b.crs, b.width, b.height) or a.transform != b.transform:
        raise ValueError(f"{what}: grids differ, refusing to combine them pixel by pixel")


def compute_ndvi(b04_path: Path, b08_path: Path, out_path: Path, block_rows: int) -> bool:
    """NDVI from Red/NIR digital numbers, via reflectance."""
    with tempfile.NamedTemporaryFile(suffix="_ndvi_tmp.tif", delete=False) as fh:
        tmp_path = fh.name
    try:
        with rasterio.open(b04_path) as red_ds, rasterio.open(b08_path) as nir_ds:
            _check_same_grid(red_ds, nir_ds, f"{b04_path.name} / {b08_path.name}")
            profile = _out_profile(red_ds)

            with rasterio.open(tmp_path, "w", **profile) as dst:
                dst.scales = (NDVI_GDAL_SCALE,)
                dst.offsets = (0.0,)

                for row0 in range(0, red_ds.height, block_rows):
                    block_h = min(block_rows, red_ds.height - row0)
                    win = Window(0, row0, red_ds.width, block_h)

                    red_dn = red_ds.read(1, window=win)
                    nir_dn = nir_ds.read(1, window=win)

                    valid = (red_dn != SRC_NODATA) & (nir_dn != SRC_NODATA)

                    # Digital number -> reflectance before the index is formed
                    red = red_dn.astype(np.float32) * SRC_DN_SCALE + SRC_DN_OFFSET
                    nir = nir_dn.astype(np.float32) * SRC_DN_SCALE + SRC_DN_OFFSET

                    den = nir + red
                    valid &= den != 0
                    # Guard the division itself; invalid pixels are replaced below
                    den = np.where(den == 0, np.float32(1), den)
                    ndvi = (nir - red) / den

                    dn = np.where(valid, np.round(ndvi * NDVI_FACTOR), NDVI_NODATA)
                    dst.write(np.clip(dn, -32768, 32767).astype("int16"), 1, window=win)

        return _write_cog(tmp_path, out_path, NDVI_NODATA)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def compute_ndvidiff(cur_path: Path, prev_path: Path, out_path: Path, block_rows: int) -> bool:
    """
    Current year's NDVI minus the previous year's, same window.

    Both rasters share one linear encoding with a zero offset, so the
    digital numbers can be subtracted directly -- (a*1000) - (b*1000)
    equals (a-b)*1000 exactly, with no float round trip.
    """
    with tempfile.NamedTemporaryFile(suffix="_ndvidiff_tmp.tif", delete=False) as fh:
        tmp_path = fh.name
    try:
        with rasterio.open(cur_path) as cur_ds, rasterio.open(prev_path) as prev_ds:
            _check_same_grid(cur_ds, prev_ds, f"{cur_path.name} / {prev_path.name}")
            profile = _out_profile(cur_ds)

            with rasterio.open(tmp_path, "w", **profile) as dst:
                dst.scales = (NDVI_GDAL_SCALE,)
                dst.offsets = (0.0,)

                for row0 in range(0, cur_ds.height, block_rows):
                    block_h = min(block_rows, cur_ds.height - row0)
                    win = Window(0, row0, cur_ds.width, block_h)

                    cur = cur_ds.read(1, window=win)
                    prev = prev_ds.read(1, window=win)

                    valid = (cur != NDVI_NODATA) & (prev != NDVI_NODATA)
                    # int32 so the subtraction cannot wrap before the mask
                    diff = cur.astype(np.int32) - prev.astype(np.int32)
                    out = np.where(valid, diff, NDVI_NODATA)
                    dst.write(np.clip(out, -32768, 32767).astype("int16"), 1, window=win)

        return _write_cog(tmp_path, out_path, NDVI_NODATA)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compute NDVI per csde mosaic window, then the year-on-year NDVI "
            "difference against the same window of the previous year."
        ),
    )
    parser.add_argument("input_dir", help="Directory of csde per-band mosaics.")
    parser.add_argument("--output-dir", default=None, help="Where to write. Default: input_dir.")
    parser.add_argument("--overwrite", action="store_true", help="Recompute existing outputs.")
    parser.add_argument("--ndvi-only", action="store_true", help="Run step 1 only.")
    parser.add_argument("--block-rows", type=int, default=2048, metavar="N",
                        help="Rows per processing block (memory/speed trade-off).")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else input_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    windows = find_windows(input_dir)
    if not windows:
        print(f"No csde band mosaics (*_b04_10m.tif) found in {input_dir}")
        sys.exit(1)

    print(f"Found {len(windows)} window(s) in {input_dir}")
    print(f"Output folder: {output_dir}")
    print(f"Encoding     : Int16, nodata {NDVI_NODATA}, ndvi = dn / {NDVI_FACTOR}\n")

    # ------------------------------------------------------------------
    # Step 1 -- NDVI
    # ------------------------------------------------------------------
    print("Step 1: NDVI")
    ndvi_paths = {}
    for idx, (year, month_day, base) in enumerate(windows, 1):
        label = base.name.split("_mosaic_")[-1]
        b04 = base.with_name(base.name + "_b04_10m.tif")
        b08 = base.with_name(base.name + "_b08_10m.tif")
        out = output_dir / f"{base.name}_ndvi_10m.tif"

        if not b08.exists():
            print(f"  [{idx}/{len(windows)}] {label}  skipped, no B08")
            continue
        if out.exists() and not args.overwrite:
            print(f"  [{idx}/{len(windows)}] {label}  already done")
            ndvi_paths[(year, month_day)] = out
            continue

        print(f"  [{idx}/{len(windows)}] {label}  -> {out.name}")
        if compute_ndvi(b04, b08, out, args.block_rows):
            ndvi_paths[(year, month_day)] = out

    if args.ndvi_only:
        print("\nDone (--ndvi-only).")
        return

    # ------------------------------------------------------------------
    # Step 2 -- NDVIdiff against the same window of the previous year
    # ------------------------------------------------------------------
    print("\nStep 2: NDVIdiff (year minus previous year)")
    by_window = defaultdict(dict)
    for (year, month_day), path in ndvi_paths.items():
        by_window[month_day][year] = path

    made = 0
    for month_day in sorted(by_window):
        years = sorted(by_window[month_day])
        for year in years:
            prev = by_window[month_day].get(year - 1)
            cur = by_window[month_day][year]
            tag = f"{year}-{month_day[0]:02d}-{month_day[1]:02d}"

            if prev is None:
                print(f"  {tag}  no {year - 1} counterpart, skipped")
                continue

            out = output_dir / cur.name.replace("_ndvi_10m.tif", "_ndvidiff_10m.tif")
            if out.exists() and not args.overwrite:
                print(f"  {tag}  already done")
                made += 1
                continue

            print(f"  {tag}  minus {year - 1}  -> {out.name}")
            if compute_ndvidiff(cur, prev, out, args.block_rows):
                made += 1

    print(f"\nDone. {len(ndvi_paths)} NDVI, {made} NDVIdiff.")


if __name__ == "__main__":
    main()
