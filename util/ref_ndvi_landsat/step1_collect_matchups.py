# Step 1 of the sensor comparison: collects pixel pairs of same-day Landsat and swissEO S2-SR images.
#
# Everything is compared on the 30 m EPSG:2056 grid of the reference (see vi_reference_config.py):
# - Landsat is warped to this grid with nearest neighbour, like the export of the GEE reference
# - each 30 m pixel gets the mean of the 9 swissEO S2-SR pixels inside it
#
# One parquet file is written per scene pair. Finished pairs are skipped when the script is restarted.
#
# Usage:
#   python util/ref_ndvi_landsat/step1_collect_matchups.py pilot   (small balanced selection of scene pairs)
#   python util/ref_ndvi_landsat/step1_collect_matchups.py all     (all suitable scene pairs)

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from xml.etree import ElementTree

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from pystac_client import Client
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from scipy.ndimage import minimum_filter, uniform_filter
from shapely.geometry import box, shape

import vi_reference_config as config

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "3",
    # Without a timeout, a stalled download waits forever and the whole run hangs
    "GDAL_HTTP_TIMEOUT": "120",
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
}
os.environ.update(GDAL_ENV)
METADATA_ATTEMPTS = 5
METADATA_WAIT_SECONDS = 10
CLASS_CODES = {"forest": 1, "vegetation": 2, "water": 3, "hard_surface": 4}
LANDSAT_BANDS = ["red", "nir08", "green", "swir16", "blue", "qa_pixel", "qa_radsat"]
QA_BITS_TO_MASK = 0b111111  # fill, dilated cloud, cirrus, cloud, cloud shadow, snow
SEASONS = config.SEASONS


##############################
# FIND SCENE PAIRS
def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def get_s2_items():
    items = []
    url = config.S2_SR_ITEMS_URL + "?limit=100"
    while url:
        page = requests.get(url, timeout=120).json()
        items.extend(page["features"])
        url = next((link["href"] for link in page["links"] if link["rel"] == "next"), None)
    return items


def get_landsat_items(sensor):
    client = Client.open(config.PC_STAC_URL)
    search = client.search(
        collections=[config.PC_LANDSAT_COLLECTION],
        bbox=config.BBOX_CH_WGS84,
        datetime=f"{config.TEST_START}/{config.TEST_END[sensor]}",
        query={"platform": {"eq": sensor}, "landsat:collection_category": {"eq": "T1"}},
        limit=500,
    )
    return [item.to_dict() for item in search.items()]


def find_pairs():
    s2_by_day = {}
    for item in get_s2_items():
        s2_by_day.setdefault(item["properties"]["datetime"][:10], []).append(item)

    swiss_box = box(*config.BBOX_CH_WGS84)
    pairs = []
    for sensor in config.TEST_SENSORS:
        for landsat_item in get_landsat_items(sensor):
            properties = landsat_item["properties"]
            day = properties["datetime"][:10]
            if properties["eo:cloud_cover"] > config.MAX_SCENE_CLOUD_PERCENT:
                continue
            for s2_item in s2_by_day.get(day, []):
                minutes = abs((parse_time(properties["datetime"]) - parse_time(s2_item["properties"]["datetime"])).total_seconds()) / 60
                overlap = shape(landsat_item["geometry"]).intersection(shape(s2_item["geometry"])).intersection(swiss_box)
                if minutes > config.MAX_TIME_DIFFERENCE_MINUTES:
                    continue
                if overlap.area / swiss_box.area < config.MIN_SCENE_OVERLAP:
                    continue
                pairs.append({"sensor": sensor, "day": day, "season": SEASONS[int(day[5:7])], "year": day[:4],
                              "cloud": properties["eo:cloud_cover"], "minutes": minutes,
                              "landsat_item": landsat_item, "s2_item": s2_item})
    return pairs


def get_landsat_token():
    response = requests.get(config.PC_TOKEN_URL, timeout=60)
    response.raise_for_status()
    return response.json()["token"]


def passes_landsat_quality(landsat_item, token):
    """Apply the same scene quality filters as the GEE reference script."""
    # mtl.xml is used because a few scenes have no mtl.json
    url = landsat_item["assets"]["mtl.xml"]["href"] + "?" + token
    for attempt in range(METADATA_ATTEMPTS):
        response = requests.get(url, timeout=60)
        if response.status_code == 200 and response.text.strip():
            break
        print(f"Metadata of {landsat_item['id']}: HTTP {response.status_code}, attempt {attempt + 1}: {response.text[:200]}")
        time.sleep(METADATA_WAIT_SECONDS)
    response.raise_for_status()
    metadata = ElementTree.fromstring(response.text)
    quality = metadata.findtext("IMAGE_ATTRIBUTES/IMAGE_QUALITY_OLI") or metadata.findtext("IMAGE_ATTRIBUTES/IMAGE_QUALITY")
    rmse = metadata.findtext("LEVEL1_PROCESSING_RECORD/GEOMETRIC_RMSE_MODEL")
    return (quality is not None and float(quality) >= config.MIN_IMAGE_QUALITY
            and rmse is not None and float(rmse) < config.MAX_GEOMETRIC_RMSE)


def select_pilot_pairs(pairs):
    """Take the clearest scene pairs per sensor and season, at most one per year."""
    selected = []
    token = get_landsat_token()
    for sensor in config.TEST_SENSORS:
        for season, wanted in config.PILOT_PAIRS_PER_SEASON.items():
            candidates = [p for p in pairs if p["sensor"] == sensor and p["season"] == season]
            years_used = set()
            for pair in sorted(candidates, key=lambda p: p["cloud"]):
                if len(years_used) == wanted:
                    break
                if pair["year"] in years_used:
                    continue
                if passes_landsat_quality(pair["landsat_item"], token):
                    selected.append(pair)
                    years_used.add(pair["year"])
    return selected


##############################
# GRID
def get_pair_bounds(pair):
    """Return the shared area of a scene pair, snapped to the 30 m grid."""
    shared_area = shape(pair["landsat_item"]["geometry"]).intersection(shape(pair["s2_item"]["geometry"]))
    left, bottom, right, top = transform_bounds("EPSG:4326", config.GRID_CRS, *shared_area.bounds)
    size = config.GRID_RESOLUTION
    left = max(config.GRID_LEFT, config.GRID_LEFT + np.floor((left - config.GRID_LEFT) / size) * size)
    right = min(config.GRID_RIGHT, config.GRID_LEFT + np.ceil((right - config.GRID_LEFT) / size) * size)
    bottom = max(config.GRID_BOTTOM, config.GRID_BOTTOM + np.floor((bottom - config.GRID_BOTTOM) / size) * size)
    top = min(config.GRID_TOP, config.GRID_BOTTOM + np.ceil((top - config.GRID_BOTTOM) / size) * size)
    return int(left), int(bottom), int(right), int(top)


def get_grid_window(bounds):
    left, bottom, right, top = bounds
    return Window((left - config.GRID_LEFT) // config.GRID_RESOLUTION, (config.GRID_TOP - top) // config.GRID_RESOLUTION,
                  (right - left) // config.GRID_RESOLUTION, (top - bottom) // config.GRID_RESOLUTION)


##############################
# READ IMAGES
def warp_landsat_band(url, bounds, output_file):
    left, bottom, right, top = bounds
    command = ["gdalwarp", "-q", "-overwrite", "-t_srs", config.GRID_CRS,
               "-te", str(left), str(bottom), str(right), str(top),
               "-tr", str(config.GRID_RESOLUTION), str(config.GRID_RESOLUTION),
               "-r", "near", "-co", "COMPRESS=DEFLATE", "/vsicurl/" + url, output_file]
    subprocess.run(command, check=True, env={**os.environ, **GDAL_ENV})


def read_landsat_bands(landsat_item, bounds, token):
    bands = {}
    with tempfile.TemporaryDirectory(dir=config.OUTPUT_DIR) as temp_dir:
        for band in LANDSAT_BANDS:
            band_file = os.path.join(temp_dir, band + ".tif")
            warp_landsat_band(landsat_item["assets"][band]["href"] + "?" + token, bounds, band_file)
            with rasterio.open(band_file) as src:
                bands[band] = src.read(1)
    return bands


def read_s2_at_10m(url, bounds):
    """Read the 10 m pixels inside bounds. Pixels outside of the file are 0."""
    left, bottom, right, top = bounds
    size = config.S2_RESOLUTION
    data = np.zeros(((top - bottom) // size, (right - left) // size), dtype=np.uint16)
    with rasterio.open(url) as src:
        src_col = int(round((left - src.bounds.left) / size))
        src_row = int(round((src.bounds.top - top) / size))
        first_col, first_row = max(src_col, 0), max(src_row, 0)
        last_col = min(src_col + data.shape[1], src.width)
        last_row = min(src_row + data.shape[0], src.height)
        if last_col <= first_col or last_row <= first_row:
            return data
        window = Window(first_col, first_row, last_col - first_col, last_row - first_row)
        data[first_row - src_row:last_row - src_row, first_col - src_col:last_col - src_col] = src.read(1, window=window)
    return data


def to_blocks(data):
    """Reshape a 10 m array so that axes 1 and 3 hold the 3 x 3 pixels of each 30 m pixel."""
    n = config.S2_PIXELS_PER_GRID_PIXEL
    height, width = data.shape[0] // n, data.shape[1] // n
    return data.reshape(height, n, width, n)


def get_s2_href(s2_item, ending):
    for name, asset in s2_item["assets"].items():
        if name.endswith(ending):
            return asset["href"]
    raise ValueError(f"Asset {ending} is missing in {s2_item['id']}")


def read_s2_bands(s2_item, bounds):
    s2 = {}
    for band in ["red", "nir"]:
        blocks = to_blocks(read_s2_at_10m(get_s2_href(s2_item, f"_{config.BANDS[band]['sentinel2']}.tif"), bounds))
        mean_dn = blocks.mean(axis=(1, 3), dtype=np.float32)
        mean_dn[(blocks == 0).any(axis=(1, 3))] = np.nan  # all 9 pixels must be valid
        s2[band] = mean_dn * config.S2_SCALE + config.S2_OFFSET
    s2["cloud_max"] = to_blocks(read_s2_at_10m(get_s2_href(s2_item, "_cloudmask_10m.tif"), bounds)).max(axis=(1, 3))
    s2["illumination_max"] = to_blocks(read_s2_at_10m(get_s2_href(s2_item, "_terrainmask_10m.tif"), bounds)).max(axis=(1, 3))
    return s2


##############################
# CLASSES
def read_static(name, window):
    with rasterio.open(os.path.join(config.STATIC_DIR, name)) as src:
        return src.read(1, window=window)


def make_classes(bounds, window):
    shape_out = (window.height, window.width)
    transform = from_origin(bounds[0], bounds[3], config.GRID_RESOLUTION, config.GRID_RESOLUTION)

    forest = read_static("forest_fraction_2056_30m.tif", window)
    vegetation = read_static("vegetation_fraction_2056_30m.tif", window)
    classes = np.zeros(shape_out, dtype=np.uint8)
    classes[vegetation >= config.MIN_MASK_FRACTION] = CLASS_CODES["vegetation"]
    classes[forest >= config.MIN_MASK_FRACTION] = CLASS_CODES["forest"]

    lakes = gpd.read_file(config.LAKES_FILE).to_crs(config.GRID_CRS)
    lakes = lakes.geometry.buffer(-config.LAKE_SHORE_BUFFER_METERS)
    lakes = lakes[~lakes.is_empty]
    classes[rasterize(lakes, out_shape=shape_out, transform=transform, fill=0, default_value=1, dtype=np.uint8) == 1] = CLASS_CODES["water"]

    hard_surfaces = gpd.read_file(os.path.join(config.STATIC_DIR, "hard_surfaces_2056.gpkg")).geometry
    classes[rasterize(hard_surfaces, out_shape=shape_out, transform=transform, fill=0, default_value=1, dtype=np.uint8) == 1] = CLASS_CODES["hard_surface"]
    return classes


def calculate_cv_3x3(band, valid):
    """Coefficient of variation in the 3 x 3 neighbourhood. NaN if a neighbour is invalid."""
    filled = np.where(valid, band, 0).astype(np.float64)
    mean = uniform_filter(filled, size=3, mode="constant")
    mean_of_squares = uniform_filter(filled ** 2, size=3, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.sqrt(np.maximum(mean_of_squares - mean ** 2, 0)) / mean
    cv[minimum_filter(valid.astype(np.uint8), size=3, mode="constant") == 0] = np.nan
    return cv.astype(np.float32)


##############################
# ONE SCENE PAIR
def process_pair(pair, output_file):
    start_time = time.time()
    bounds = get_pair_bounds(pair)
    window = get_grid_window(bounds)
    print(f"{pair['day']} {pair['sensor']}: {window.width} x {window.height} pixels")

    landsat = read_landsat_bands(pair["landsat_item"], bounds, get_landsat_token())
    s2 = read_s2_bands(pair["s2_item"], bounds)
    print(f"  read images in {time.time() - start_time:.0f} s")

    l_red = landsat["red"].astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET
    l_nir = landsat["nir08"].astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET
    l_green = landsat["green"].astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET
    l_swir = landsat["swir16"].astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET
    l_blue = landsat["blue"].astype(np.float32) * config.LANDSAT_SCALE + config.LANDSAT_OFFSET

    classes = make_classes(bounds, window)
    slope = read_static("slope_max_2056_30m.tif", window)

    landsat_clear = ((landsat["qa_pixel"] & QA_BITS_TO_MASK) == 0) & (landsat["qa_pixel"] > 0) & (landsat["qa_radsat"] == 0) & (landsat["red"] > 0)
    s2_clear = np.isfinite(s2["red"]) & np.isfinite(s2["nir"]) & (s2["cloud_max"] == 0) & (s2["illumination_max"] < config.MAX_ILLUMINATION_ANGLE)
    valid = landsat_clear & s2_clear
    flat = (slope >= 0) & (slope <= config.MAX_SLOPE_DEGREES)

    # Water is bright in NDSI without being snow, so the snow mask is only used on land
    with np.errstate(divide="ignore", invalid="ignore"):
        ndsi = (l_green - l_swir) / (l_green + l_swir)
    no_snow = (ndsi <= config.NDSI_SNOW_THRESHOLD) | (classes == CLASS_CODES["water"])

    cv_s2_nir = calculate_cv_3x3(s2["nir"], np.isfinite(s2["nir"]))
    cv_landsat_nir = calculate_cv_3x3(l_nir, landsat_clear)

    rng = np.random.default_rng(config.RANDOM_SEED)
    tables = []
    for class_name, class_code in CLASS_CODES.items():
        rows, cols = np.nonzero(valid & flat & no_snow & (classes == class_code))
        print(f"  {class_name}: {rows.size} pixels")
        if rows.size > config.MAX_PIXELS_PER_CLASS:
            keep = rng.choice(rows.size, config.MAX_PIXELS_PER_CLASS, replace=False)
            rows, cols = rows[keep], cols[keep]
        tables.append(pd.DataFrame({
            "class_name": class_name,
            "x": bounds[0] + (cols + 0.5) * config.GRID_RESOLUTION,
            "y": bounds[3] - (rows + 0.5) * config.GRID_RESOLUTION,
            "landsat_red": l_red[rows, cols], "landsat_nir": l_nir[rows, cols], "landsat_blue": l_blue[rows, cols],
            "s2_red": s2["red"][rows, cols], "s2_nir": s2["nir"][rows, cols],
            "slope": slope[rows, cols],
            "cv_s2_nir": cv_s2_nir[rows, cols], "cv_landsat_nir": cv_landsat_nir[rows, cols],
        }))

    table = pd.concat(tables, ignore_index=True)
    table["pair_id"] = os.path.basename(output_file).replace(".parquet", "")
    table["sensor"] = pair["sensor"]
    table["day"] = pair["day"]
    table["season"] = pair["season"]
    table["minutes"] = pair["minutes"]
    table["wrs_path"] = pair["landsat_item"]["properties"]["landsat:wrs_path"]
    table["off_nadir"] = pair["landsat_item"]["properties"].get("view:off_nadir")
    table["s2_item"] = pair["s2_item"]["id"]
    table.to_parquet(output_file)
    print(f"  wrote {len(table)} pixels in {time.time() - start_time:.0f} s")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    if mode not in ("pilot", "all"):
        raise ValueError("Use 'pilot' or 'all'")

    os.makedirs(config.MATCHUP_DIR, exist_ok=True)
    os.makedirs(config.RESULT_DIR, exist_ok=True)
    pairs = find_pairs()
    print(f"Found {len(pairs)} suitable scene pairs")
    if mode == "pilot":
        pairs = select_pilot_pairs(pairs)
    else:
        token = get_landsat_token()
        pairs = [pair for pair in pairs if passes_landsat_quality(pair["landsat_item"], token)]
        print(f"{len(pairs)} scene pairs pass the Landsat quality filters")

    summary = pd.DataFrame([{k: p[k] for k in ["sensor", "day", "season", "cloud", "minutes"]}
                            | {"landsat_id": p["landsat_item"]["id"], "s2_id": p["s2_item"]["id"]} for p in pairs])
    summary.to_csv(os.path.join(config.RESULT_DIR, f"{mode}_pairs.csv"), index=False)
    print(summary.drop(columns=["landsat_id"]).to_string())

    failed_pairs = []
    for number, pair in enumerate(pairs, start=1):
        pair_id = f"{pair['day']}_{pair['landsat_item']['id']}_{pair['s2_item']['id']}"
        output_file = os.path.join(config.MATCHUP_DIR, pair_id + ".parquet")
        if os.path.exists(output_file):
            continue
        print(f"\nScene pair {number} of {len(pairs)}")
        # One failed scene pair should not stop a long run. Restarting the script tries it again
        try:
            process_pair(pair, output_file)
        except Exception as error:
            print(f"  FAILED: {error}")
            failed_pairs.append(pair_id)

    print(f"\nFinished. {len(failed_pairs)} scene pairs failed: {failed_pairs}")


if __name__ == "__main__":
    main()
