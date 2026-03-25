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
S3_SECRETS = os.path.join("secrets", "s3_prod_ebp.json")


# File and directory paths
GEE_RUNNING_TASKS = os.path.join("processing", "running_tasks.csv")
GEE_COMPLETED_TASKS = os.path.join("tools", "completed_tasks.csv")
EMPTY_ASSET_LIST = os.path.join("tools", "step0_empty_assets.csv")
PROCESSING_DIR = "processing"
LAST_PRODUCT_UPDATES = os.path.join("tools", "last_updates.csv")

# Set GCS Bucket name of Google Cloud Storage
GCLOUD_BUCKET = "s2_sr_registration_swiss" # TODO chnage to EBP

# set S3 path
S3_BUCKET_NAME = "s3-topo-satromo-prod"
S3_BUCKET_PATH="data"



# General product parameters
# ---------------------------
# Switzerland border with 10km buffer: [5.78, 45.70, 10.69, 47.89] , Schönbühl [ 7.471940, 47.011335, 7.497431, 47.027602] Martigny [ 7.075402, 46.107098, 7.100894, 46.123639]
# Defines the initial extent to search for image tiles This is not the final extent is defined by BUFFER
# TODO: check if needed in context with step0
ROI_RECTANGLE = [5.78, 45.70, 10.69, 47.89]
ROI_BORDER_BUFFER = 5000  # Buffer around Switzerland

# No data value
NODATA = 9999


## PRODUCTS, INDICES and custom COLLECTIONS ###
# ---------------------------
# See https://github.com/swisstopo/topo-satromo/tree/main?tab=readme-ov-file#configuration-in-_configpy for details
# TL;DR : First define in A) PRODUCTS, INDICES: for step0 (cloud, shadow, co-register, mosaic) the TOA SR data  custom  "step0_collection" to be generated / used
# then

# A) PRODUCTS, INDICES
# ********************

#  ch.swisstopo.swisseo_s2-sr
PRODUCT_S2_LEVEL_CSPLUS = {
    "image_collection": "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED",
    "temporal_coverage": 1,  # Days
    "step0_collection": f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS"
}
#  ch.swisstopo.swisseo_s2-sr
PRODUCT_S2_LEVEL_2A = {
    # "prefix": "S2_L2A_SR",
    # TODO: check if needed in context with step0
    "image_collection": "COPERNICUS/S2_SR_HARMONIZED",
    "geocat_id": "7ae5cd5b-e872-4719-92c0-dc2f86c4d471",
    "temporal_coverage": 1,  # Days
    "spatial_scale_export": 10,  # Meters # TODO: check if needed in context with step0
    "asset_size": 5,
    "spatial_scale_export_mask": 10,
    "product_name": "ch.swisstopo.swisseo_s2-sr_v100",
    "no_data": 9999,
    #"step0_collection": f"s3://{S3_BUCKET_NAME}/data/CLOUD_SCORE_PLUS"
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
    # 'projects/satromo-exolabs/assets/col_s2_toa': {
    #    'step0_function': 'step0_processor_s2_toa.generate_s2_toa_mosaic_for_single_date',
    #    # cleaning_older_than: 2 # entry used to clean assets
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
STAC_FSDI_HOSTNAME = 'data.geo.admin.ch'
STAC_FSDI_API = '/api/stac/v0.9/'
