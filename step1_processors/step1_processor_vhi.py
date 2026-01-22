import configuration as config

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

product_name = config.PRODUCT_VHI['product_name']
print("********* processing {} *********".format(product_name))

##############################
# SWITCHES
# Enable/disable execution of individual steps

##############################
# CONFIGURATION / PARAMETERS

##############################
# TIME

##############################
# SPACE / ROI

##############################
# INPUT DATA: REFLECTANCE
# Load satellite reflectance data

##############################
# INPUT DATA: TEMPERATURE
# Load temperature/thermal data

##############################
# INPUT DATA: REFERENCE NDVI
# Load or compute long-term NDVI statistics for climate reference period (1991-2020)

##############################
# INPUT DATA: REFERENCE LST
# Load or compute long-term LST statistics for climate reference period (1991-2020)

##############################
# APPLY CLOUD MASK

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

print("********* finished processing {} *********".format(product_name))