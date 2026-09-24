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
    1. Take the time-range stack of Sentinel-2 L2A observations.
    2. Mark an observation invalid if "Cloud mask - 10m" is 1 (thick
       cloud), 2 (thin cloud) or 3 (cloud shadow), or if the band itself is
       no-data (0) because the scene does not cover that pixel.
    3. Discard invalid observations. The remaining count is written to the
       "Observation - 10m" output (positive integer, 0 = no data).
    4. Sort the valid observations of each band separately.
    5. Take the first-quartile (Q1) value as the output digital number.
    6. If there are no valid observations, output 0 (no-data) for every
       band and for the observations count.

Outputs -- one file per band, following step1_processor_s2_sr.py's naming
convention (<stem>_<band>_10m.tif):
    <stem>_b04_10m.tif   "Red (band 4) - 10m"
    <stem>_b03_10m.tif   "Green (band 3) - 10m"
    <stem>_b02_10m.tif   "Blue (band 2) - 10m"
    <stem>_b08_10m.tif   "NIR 1 (band 8) - 10m"
    <stem>_observation_10m.tif  "Observation - 10m"  (valid-observation count)
    <stem>_tci_10m.tif   "True color image - 10m"

The band mosaics keep the same encoding as the swissEO S2-SR scene assets
they are built from -- uint16, no-data 0, reflectance = dn * 0.0001 - 0.1 --
so they are directly interchangeable with the step1 band outputs. The scale
and offset are also recorded on the band itself. Because the encoding is
preserved, the percentile is taken on raw digital numbers: an affine,
strictly increasing transform commutes with percentile selection, so
converting to reflectance and back would cancel out exactly.

A true-color image (TCI) is then rendered from the resulting B04/B03/B02
band files using the same enhancement as main_create_rgb.py.

Usage (CLI):
    python main_functions/main_cloudfree_mosaic_csde.py [options]

    Options:
      --start-date DATE   Start of the search window (YYYY-MM-DD). Default: 2025-06-01
      --end-date DATE     End of the search window (YYYY-MM-DD). Default: 2025-07-01
      --quartile Q        Percentile per band. Default: 25 (first quartile). Use 50 for a median mosaic.
      --output PATH       Output base path (auto-generated from params if omitted). The
                           per-band, observation and TCI files are derived from its stem.
      --block-rows N      Row block height used for time-series compositing (memory/speed
                           trade-off -- a full-country stack of all scenes never fits in
                           memory at once). Default: 256.
      --workers N         Scenes read in parallel per band/block via a thread pool
                           (each scene read is a separate STAC HTTP request -- this is
                           what actually uses a many-core machine). Default: 16.
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
        python3 main_functions/main_cloudfree_mosaic_csde.py   --start-date 2025-07-01 --end-date 2025-08-31   --block-rows 2500 --workers 16   --output temp/

Programmatic:
    from main_functions.main_cloudfree_mosaic_csde import create_cloudfree_mosaic_csde
    out = create_cloudfree_mosaic_csde(start_date="2025-06-01", end_date="2025-07-01")
"""

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
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

# Sentinel-2 band -> swisstopo STAC asset title (10 m bands only), in output order (R, G, B, NIR).
# The same titles are used for the mosaic's own per-band output assets.
BAND_ASSET_TITLES = {
    "B04": "Red (band 4) - 10m",
    "B03": "Green (band 3) - 10m",
    "B02": "Blue (band 2) - 10m",
    "B08": "NIR 1 (band 8) - 10m",
}
BAND_ORDER = ["B04", "B03", "B02", "B08"]

# Filename suffixes, matching step1_processor_s2_sr.py's convention
# (<stem>_mosaic_<timestamp>_<band>_<resolution>m.tif -> one file per band).
BAND_FILE_SUFFIX = {band: band.lower() for band in BAND_ORDER}
OBSERVATION_SUFFIX = "observation"
OBSERVATION_TITLE  = "Observation - 10m"
TCI_SUFFIX = "tci"
TCI_TITLE  = "True color image - 10m"

# Reflectance convention of the swissEO S2-SR band assets, both the scene
# assets read from STAC and the mosaic bands written here (verified against
# the catalogue: uint16, nodata 0): reflectance = dn * SCALE + OFFSET.
# Because the mosaic keeps this same encoding, the percentile can be taken on
# raw digital numbers directly -- an affine, strictly increasing transform
# commutes with percentile selection and linear interpolation, so converting
# to reflectance and back would be an exact no-op costing two full passes
# over a multi-GB stack.
DN_SCALE  = 0.0001
DN_OFFSET = -0.1

CLOUD_MASK_INVALID_VALUES = (1, 2, 3)  # thick cloud, thin cloud, cloud shadow

NODATA_DN  = 0  # uint16 no-data for the band digital numbers (matches the source assets)
NODATA_OBS = 0  # uint16 no-data for the observations count band
MAX_DN     = 65535


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
    """
    Convert a temp GeoTIFF to a Cloud-Optimized GeoTIFF.

    gdal_translate rather than gdalwarp: the temp file is already on the
    target grid, so this is a pure format conversion, and translate carries
    the band's scale/offset and nodata through unchanged.
    """
    gdal_cmd = [
        "gdal_translate",
        "-of", "COG",
        "-co", "BIGTIFF=YES",
        "-co", "NUM_THREADS=ALL_CPUS",
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
    ] + (extra_args or []) + [tmp_path, str(out_path)]

    result = subprocess.run(gdal_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [error] gdal_translate failed:\n{result.stderr}")
        return False
    return True


def _nanpercentile_along_axis0(stack: np.ndarray, quartile: float, valid_count: np.ndarray) -> np.ndarray:
    """
    Percentile along axis 0 of `stack`, ignoring NaNs, at country-grid scale.

    np.nanpercentile's per-pixel-variable-count handling makes it 30-60x
    slower than a plain sort/gather here (its cost scales with the number
    of *output* pixels, not the stack size -- measured ~70s for a block a
    fraction of the real width, vs ~2s below, identical results). NaN
    already sorts to the end ascending, so a plain in-place sort plus a
    take_along_axis gather reproduces numpy's default linear-interpolation
    percentile exactly, using `valid_count` (already computed by the
    caller) instead of re-deriving it from NaNs.
    """
    stack.sort(axis=0)  # in-place; NaNs move to the end
    idx = (quartile / 100.0) * (valid_count - 1)
    idx = np.clip(idx, 0, stack.shape[0] - 1)  # valid_count==0 rows are overwritten by the caller
    lo = np.floor(idx).astype(np.intp)
    hi = np.ceil(idx).astype(np.intp)
    frac = (idx - lo).astype(stack.dtype)
    lo_val = np.take_along_axis(stack, lo[None, :, :], axis=0)[0]
    hi_val = np.take_along_axis(stack, hi[None, :, :], axis=0)[0]
    return lo_val * (1 - frac) + hi_val * frac


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_cloudfree_mosaic_csde(
    start_date: str = "2025-06-01",
    end_date: str = "2025-07-01",
    quartile: float = 25.0,
    output_name: Optional[str] = None,
    block_rows: int = 256,
    max_workers: int = 16,
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
        Output base path; its stem drives the per-band, observation and TCI
        filenames (see the module docstring). Auto-generated from the
        parameters if None.
    block_rows : int
        Row block height used while compositing the time series. The full
        country extent never fits all scenes in memory at once, so the
        grid is processed in horizontal strips of this height. Lower it
        if the process runs out of memory; raise it (fewer, bigger
        blocks) for speed if memory allows.
    max_workers : int
        Number of scenes read in parallel per band/block via a thread
        pool. Each scene read is a separate HTTP request against the STAC
        assets, so this is what actually keeps a many-core machine busy
        (the numpy math itself is single-threaded). Raise it on a fast
        connection; lower it if the STAC server starts throttling.
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
    print(f"  Block rows    : {block_rows}")
    print(f"  Max workers   : {max_workers}")
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
    # Collect each scene's band + cloud-mask asset URLs. Each parallel
    # worker opens its own rasterio dataset + WarpedVRT per read (see
    # _read_scenes_into below) rather than reusing one shared across
    # threads -- GDAL datasets opened in one thread and read from another
    # are not a supported pattern and caused the compositing loop to
    # effectively hang under a thread pool.
    # ------------------------------------------------------------------
    warp_kwargs = dict(crs=ref_crs, transform=ref_transform, width=width, height=height, resampling=Resampling.nearest)

    scene_hrefs = [
        {"cloud": cloud_asset.href, **{band: asset.href for band, asset in assets.items()}}
        for _, assets, cloud_asset in valid_items
    ]
    n_scenes = len(scene_hrefs)
    print(f"{n_scenes} scenes ready for compositing.\n")

    # ------------------------------------------------------------------
    # Build output paths -- one file per band, following
    # step1_processor_s2_sr.py's <stem>_<band>_<resolution>m.tif convention.
    # ------------------------------------------------------------------
    if output_name:
        base_path = Path(output_name)
    else:
        base_path = Path(f"mosaic_csde_{start_date}_{end_date}_q{int(quartile)}.tif")
    base_path.parent.mkdir(parents=True, exist_ok=True)
    stem = base_path.with_suffix("")

    band_paths = {
        band: stem.with_name(f"{stem.name}_{BAND_FILE_SUFFIX[band]}_10m.tif")
        for band in BAND_ORDER
    }
    obs_path = stem.with_name(f"{stem.name}_{OBSERVATION_SUFFIX}_10m.tif")
    tci_path = stem.with_name(f"{stem.name}_{TCI_SUFFIX}_10m.tif")

    # ------------------------------------------------------------------
    # Composite the time series block by block (a full-country stack of
    # all scenes at once does not fit in memory), writing straight into
    # temp GeoTIFFs that get converted to COGs at the end.
    # ------------------------------------------------------------------
    dn_profile = dict(
        driver="GTiff", width=width, height=height, count=1, dtype="uint16",
        crs=ref_crs, transform=ref_transform, nodata=NODATA_DN,
        tiled=True, blockxsize=512, blockysize=512,
        compress="deflate", predictor=2, bigtiff="YES",
    )
    obs_profile = dict(dn_profile, nodata=NODATA_OBS)

    band_tmp_paths = {}
    for band in BAND_ORDER:
        with tempfile.NamedTemporaryFile(suffix=f"_{band}_tmp.tif", delete=False) as fh:
            band_tmp_paths[band] = fh.name
    with tempfile.NamedTemporaryFile(suffix="_obs_tmp.tif", delete=False) as fh:
        obs_tmp_path = fh.name

    band_dst = {}
    try:
        with contextlib.ExitStack() as stack, \
             rasterio.open(obs_tmp_path, "w", **obs_profile) as obs_dst, \
             ThreadPoolExecutor(max_workers=max_workers) as pool:

            for band in BAND_ORDER:
                dst = stack.enter_context(rasterio.open(band_tmp_paths[band], "w", **dn_profile))
                # Record the reflectance convention on the band itself, so the
                # output is self-describing: reflectance = dn * scale + offset.
                dst.scales = (DN_SCALE,)
                dst.offsets = (DN_OFFSET,)
                dst.update_tags(1, data_ignore_value=str(NODATA_DN))
                band_dst[band] = dst

            def _read_scenes_into(dest: np.ndarray, key: str, window: Window) -> None:
                """Read `window` from every scene's `key` asset into dest[i], in
                parallel -- each scene is its own HTTP request against the STAC
                asset, so this is the actual place a many-core machine helps.
                Each task opens its own dataset + WarpedVRT rather than sharing
                one across threads (GDAL datasets are not safe to open in one
                thread and read from another)."""
                def _read_one(i_href):
                    i, href = i_href
                    # Cloud-optimized reads over HTTP: skip GDAL's default
                    # sidecar-file probing (.aux.xml/.ovr/directory listing) and
                    # existence-check HEAD request -- each fresh open otherwise
                    # costs 2-3 extra round trips before any data is fetched,
                    # which dominates wall time once opens are this frequent.
                    # GDAL_CACHEMAX capped low: with a fresh dataset opened per
                    # read (thousands over a full run), an unbounded/default
                    # (RAM-percentage-scaled) block cache accumulates across all
                    # of them and competes with our own multi-GB numpy arrays --
                    # a plausible cause of block-over-block slowdown from memory
                    # pressure. We read each window once and never revisit it,
                    # so caching buys nothing here anyway.
                    with rasterio.Env(
                        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                        CPL_VSIL_CURL_USE_HEAD="NO",
                        GDAL_HTTP_MULTIPLEX="YES",
                        GDAL_CACHEMAX=128,
                        VSI_CACHE="FALSE",
                    ), rasterio.open(href) as raw, WarpedVRT(raw, **warp_kwargs) as vrt:
                        dest[i] = vrt.read(1, window=window)
                list(pool.map(_read_one, enumerate(entry[key] for entry in scene_hrefs)))

            n_blocks = ceil(height / block_rows)
            for block_idx in range(n_blocks):
                row0 = block_idx * block_rows
                row1 = min(row0 + block_rows, height)
                block_h = row1 - row0
                window = Window(0, row0, width, block_h)

                block_t0 = time.time()
                print(f"  Block {block_idx + 1:3d}/{n_blocks}  rows {row0}-{row1}")

                # Cloud-mask validity (shared by every band)
                t0 = time.time()
                cloud_blocks = np.empty((n_scenes, block_h, width), dtype=np.uint8)
                _read_scenes_into(cloud_blocks, "cloud", window)
                print(f"    cloud read   : {time.time() - t0:6.1f}s")
                cloud_valid_stack = ~np.isin(cloud_blocks, CLOUD_MASK_INVALID_VALUES)
                del cloud_blocks

                # A scene only really "observes" a pixel where at least one band
                # has data there (a digital number of 0 marks no-data outside a
                # scene's own footprint) -- accumulated while reading each band.
                footprint_stack = np.zeros((n_scenes, block_h, width), dtype=bool)

                # Per band: quartile across valid observations. The percentile
                # runs on raw digital numbers -- the mosaic keeps the source
                # encoding, so converting to reflectance and back would cancel
                # out exactly (see DN_SCALE/DN_OFFSET).
                band_dn = {}
                for band in BAND_ORDER:
                    band_stack = np.empty((n_scenes, block_h, width), dtype=np.float32)
                    t0 = time.time()
                    _read_scenes_into(band_stack, band, window)
                    print(f"    {band} read     : {time.time() - t0:6.1f}s")

                    t0 = time.time()
                    has_data = band_stack > NODATA_DN
                    footprint_stack |= has_data
                    band_valid = cloud_valid_stack & has_data
                    band_stack[~band_valid] = np.nan
                    band_valid_count = band_valid.sum(axis=0)
                    print(f"    {band} prep     : {time.time() - t0:6.1f}s")

                    t0 = time.time()
                    with np.errstate(all="ignore"):
                        q1 = _nanpercentile_along_axis0(band_stack, quartile, band_valid_count)
                    print(f"    {band} percentile: {time.time() - t0:6.1f}s")

                    # uint16 with 0 as no-data, so a valid pixel never rounds to 0.
                    dn = np.clip(np.round(q1), 1, MAX_DN)
                    dn = np.where(band_valid_count == 0, NODATA_DN, dn)
                    band_dn[band] = dn.astype("uint16")

                # A pixel counts as observed only where a scene both cleared the
                # cloud mask AND actually covered it (see footprint_stack above).
                observations = (cloud_valid_stack & footprint_stack).sum(axis=0).astype("uint16")
                if aoi_mask_full is not None:
                    observations[~aoi_mask_full[row0:row1, :]] = NODATA_OBS
                obs_dst.write(observations, window=window, indexes=1)

                for band, dn in band_dn.items():
                    dn[observations == NODATA_OBS] = NODATA_DN
                    if aoi_mask_full is not None:
                        dn[~aoi_mask_full[row0:row1, :]] = NODATA_DN
                    band_dst[band].write(dn, window=window, indexes=1)

                print(f"    block total  : {time.time() - block_t0:6.1f}s")

        print("\nWriting COGs ...")
        written = {}
        for band in BAND_ORDER:
            if _write_cog(band_tmp_paths[band], band_paths[band]):
                written[band] = band_paths[band]
        obs_ok = _write_cog(obs_tmp_path, obs_path)
    finally:
        for tmp in band_tmp_paths.values():
            Path(tmp).unlink(missing_ok=True)
        Path(obs_tmp_path).unlink(missing_ok=True)

    if len(written) != len(BAND_ORDER):
        print("Done. Not all band mosaics were written.")
        return None

    for band in BAND_ORDER:
        print(f"  {BAND_ASSET_TITLES[band]:<24}: {band_paths[band]}")
    if obs_ok:
        print(f"  {OBSERVATION_TITLE:<24}: {obs_path}")

    # ------------------------------------------------------------------
    # TCI: reuse main_create_rgb's enhancement, fed with the mosaic's own
    # B04/B03/B02 band files (same encoding as the scene assets it normally
    # consumes, so the standard scale/offset apply unchanged).
    # ------------------------------------------------------------------
    if create_tci:
        if aoi_gpkg is not None and Path(aoi_gpkg).exists():
            print("\nRendering TCI from mosaic bands ...")
            main_create_rgb.create_enhanced_rgb(
                b04_path=band_paths["B04"],
                b03_path=band_paths["B03"],
                b02_path=band_paths["B02"],
                clip_orbit=aoi_gpkg,
                output_path=tci_path,
                scale=DN_SCALE,
                offset=DN_OFFSET,
                create_cog=True,
            )
            print(f"  {TCI_TITLE:<24}: {tci_path}")
        else:
            print("\n[warn] No AOI GeoPackage available — skipping TCI (create_enhanced_rgb needs one).")

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)

    return band_paths["B04"]


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
    parser.add_argument(
        "--workers", type=int, default=16, metavar="N",
        help="Scenes read in parallel per band/block (each is a separate STAC HTTP request).",
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
        max_workers=args.workers,
        create_tci=not args.skip_tci,
        stac_url=args.stac_url,
        collection_id=args.collection,
        cloud_mask_title=args.cloud_mask_title,
        aoi_gpkg=aoi,
    )


if __name__ == "__main__":
    main()
