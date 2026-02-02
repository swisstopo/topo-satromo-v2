import rasterio
from pystac_client import Client
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
stac_swisstopo = 'https://sys-data.int.bgdi.ch/' # swissTOPO STAC API base URL
stac_swisstopo_version = 'api/stac/v0.9/'
s2_sr_collection_id = 'ch.swisstopo.swisseo_s2-sr_v200' # swissEO S2-SR collection name
s3_bucket_satromo = 's3-topo-satromo-prod/'
s3_path_key_ndvi_ref = 'data/NDVI_REFERENCE/1991-2020_NDVI_SWISS/' # needs file name addition

# Constants
s2_nodata = 0 # NoData value in swissEO S2-SR products
s2_scale_factor = 0.0001 # Scale factor for reflectance values in swissEO S2-SR products
s2_offset = -0.1 # Offset for reflectance values in swissEO S2-SR products
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
roi = (2802000, 1125000, 2809000, 1135000) # ROI in EPSG:2056 (min_x, min_y, max_x, max_y)

                # # Get bounds from GeoPackage for orbit
                # gdf = gpd.read_file(orbit_clipfile)
                # bounds_2056 = gdf.total_bounds  # in EPSG:2056
                # # Transform bounds to EPSG:32632
                # from shapely.geometry import box
                # bbox_gdf = gpd.GeoDataFrame(
                #     geometry=[box(*bounds_2056)],
                #     crs='EPSG:2056'
                # )

############################################################
# INPUT DATA: REFLECTANCE AND MASKS
client = Client.open(stac_swisstopo + stac_swisstopo_version) # connect to STAC API
client.add_conforms_to('COLLECTIONS') # due to the implementation of the swisstopo STAC API, we need to add conformance classes
client.add_conforms_to('ITEM_SEARCH')
s2_sr_collection = client.get_collection(s2_sr_collection_id)

# Filter by date and collection
s2_sr_items = []
for item in s2_sr_collection.get_items():
    if current_date_str in item.id:
        s2_sr_items.append(item.id)
# print(s2_sr_items)

# TODO: currently only works for first item per date -> handle multiple items (tiles) per date
# Get file paths for required bands
item_path = stac_swisstopo + s2_sr_collection_id + '/' + s2_sr_items[0] + '/swisseo_s2-sr_v200_mosaic_' + s2_sr_items[0]
red_path = item_path + '_b04_10m.tif'
nir_path = item_path + '_b08_10m.tif'
green_path = item_path + '_b03_10m.tif'
swir_path = item_path + '_b11_20m.tif'

# Function to load a band and apply offset and scale factors, also preserving nodata values
def load_and_scale_band(filepath, roi, nodata=s2_nodata, scale=s2_scale_factor, offset=s2_offset):
    """
    Load a raster band and apply scaling and offset, preserving nodata values.
    
    Parameters:
    -----------
    filepath : str
        Path to the raster file
    roi : tuple
        Bounding box (minx, miny, maxx, maxy) for windowed reading
    nodata : int or float, optional
        NoData value (default: 0)
    scale : float, optional
        Scale factor (default: 0.0001)
    offset : float, optional
        Offset value (default: -0.1)
    
    Returns:
    --------
    numpy.ndarray
        Scaled band with nodata preserved as np.nan
    """
    with rasterio.open(filepath) as src:
        window = from_bounds(*roi, src.transform)
        data = src.read(1, window=window)
    
    scaled = data.astype(float) # Convert to float for scaled values
    valid_mask = data != nodata # Create mask for valid data (not nodata)
    scaled[valid_mask] = (data[valid_mask] + offset) * scale # Apply scaling only to valid pixels
    scaled[~valid_mask] = np.nan # Set nodata pixels to NaN for easier handling
    
    return scaled

# Load bands and apply offset and scale factor
red = load_and_scale_band(red_path, roi)
nir = load_and_scale_band(nir_path, roi)
green = load_and_scale_band(green_path, roi)
swir = load_and_scale_band(swir_path, roi)

# Load cloud mask
cloud_mask_path = item_path + '_cloudmask_10m.tif'
with rasterio.open(cloud_mask_path) as src_cloud:
    window = from_bounds(*roi, src_cloud.transform)
    cloud_mask = src_cloud.read(1, window=window)

# TODO: Load terrain shadow masks

# Load SCL band
scl_path = item_path + '_scl_20m.tif'
with rasterio.open(scl_path) as src_scl:
    window = from_bounds(*roi, src_scl.transform)
    scl_band = src_scl.read(1, window=window)
# 0: No data, 1: Saturated or defective, 2: Dark area pixels, 3: Cloud shadows,
# 4: Vegetation, 5: Bare soils, 6: Water, 7: Clouds low probability / unclassified,
# 8: Clouds medium probability, 9: Clouds high probability, 10: Thin cirrus,
# 11: Snow or ice

##############################
# CALCULATE SNOW MASK
# From NDSI

# From SCL band

##############################
# APPLY CLOUD, CLOUD SHADOW, TERRAIN SHADOW AND SNOW MASKS
def apply_masks(band, cloudmask=cloud_mask,
                # snowmask=snow_mask, th_snow=threshold_ndsi, # TODO
                # illuminationmask=illumination_mask,  th_illumination=threshold_illumination, # TODO
                ):
    """
    Load a raster band and apply masks.
    
    Parameters:
    -----------
    band : numpy.ndarray
        Input band to be masked 
    cloudmask : numpy.ndarray
        Cloud mask (0=Clear, 1=Thick Cloud, 2=Thin Cloud, 3=Cloud Shadow)
    snowmask : numpy.ndarray
        NDSI (normalized difference snow index) generally used for snow detection
    th_snow : float
        Threshold for snow detection
    illuminationmask : numpy.ndarray
        Illumination mask
    th_illumination : float
        Threshold for illumination detection
    
    Returns:
    --------
    numpy.ndarray
        Masked band with nodata preserved as np.nan
    """
    masked_band = band.copy()

    # Apply cloud mask
    if cloudmask is not None:
        cloud_mask_condition = cloudmask != 0 
        masked_band[cloud_mask_condition] = np.nan

    # TODO: Apply terrain shadow mask
    # if illuminationmask is not None:
    #     shadow_condition = illuminationmask > th_illumination
    #     masked_band[shadow_condition] = np.nan

    # TODO: Apply snow mask based on NDSI
    # if snowmask is not None:
    #     snow_condition = snowmask > th_snow
    #     masked_band[snow_condition] = np.nan

    # TODO: Apply snow mask based on SCL
    
    return masked_band

# Apply masks to bands
red_masked = apply_masks(red)
nir_masked = apply_masks(nir)

##############################
# CALCULATE NDVI
ndvi = (nir_masked - red_masked) / (nir_masked + red_masked)

# Simple plot
import matplotlib.pyplot as plt
plt.figure(figsize=(10, 8))
plt.imshow(ndvi, cmap='RdYlGn')
plt.colorbar()
plt.show()

print('test')

##############################
# INPUT DATA: REFERENCE NDVI
# Load or compute long-term NDVI statistics for climate reference period (1991-2020)
s3_path_ndvi_ref = 's3://' + s3_bucket_satromo + s3_path_key_ndvi_ref + 'NDVI_Stats_DOY' + doy_str + '.tif'

with rasterio.open(s3_path_ndvi_ref) as src_ref:
    # Define window from ROI
    window = from_bounds(*roi, src_ref.transform)

    # Read relevant bands based on the chosen method
    if workWithPercentiles is True:
        ndvi_ref_min = src_ref.read(6, window=window)  # 5th percentile
        ndvi_ref_max = src_ref.read(7, window=window)  # 95th percentile
        # Define confidence interval method
        CI_method = '5th_and_95th_percentile'
    else:
        ndvi_ref_min = src_ref.read(1, window=window)  # minimum
        ndvi_ref_max = src_ref.read(2, window=window)  # maximum
        CI_method = 'min_and_max'

##############################
# CALCULATE VCI

# Resample reference NDVI to match current NDVI resolution
# TODO

# VCI = 100 * (NDVI - NDVI_min) / (NDVI_max - NDVI_min)
vci = 100 * (ndvi - ndvi_ref_min) / (ndvi_ref_max - ndvi_ref_min)

############################################################
# INPUT DATA: TEMPERATURE
# Load temperature/thermal data

##############################
# CALCULATE LST
# From temperature data

##############################
# INPUT DATA: REFERENCE LST
# Load or compute long-term LST statistics for climate reference period (1991-2020)

##############################
# CALCULATE TCI
# TCI = 100 * (LST_max - LST) / (LST_max - LST_min)

############################################################
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