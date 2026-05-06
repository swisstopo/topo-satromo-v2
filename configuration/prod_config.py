# -*- coding: utf-8 -*-
import os

# General variables
# --------------------------

# GitHub repository
GITHUB_OWNER = "swisstopo"
GITHUB_REPO = "topo-satromo-v2"

# Secrets
FSDI_SECRETS = os.path.join("secrets", "stac_fsdi-prod.json")
S3_SECRETS = os.path.join("secrets", "s3_prod.json")
COPERNICUS_SECRETS = os.path.join("secrets", "copernicus_reprocess.json")

# set S3 path
#S3_BUCKET_NAME = "satromoint"
S3_BUCKET_NAME = "s3-topo-satromo-prod"
S3_BUCKET_PATH = "data"


# General product parameters
# ---------------------------
EMPTY_ASSET_LIST = os.path.join("tools", "step0_empty_assets.csv")

# Switzerland border with 10km buffer: [5.78, 45.70, 10.69, 47.89] , Schönbühl [ 7.471940, 47.011335, 7.497431, 47.027602] Martigny [ 7.075402, 46.107098, 7.100894, 46.123639]
# Defines the initial extent to search for image tiles This is not the final extent is defined by BUFFER
# TODO: check if needed in context with step0
ROI_RECTANGLE = [5.78, 45.70, 10.69, 47.89]
ROI_BORDER_BUFFER = 5000  # Buffer around Switzerland

# Switzerland border and lakes with 5km buffer :
BUFFER = os.path.join("assets", "swissboundary_buffer_5000m.gpkg")

OVERVIEW_LAKES = os.path.join("assets", "overview_lakes_2056.gpkg")
OVERVIEW_RIVERS = os.path.join("assets", "overview_rivers_2056.gpkg")

DSM_FILE=os.path.join("local_assets","DSM_10m_EPSG2056_CH_clipped_10km_extended_9999.tif")
GPU_ENFORCEMENT = True # Set to True to enforce GPU usage for AROSICS, False to allow CPU fallback (only for testing purposes)

## PRODUCTS, INDICES and custom COLLECTIONS ###
# ---------------------------

# A) PRODUCTS, INDICES
# ********************


#  ch.swisstopo.swisseo_s2-sr
#Sentinel-2 L2A Band configurations
SENTINEL2_BAND_CONFIG ={
    10:['B02', 'B03', 'B04', 'B08',], # 10m bands: BLUE, GREEN, RED, NIR
    20:['B05', 'B06', 'B07', 'B8A', 'B11', 'B12', 'SCL',], # 20m bands: SWIR and RedEdge bands and SCL
    60:['B01', 'B09', 'AOT',] # 60m bands: Coastal Aerosol  Water Vapor and Aerosol
}

#Sentinel-2 L2A Band Names
SENTINEL2_BAND_NAMES = {
    'B02': "Blue (band 2) - 10m",
    'B03': "Green (band 3) - 10m",
    'B04': "Red (band 4) - 10m",
    'B08': "NIR 1 (band 8) - 10m",
    # 'TCI': "True color image (TCI) - 10m", # TCI calculated by ourselves to avoid oversaturation over snow and clouds
    'CLOUDMASK': "Cloud mask - 10m",
    'B05': "Red edge 1 (band 5) - 20m",
    'B06': "Red edge 2 (band 6) - 20m",
    'B07': "Red edge 3 (band 7) - 20m",
    'B8A': "NIR 2 (band 8A) - 20m",
    'B11': "SWIR 1 (band 11) - 20m",
    'B12': "SWIR 2 (band 12) - 20m",
    'SCL': "Scene classification map (SCL) - 20m",
    'B01': "Coastal aerosol (band 1) - 60m",
    'B09': "NIR 3 (band 9) - 60m",
    'AOT': "Aerosol optical thickness (AOT) - 60m",
}

PRODUCT_S2_LEVEL_2A = {
    "image_collection": "S2_SR_HARMONIZED_SWISS",
    "geocat_id": "a4bc1c7a-3e2f-4d95-9d86-a1a0b09b11a7",
    "temporal_coverage": 1,  # Days # TODO: check if needed in context with V2
    "product_name": "ch.swisstopo.swisseo_s2-sr_v200",
    "copernicus_collection": "sentinel-2-l2a", # local copernnicus STAC Collection
    "band_config": SENTINEL2_BAND_CONFIG,
    "band_names": SENTINEL2_BAND_NAMES,
    "step0_collection": "https://data.geo.admin.ch/#/collections/ch.swisstopo.swisseo_s2-sr_v200" # TODO: check copernicus bucket as step 0 and this as step 1
}



# MSG – MeteoSchweiz: only used for repreocessing
PRODUCT_MSG_CLIMA = {
    #
    # this is  placeholder, needed for the step0 function,
    "image_collection": "METEOSCHWEIZ/MSG",
    "temporal_coverage": 1,  # Days
    "product_name": "ch.meteoschweiz.landoberflaechentemperatur",
    "no_data": 0,
    # 'step0_collection': 'projects/satromo-int/assets/LST_CLIMA_SWISS'
}


# B custom COLLECTION
# ********************
# Contains dictionary used to manage custom collection (asset) in GEE,
# for example to clear old images not used anymore.

# Configure the dict containing
# -  the name of the custom collection (asset) in GEE, (eg: projects/satromo-int/assets/COL_S2_SR_HARMONIZED_SWISS )
# -  the function to process the raw data for teh collection (eg:step0_processor_s2_sr.generate_s2_sr_mosaic_for_single_date )

# Make sure that the products above use the corresponding custom collection (assets)

step0 = {
    "https://data.geo.admin.ch/#/collections/ch.swisstopo.swisseo_s2-sr_v200": {
        'step0_function': 'step1_processor_s2_sr.process_product_s2_sr'
        # cleaning_older_than: 2 # entry used to clean assets
    }
    # 'projects/satromo-int/assets/LST_SWISS': {
    #     'step0_function': 'step0_processor_msg_lst.generate_msg_lst_mosaic_for_single_date'
    #     # cleaning_older_than: 2 # entry used to clean assets
    # },
    # f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS": {
    #     'step0_function': 'step0_processor_csplus.generate_csplus_mosaic_for_single_date'
    #     # cleaning_older_than: 2 # entry used to clean assets
    # }
}



# STAC FSDI
# ---------------

STAC_FSDI_SCHEME = 'https'
STAC_FSDI_HOSTNAME = 'data.geo.admin.ch'
STAC_FSDI_API = '/api/stac/v0.9/'

# C AROSICS configuration
# ***********************
# Contains dictionary used for co-registration of satellite imagery
# using a reference image.

AROSICS_CONFIG = {
    'omnicloud_cloudfree_class': 0,
    'csplus_threshold': 65,
    'cloud_nodata': 255,
    'grid_res_multiplier': 5,
    'max_points': 5000,
    'window_size': [128, 128],
    'max_iter': 10,
    'max_shift': 5,
    'reference_band': 1,
    'reference_image': os.path.join("local_assets", "SI_SPOT5_WGS84_UTM32N_10m_RED_COG.tif"),
    'output_options': ['COMPRESS=DEFLATE', 'PREDICTOR=2', 'NUM_THREADS=ALL_CPUS', 'BIGTIFF=YES'],
    'data_folder': 'sentinel-2-l2a',
    'multiband_mosaic_pattern_10m': 'S2-L2A-multiband_*_10m.vrt',
    'singleband_mosaic_pattern_10m': 'S2-L2A-mosaic_*_B04_10m.vrt',
    'singleband_mosaic_pattern': 'S2-L2A-mosaic_*',
    'cloudprob_tile_pattern': 'S2*_MSIL1C',
    'cloudprob_mosaic_pattern': 'S2-L1C-mosaic_*_cloud.vrt',
    'coreg_file_suffix': '_coreg',
}