# Settings for the vegetation index reference work.
# All scripts in this folder read their settings from here.

import os

##############################
# PATHS
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(BASE_DIR))  # util/ref_ndvi_landsat/ -> repository
OUTPUT_DIR = os.path.join(BASE_DIR, "output")  # not committed, see .gitignore in this folder
STATIC_DIR = os.path.join(OUTPUT_DIR, "static")
MATCHUP_DIR = os.path.join(OUTPUT_DIR, "matchups")
RESULT_DIR = os.path.join(OUTPUT_DIR, "results")
BAND_CORRECTIONS_FILE = os.path.join(RESULT_DIR, "band_corrections.csv")

LAKES_FILE = os.path.join(REPO_DIR, "assets", "overview_lakes_2056.gpkg")
SWISS_BUFFER_FILE = os.path.join(REPO_DIR, "assets", "swissboundary_buffer_5000m.gpkg")

##############################
# DATA SOURCES
S2_SR_ITEMS_URL = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swisseo_s2-sr_v200/items"
PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2"
PC_LANDSAT_COLLECTION = "landsat-c2-l2"
SWISSALTI3D_ITEMS_URL = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
SWISSTLM3D_ZIP_FILE = os.path.join(STATIC_DIR, "swisstlm3d_2026-02_shp.zip")
VEGETATION_MASK_URL = "/vsis3/s3-topo-satromo-prod/data/MASKS/Vegetation/vegetation_mask_2056_habitatv1-2_2025_CLCplus_2023.tif"
FOREST_MASK_URL = "/vsis3/s3-topo-satromo-prod/data/MASKS/Vegetation/forest_mask_2056_habitatv1-2_2025_CLCplus_2023.tif"

BBOX_CH_WGS84 = [5.9, 45.8, 10.5, 47.85]

##############################
# GRID
# All outputs are in EPSG:2056. This is the 30 m grid of the current reference files.
# Its coordinates are divisible by 10, so every 30 m pixel covers exactly 9 swissEO S2-SR pixels.
GRID_CRS = "EPSG:2056"
GRID_RESOLUTION = 30
GRID_LEFT = 2478990
GRID_BOTTOM = 1069290
GRID_RIGHT = 2841690
GRID_TOP = 1305600
GRID_WIDTH = (GRID_RIGHT - GRID_LEFT) // GRID_RESOLUTION
GRID_HEIGHT = (GRID_TOP - GRID_BOTTOM) // GRID_RESOLUTION
S2_RESOLUTION = 10
S2_PIXELS_PER_GRID_PIXEL = GRID_RESOLUTION // S2_RESOLUTION

##############################
# SCALING: digital number (DN) to surface reflectance
S2_SCALE = 0.0001
S2_OFFSET = -0.1
LANDSAT_SCALE = 0.0000275
LANDSAT_OFFSET = -0.2

##############################
# BANDS AND INDICES
# The Landsat names are the Planetary Computer asset names. They are the same for Landsat 5, 7 and 8.
BANDS = {
    "red": {"sentinel2": "b04_10m", "landsat": "red"},
    "nir": {"sentinel2": "b08_10m", "landsat": "nir08"},
    # "swir1": {"sentinel2": "b11_20m", "landsat": "swir16"},  # needed for NDMI
}

# Every index here is calculated as (band_a - band_b) / (band_a + band_b)
INDICES = {
    "NDVI": {"band_a": "nir", "band_b": "red"},
    # "NDMI": {"band_a": "nir", "band_b": "swir1"},  # test the swir1 band first
}

INDEX_NAME = "NDVI"
TIME_STEP = "DOY"  # options: "DOY", "M" (calendar month), "MM" (the month and the month before, MM03 = Feb + Mar)
HALF_WINDOW_DAYS = 7  # only used for "DOY": 7 days on each side = 15 days

REFERENCE_START = "1991-01-01"
REFERENCE_END = "2020-12-31"

# Landsat 9 is not included because it started in 2021.
# Revisit this when the climate reference period is shifted.
LANDSAT_SENSORS = ["landsat-5", "landsat-7", "landsat-8"]

##############################
# SENSOR COMPARISON
# Landsat 5 ended before Sentinel-2 started, so step 3 uses the Landsat 7 corrections for it.
TEST_SENSORS = ["landsat-7", "landsat-8"]
TEST_START = "2015-07-01"
TEST_END = {"landsat-7": "2020-12-31", "landsat-8": "2026-10-30"}  # Landsat 7 only while it is in the reference

MAX_TIME_DIFFERENCE_MINUTES = 30
MAX_SCENE_CLOUD_PERCENT = 30
MIN_SCENE_OVERLAP = 0.15  # share of the Swiss bounding box covered by both images
MAX_GEOMETRIC_RMSE = 15  # same filter as the GEE script
MIN_IMAGE_QUALITY = 9  # same filter as the GEE script

# Number of scene pairs per sensor and season in the pilot
PILOT_PAIRS_PER_SEASON = {"DJF": 1, "MAM": 3, "JJA": 3, "SON": 3}

MAX_SLOPE_DEGREES = 5  # the GEE topographic correction is only applied from 5 degrees upwards
MAX_ILLUMINATION_ANGLE = 70  # same threshold as step1_processor_vhi.py
NDSI_SNOW_THRESHOLD = 0  # same threshold as step1_processor_vhi.py
MIN_MASK_FRACTION = 0.99  # share of a Landsat pixel that must be covered by a class
LAKE_SHORE_BUFFER_METERS = 300  # stay this far away from the shore
ROOF_EDGE_BUFFER_METERS = 30  # stay this far inside large roofs
MIN_ROOF_AREA_M2 = 20000
MAX_PIXELS_PER_CLASS = 20000  # maximum number of pixels kept per class and scene pair
RANDOM_SEED = 42

# Maximum coefficient of variation of NIR in the 3 x 3 Landsat pixel neighbourhood.
# None means the check is skipped: water is too dark for a coefficient of variation to be useful.
MAX_NIR_CV = {"forest": 0.10, "vegetation": 0.10, "hard_surface": 0.15, "water": None}

##############################
# RESULTS
BIAS_THRESHOLD = 0.01  # correct if a verified NDVI bin differs by more than this
NDVI_BIN_WIDTH = 0.1
MIN_DATES_PER_BIN = 5  # bins with less data are reported as not verified
MIN_PIXELS_PER_BIN = 1000
BOOTSTRAP_RUNS = 500  # scene pairs are resampled, because pixels of one scene are not independent

##############################
# CORRECTION CURVES (step 3)
# The correction is a smooth curve: NDVI of S2-SR minus NDVI of Landsat, as a function of the Landsat NDVI.
# There is one curve per Landsat sensor group and season.
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
SEASON_NAMES = ["DJF", "MAM", "JJA", "SON"]

# Which curve each Landsat sensor uses. Landsat 5 ended before Sentinel-2 started, so it cannot be tested
# and uses the Landsat 7 curves (same processing chain). Change a value here to use another curve.
CURVE_GROUP_OF_SENSOR = {"landsat-5": "landsat-7", "landsat-7": "landsat-7", "landsat-8": "landsat-8"}
CURVE_GROUPS = ["landsat-7", "landsat-8"]  # groups that have test data
CURVE_CLASSES = ["vegetation", "forest"]  # only these pixels are used to fit the curves

CURVE_BIN_WIDTH = 0.05  # the curve is fitted to the median difference in bins of the Landsat NDVI
CURVE_MIN_NDVI = 0.0  # the curve is calculated from here ...
CURVE_MAX_NDVI = 1.0  # ... to here, in steps of CURVE_STEP
CURVE_STEP = 0.01
CURVE_MIN_PIXELS_PER_SCENE_BIN = 100  # a scene pair must have this many pixels in a bin to count there
CURVE_MIN_SCENES_PER_BIN = 3  # bins with fewer scene pairs are not used for the fit
CURVE_DEGREE = 3  # degree of the polynomial (a test that left out one year at a time found no gain above 3)
CURVE_DEGREE_WINTER = 0  # 0 = a constant. In winter a line or curve predicted the left-out years no better than a constant
CORRECTION_CURVES_FILE = os.path.join(RESULT_DIR, "ndvi_correction_curves.csv")

##############################
# REFERENCE (step 4)
REFERENCE_DIR = os.path.join(OUTPUT_DIR, "reference")  # finished statistics files
SCENE_CACHE_DIR = os.path.join(OUTPUT_DIR, "scene_cache")  # index per scene, deleted when no longer needed
SCENE_LIST_FILE = os.path.join(OUTPUT_DIR, "reference_scenes.csv")  # all Landsat scenes with their quality check

# Which files to build. For TIME_STEP "DOY" these are days of the year (1-366), for "M" and "MM" months (1-12).
# For all DOY files use list(range(1, 367)), for all months list(range(1, 13)).
REFERENCE_PERIODS = list(range(1, 367))  # all DOYs (DOY 366 is also stored as DOY 000)
APPLY_CORRECTION = True  # add the correction curve of step 3 to the Landsat index

# Upload every finished file to S3 and delete it locally, because all files together do not fit on the disk.
# A file that is already on S3 is not built again. The DOY 366 file is also uploaded as DOY 000 (same content),
# as for the current reference files.
UPLOAD_TO_S3 = True
S3_SECRETS_FILE = os.path.join(REPO_DIR, "secrets", "s3_prod.json")  # the same credentials as the main pipeline
S3_BUCKET = "s3-topo-satromo-prod"
S3_FOLDERS = {
    "DOY": "data/NDVI_REFERENCE/1991-2020_NDVI_SWISS_V2/",
    "M": "data/NDVI_REFERENCE/1991-2020_NDVI_SWISS_M_V2/",
    "MM": "data/NDVI_REFERENCE/1991-2020_NDVI_SWISS_MM_V2/",
}

MIN_VALID_PERCENT = 2  # same as the GEE script: scenes with fewer valid pixels are skipped
SCENE_WORKERS = 8  # Landsat scenes processed at the same time
STATS_BLOCK_SIZE = 1024  # the statistics are calculated in blocks of this many pixels per side

# Masks, the same as in the sensor comparison (step 1)
LANDSAT_QA_BITS_TO_MASK = 0b111111  # fill, dilated cloud, cirrus, cloud, cloud shadow, snow

# Topographic correction (SCSc, Soenen et al. 2005), the same thresholds as the GEE script
TOPO_MIN_SLOPE_DEGREES = 5  # flatter pixels are not corrected
TOPO_MIN_ILLUMINATION = 0.1  # pixels with a lower illumination condition are not corrected
TOPO_MAX_FIT_PIXELS = 1000000  # pixels used to fit the relation between illumination and reflectance

# Terrain shadow with HORAYZON, the same code as for swissEO S2-SR (main_functions/main_terrain_module.py)
SHADOW_DEM_FILE = os.path.join(STATIC_DIR, "swissalti3d_2056_10m.tif")
SHADOW_TILE_SIZE = 60000  # [m], a multiple of GRID_RESOLUTION
SHADOW_SEARCH_DIST = 20000  # [m], the same as for swissEO S2-SR
PLANETS = {"path": os.path.join(REPO_DIR, "assets", "planets"), "bsp_file": "de421.bsp"}
