# Step 4: builds the reference statistics of a vegetation index (NDVI by default) from Landsat 5, 7 and 8,
# for the reference period 1991-2020. It replaces the GEE script.
#
# For every Landsat scene:
# 1. The bands are read on the 30 m EPSG:2056 grid (nearest neighbour, as in step 1).
# 2. Clouds, cloud shadow and snow are masked with the Landsat QA band, snow also with NDSI (water exempt).
#    These are the same masks as in the sensor comparison of step 1. There is no blue band filter.
# 3. Terrain shadow is masked with HORAYZON, the same code that swissEO S2-SR uses.
# 4. The bands of the index get the SCSc topographic correction of the GEE script (slopes of 5 degrees and more).
# 5. The index is calculated and the correction curve of step 3 is added (sensor group and season of the scene).
# 6. The result is written to a cache file, one per scene.
#
# For every output file (for example NDVI_Stats_DOY180.tif):
# 7. All scenes in the time window are collected (DOY: 7 days on each side, M: one month,
#    MM: the month and the month before).
#    Scenes from the same day and the same overpass (same path, neighbouring rows) count as one observation.
# 8. Per pixel: min, max, mean, median, std, p05, p95 and the number of observations.
#    M and MM files also get meanAD and medianAD, like the current M and MM files (see get_stat_names).
# 9. The file is written as a Cloud Optimized GeoTIFF (COG). The list of scenes used is in the metadata tag
#    "scene_list". The overviews are made after all statistics are calculated, so they never enter a calculation.
# 10. The file is uploaded to S3 and deleted locally (UPLOAD_TO_S3). DOY 366 is also stored as DOY 000.
#
# The output files are processed in order. A scene is processed once, when its day first enters the time window,
# and its cache file is deleted after the last output file that needs it. So only about 15 days of scenes are
# on the disk at the same time (roughly 5-10 GB).
#
# The script can be stopped and started again: output files already on S3 (or locally, without upload)
# and cached scenes are not processed again.
#
# Usage: python util/ref_ndvi_landsat/step4_build_reference.py            builds the files in REFERENCE_PERIODS of the config
#        python util/ref_ndvi_landsat/step4_build_reference.py 180        builds only DOY 180
#        python util/ref_ndvi_landsat/step4_build_reference.py 180 181    builds DOY 180 and 181
#        python util/ref_ndvi_landsat/step4_build_reference.py MM 3       builds only MM03 (February and March)
#        python util/ref_ndvi_landsat/step4_build_reference.py MM M       builds all 12 MM and all 12 M files in one run
# Without DOY, M or MM at the start, TIME_STEP of vi_reference_config.py is used.
# The numbers are days of the year for DOY and months for M and MM.

# On Windows, the GDAL Python bindings (used by the HORAYZON terrain module of the main pipeline) crash
# if they are imported after rasterio. So they are imported first, even though this script does not use them.
from osgeo import gdal  # noqa: F401

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime, timezone

import boto3
import numpy as np
import pandas as pd
import rasterio
from botocore.exceptions import ClientError
from pystac_client import Client
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from shapely import wkt
from shapely.geometry import shape

import vi_reference_config as config
from step1_collect_matchups import get_grid_window, get_landsat_token, parse_time, passes_landsat_quality, read_static, warp_landsat_band

sys.path.insert(0, os.path.join(config.REPO_DIR, "main_functions"))
import horayzon  # noqa: E402
from main_terrain_module import SonnenWinkel  # noqa: E402

CACHE_SCALE = 10000  # the cached index is stored as int16: index * CACHE_SCALE
CACHE_NODATA = -32768
OUTPUT_NODATA = 255
METADATA_ASSET = "mtl.xml"


##############################
# SETTINGS THAT DEPEND ON THE INDEX
def get_index_bands():
    """Return the Landsat asset names of the two bands of the index: (band_a, band_b)."""
    index = config.INDICES[config.INDEX_NAME]
    return config.BANDS[index["band_a"]]["landsat"], config.BANDS[index["band_b"]]["landsat"]


def get_landsat_assets():
    """All Landsat assets that are read: the index bands, green and swir16 for the NDSI snow mask, and the QA bands."""
    assets = list(get_index_bands())
    for name in ["green", "swir16", "qa_pixel", "qa_radsat"]:
        if name not in assets:
            assets.append(name)
    return assets


##############################
# TIME WINDOWS
def get_slot(day):
    """The time unit a scene belongs to: its day of the year for DOY, its month for M and MM."""
    date = datetime.strptime(day, "%Y-%m-%d")
    if config.TIME_STEP == "DOY":
        return date.timetuple().tm_yday
    return date.month


def get_window_slots(period):
    """All slots that go into one output file."""
    if config.TIME_STEP == "DOY":
        # 366 days, so that the window wraps around the end of the year (DOY 1 also uses DOY 360-366)
        return [(period - 1 + shift) % 366 + 1 for shift in range(-config.HALF_WINDOW_DAYS, config.HALF_WINDOW_DAYS + 1)]
    if config.TIME_STEP == "M":
        return [period]
    if config.TIME_STEP == "MM":
        # Two calendar months: the month itself and the one before (MM03 = February and March, MM01 = December and January)
        return [(period - 2) % 12 + 1, period]
    raise ValueError(f"Unknown TIME_STEP {config.TIME_STEP}")


def get_stat_names():
    """The statistics in the output file, in the band order of the current reference files.
    The current M and MM files also have meanAD (mean of |value - mean|) and medianAD (median of |value - median|).
    The number of observations is added as the last band."""
    if config.TIME_STEP == "DOY":
        return ["min", "max", "mean", "median", "std", "p05", "p95"]
    return ["min", "max", "mean", "median", "std", "meanAD", "medianAD", "p05", "p95"]


def get_output_file(period):
    if config.TIME_STEP == "DOY":
        name = f"{config.INDEX_NAME}_Stats_DOY{period:03d}.tif"
    else:
        name = f"{config.INDEX_NAME}_Stats_{config.TIME_STEP}{period:02d}.tif"
    return os.path.join(config.REFERENCE_DIR, name)


##############################
# SCENE LIST
def search_landsat_scenes():
    """All Tier 1 Level 2 scenes of the reference period over Switzerland, from Planetary Computer."""
    client = Client.open(config.PC_STAC_URL)
    rows = []
    for sensor in config.LANDSAT_SENSORS:
        search = client.search(
            collections=[config.PC_LANDSAT_COLLECTION],
            bbox=config.BBOX_CH_WGS84,
            datetime=f"{config.REFERENCE_START}/{config.REFERENCE_END}",
            query={"platform": {"eq": sensor}, "landsat:collection_category": {"eq": "T1"}},
            limit=500,
        )
        items = [item.to_dict() for item in search.items()]
        print(f"{sensor}: {len(items)} scenes")
        for item in items:
            properties = item["properties"]
            row = {
                "id": item["id"],
                "sensor": sensor,
                "datetime": properties["datetime"],
                "day": properties["datetime"][:10],
                "wrs_path": properties["landsat:wrs_path"],
                "wrs_row": properties["landsat:wrs_row"],
                "sun_azimuth": properties["view:sun_azimuth"],
                "sun_elevation": properties["view:sun_elevation"],
                "geometry": shape(item["geometry"]).wkt,
                "href_metadata": item["assets"][METADATA_ASSET]["href"],
            }
            for asset in get_landsat_assets():
                row["href_" + asset] = item["assets"][asset]["href"]
            rows.append(row)
    return pd.DataFrame(rows)


def make_scene_list():
    """Search the scenes and apply the quality filters of the GEE script once, then reuse the saved list."""
    if os.path.exists(config.SCENE_LIST_FILE):
        return pd.read_csv(config.SCENE_LIST_FILE, dtype={"wrs_path": str, "wrs_row": str})

    scenes = search_landsat_scenes()
    print(f"Checking the quality of {len(scenes)} scenes (image quality and geometric RMSE)")
    passes = []
    token = get_landsat_token()
    for number, scene in enumerate(scenes.to_dict("records"), start=1):
        if number % 500 == 0:
            print(f"  {number} of {len(scenes)}")
            token = get_landsat_token()  # the token expires after some time
        item = {"id": scene["id"], "assets": {METADATA_ASSET: {"href": scene["href_metadata"]}}}
        try:
            passes.append(passes_landsat_quality(item, token))
        except Exception as error:
            print(f"  quality check of {scene['id']} failed, the scene is left out: {error}")
            passes.append(False)
    scenes["passes_quality"] = passes
    os.makedirs(os.path.dirname(config.SCENE_LIST_FILE), exist_ok=True)
    scenes.to_csv(config.SCENE_LIST_FILE, index=False)
    print(f"{scenes['passes_quality'].sum()} of {len(scenes)} scenes pass the quality filters")
    return scenes


##############################
# ONE SCENE: CACHE FILES
def get_cache_files(scene):
    # The slot is taken from the scene list (set in main), not from get_slot(): on Windows the worker processes
    # read the config again and would use TIME_STEP of the config file, not the time step of the command line
    folder = os.path.join(config.SCENE_CACHE_DIR, f"slot_{scene['slot']:03d}")
    return os.path.join(folder, scene["id"] + ".tif"), os.path.join(folder, scene["id"] + ".skip")


def get_scene_bounds(geometry_wkt):
    """The part of the 30 m grid covered by the scene."""
    left, bottom, right, top = transform_bounds("EPSG:4326", config.GRID_CRS, *wkt.loads(geometry_wkt).bounds)
    size = config.GRID_RESOLUTION
    left = max(config.GRID_LEFT, config.GRID_LEFT + np.floor((left - config.GRID_LEFT) / size) * size)
    right = min(config.GRID_RIGHT, config.GRID_LEFT + np.ceil((right - config.GRID_LEFT) / size) * size)
    bottom = max(config.GRID_BOTTOM, config.GRID_BOTTOM + np.floor((bottom - config.GRID_BOTTOM) / size) * size)
    top = min(config.GRID_TOP, config.GRID_BOTTOM + np.ceil((top - config.GRID_BOTTOM) / size) * size)
    return int(left), int(bottom), int(right), int(top)


def get_token_with_retries(attempts=5, wait_seconds=20):
    """With several workers asking at the same time, a token request sometimes times out. Then try again."""
    for attempt in range(1, attempts + 1):
        try:
            return get_landsat_token()
        except Exception as error:
            if attempt == attempts:
                raise
            print(f"  token request failed (attempt {attempt} of {attempts}), trying again: {error}")
            time.sleep(wait_seconds)


def read_landsat_bands(scene, bounds, token):
    bands = {}
    with tempfile.TemporaryDirectory(dir=config.OUTPUT_DIR) as temp_dir:
        for asset in get_landsat_assets():
            band_file = os.path.join(temp_dir, asset + ".tif")
            warp_landsat_band(scene["href_" + asset] + "?" + token, bounds, band_file)
            with rasterio.open(band_file) as src:
                bands[asset] = src.read(1)
    return bands


def to_reflectance(dn):
    return dn.astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET


##############################
# ONE SCENE: TERRAIN
def calculate_terrain_shadow(scene, bounds, shape_out):
    """Terrain shadow at the time of the scene. True = in shadow.
    HORAYZON works in square tiles. The tiles start at the grid origin, so their pixels fall on the 30 m grid."""
    left, bottom, right, top = bounds
    tile = config.SHADOW_TILE_SIZE
    size = config.GRID_RESOLUTION
    acquisition = parse_time(scene["datetime"]).astimezone(timezone.utc)
    shadow = np.zeros(shape_out, dtype=bool)

    with tempfile.TemporaryDirectory(dir=config.OUTPUT_DIR) as temp_dir:
        sun = SonnenWinkel(config.SHADOW_DEM_FILE, config.PLANETS, config.SHADOW_SEARCH_DIST, temp_dir)
        first_east = config.GRID_LEFT + (left - config.GRID_LEFT) // tile * tile
        first_north = config.GRID_BOTTOM + (bottom - config.GRID_BOTTOM) // tile * tile
        for east in range(first_east, right, tile):
            for north in range(first_north, top, tile):
                illuminated, transform, nodata = sun.calc_illuminate_grid(
                    east, north, tile, size, acquisition.strftime("%H:%M:%S"), acquisition.strftime("%Y-%m-%d"))
                # Copy the part of the tile that lies inside the scene
                tile_left, tile_top = int(round(transform.c)), int(round(transform.f))
                part_left, part_right = max(tile_left, left), min(tile_left + illuminated.shape[1] * size, right)
                part_top, part_bottom = min(tile_top, top), max(tile_top - illuminated.shape[0] * size, bottom)
                if part_left >= part_right or part_bottom >= part_top:
                    continue
                tile_rows = slice((tile_top - part_top) // size, (tile_top - part_bottom) // size)
                tile_cols = slice((part_left - tile_left) // size, (part_right - tile_left) // size)
                scene_rows = slice((top - part_top) // size, (top - part_bottom) // size)
                scene_cols = slice((part_left - left) // size, (part_right - left) // size)
                shadow[scene_rows, scene_cols] = illuminated[tile_rows, tile_cols] == 0
    return shadow


def calculate_illumination(slope_degrees, aspect_degrees, sun_zenith_degrees, sun_azimuth_degrees):
    """Illumination condition (cosine of the solar incidence angle), as in the GEE script."""
    slope = np.radians(slope_degrees)
    zenith = np.radians(sun_zenith_degrees)
    return (np.cos(zenith) * np.cos(slope)
            + np.sin(zenith) * np.sin(slope) * np.cos(np.radians(sun_azimuth_degrees) - np.radians(aspect_degrees)))


def apply_scsc_correction(band, illumination, slope_degrees, sun_zenith_degrees, to_correct, rng):
    """Sun-canopy-sensor + C topographic correction (Soenen et al. 2005), as in the GEE script.
    The relation between illumination and reflectance is fitted on the pixels to correct: band = a + b * illumination.
    Then c = a / b and corrected = band * (cos(slope) * cos(zenith) + c) / (illumination + c)."""
    rows, cols = np.nonzero(to_correct)
    if rows.size < 1000:
        return band  # almost no sloped pixels in this scene
    if rows.size > config.TOPO_MAX_FIT_PIXELS:
        keep = rng.choice(rows.size, config.TOPO_MAX_FIT_PIXELS, replace=False)
        rows, cols = rows[keep], cols[keep]
    # float64 is needed: with float32 and a million pixels, numpy treats the fit as nearly singular and
    # returns wrong coefficients when the illumination values lie close together (which they usually do)
    b, a = np.polyfit(illumination[rows, cols].astype(np.float64), band[rows, cols].astype(np.float64), 1)
    if b <= 0:
        # Reflectance should rise with illumination. If not, the correction would make things worse
        print(f"  no topographic correction: reflectance does not rise with illumination (b = {b:.4f})")
        return band
    c = a / b
    corrected = band.copy()
    cos_slope_zenith = np.cos(np.radians(slope_degrees)) * np.cos(np.radians(sun_zenith_degrees))
    corrected[to_correct] = (band * (cos_slope_zenith + c) / (illumination + c))[to_correct]
    return corrected


##############################
# ONE SCENE: CORRECTION CURVE
def load_correction_curves():
    """Return a dictionary {(curve group, season): (ndvi values, corrections)}."""
    curves = pd.read_csv(config.CORRECTION_CURVES_FILE)
    result = {}
    for (group, season), curve in curves.groupby(["curve_group", "season"]):
        curve = curve.sort_values("ndvi")
        result[(group, season)] = (curve["ndvi"].to_numpy(), curve["correction"].to_numpy())
    return result


def apply_correction_curve(index, sensor, day):
    group = config.CURVE_GROUP_OF_SENSOR[sensor]
    season = config.SEASONS[int(day[5:7])]
    ndvi_values, corrections = load_correction_curves()[(group, season)]
    # np.interp keeps the first and last value outside the curve, like the flat (dotted) parts in the figures
    return index + np.interp(index, ndvi_values, corrections)


##############################
# ONE SCENE: ALL STEPS
def process_scene(scene):
    """Calculate the corrected index of one scene and write it to the cache. Returns a short message."""
    output_file, skip_file = get_cache_files(scene)
    if os.path.exists(output_file) or os.path.exists(skip_file):
        return f"{scene['id']}: already done"
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    start_time = time.time()
    rng = np.random.default_rng(config.RANDOM_SEED)

    bounds = get_scene_bounds(scene["geometry"])
    window = get_grid_window(bounds)
    bands = read_landsat_bands(scene, bounds, get_token_with_retries())

    # Step 2: clouds, cloud shadow and snow (same masks as step 1)
    qa = bands["qa_pixel"]
    has_data = (qa > 0) & ((qa & 1) == 0)
    clear = has_data & ((qa & config.LANDSAT_QA_BITS_TO_MASK) == 0) & (bands["qa_radsat"] == 0)
    if clear.sum() < config.MIN_VALID_PERCENT / 100 * max(has_data.sum(), 1):
        open(skip_file, "w").write(f"fewer than {config.MIN_VALID_PERCENT}% clear pixels\n")
        return f"{scene['id']}: skipped, fewer than {config.MIN_VALID_PERCENT}% clear pixels"
    green = to_reflectance(bands["green"])
    swir = to_reflectance(bands["swir16"])
    with np.errstate(divide="ignore", invalid="ignore"):
        ndsi = (green - swir) / (green + swir)
    is_lake = read_static("lakes_2056_30m.tif", window) == 1
    # Water is bright in NDSI without being snow, so the NDSI snow mask is not used on lakes
    no_snow = (ndsi <= config.NDSI_SNOW_THRESHOLD) | is_lake
    valid = clear & no_snow

    # Step 3: terrain shadow
    valid = valid & ~calculate_terrain_shadow(scene, bounds, valid.shape)

    # Step 4: topographic correction of the index bands
    slope = read_static("slope_2056_30m.tif", window).astype(np.float32)
    aspect = read_static("aspect_2056_30m.tif", window).astype(np.float32)
    slope[slope < 0] = 0  # outside of swissALTI3D (nodata): no correction
    aspect[aspect < 0] = 0
    sun_zenith = 90 - float(scene["sun_elevation"])
    illumination = calculate_illumination(slope, aspect, sun_zenith, float(scene["sun_azimuth"]))
    band_a_name, band_b_name = get_index_bands()
    band_a = to_reflectance(bands[band_a_name])
    band_b = to_reflectance(bands[band_b_name])
    nir = to_reflectance(bands["nir08"]) if "nir08" in bands else band_a
    to_correct = (valid & (slope >= config.TOPO_MIN_SLOPE_DEGREES)
                  & (illumination >= config.TOPO_MIN_ILLUMINATION) & (nir > -0.1))
    band_a = apply_scsc_correction(band_a, illumination, slope, sun_zenith, to_correct, rng)
    band_b = apply_scsc_correction(band_b, illumination, slope, sun_zenith, to_correct, rng)

    # Step 5: index and correction curve. Step 1 only used pixels with positive reflectance, so the same here
    valid = valid & (band_a > 0) & (band_b > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        index = (band_a - band_b) / (band_a + band_b)
    if config.APPLY_CORRECTION:
        index = apply_correction_curve(index, scene["sensor"], scene["day"])

    # Step 6: write the cache file
    cached = np.full(index.shape, CACHE_NODATA, dtype=np.int16)
    cached[valid] = np.round(np.clip(index[valid], -1, 1) * CACHE_SCALE).astype(np.int16)
    transform = from_origin(bounds[0], bounds[3], config.GRID_RESOLUTION, config.GRID_RESOLUTION)
    temp_file = output_file + ".part"
    with rasterio.open(temp_file, "w", driver="GTiff", width=cached.shape[1], height=cached.shape[0], count=1,
                       dtype="int16", crs=config.GRID_CRS, transform=transform, nodata=CACHE_NODATA,
                       tiled=True, compress="deflate", predictor=2) as dst:
        dst.write(cached, 1)
    os.replace(temp_file, output_file)
    return f"{scene['id']}: {valid.sum()} valid pixels, {time.time() - start_time:.0f} s"


def process_scene_safely(scene):
    """Used by the worker processes: one failed scene must not stop the others."""
    try:
        return True, process_scene(scene)
    except Exception as error:
        return False, f"{scene['id']}: FAILED: {error}"


def prepare_slots(slots, scenes):
    """Process all scenes of these slots that are not in the cache yet."""
    todo = []
    for scene in scenes[scenes["slot"].isin(slots)].to_dict("records"):
        output_file, skip_file = get_cache_files(scene)
        if not os.path.exists(output_file) and not os.path.exists(skip_file):
            todo.append(scene)
    if not todo:
        return
    # Failed scenes (mostly network problems) get one more try after the others
    for attempt in [1, 2]:
        print(f"Processing {len(todo)} scenes with {config.SCENE_WORKERS} workers (attempt {attempt})")
        failed = []
        # maxtasksperchild=1: every scene gets a fresh process, so HORAYZON never reuses an old terrain scene
        with multiprocessing.Pool(config.SCENE_WORKERS, maxtasksperchild=1) as pool:
            for number, (success, message) in enumerate(pool.imap_unordered(process_scene_safely, todo), start=1):
                print(f"  [{number}/{len(todo)}] {message}")
                if not success:
                    failed.append(message)
        if not failed:
            return
        todo = [scene for scene in todo if not os.path.exists(get_cache_files(scene)[0])
                and not os.path.exists(get_cache_files(scene)[1])]
    print(f"WARNING: {len(failed)} scenes failed twice and are missing in this window:")
    for message in failed:
        print("  " + message)


def delete_slot(slot):
    folder = os.path.join(config.SCENE_CACHE_DIR, f"slot_{slot:03d}")
    if os.path.exists(folder):
        shutil.rmtree(folder)


##############################
# STATISTICS
def get_overpasses(slots, scenes):
    """Group the cached scenes of these slots by overpass (same day, sensor and path).
    Returns a dictionary {(day, sensor, path): [cache files]}."""
    overpasses = {}
    for scene in scenes[scenes["slot"].isin(slots)].to_dict("records"):
        output_file, _ = get_cache_files(scene)
        if os.path.exists(output_file):
            key = (scene["day"], scene["sensor"], scene["wrs_path"])
            overpasses.setdefault(key, []).append(output_file)
    return overpasses


def read_block(dataset, block):
    """Read the part of a cached scene that lies inside a block of the grid. Returns NaN where there is no data."""
    values = np.full((block.height, block.width), np.nan, dtype=np.float32)
    first_col = int(round((dataset.bounds.left - config.GRID_LEFT) / config.GRID_RESOLUTION))
    first_row = int(round((config.GRID_TOP - dataset.bounds.top) / config.GRID_RESOLUTION))
    col_start = max(block.col_off, first_col)
    col_stop = min(block.col_off + block.width, first_col + dataset.width)
    row_start = max(block.row_off, first_row)
    row_stop = min(block.row_off + block.height, first_row + dataset.height)
    if col_start >= col_stop or row_start >= row_stop:
        return values
    data = dataset.read(1, window=Window(col_start - first_col, row_start - first_row, col_stop - col_start, row_stop - row_start))
    part = np.where(data == CACHE_NODATA, np.nan, data / CACHE_SCALE)
    values[row_start - block.row_off:row_stop - block.row_off, col_start - block.col_off:col_stop - block.col_off] = part
    return values


def get_percentile(ordered, count, percent):
    """Percentile of the sorted observations, with linear interpolation (the same as numpy's default).
    np.nanpercentile gives the same result, but is about 20 times slower."""
    position = percent / 100 * (count - 1)
    lower = np.floor(position).astype(int)
    upper = np.minimum(lower + 1, np.maximum(count - 1, 0))
    weight = position - lower
    lower_value = np.take_along_axis(ordered, np.maximum(lower, 0)[None], axis=0)[0]
    upper_value = np.take_along_axis(ordered, upper[None], axis=0)[0]
    value = lower_value + weight * (upper_value - lower_value)
    value[count == 0] = np.nan
    return value


def calculate_statistics(stack):
    """Statistics over the first axis (the observations). NaN where a pixel has no observation."""
    ordered = np.sort(stack, axis=0)  # missing values (NaN) are sorted to the end
    count = np.isfinite(stack).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # pixels without any observation
        mean = np.nanmean(stack, axis=0)
        std = np.nanstd(stack, axis=0)
    median = get_percentile(ordered, count, 50)
    statistics = {"min": get_percentile(ordered, count, 0), "max": get_percentile(ordered, count, 100), "mean": mean,
                  "median": median, "std": std,
                  "p05": get_percentile(ordered, count, 5), "p95": get_percentile(ordered, count, 95)}
    if "meanAD" in get_stat_names():
        # Absolute deviations from the mean and from the median, as in the GEE script of the M and MM files
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            statistics["meanAD"] = np.nanmean(np.abs(stack - mean), axis=0)
        ordered_deviations = np.sort(np.abs(stack - median), axis=0)
        statistics["medianAD"] = get_percentile(ordered_deviations, count, 50)
    return statistics


def overlaps_block(dataset, block):
    """True if a cached scene covers at least one pixel of the block."""
    first_col = int(round((dataset.bounds.left - config.GRID_LEFT) / config.GRID_RESOLUTION))
    first_row = int(round((config.GRID_TOP - dataset.bounds.top) / config.GRID_RESOLUTION))
    return (first_col < block.col_off + block.width and first_col + dataset.width > block.col_off
            and first_row < block.row_off + block.height and first_row + dataset.height > block.row_off)


def encode(values, inside):
    """Same encoding as the current reference files: (value + 1) * 100 as uint8, 255 = no data.
    The value is rounded to the nearest step (the GEE script cut it off, which caused a bias of about -0.005)."""
    encoded = np.full(values.shape, OUTPUT_NODATA, dtype=np.uint8)
    good = np.isfinite(values) & inside
    encoded[good] = np.clip(np.round((values[good] + 1) * 100), 0, 200).astype(np.uint8)
    return encoded


def convert_to_cog(input_file, output_file):
    """Writes input_file as a Cloud Optimized GeoTIFF, with GDAL on the command line.
    The tags and band names are copied. The overviews use NEAREST, so they hold real pixel values and never
    averages. They are only for viewing: all statistics are calculated at full resolution before this step."""
    subprocess.run(["gdal_translate", "-q", "-of", "COG",
                    "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=YES", "-co", "BLOCKSIZE=512",
                    "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "NUM_THREADS=ALL_CPUS",
                    input_file, output_file], check=True)


def build_statistics(period, slots, scenes):
    output_file = get_output_file(period)
    stat_names = get_stat_names()
    overpasses = get_overpasses(slots, scenes)
    print(f"Statistics from {len(overpasses)} overpasses ({sum(len(f) for f in overpasses.values())} scenes)")
    datasets = {key: [rasterio.open(file) for file in files] for key, files in overpasses.items()}
    inside_switzerland = read_static("swiss_buffer_2056_30m.tif", Window(0, 0, config.GRID_WIDTH, config.GRID_HEIGHT)) == 1

    # The list of scenes used, like the index_list of the GEE assets. It is written to the metadata tag "scene_list"
    used = pd.DataFrame([{"day": key[0], "sensor": key[1], "wrs_path": key[2],
                          "scene_id": os.path.basename(file).replace(".tif", "")}
                         for key, files in overpasses.items() for file in files])

    transform = from_origin(config.GRID_LEFT, config.GRID_TOP, config.GRID_RESOLUTION, config.GRID_RESOLUTION)
    temp_file = output_file + ".part"
    block_size = config.STATS_BLOCK_SIZE
    with rasterio.open(temp_file, "w", driver="GTiff", width=config.GRID_WIDTH, height=config.GRID_HEIGHT,
                       count=len(stat_names) + 1, dtype="uint8", crs=config.GRID_CRS, transform=transform,
                       nodata=OUTPUT_NODATA, tiled=True, blockxsize=512, blockysize=512,
                       compress="deflate", predictor=2) as dst:
        for row in range(0, config.GRID_HEIGHT, block_size):
            for col in range(0, config.GRID_WIDTH, block_size):
                block = Window(col, row, min(block_size, config.GRID_WIDTH - col), min(block_size, config.GRID_HEIGHT - row))
                inside = inside_switzerland[row:row + block.height, col:col + block.width]
                # One layer per overpass. Neighbouring rows of the same overpass overlap, there the mean is used
                layers = []
                for overpass_datasets in datasets.values():
                    touching = [dataset for dataset in overpass_datasets if overlaps_block(dataset, block)]
                    if not touching:
                        continue
                    parts = np.stack([read_block(dataset, block) for dataset in touching])
                    if np.isfinite(parts).any():
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=RuntimeWarning)
                            layers.append(np.nanmean(parts, axis=0))
                if layers:
                    stack = np.stack(layers)
                    statistics = calculate_statistics(stack)
                    count = np.isfinite(stack).sum(axis=0)
                else:
                    statistics = {name: np.full((block.height, block.width), np.nan) for name in stat_names}
                    count = np.zeros((block.height, block.width), dtype=int)
                for band_number, name in enumerate(stat_names, start=1):
                    dst.write(encode(statistics[name], inside), band_number, window=block)
                count[~inside] = 0
                dst.write(np.minimum(count, 254).astype(np.uint8), len(stat_names) + 1, window=block)
            print(f"  rows {row} to {min(row + block_size, config.GRID_HEIGHT)} of {config.GRID_HEIGHT} done")

        for band_number, name in enumerate(stat_names + ["count"], start=1):
            dst.set_band_description(band_number, name)
        dst.update_tags(
            index=config.INDEX_NAME, time_step=config.TIME_STEP, period=period, slots=",".join(str(s) for s in slots),
            reference_period=f"{config.REFERENCE_START}/{config.REFERENCE_END}",
            scale=100, offset=100,
            note=f"value = DN / scale - 1 for bands 1-{len(stat_names)}; band {len(stat_names) + 1} = number of observations",
            overpasses=len(overpasses), scenes=sum(len(f) for f in overpasses.values()),
            correction_curves=os.path.basename(config.CORRECTION_CURVES_FILE) if config.APPLY_CORRECTION else "none",
            created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            scene_list=used.to_csv(index=False),  # read with pd.read_csv(io.StringIO(tags["scene_list"]))
        )
    for overpass_datasets in datasets.values():
        for dataset in overpass_datasets:
            dataset.close()

    # Write the COG to a second temporary file first, so a finished output file always means the file is complete
    cog_temp_file = output_file + ".cog.part"
    convert_to_cog(temp_file, cog_temp_file)
    os.replace(cog_temp_file, output_file)
    os.remove(temp_file)
    print(f"Written {output_file}")


##############################
# S3 UPLOAD
def get_s3_client():
    with open(config.S3_SECRETS_FILE) as file:
        secrets = json.load(file)
    return boto3.client("s3", aws_access_key_id=secrets["aws_access_key_id"],
                        aws_secret_access_key=secrets["aws_secret_access_key"],
                        region_name=secrets["aws_region_name"])


def get_s3_key(file_name):
    return config.S3_FOLDERS[config.TIME_STEP] + file_name


def get_s3_size(s3, file_name):
    """Size of the file on S3 in bytes, or None if it is not there."""
    try:
        return s3.head_object(Bucket=config.S3_BUCKET, Key=get_s3_key(file_name))["ContentLength"]
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def get_copy_name(period):
    """DOY 366 is also stored as DOY 000 with the same content, as for the current reference files.
    Returns the name of that copy, or None for all other files."""
    if config.TIME_STEP == "DOY" and period == 366:
        return f"{config.INDEX_NAME}_Stats_DOY000.tif"
    return None


def upload_file(s3, local_file, file_name):
    s3.upload_file(local_file, config.S3_BUCKET, get_s3_key(file_name))
    if get_s3_size(s3, file_name) != os.path.getsize(local_file):
        raise RuntimeError(f"Upload of {file_name} is incomplete")
    print(f"Uploaded to s3://{config.S3_BUCKET}/{get_s3_key(file_name)}")


def save_output(s3, period):
    """Uploads the finished file (and the DOY 000 copy) and deletes it locally.
    Without upload, the DOY 000 copy is made locally instead."""
    output_file = get_output_file(period)
    copy_name = get_copy_name(period)
    if not config.UPLOAD_TO_S3:
        if copy_name:
            shutil.copy(output_file, os.path.join(config.REFERENCE_DIR, copy_name))
        return
    # The copy is uploaded first: if the script stops in between, the main file is missing and both are made again
    if copy_name:
        upload_file(s3, output_file, copy_name)
    upload_file(s3, output_file, os.path.basename(output_file))
    os.remove(output_file)


def is_done(s3, period):
    if config.UPLOAD_TO_S3:
        return get_s3_size(s3, os.path.basename(get_output_file(period))) is not None
    return os.path.exists(get_output_file(period))


##############################
# MAIN
def check_settings():
    if config.TIME_STEP not in ("DOY", "M", "MM"):
        raise ValueError(f"TIME_STEP must be DOY, M or MM, not {config.TIME_STEP}")
    if config.APPLY_CORRECTION and config.INDEX_NAME != "NDVI":
        raise ValueError("Correction curves exist only for NDVI. Run steps 1 and 3 for the new index first, "
                         "or set APPLY_CORRECTION = False")
    if config.APPLY_CORRECTION and not os.path.exists(config.CORRECTION_CURVES_FILE):
        raise FileNotFoundError(f"Run step 3 first: {config.CORRECTION_CURVES_FILE} is missing")
    egm_folder = os.path.join(open(os.path.join(os.path.dirname(horayzon.__file__), "path_aux_data.txt")).read(), "EGM96")
    if not os.path.isdir(os.path.join(config.REPO_DIR, egm_folder)) and not os.path.isdir(egm_folder):
        raise FileNotFoundError(f"HORAYZON needs the EGM96 geoid in {egm_folder}. Copy it from the EC2 instance "
                                "(local_assets/EGM) into the repository")
    if config.UPLOAD_TO_S3 and not os.path.exists(config.S3_SECRETS_FILE):
        raise FileNotFoundError(f"The S3 credentials are missing: {config.S3_SECRETS_FILE}")


def read_command_line():
    """Returns the time steps and the periods to build.
    Time steps (DOY, M, MM) at the start of the command line replace TIME_STEP of the config.
    Numbers after them replace REFERENCE_PERIODS (DOY) or all 12 months (M and MM)."""
    arguments = sys.argv[1:]
    time_steps = []
    while arguments and arguments[0].upper() in ("DOY", "M", "MM"):
        time_steps.append(arguments.pop(0).upper())
    if not time_steps:
        time_steps = [config.TIME_STEP]
    if "DOY" in time_steps and len(time_steps) > 1:
        raise ValueError("DOY uses days as time unit, M and MM use months. Build DOY in a separate run")

    if arguments:
        periods = [int(argument) for argument in arguments]
    elif time_steps == ["DOY"]:
        periods = config.REFERENCE_PERIODS
    else:
        periods = list(range(1, 13))
    largest_period = 366 if time_steps == ["DOY"] else 12
    for period in periods:
        if period < 1 or period > largest_period:
            raise ValueError(f"{period} is not valid for {time_steps}: use 1 to {largest_period}")
    return time_steps, periods


def main():
    check_settings()

    # The files to build. M and MM can be built in the same run: they both use months as time unit,
    # so they share the scene cache and every scene is processed only once.
    time_steps, periods = read_command_line()
    jobs = [(time_step, period) for period in periods for time_step in time_steps]
    print(f"Files to build: {', '.join(f'{time_step}{period}' for time_step, period in jobs)}")

    os.makedirs(config.REFERENCE_DIR, exist_ok=True)
    os.makedirs(config.SCENE_CACHE_DIR, exist_ok=True)
    s3 = get_s3_client() if config.UPLOAD_TO_S3 else None

    # The functions of this script read the time step from the config, so it is set here for every file
    config.TIME_STEP = time_steps[0]
    scenes = make_scene_list()
    scenes = scenes[scenes["passes_quality"]].copy()
    scenes["slot"] = [get_slot(day) for day in scenes["day"]]
    print(f"{len(scenes)} scenes pass the quality filters")

    # For every slot: the last output file that needs it. After that file, its cache is deleted
    last_use = {}
    for number, (time_step, period) in enumerate(jobs):
        config.TIME_STEP = time_step
        for slot in get_window_slots(period):
            last_use[slot] = number

    for number, (time_step, period) in enumerate(jobs):
        config.TIME_STEP = time_step
        slots = get_window_slots(period)
        print(f"\n=== {os.path.basename(get_output_file(period))} ({number + 1} of {len(jobs)}), slots {slots[0]} to {slots[-1]}")
        start_time = time.time()
        if is_done(s3, period):
            if config.UPLOAD_TO_S3:
                where = f"s3://{config.S3_BUCKET}/{get_s3_key(os.path.basename(get_output_file(period)))}"
            else:
                where = get_output_file(period)
            print(f"Skipped: the file already exists in {where}. Delete it there to build it again")
        else:
            # A local file here was built before the script stopped, but not uploaded yet
            if not os.path.exists(get_output_file(period)):
                prepare_slots(slots, scenes)
                build_statistics(period, slots, scenes)
            save_output(s3, period)
            print(f"Done in {(time.time() - start_time) / 60:.0f} min")
        for slot, last_number in last_use.items():
            if last_number == number:
                delete_slot(slot)


if __name__ == "__main__":
    main()
