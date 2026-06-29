"""
main_cloudfree_mosaic.py -- Cloud-free temporal mosaic from swisstopo STAC Sentinel-2 assets.

Queries the swisstopo STAC catalogue for a given date window, uses the
pre-computed per-day cloud mask ("Cloud mask - 10m", value 0 = cloud-free)
and the alpha band of the TCI asset (band 4, value 0 = no data) to build a
temporal cloud-free composite following the approach of the S2Mosaic library
(https://github.com/DPIRD-DMA/S2Mosaic).

The STAC catalogue is publicly accessible — no authentication required.

Usage (CLI):
    python main_functions/main_cloudfree_mosaic.py [options]

    Options:
      --date DATE        End date of the search window (YYYY-MM-DD). Default: 2025-07-01
      --days N           Days to look back from --date. Default: 15
      --sort METHOD      Scene ordering: valid_data | oldest | newest. Default: valid_data
      --method METHOD    Pixel combination: first | mean. Default: first
      --threshold F      Stop early when remaining AOI fraction ≤ F. Default: 0.001
                         Use 0 to disable early stopping.
      --output PATH      Output file path (auto-generated from params if omitted)
      --bands TITLE ...  Asset title(s) to mosaic. Default: "True color image - 10m"
      --stac-url URL     STAC catalogue base URL. Default: data.geo.admin.ch
      --collection ID    STAC collection ID. Default: ch.swisstopo.swisseo_s2-sr_v200
      --cloud-mask-title TITLE
                         Asset title of the cloud-mask COGtif. Default: "Cloud mask - 10m"
      --aoi PATH         GeoPackage with area-of-interest polygon. Default: assets/swissboundary_buffer_5000m.gpkg
                         Pass '' or 'none' to disable.

    Examples:
        python main_functions/main_cloudfree_mosaic.py
        python main_functions/main_cloudfree_mosaic.py --date 2025-07-01 --days 30 --sort valid_data --method first
        python main_functions/main_cloudfree_mosaic.py --date 2025-08-15 --days 60 --threshold 0 --output my_mosaic.tif

Programmatic:
    from main_functions.main_cloudfree_mosaic import create_cloudfree_mosaic
    out = create_cloudfree_mosaic(date="2025-07-01", time_range_days=30)
"""

import argparse
import subprocess
import sys
import tempfile
from math import ceil, floor
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Union

import geopandas as gpd
import numpy as np
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.vrt import WarpedVRT

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Catalogue constants
# ---------------------------------------------------------------------------
STAC_BASE_URL    = "https://data.geo.admin.ch/api/stac/v0.9/"
COLLECTION_ID    = "ch.swisstopo.swisseo_s2-sr_v200"
CLOUD_MASK_TITLE = "Cloud mask - 10m"
TCI_TITLE        = "True color image - 10m"
AOI_GPKG         = Path(__file__).resolve().parent.parent / "assets" / "swissboundary_buffer_5000m.gpkg"
# The TCI COGtif has 3 bands (R/G/B) only. No-data is stored as a GDAL
# internal PER_DATASET mask band (255 = valid, 0 = no-data), NOT as a
# 4th alpha band. Use ds.dataset_mask() to read it.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_asset_by_title(item, title: str):
    """Return the first STAC asset whose title matches, or None."""
    for _, asset in item.get_assets().items():
        if getattr(asset, "title", None) == title:
            return asset
    return None


def _compute_valid_fraction(cloud_mask_href: str, tci_href: str) -> float:
    """
    Estimate fraction of cloud-free, non-no-data pixels at coarse resolution.

    Both rasters are read at the same fixed 256×256 shape to avoid any
    extent-mismatch between scenes.
    """
    SCORE_H, SCORE_W = 256, 256
    try:
        with rasterio.open(cloud_mask_href) as cm_ds:
            cloud = cm_ds.read(
                1,
                out_shape=(SCORE_H, SCORE_W),
                resampling=Resampling.nearest,
            )
        with rasterio.open(tci_href) as tci_ds:
            tci_bands = tci_ds.read(
                out_shape=(tci_ds.count, SCORE_H, SCORE_W),
                resampling=Resampling.nearest,
            )

        # Cloud mask value 0 = clear; 1 = cloud; 2 = thin cloud; 3 = shadow.
        # Cloud mask value 0 also means no-data outside the orbit footprint,
        # so only count pixels where TCI actually has data (at least one band > 0).
        has_data = tci_bands.max(axis=0) > 0
        valid = has_data & (cloud == 0)
        return float(valid.sum()) / max(float(has_data.sum()), 1)
    except Exception as exc:
        print(f"    [warn] valid_fraction estimation failed: {exc}")
        return 0.0


def _sort_items(
    items: list,
    sort_method: str,
    asset_title: str,
    cloud_mask_title: str,
) -> list:
    """Order STAC items according to sort_method."""
    if sort_method == "oldest":
        return sorted(items, key=lambda x: x.datetime)

    if sort_method == "newest":
        return sorted(items, key=lambda x: x.datetime, reverse=True)

    if sort_method == "valid_data":
        print(f"  Computing valid-data coverage for {len(items)} items ...")
        scored = []
        for item in items:
            cm_asset  = _find_asset_by_title(item, cloud_mask_title)
            tci_asset = _find_asset_by_title(item, asset_title)
            if cm_asset and tci_asset:
                score = _compute_valid_fraction(cm_asset.href, tci_asset.href)
            else:
                score = 0.0
            scored.append((score, item))
            print(f"    {item.datetime.date() if item.datetime else item.id}  valid={score:.1%}")
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    raise ValueError(
        f"Unknown sort_method '{sort_method}'. Choose: 'valid_data', 'oldest', 'newest'."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_cloudfree_mosaic(
    date: str = "2025-07-01",
    time_range_days: int = 30,
    sort_method: str = "valid_data",
    required_bands: Optional[List[str]] = None,
    no_data_threshold: Optional[float] = 0.001,
    output_name: Optional[str] = None,
    mosaic_method: str = "first",
    stac_url: str = STAC_BASE_URL,
    collection_id: str = COLLECTION_ID,
    cloud_mask_title: str = CLOUD_MASK_TITLE,
    aoi_gpkg: Optional[Union[str, Path]] = AOI_GPKG,
) -> Optional[Path]:
    """
    Create a cloud-free mosaic from STAC Sentinel-2 assets.

    The function queries the swisstopo STAC catalogue for the time window
    [date - time_range_days, date], combines scenes into a composite using
    the pre-computed cloud mask and TCI alpha band, and writes a GeoTIFF.

    Parameters
    ----------
    date : str
        End date of the search window (YYYY-MM-DD). Default "2025-07-01".
    time_range_days : int
        Days to look back from `date`. Default 30.
    sort_method : str
        Scene ordering strategy:
        - "valid_data" : most cloud-free / valid pixels first (recommended)
        - "oldest"     : chronological order
        - "newest"     : reverse chronological order
    required_bands : list of str, optional
        STAC asset titles to mosaic. Each title must match an asset title in
        the catalogue items. Defaults to ["True color image - 10m"].
    no_data_threshold : float or None
        Stop cloud-free filling once the fraction of remaining unfilled AOI
        pixels drops at or below this value. Default 0.001. None = process all scenes.
    output_name : str or None
        Path of the output GeoTIFF. Auto-generated from parameters if None.
    mosaic_method : str
        Pixel combination method:
        - "first" : use first valid observation (fastest)
        - "mean"  : average all valid observations
    stac_url : str
        STAC catalogue base URL.
    collection_id : str
        STAC collection identifier.
    cloud_mask_title : str
        Asset title of the cloud-mask COGtif (uint8, 0=clear, 1=cloud, 2=thin cloud, 3=shadow).
    aoi_gpkg : str, Path, or None
        GeoPackage defining the area of interest (e.g. Switzerland boundary
        + 5 km buffer). When provided:
        - fill % and the no_data_threshold are computed over AOI pixels only,
          preventing premature early stopping caused by the bounding-box
          margin that no scene ever covers;
        - pixels outside the AOI are always written as no-data.
        Defaults to assets/swissboundary_buffer_5000m.gpkg.

    Returns
    -------
    Path or None
        Path to the written GeoTIFF, or None when no items were found or
        all items were skipped.
    """
    if required_bands is None:
        required_bands = [TCI_TITLE]

    if mosaic_method not in ("first", "mean"):
        raise ValueError(f"mosaic_method must be 'first' or 'mean', got '{mosaic_method}'.")

    # Build date range string for STAC search
    end_dt   = datetime.strptime(date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=time_range_days)
    datetime_str = (
        f"{start_dt.strftime('%Y-%m-%dT00:00:00Z')}/"
        f"{end_dt.strftime('%Y-%m-%dT23:59:59Z')}"
    )

    print("=" * 60)
    print("Cloud-free mosaic")
    print(f"  Date range    : {start_dt.date()} → {end_dt.date()} ({time_range_days} days)")
    print(f"  Assets        : {required_bands}")
    print(f"  Sort method   : {sort_method}")
    print(f"  Mosaic method : {mosaic_method}")
    print(f"  No-data thr.  : {no_data_threshold}")
    print(f"  STAC          : {stac_url}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # STAC search (mirrors util_reprocess_tci_tap.py approach)
    # ------------------------------------------------------------------
    client = pystac_client.Client.open(stac_url)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")

    search = client.search(
        collections=[collection_id],
        datetime=datetime_str,
        max_items=None,
    )
    items = list(search.items())
    print(f"\nFound {len(items)} STAC items in date window.\n")

    if not items:
        print("No items found — aborting.")
        return None

    # ------------------------------------------------------------------
    # Process each requested asset title
    # ------------------------------------------------------------------
    output_paths: List[Path] = []

    for asset_title in required_bands:
        print(f"\n{'─' * 60}")
        print(f"Asset: {asset_title}")
        print(f"{'─' * 60}")

        # Keep only items that carry both the target asset and cloud mask
        valid_items = []
        for item in items:
            has_asset = _find_asset_by_title(item, asset_title) is not None
            has_cloud = _find_asset_by_title(item, cloud_mask_title) is not None
            if has_asset and has_cloud:
                valid_items.append(item)
            else:
                missing = []
                if not has_asset:
                    missing.append(f"'{asset_title}'")
                if not has_cloud:
                    missing.append(f"'{cloud_mask_title}'")
                print(f"  Skip {item.id}: missing {' and '.join(missing)}")

        if not valid_items:
            print(f"  No usable items for '{asset_title}'.")
            continue

        # Sort scenes
        sorted_items = _sort_items(valid_items, sort_method, asset_title, cloud_mask_title)

        # ------------------------------------------------------------------
        # Peek at one item for CRS, band count, and dtype
        # ------------------------------------------------------------------
        ref_href = _find_asset_by_title(sorted_items[0], asset_title).href
        with rasterio.open(ref_href) as ref_ds:
            ref_profile_src = ref_ds.profile.copy()
            data_bands      = ref_ds.count      # 3 for TCI (R/G/B)
            dtype           = ref_ds.dtypes[0]  # e.g. 'uint8'
            ref_crs         = ref_ds.crs

        # ------------------------------------------------------------------
        # Build reference grid from the AOI bounding box (not from any
        # single item).  The 4 Sentinel-2 orbits covering Switzerland each
        # have a different footprint, so the reference grid must span the
        # entire AOI so every orbit can contribute its portion.
        # ------------------------------------------------------------------
        PIXEL = 10.0  # native 10 m resolution

        if aoi_gpkg is not None and Path(aoi_gpkg).exists():
            gdf     = gpd.read_file(aoi_gpkg).to_crs(ref_crs)
            bnd     = gdf.total_bounds          # (minx, miny, maxx, maxy)
            gpkg_name = Path(aoi_gpkg).name
        else:
            # Fallback: use first item's extent
            with rasterio.open(ref_href) as ref_ds:
                b = ref_ds.bounds
            bnd = (b.left, b.bottom, b.right, b.top)
            gdf = None
            gpkg_name = None

        # TAP-align to PIXEL grid
        minx = floor(bnd[0] / PIXEL) * PIXEL
        miny = floor(bnd[1] / PIXEL) * PIXEL
        maxx = ceil (bnd[2] / PIXEL) * PIXEL
        maxy = ceil (bnd[3] / PIXEL) * PIXEL

        width  = int(round((maxx - minx) / PIXEL))
        height = int(round((maxy - miny) / PIXEL))

        from rasterio.transform import from_origin
        ref_transform = from_origin(minx, maxy, PIXEL, PIXEL)

        ref_profile = ref_profile_src.copy()
        ref_profile.update(
            width=width, height=height,
            transform=ref_transform, crs=ref_crs,
            count=data_bands, dtype=dtype,
        )

        print(f"\n  Reference grid: {height} × {width} px, {data_bands} bands ({dtype})")
        print(f"  CRS           : {ref_crs.to_epsg()}")
        print(f"  Extent        : ({minx:.0f}, {miny:.0f}) → ({maxx:.0f}, {maxy:.0f})")

        # ------------------------------------------------------------------
        # AOI mask — rasterise the boundary polygon onto the reference grid
        # ------------------------------------------------------------------
        if gdf is not None:
            aoi_mask = ~geometry_mask(
                gdf.geometry,
                transform=ref_transform,
                invert=False,
                out_shape=(height, width),
            )   # True = inside AOI
            total_px = int(aoi_mask.sum())
            print(f"  AOI           : {gpkg_name}  ({total_px:,} px within boundary)")
        else:
            aoi_mask = np.ones((height, width), dtype=bool)
            total_px = height * width
            print(f"  AOI           : none (full bounding box)")

        # ------------------------------------------------------------------
        # Mosaic accumulators
        # ------------------------------------------------------------------
        mosaic_acc = np.zeros((data_bands, height, width), dtype=np.float32)
        weight     = np.zeros((height, width), dtype=np.int32)
        filled     = np.zeros((height, width), dtype=bool)

        warp_kwargs = dict(
            crs=ref_crs,
            transform=ref_transform,
            width=width,
            height=height,
            resampling=Resampling.nearest,
        )

        # ------------------------------------------------------------------
        # Cloud-free mosaic loop
        # ------------------------------------------------------------------
        for idx, item in enumerate(sorted_items, 1):
            filled_aoi     = int((filled & aoi_mask).sum())
            filled_frac    = filled_aoi / total_px
            remaining_frac = 1.0 - filled_frac

            print(
                f"\n  [{idx:2d}/{len(sorted_items)}] "
                f"{item.datetime.date() if item.datetime else item.id}"
                f"  — filled {filled_frac:.1%}, remaining {remaining_frac:.1%}"
            )

            # Early stop — checked against AOI fill fraction
            if no_data_threshold is not None and remaining_frac <= no_data_threshold:
                print(f"  No-data threshold ({no_data_threshold:.1%}) reached — stopping early.")
                break

            tci_asset = _find_asset_by_title(item, asset_title)
            cm_asset  = _find_asset_by_title(item, cloud_mask_title)

            try:
                with rasterio.open(tci_asset.href) as _tci_raw:
                    with WarpedVRT(_tci_raw, **warp_kwargs) as tci_ds:
                        bands = tci_ds.read()       # (data_bands, H, W) uint8
                with rasterio.open(cm_asset.href) as _cm_raw:
                    with WarpedVRT(_cm_raw, **warp_kwargs) as cm_ds:
                        cloud = cm_ds.read(1)        # uint8: 0=clear
            except Exception as exc:
                print(f"    [error] Could not read asset: {exc} — skipping item.")
                continue

            # WarpedVRT fills pixels outside the scene footprint with 0 in all bands.
            # dataset_mask() is unreliable through WarpedVRT; use band values instead.
            # Cloud mask: 0=clear, 1=cloud, 2=thin cloud, 3=shadow — keep only 0.
            # Cloud mask 0 also means no-data outside the orbit, so restrict to
            # pixels where TCI actually has data (at least one band > 0).
            has_data = bands.max(axis=0) > 0
            valid = has_data & (cloud == 0) & aoi_mask
            n_valid      = int(valid.sum())
            n_tci_in_aoi = int((has_data & aoi_mask).sum())
            print(
                f"    TCI coverage in AOI: {n_tci_in_aoi:,} px  |  "
                f"cloud-free: {n_valid:,} px "
                f"({n_valid/max(n_tci_in_aoi,1):.1%} of TCI coverage)"
            )

            if not valid.any():
                print("    No valid pixels — skipping.")
                continue

            if mosaic_method == "first":
                new_px = valid & ~filled
                if new_px.any():
                    for b in range(data_bands):
                        mosaic_acc[b][new_px] = bands[b][new_px]
                    filled |= new_px
            else:  # mean
                for b in range(data_bands):
                    mosaic_acc[b][valid] += bands[b][valid].astype(np.float32)
                weight[valid] += 1
                filled |= valid

        # ------------------------------------------------------------------
        # Finalise mosaic
        # ------------------------------------------------------------------
        if mosaic_method == "mean":
            has_data_w = weight > 0
            for b in range(data_bands):
                mosaic_acc[b][has_data_w] /= weight[has_data_w]

        max_val = np.iinfo(dtype).max if np.issubdtype(np.dtype(dtype), np.integer) else 1.0
        result  = np.clip(mosaic_acc, 0, max_val).astype(dtype)

        # Report final coverage
        coverage = int((filled & aoi_mask).sum()) / total_px
        unfilled = int((aoi_mask & ~filled).sum())
        print(f"\n  Coverage: {coverage:.1%} of AOI filled")
        if unfilled > 0:
            print(f"  Unfilled: {unfilled:,} AOI pixels (all scenes cloudy or no data there)")

        # Output mask: valid where result has actual non-zero data AND inside AOI.
        # Using result.max(axis=0) > 0 (band-based) rather than `filled` so the
        # mask stays consistent with the band-value no-data convention.
        out_mask = np.where(
            (result.max(axis=0) > 0) & aoi_mask,
            np.uint8(255),
            np.uint8(0),
        )

        # ------------------------------------------------------------------
        # Build output path
        # ------------------------------------------------------------------
        if output_name:
            out_path = Path(output_name)
        else:
            safe = (
                asset_title.lower()
                .replace(" ", "_")
                .replace("-", "_")
                .replace("/", "_")
            )
            out_path = Path(
                f"mosaic_{safe}"
                f"_{date}"
                f"_{time_range_days}d"
                f"_{sort_method}"
                f"_{mosaic_method}.tif"
            )

        # ------------------------------------------------------------------
        # Export: write temp GeoTIFF → gdalwarp → final COG
        # ------------------------------------------------------------------
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Step 1: write mosaic array + internal mask to an uncompressed temp file.
        # The source TCI uses PHOTOMETRIC=YCBCR which requires COMPRESS=JPEG.
        # Override both so the uncompressed temp write is valid.
        tmp_profile = ref_profile.copy()
        tmp_profile.update(
            driver="GTiff",
            count=data_bands,
            dtype=dtype,
            compress="none",
            photometric="RGB",
        )
        for key in ("AREA_OR_POINT", "JPEGTABLESMODE", "JPEG_QUALITY"):
            tmp_profile.pop(key, None)

        with tempfile.NamedTemporaryFile(suffix="_mosaic_tmp.tif", delete=False) as tmp_fh:
            tmp_path = tmp_fh.name

        try:
            with rasterio.open(tmp_path, "w", **tmp_profile) as dst:
                dst.write(result)
                # Internal PER_DATASET mask: 255 = valid, 0 = no-data
                # gdalwarp -dstalpha picks this up and creates the alpha band
                dst.write_mask(out_mask)

            # Step 2: gdalwarp → COG with JPEG compression + alpha + TAP alignment
            print(f"    Writing COG via gdalwarp ...")
            gdal_cmd = [
                "gdalwarp",
                "-of", "COG",
                "-co", "BIGTIFF=YES",
                "-co", "NUM_THREADS=ALL_CPUS",
                "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                "-co", "COMPRESS=JPEG",
                "-co", "QUALITY=85",
                "-dstalpha",
                "-tr", "10", "10",
                "-tap",
                "-overwrite",
                tmp_path,
                str(out_path),
            ]
            result_proc = subprocess.run(gdal_cmd, capture_output=True, text=True)
            if result_proc.returncode != 0:
                print(f"    [error] gdalwarp failed:\n{result_proc.stderr}")
                continue
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        coverage = filled.sum() / total_px
        print(f"\n  Output written : {out_path}")
        print(f"  Final coverage : {coverage:.1%} of pixels filled")
        output_paths.append(out_path)

    print("\n" + "=" * 60)
    if output_paths:
        print(f"Done. {len(output_paths)} mosaic(s) created.")
        for p in output_paths:
            print(f"  {p}")
    else:
        print("Done. No mosaics were created (all items skipped).")
    print("=" * 60)

    if not output_paths:
        return None
    return output_paths[0] if len(output_paths) == 1 else output_paths  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a cloud-free temporal mosaic from the swisstopo STAC "
            "Sentinel-2 catalogue."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--date",
        default="2025-07-01",
        help="End date of the search window (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=15,
        metavar="N",
        help="Number of days to look back from --date.",
    )
    parser.add_argument(
        "--sort",
        default="valid_data",
        choices=["valid_data", "oldest", "newest"],
        help="Scene sort strategy.",
    )
    parser.add_argument(
        "--method",
        default="first",
        choices=["first", "mean"],
        help="Pixel combination method.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.001,
        metavar="F",
        help="Stop early when remaining AOI fraction ≤ F. 0 = no early stop.",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Output file path (auto-generated if omitted).",
    )
    parser.add_argument(
        "--bands",
        nargs="+",
        default=None,
        metavar="TITLE",
        help='Asset title(s) to mosaic. Default: "True color image - 10m".',
    )
    parser.add_argument(
        "--stac-url",
        default=STAC_BASE_URL,
        help="STAC catalogue base URL.",
    )
    parser.add_argument(
        "--collection",
        default=COLLECTION_ID,
        help="STAC collection ID.",
    )
    parser.add_argument(
        "--cloud-mask-title",
        default=CLOUD_MASK_TITLE,
        help="Asset title of the cloud-mask COGtif.",
    )
    parser.add_argument(
        "--aoi",
        default=str(AOI_GPKG),
        metavar="PATH",
        help=(
            "GeoPackage with the area-of-interest polygon. Fill %% and the "
            "no-data threshold are computed over AOI pixels only. "
            "Pass '' or 'none' to disable."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args      = _parse_args()
    threshold = args.threshold if args.threshold > 0 else None
    aoi       = None if args.aoi.lower() in ("", "none") else args.aoi

    create_cloudfree_mosaic(
        date=args.date,
        time_range_days=args.days,
        sort_method=args.sort,
        required_bands=args.bands,
        no_data_threshold=threshold,
        output_name=args.output,
        mosaic_method=args.method,
        stac_url=args.stac_url,
        collection_id=args.collection,
        cloud_mask_title=args.cloud_mask_title,
        aoi_gpkg=aoi,
    )


if __name__ == "__main__":
    main()