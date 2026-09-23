"""
main_cloudfree_mosaic_csde.py -- Statistical (first-quartile) cloud-free mosaic.

Queries the swisstopo STAC catalogue (ch.swisstopo.swisseo_s2-sr_v200) for a
given start/end date window and builds a temporal composite from the four
10 m bands (Red B04, Green B03, Blue B02, NIR B08) using the per-scene
"Cloud mask - 10m" asset, following the Sentinel-2 quarterly mosaic
algorithm used by the Copernicus Data Space Ecosystem:
https://documentation.dataspace.copernicus.eu/Data/SentinelMissions/Sentinel2.html#sentinel-2-level-3-quarterly-mosaics

Unlike main_cloudfree_mosaic.py (which stitches the first cloud-free scene
per pixel), this script uses the whole time series: for every pixel and
band it takes the first quartile of the valid-observation distribution,
which drops bright pixels that were misclassified as cloud-free ("first
quartile" is more robust to cloud/haze residuals than a plain minimum or
mean). A pixel with no valid observation in the whole window is written
as no-data.

The STAC catalogue is publicly accessible -- no authentication required.

Algorithm (run independently per pixel and per band B02/B03/B04/B08):
    1. Take the time-range stack of Sentinel-2 L2A observations. Convert
       B02-B08 digital numbers to reflectance.
    2. Mark an observation invalid if "Cloud mask - 10m" is 1 (thick
       cloud), 2 (thin cloud) or 3 (cloud shadow).
    3. Discard invalid observations. The remaining count is written to the
       observations output band (positive integer, 0 = no data).
    4. Sort the valid observations of each band separately.
    5. Take the first-quartile (Q1) value and multiply by 10000 to obtain
       the output digital number.
    6. If there are no valid observations, output -32768 (no-data) for
       every band, and 0 (no-data) for the observations band.

A true-color image (TCI) is then rendered from the resulting B04/B03/B02
mosaic bands using the same enhancement as main_create_rgb.py.

Usage (CLI):
    python main_functions/main_cloudfree_mosaic_csde.py [options]

    Options:
      --start-date DATE   Start of the search window (YYYY-MM-DD). Default: 2025-06-01
      --end-date DATE     End of the search window (YYYY-MM-DD). Default: 2025-07-01
      --quartile Q        Percentile per band. Default: 25 (first quartile). Use 50 for a median mosaic.
      --output PATH       Digital-number mosaic path (auto-generated from params if omitted).
                           The observations count and TCI are written next to it.
      --block-rows N      Row block height used for time-series compositing (memory/speed
                           trade-off -- a full-country stack of all scenes never fits in
                           memory at once). Default: 256. Why --block-rows 2000: the default (256) is tuned to be safe on a modest machine, and processes the full 35841×24343 px grid in ~96 row-blocks. With 63 scenes, peak memory per block is roughly n_scenes × block_rows × width × 6 bytes ≈ 27 GB at block_rows=2000 — comfortable within your 125 GB, and cuts the number of blocks (and therefore the number of separate HTTP range-reads against the STAC assets) down to ~13, which should noticeably speed things up. You have enough RAM to go higher (e.g. 4000 → ~54 GB peak) if you want it faster; I'd stay under ~6000 to leave headroom for GDAL/OS buffers.
      --skip-tci          Do not render the TCI from the resulting mosaic.
      --stac-url URL      STAC catalogue base URL. Default: data.geo.admin.ch
      --collection ID     STAC collection ID. Default: ch.swisstopo.swisseo_s2-sr_v200
      --cloud-mask-title TITLE
                           Asset title of the cloud-mask COGtif. Default: "Cloud mask - 10m"
      --aoi PATH          GeoPackage with area-of-interest polygon. Default: assets/swissboundary_buffer_5000m.gpkg
                           Pass '' or 'none' to disable.

    Examples:
        python main_functions/main_cloudfree_mosaic_csde.py
        python main_functions/main_cloudfree_mosaic_csde.py --start-date 2025-06-01 --end-date 2025-08-31
        python main_functions/main_cloudfree_mosaic_csde.py --start-date 2025-01-01 --end-date 2025-03-31 --quartile 50

Programmatic:
    from main_functions.main_cloudfree_mosaic_csde import create_cloudfree_mosaic_csde
    out = create_cloudfree_mosaic_csde(start_date="2025-06-01", end_date="2025-07-01")
"""

import argparse
import subprocess
import sys
import tempfile
from math import ceil, floor
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main_functions import main_create_rgb  # noqa: E402

# ---------------------------------------------------------------------------
# Catalogue constants
# ---------------------------------------------------------------------------
STAC_BASE_URL    = "https://data.geo.admin.ch/api/stac/v0.9/"
COLLECTION_ID    = "ch.swisstopo.swisseo_s2-sr_v200"
CLOUD_MASK_TITLE = "Cloud mask - 10m"
AOI_GPKG         = Path(__file__).resolve().parent.parent / "assets" / "swissboundary_buffer_5000m.gpkg"

# Sentinel-2 band -> swisstopo STAC asset title (10 m bands only), in output order (R, G, B, NIR)
BAND_ASSET_TITLES = {
    "B04": "Red (band 4) - 10m",
    "B03": "Green (band 3) - 10m",
    "B02": "Blue (band 2) - 10m",
    "B08": "NIR 1 (band 8) - 10m",
}
BAND_ORDER = ["B04", "B03", "B02", "B08"]

# Raw scene digital-number <-> reflectance convention (Sentinel-2 processing
# baseline >= 04.00, +1000 DN offset): reflectance = raw_dn * SCALE + OFFSET.
# Used only to convert the STAC scene assets before compositing.
RAW_DN_SCALE  = 0.0001
RAW_DN_OFFSET = -0.1

# The mosaic's OWN output digital number is plain reflectance * 10000 (no
# baseline offset -- see algorithm step 5), so it decodes with offset 0.
MOSAIC_DN_SCALE  = 0.0001
MOSAIC_DN_OFFSET = 0.0

CLOUD_MASK_INVALID_VALUES = (1, 2, 3)  # thick cloud, thin cloud, cloud shadow

NODATA_DN  = -32768  # int16 no-data for the band digital numbers
NODATA_OBS = 0        # uint16 no-data for the observations count band


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_asset_by_title(item, title: str):
    """Return the first STAC asset whose title matches, or None."""
    for _, asset in item.get_assets().items():
        if getattr(asset, "title", None) == title:
            return asset
    return None


def _write_cog(tmp_path: str, out_path: Path, extra_args: Optional[list] = None) -> bool:
    """Convert a temp GeoTIFF to a Cloud-Optimized GeoTIFF via gdalwarp."""
    gdal_cmd = [
        "gdalwarp",
        "-of", "COG",
        "-co", "BIGTIFF=YES",
        "-co", "NUM_THREADS=ALL_CPUS",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
        "-overwrite",
    ] + (extra_args or []) + [tmp_path, str(out_path)]

    result = subprocess.run(gdal_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] gdalwarp failed:\n{result.stderr}")
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_cloudfree_mosaic_csde(
    start_date: str = "2025-06-01",
    end_date: str = "2025-07-01",
    quartile: float = 25.0,
    output_name: Optional[str] = None,
    block_rows: int = 256,
    create_tci: bool = True,
    stac_url: str = STAC_BASE_URL,
    collection_id: str = COLLECTION_ID,
    cloud_mask_title: str = CLOUD_MASK_TITLE,
    aoi_gpkg: Optional[Union[str, Path]] = AOI_GPKG,
) -> Optional[Path]:
    """
    Build a first-quartile cloud-free mosaic from swisstopo STAC Sentinel-2 assets.

    See the module docstring for the full algorithm description.

    Parameters
    ----------
    start_date, end_date : str
        Search window (YYYY-MM-DD), inclusive.
    quartile : float
        Percentile taken per pixel/band from the valid-observation
        distribution. 25 = first quartile (default, per the CDSE
        algorithm). Use 50 for a median mosaic.
    output_name : str or None
        Path of the digital-number mosaic GeoTIFF. Auto-generated from
        parameters if None. The observations count and TCI files are
        written next to it.
    block_rows : int
        Row block height used while compositing the time series. The full
        country extent never fits all scenes in memory at once, so the
        grid is processed in horizontal strips of this height. Lower it
        if the process runs out of memory; raise it (fewer, bigger
        blocks) for speed if memory allows.
    create_tci : bool
        Render a true-color image from the resulting B04/B03/B02 mosaic
        bands via main_create_rgb.create_enhanced_rgb.
    stac_url, collection_id, cloud_mask_title, aoi_gpkg :
        Same meaning as in main_cloudfree_mosaic.py.

    Returns
    -------
    Path or None
        Path to the digital-number mosaic GeoTIFF, or None when no items
        were found or usable.
    """
    print("=" * 60)
    print("Cloud-free mosaic (first-quartile, CDSE-style)")
    print(f"  Date range    : {start_date} → {end_date}")
    print(f"  Bands         : {BAND_ORDER}")
    print(f"  Percentile    : {quartile}")
    print(f"  STAC          : {stac_url}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # STAC search
    # ------------------------------------------------------------------
    client = pystac_client.Client.open(stac_url)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")

    datetime_str = f"{start_date}T00:00:00Z/{end_date}T23:59:59Z"
    search = client.search(collections=[collection_id], datetime=datetime_str, max_items=None)
    items = list(search.items())
    print(f"\nFound {len(items)} STAC items in date window.\n")

    if not items:
        print("No items found — aborting.")
        return None

    # Keep only items that carry all four bands and the cloud mask
    valid_items = []
    for item in items:
        assets = {band: _find_asset_by_title(item, title) for band, title in BAND_ASSET_TITLES.items()}
        cloud_asset = _find_asset_by_title(item, cloud_mask_title)
        missing = [band for band, asset in assets.items() if asset is None]
        if cloud_asset is None:
            missing.append("cloud mask")
        if missing:
            print(f"  Skip {item.id}: missing {', '.join(missing)}")
            continue
        valid_items.append((item, assets, cloud_asset))

    if not valid_items:
        print("No usable items in the date range.")
        return None

    print(f"Using {len(valid_items)} of {len(items)} scenes.\n")

    # ------------------------------------------------------------------
    # Reference grid: TAP-aligned pixel grid spanning the AOI (or the
    # first usable item's extent), same approach as main_cloudfree_mosaic.py.
    # ------------------------------------------------------------------
    ref_href = valid_items[0][1]["B04"].href
    with rasterio.open(ref_href) as ref_ds:
        ref_crs = ref_ds.crs

    PIXEL = 10.0
    if aoi_gpkg is not None and Path(aoi_gpkg).exists():
        gdf = gpd.read_file(aoi_gpkg).to_crs(ref_crs)
        bnd = gdf.total_bounds
        gpkg_name = Path(aoi_gpkg).name
    else:
        with rasterio.open(ref_href) as ref_ds:
            b = ref_ds.bounds
        bnd = (b.left, b.bottom, b.right, b.top)
        gdf = None
        gpkg_name = None

    minx = floor(bnd[0] / PIXEL) * PIXEL
    miny = floor(bnd[1] / PIXEL) * PIXEL
    maxx = ceil(bnd[2] / PIXEL) * PIXEL
    maxy = ceil(bnd[3] / PIXEL) * PIXEL
    width  = int(round((maxx - minx) / PIXEL))
    height = int(round((maxy - miny) / PIXEL))
    ref_transform = from_origin(minx, maxy, PIXEL, PIXEL)

    print(f"  Reference grid: {height} × {width} px")
    print(f"  CRS           : {ref_crs.to_epsg()}")
    print(f"  Extent        : ({minx:.0f}, {miny:.0f}) → ({maxx:.0f}, {maxy:.0f})")

    if gdf is not None:
        aoi_mask_full = ~geometry_mask(gdf.geometry, transform=ref_transform, invert=False, out_shape=(height, width))
        print(f"  AOI           : {gpkg_name}  ({int(aoi_mask_full.sum()):,} px within boundary)")
    else:
        aoi_mask_full = None
        print("  AOI           : none (full bounding box)")

    # ------------------------------------------------------------------
    # Open every scene's band + cloud-mask asset as a WarpedVRT aligned to
    # the reference grid. Opening is lazy (no data is fetched yet), so
    # keeping all scenes open for the whole run is cheap.
    # ------------------------------------------------------------------
    warp_kwargs = dict(crs=ref_crs, transform=ref_transform, width=width, height=height, resampling=Resampling.nearest)

    print("\nOpening scene rasters ...")
    scene_handles = []
    raw_datasets = []
    for item, assets, cloud_asset in valid_items:
        try:
            raw_cloud = rasterio.open(cloud_asset.href)
            entry = {"cloud": WarpedVRT(raw_cloud, **warp_kwargs)}
            raw_datasets.append(raw_cloud)
            for band, asset in assets.items():
                raw_band = rasterio.open(asset.href)
                entry[band] = WarpedVRT(raw_band, **warp_kwargs)
                raw_datasets.append(raw_band)
        except Exception as exc:
            print(f"  [warn] Could not open {item.id}: {exc} — skipping.")
            continue
        scene_handles.append(entry)

    if not scene_handles:
        print("No scenes could be opened.")
        return None

    n_scenes = len(scene_handles)
    print(f"{n_scenes} scenes ready for compositing.\n")

    # ------------------------------------------------------------------
    # Build output path(s)
    # ------------------------------------------------------------------
    if output_name:
        dn_path = Path(output_name)
    else:
        dn_path = Path(f"mosaic_csde_{start_date}_{end_date}_q{int(quartile)}.tif")
    dn_path.parent.mkdir(parents=True, exist_ok=True)
    obs_path = dn_path.with_name(dn_path.stem + "_observations.tif")
    tci_path = dn_path.with_name(dn_path.stem + "_tci.tif")

    # ------------------------------------------------------------------
    # Composite the time series block by block (a full-country stack of
    # all scenes at once does not fit in memory), writing straight into
    # temp GeoTIFFs that get converted to COGs at the end.
    # ------------------------------------------------------------------
    dn_profile = dict(
        driver="GTiff", width=width, height=height, count=len(BAND_ORDER), dtype="int16",
        crs=ref_crs, transform=ref_transform, nodata=NODATA_DN,
        tiled=True, blockxsize=512, blockysize=512,
        compress="deflate", predictor=2, bigtiff="YES",
    )
    obs_profile = dict(dn_profile, count=1, dtype="uint16", nodata=NODATA_OBS)

    with tempfile.NamedTemporaryFile(suffix="_dn_tmp.tif", delete=False) as fh:
        dn_tmp_path = fh.name
    with tempfile.NamedTemporaryFile(suffix="_obs_tmp.tif", delete=False) as fh:
        obs_tmp_path = fh.name

    try:
        with rasterio.open(dn_tmp_path, "w", **dn_profile) as dn_dst, \
             rasterio.open(obs_tmp_path, "w", **obs_profile) as obs_dst:

            n_blocks = ceil(height / block_rows)
            for block_idx in range(n_blocks):
                row0 = block_idx * block_rows
                row1 = min(row0 + block_rows, height)
                block_h = row1 - row0
                window = Window(0, row0, width, block_h)

                print(f"  Block {block_idx + 1:3d}/{n_blocks}  rows {row0}-{row1}")

                # Cloud-mask validity (shared by every band)
                cloud_valid_stack = np.empty((n_scenes, block_h, width), dtype=bool)
                for i, handles in enumerate(scene_handles):
                    cloud_block = handles["cloud"].read(1, window=window)
                    cloud_valid_stack[i] = ~np.isin(cloud_block, CLOUD_MASK_INVALID_VALUES)

                # A scene only really "observes" a pixel where at least one band
                # has data there (a digital number of 0 marks no-data outside a
                # scene's own footprint) -- accumulated while reading each band.
                footprint_stack = np.zeros((n_scenes, block_h, width), dtype=bool)

                # Per band: reflectance, quartile across valid observations
                band_dn = {}
                for band_idx, band in enumerate(BAND_ORDER, start=1):
                    band_stack = np.empty((n_scenes, block_h, width), dtype=np.float32)
                    for i, handles in enumerate(scene_handles):
                        band_stack[i] = handles[band].read(1, window=window).astype(np.float32)

                    has_data = band_stack > 0
                    footprint_stack |= has_data
                    band_valid = cloud_valid_stack & has_data
                    reflectance = band_stack * RAW_DN_SCALE + RAW_DN_OFFSET
                    reflectance[~band_valid] = np.nan

                    with np.errstate(all="ignore"):
                        q1 = np.nanpercentile(reflectance, quartile, axis=0)

                    dn = np.where(np.isnan(q1), NODATA_DN, np.round(q1 * 10000))
                    band_dn[band_idx] = np.clip(dn, -32767, 32767).astype("int16")

                # A pixel counts as observed only where a scene both cleared the
                # cloud mask AND actually covered it (see footprint_stack above).
                observations = (cloud_valid_stack & footprint_stack).sum(axis=0).astype("uint16")
                if aoi_mask_full is not None:
                    observations[~aoi_mask_full[row0:row1, :]] = NODATA_OBS
                obs_dst.write(observations, window=window, indexes=1)

                for band_idx, dn in band_dn.items():
                    dn[observations == NODATA_OBS] = NODATA_DN
                    if aoi_mask_full is not None:
                        dn[~aoi_mask_full[row0:row1, :]] = NODATA_DN
                    dn_dst.write(dn, window=window, indexes=band_idx)

        print("\nWriting COGs via gdalwarp ...")
        dn_ok = _write_cog(
            dn_tmp_path, dn_path,
            extra_args=["-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2"],
        )
        obs_ok = _write_cog(
            obs_tmp_path, obs_path,
            extra_args=["-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2"],
        )
    finally:
        for entry in scene_handles:
            for handle in entry.values():
                handle.close()
        for raw in raw_datasets:
            raw.close()
        Path(dn_tmp_path).unlink(missing_ok=True)
        Path(obs_tmp_path).unlink(missing_ok=True)

    if not dn_ok:
        print("Done. Digital-number mosaic failed to write.")
        return None

    print(f"\n  Mosaic written      : {dn_path}")
    if obs_ok:
        print(f"  Observations written: {obs_path}")

    # ------------------------------------------------------------------
    # TCI: reuse main_create_rgb's enhancement, fed with the mosaic's own
    # B04/B03/B02 bands.
    # ------------------------------------------------------------------
    if create_tci:
        print("\nRendering TCI from mosaic bands ...")
        tmp_dir = Path(tempfile.mkdtemp(prefix="mosaic_csde_tci_"))
        try:
            band_files = {}
            for band_idx, band in enumerate(("B04", "B03", "B02"), start=1):
                band_tmp = tmp_dir / f"{band}.tif"
                extract_cmd = [
                    "gdal_translate", "-b", str(band_idx),
                    "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
                    str(dn_path), str(band_tmp),
                ]
                result = subprocess.run(extract_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"    [error] gdal_translate failed for {band}:\n{result.stderr}")
                    band_files = None
                    break
                band_files[band] = band_tmp

            if band_files and aoi_gpkg is not None and Path(aoi_gpkg).exists():
                main_create_rgb.create_enhanced_rgb(
                    b04_path=band_files["B04"],
                    b03_path=band_files["B03"],
                    b02_path=band_files["B02"],
                    clip_orbit=aoi_gpkg,
                    output_path=tci_path,
                    scale=MOSAIC_DN_SCALE,
                    offset=MOSAIC_DN_OFFSET,
                    create_cog=True,
                )
                print(f"  TCI written         : {tci_path}")
            elif band_files:
                print("    [warn] No AOI GeoPackage available — skipping TCI (create_enhanced_rgb needs one).")
        finally:
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)

    return dn_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a first-quartile cloud-free temporal mosaic (CDSE quarterly-mosaic "
            "style) from the swisstopo STAC Sentinel-2 catalogue, plus a TCI."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start-date", default="2025-06-01", help="Start of the search window (YYYY-MM-DD).")
    parser.add_argument("--end-date", default="2025-07-01", help="End of the search window (YYYY-MM-DD).")
    parser.add_argument(
        "--quartile", type=float, default=25.0, metavar="Q",
        help="Percentile taken per pixel/band. 25 = first quartile. Use 50 for a median mosaic.",
    )
    parser.add_argument("--output", default=None, metavar="PATH", help="Digital-number mosaic path (auto-generated if omitted).")
    parser.add_argument(
        "--block-rows", type=int, default=256, metavar="N",
        help="Row block height used for time-series compositing (memory/speed trade-off).",
    )
    parser.add_argument("--skip-tci", action="store_true", help="Do not render the TCI from the resulting mosaic.")
    parser.add_argument("--stac-url", default=STAC_BASE_URL, help="STAC catalogue base URL.")
    parser.add_argument("--collection", default=COLLECTION_ID, help="STAC collection ID.")
    parser.add_argument("--cloud-mask-title", default=CLOUD_MASK_TITLE, help="Asset title of the cloud-mask COGtif.")
    parser.add_argument(
        "--aoi", default=str(AOI_GPKG), metavar="PATH",
        help="GeoPackage with the area-of-interest polygon. Pass '' or 'none' to disable.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    aoi = None if args.aoi.lower() in ("", "none") else args.aoi

    create_cloudfree_mosaic_csde(
        start_date=args.start_date,
        end_date=args.end_date,
        quartile=args.quartile,
        output_name=args.output,
        block_rows=args.block_rows,
        create_tci=not args.skip_tci,
        stac_url=args.stac_url,
        collection_id=args.collection,
        cloud_mask_title=args.cloud_mask_title,
        aoi_gpkg=aoi,
    )


if __name__ == "__main__":
    main()
