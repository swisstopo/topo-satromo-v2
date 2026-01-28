import rasterio
import os
import numpy as np
# import configuration as config
from datetime import datetime
from rasterio.windows import from_bounds

##############################
# INTRODUCTION
# This script provides a tool to process vegetation health index (VHI) data over Switzerland
# It uses reference data for NDVI and LST from SATROMO assets and calculates the current NDVI
# from swissEO S2-SR products as well as LST from radiance data.

##############################
# CONTENT
# The switches enable / disable the execution of individual steps in this script

# This script includes the following steps:
# 1. Calculating the NDVI and LST data for a specific date
# 2. Calculating the VCI from a specific date and the NDVI reference
# 3. Calculating the TCI from a specific date and the LST reference
# 4. Combining the VCI and TCI to generate the VHI
# 5. Masking for forest or all vegetation
# 6. Generating and updating metadata files
# 7. Exporting the resulting VHI

##############################
# PROCESSING FUNCTION
# def process_product_vhi(
#     day_to_process: str, 
#         collection: str, 
#         roi: tuple[float, float, float, float] | None = None
#     ) -> None:
#         """
#         Process Vegetation Health Index for a given day.
        
#         Args:
#             day_to_process: Date string (e.g., 'YYYY-MM-DD')
#             collection: Name of the data collection to process
#             roi: Optional bounding box as (min_x, min_y, max_x, max_y) in EPSG:2056.
#                 If None, processes all available data.
#         """

# product_name = config.PRODUCT_VHI['product_name']
# print("********* processing {} *********".format(product_name))

##############################
# SWITCHES
# Enable/disable execution of individual steps

workWithPercentiles = True
# options: True, False - defines if the p05 and p95 percentiles of the reference data sets are used,
# otherwise the min and max will be used (False)

##############################
# CONFIGURATION / PARAMETERS
# Paths
s3_bucket = 's3://s3-topo-satromo-prod/'
s3_path_key_ndvi_ref = 'data/NDVI_REFERENCE/1991-2020_NDVI_SWISS/' # needs file name addition

# Constants
alpha = 0.5 # Weighting factor for VHI calculation (0.5 means equal weight for VCI and TCI)
no_data = 255 # Value used for pixels with no input data
missing_data = 110 # Value used for pixels where data is missing (e.g., cloud-covered areas)
threshold_ndsi = 0.43 # values equal or above indicate snow
threshold_illumination = 0.65 # values equal or above indicate insufficient illumination

# Environments
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES' # to access public S3 buckets without credentials

##############################
# TIME
current_date_str = "2025-06-01" # TODO: replace with dynamic date input from config / satromo_processor.py

doy = datetime.strptime(current_date_str, '%Y-%m-%d').timetuple().tm_yday
doy_str = f'{doy:03d}' # zero-padded three-digit day of year

##############################
# SPACE / ROI
roi = (2802000, 1125000, 2809000, 1135000) # Example ROI in EPSG:2056 (min_x, min_y, max_x, max_y)

##############################
# INPUT DATA: REFLECTANCE
# Load satellite reflectance data
# 'https://sys-data.int.bgdi.ch/ch.swisstopo.swisseo_s2-sr_v200/2025-06-01t101041/swisseo_s2-sr_v200_mosaic_2025-06-01t101041_b04_10m.tif'

s3_path_reflectance = 's3://sys-data.int.bgdi.ch/ch.swisstopo.swisseo_s2-sr_v200/'



# Simple plot
import matplotlib.pyplot as plt
plt.figure(figsize=(10, 8))
plt.imshow(ndvi, cmap='RdYlGn')
plt.show()

print('test')


##############################
# INPUT DATA: TEMPERATURE
# Load temperature/thermal data

##############################
# INPUT DATA: REFERENCE NDVI
# Load or compute long-term NDVI statistics for climate reference period (1991-2020)
s3_path_ndvi_ref = s3_bucket + s3_path_key_ndvi_ref + 'NDVI_Stats_DOY' + doy_str + '.tif'

with rasterio.open(s3_path_ndvi_ref) as src:
    # Define window from ROI
    window = from_bounds(*roi, src.transform)

    # Read relevant bands based on the chosen method
    if workWithPercentiles is True:
        ndvi_ref_min = src.read(6, window=window)  # 5th percentile
        ndvi_ref_max = src.read(7, window=window)  # 95th percentile
        # Define confidence interval method
        CI_method = '5th_and_95th_percentile'
    else:
        ndvi_ref_min = src.read(1, window=window)  # minimum
        ndvi_ref_max = src.read(2, window=window)  # maximum
        CI_method = 'min_and_max'



##############################
# INPUT DATA: REFERENCE LST
# Load or compute long-term LST statistics for climate reference period (1991-2020)

##############################
# APPLY CLOUD, CLOUD SHADOW AND TERRAIN SHADOW MASKS

##############################
# CALCULATE AND APPLY SNOW MASK

##############################
# CALCULATE NDVI
# From reflectance data

##############################
# CALCULATE LST
# From temperature data

##############################
# CALCULATE VCI
# VCI = 100 * (NDVI - NDVI_min) / (NDVI_max - NDVI_min)

##############################
# CALCULATE TCI
# TCI = 100 * (LST_max - LST) / (LST_max - LST_min)

##############################
# CALCULATE VHI
# VHI = a*VCI + (1-a)*TCI

##############################
# APPLY VEGETATION MASK

##############################
# GENERATE METADATA

##############################
# EXPORT VHI
# Save to file with metadata

# print("********* finished processing {} *********".format(product_name))