# -*- coding: utf-8 -*-
import os

# General variables
# --------------------------

# GitHub repository
GITHUB_OWNER = "swisstopo"
GITHUB_REPO = "topo-satromo-v2"

# Secrets
GOOGLE_SECRETS = os.path.join("secrets", "geetest-credentials-int.secret")
FSDI_SECRETS = os.path.join("secrets", "stac_fsdi-int.json")
S3_SECRETS = os.path.join("secrets", "s3_int.json")
COPERNICUS_SECRETS = os.path.join("secrets", "copernicus_oed.json")


# File and directory paths
GEE_RUNNING_TASKS = os.path.join("processing", "running_tasks.csv")
GEE_COMPLETED_TASKS = os.path.join("tools", "completed_tasks.csv")
EMPTY_ASSET_LIST = os.path.join("tools", "step0_empty_assets.csv")
PROCESSING_DIR = "processing"
LAST_PRODUCT_UPDATES = os.path.join("tools", "last_updates.csv")

# Set GCS Bucket name of Google Cloud Storage
GCLOUD_BUCKET = "s2_sr_registration_swiss"

# set S3 path
S3_BUCKET_NAME = "satromoint"
#S3_BUCKET_NAME = "s3-topo-satromo-prod"
S3_BUCKET_PATH="data"



# General product parameters
# ---------------------------

# Coordinate Reference System (EPSG:4326 for WGS84, EPSG:2056 for CH1903+, see epsg.io)
OUTPUT_CRS = "EPSG:2056"


# Switzerland border with 10km buffer: [5.78, 45.70, 10.69, 47.89] , Schönbühl [ 7.471940, 47.011335, 7.497431, 47.027602] Martigny [ 7.075402, 46.107098, 7.100894, 46.123639]
# Defines the initial extent to search for image tiles This is not the final extent is defined by BUFFER
# TODO: check if needed in context with step0
ROI_RECTANGLE = [5.78, 45.70, 10.69, 47.89]
ROI_BORDER_BUFFER = 5000  # Buffer around Switzerland

# Switzerland border and lakes with 5km buffer :
BUFFER = os.path.join("assets", "swissboundary_buffer_5000m.gpkg")

# No data value
NODATA = 9999



## PRODUCTS, INDICES and custom COLLECTIONS ###
# ---------------------------
# See https://github.com/swisstopo/topo-satromo/tree/main?tab=readme-ov-file#configuration-in-_configpy for details
# TL;DR : First define in A) PRODUCTS, INDICES: for step0 (cloud, shadow, co-register, mosaic) the TOA SR data  custom  "step0_collection" to be generated / used
# then

#Sentinel-2 L2A Band configurations
SENTINEL2_BAND_CONFIG ={
    10:['B02', 'B03', 'B04', 'B08', 'TCI',], # 10m bands: BLUE, GREEN, RED, NIR
    20:['B05', 'B06', 'B07', 'B8A', 'B11', 'B12', 'SCL',], # 20m bands: SWIR and RedEdge bands and SCL
    60:['B01', 'B09', 'AOT',] # 60m bands: Coastal Aerosol  Water Vapor and Aerosol
}

#Sentinel-2 L2A Band Names
SENTINEL2_BAND_NAMES = {
    'B02': "Blue (band 2) - 10m",
    'B03': "Green (band 3) - 10m",
    'B04': "Red (band 4) - 10m",
    'B08': "NIR 1 (band 8) - 10m",
    'TCI': "True color image (TCI) - 10m",
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

# A) PRODUCTS, INDICES
# ********************

#  ch.swisstopo.swisseo_s2-sr
PRODUCT_S2_LEVEL_CSPLUS = {
    "image_collection": "S2_SR_HARMONIZED_SWISS",
    "temporal_coverage": 1,  # Days
    "step0_collection": f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS"
}
#  ch.swisstopo.swisseo_s2-sr
PRODUCT_S2_LEVEL_2A = {
    "image_collection": "S2_SR_HARMONIZED_SWISS",
    "geocat_id": "7ae5cd5b-e872-4719-92c0-dc2f86c4d471",
    "temporal_coverage": 1,  # Days # TODO: check if needed in context with V2
    "spatial_scale_export": 10,  # Meters # TODO: check if needed in context with V2
    "asset_size": 5, # TODO: check if needed in context with V2
    "spatial_scale_export_mask": 10, # TODO: check if needed in context with V2
    "product_name": "ch.swisstopo.swisseo_s2-sr_v200",
    "no_data": 9999,
    "band_config": SENTINEL2_BAND_CONFIG,
    "band_names": SENTINEL2_BAND_NAMES,
    "step0_collection": f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS"
}

# VHI – Trockenstress ch.swisstopo.swisseo_vhi_v100
PRODUCT_VHI = {
    # TODO: check if needed in context with step0
    "image_collection": "COPERNICUS/S2_SR_HARMONIZED",
    "geocat_id": "bc4d0e6b-e92e-4f28-a7d2-f41bf61e98bc",
    "temporal_coverage": 7,  # Days
    "spatial_scale_export": 10,  # Meters
    "product_name": "ch.swisstopo.swisseo_vhi_v100",
    "no_data": 255,
    "missing_data": 110,
    "asset_size": 2,
    'NDVI_reference_data': 'projects/satromo-prod/assets/col/1991-2020_NDVI_SWISS',
    'LST_reference_data': 'projects/satromo-prod/assets/col/1991-2020_LST_SWISS',
    'LST_current_data': 'projects/satromo-prod/assets/col/LST_SWISS',
    "step1_collection": 'projects/satromo-prod/assets/col/VHI_SWISS',
    #"step0_collection": "projects/satromo-prod/assets/col/S2_SR_HARMONIZED_SWISS"
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
    # 'projects/satromo-int/assets/COL_S2_SR_HARMONIZED_SWISS': { # TODO change to STAC BGDI SOURCE
    #     'step0_function': 'step0_processor_s2_sr.generate_s2_sr_mosaic_for_single_date'
    #     # cleaning_older_than: 2 # entry used to clean assets
    # },
    # 'projects/satromo-int/assets/LST_SWISS': {
    #     'step0_function': 'step0_processor_msg_lst.generate_msg_lst_mosaic_for_single_date'
    #     # cleaning_older_than: 2 # entry used to clean assets
    # },
    f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS": {
        'step0_function': 'step0_processor_csplus.generate_csplus_mosaic_for_single_date'
        # cleaning_older_than: 2 # entry used to clean assets
    }
}




# STAC FSDI
# ---------------

STAC_FSDI_SCHEME = 'https'
STAC_FSDI_HOSTNAME = 'sys-data.int.bgdi.ch'
STAC_FSDI_API = '/api/stac/v0.9/'


# C AROSICS configuration
# ***********************
# Contains dictionary used for co-registration of satellite imagery
# using a reference image.

AROSICS_CONFIG = {
    'cloud_threshold': 65,
    'csplus_threshold': 65,
    'cloud_nodata': 255,
    'grid_res_multiplier': 5,
    'max_points': 5000,
    'window_size': [128, 128],
    'max_iter': 10,
    'max_shift': 5,
    'reference_band': 1,
    #'reference_image': '/mnt/d/SATROMO/AROSICS_Coregistration/AROSICS/assets/base_data/SI_SPOT5_WGS84_UTM32N_5m_RED_COG.tif',
    'reference_image': '/mnt/d/SATROMO/AROSICS_Coregistration/AROSICS/assets/base_data/SI_SPOT5_WGS84_UTM32N_10m_RED_COG.tif',
    # 'reference_image': '/mnt/d/SATROMO/AROSICS_Coregistration/AROSICS/assets/base_data/S2_GRI.tif',
    #'output_options': ['COMPRESS=DEFLATE', 'PREDICTOR=2', 'NUM_THREADS=ALL_CPUS'],
    'output_options': ['COMPRESS=DEFLATE', 'PREDICTOR=2', 'NUM_THREADS=ALL_CPUS', 'BIGTIFF=YES'],
    'multiband_mosaic_pattern_10m': 'S2-L2A-multiband_*_10m.vrt',
    'singleband_mosaic_pattern_10m': 'S2-L2A-mosaic_*_B04_10m.vrt',
    'singleband_mosaic_pattern': 'S2-L2A-mosaic_*',
    'cloudprob_tile_pattern': 'S2*_MSIL1C',
    'cloudprob_mosaic_pattern': 'S2-L1C-mosaic_*_cloud.vrt',
    'coreg_file_suffix': '_coreg',
    'omnicloudmask_venv_path': '.venv/omnicloud_venv/bin/python3',
    'omnicloudmask_script_path': 'main_functions/main_omnicloudmask.py',
}
