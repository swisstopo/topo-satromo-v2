import rasterio
import xarray as xr
from pystac_client import Client
import os
import numpy as np
# import configuration as config
from datetime import datetime, timedelta
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_bounds, Resampling
from pyproj import Transformer
from affine import Affine
import matplotlib.pyplot as plt

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
maskSnowWithNDSI = False
# options: True, False - defines if snow masking is applied based on NDSI values
# otherwise the SCL band will be used (False)

##############################
# CONFIGURATION / PARAMETERS
# Paths
stac_swisstopo = 'https://sys-data.int.bgdi.ch/' # swissTOPO STAC API base URL TODO: change S2-SR input data to prod path
stac_swisstopo_prod = 'https://data.geo.admin.ch/'
stac_swisstopo_version = 'api/stac/v0.9/'
s2_sr_collection_id = 'ch.swisstopo.swisseo_s2-sr_v200' # swissEO S2-SR collection name
lst_collection_id = 'ch.meteoschweiz.landoberflaechentemperatur'
s3_bucket_satromo = 's3-topo-satromo-prod/'
s3_path_key_ndvi_ref = 'data/NDVI_REFERENCE/1991-2020_NDVI_SWISS/'
s3_path_key_lst_ref = 'data/LST_REFERENCE/2004-2020_LST_MSGch02/' # options: 2004-2020_LST_MSGch02, 2004-2020_LST_MSGch02_M, 2004-2020_LST_MSGch05_M
lst_aggregation = '11am' # options: 'mean', 'max', '11am'
lst_ref_file = f'_MSG_ch02_2004-2020_7days_{lst_aggregation}' # MSG (2004-2020) / MFG (1991-2003)

# Constants
s2_nodata = 0 # NoData value in swissEO S2-SR products
s2_scale_factor = 0.0001 # Scale factor for reflectance values in swissEO S2-SR products
s2_offset = -0.1 # Offset for reflectance values in swissEO S2-SR products
ref_ndvi_nodata = 255 # NoData value in reference NDVI statistics
ref_ndvi_scale_factor = 0.01 # Scale factor for reference NDVI statistics
ref_ndvi_offset = -100 # Offset for reference NDVI statistics
alpha = 0.5 # Weighting factor for VHI calculation (0.5 means equal weight for VCI and TCI)
no_data = 255 # Value used for pixels with no input data
missing_data = 110 # Value used for pixels where data is missing (e.g., cloud-covered areas)
threshold_ndsi = 0.43 # values equal or above indicate snow
threshold_illumination = 0.65 # values equal or above indicate insufficient illumination

# Environments
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES' # to access public S3 buckets without credentials

##############################
# TIME
current_date_str = "2026-03-03" # TODO: replace with dynamic date input from config / satromo_processor.py
current_date = datetime.strptime(current_date_str, '%Y-%m-%d')
# 2026-03-19
doy = current_date.timetuple().tm_yday
doy_str = f'{doy:03d}' # zero-padded three-digit day of year

year = current_date_str[:4]
month = current_date_str[5:7]
day = current_date_str[8:10]

##############################
# SPACE / ROI
# Set to None for operational mode, or define a bbox in EPSG:2056 for testing
roi_override = None
# roi_override = (2802000, 1125000, 2809000, 1135000) # ROI in EPSG:2056 (min_x, min_y, max_x, max_y)

############################################################
# INPUT DATA: REFLECTANCE, NDVI CALCULATION AND MASKS
client = Client.open(stac_swisstopo + stac_swisstopo_version) # connect to STAC API
client.add_conforms_to('COLLECTIONS') # due to the implementation of the swisstopo STAC API, we need to add conformance classes
client.add_conforms_to('ITEM_SEARCH')
s2_sr_collection = client.get_collection(s2_sr_collection_id)

# Filter by date and collection
s2_sr_items = []
for item in s2_sr_collection.get_items():
    if current_date_str in item.id:
        s2_sr_items.append(item.id)
print(f'Starting the VHI calculation for {current_date_str}')

# TODO: currently only works for first item per date -> handle multiple items (tiles) per date
# Get file paths for required bands
item_path = stac_swisstopo + s2_sr_collection_id + '/' + s2_sr_items[0] + '/swisseo_s2-sr_v200_mosaic_' + s2_sr_items[0]
red_path = item_path + '_b04_10m.tif'
nir_path = item_path + '_b08_10m.tif'
green_path = item_path + '_b03_10m.tif' # needed for NDSI-based snow masking
swir_path = item_path + '_b11_20m.tif' # needed for NDSI-based snow masking, will be resampled to 10m grid

# Resolve ROI
if roi_override is not None:
    roi = roi_override
else:
    # Derive from S2 asset extent (operational mode)
    with rasterio.open(red_path) as src:
        b = src.bounds
        roi = (b.left, b.bottom, b.right, b.top)

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
        scaled = src.read(1, window=window, out_dtype=np.float32)
    
    nodata_mask = scaled == nodata # Create nodata mask before modifying data
    # Apply scaling in-place on the full array — no temporary copy
    scaled += offset
    scaled *= scale
    
    # Restore nodata pixels to NaN
    scaled[nodata_mask] = np.nan
    
    return scaled

# Establish 10m grid from any 10m band (needed for resampling 20m bands and to ensure all bands have the same grid, transform and shape)
with rasterio.open(red_path) as src:
    window_10m = from_bounds(*roi, src.transform)
    target_transform = src.window_transform(window_10m)
    target_shape = (int(window_10m.height), int(window_10m.width))

# Function to load a 20m band, resample to 10m grid and apply offset and scale factors
def load_scale_and_resample_20m_to_10m(filepath, roi, target_transform, target_shape,
                                   nodata=s2_nodata, scale=s2_scale_factor, offset=s2_offset):
    """
    Load a raster band and apply scaling and offset, preserving nodata values.
    Resample 20m band to 10m grid.
    
    Parameters:
    -----------
    filepath : str
        Path to the raster file
    roi : tuple
        Bounding box (minx, miny, maxx, maxy) for windowed reading
    target_transform : affine.Affine
        Target transform for resampling to 10m grid
    target_shape : tuple
        Target shape (height, width) for resampling to 10m grid
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
        data_20m = src.read(1, window=window, out_dtype=np.float32)
        transform_20m = src.window_transform(window)    
    
    # Resample to 10m grid
    data_10m = np.empty(target_shape, dtype=np.float32)
    reproject(
        source=data_20m,
        destination=data_10m,
        src_transform=transform_20m,
        src_crs=src.crs,
        dst_transform=target_transform,
        dst_crs=src.crs,
        resampling=Resampling.nearest,
        src_nodata=nodata,
        dst_nodata=np.nan
    )
    del data_20m  # free memory of the original 20m data array as soon as possible

    # Apply scaling and offset
    nodata_mask = data_10m == nodata # Create nodata mask before modifying data
    # Apply scaling in-place on the full array — no temporary copy
    data_10m += offset
    data_10m *= scale
    
    # Restore nodata pixels to NaN
    data_10m[nodata_mask] = np.nan

    return data_10m

# Load 10 m bands and apply offset and scale factor to reflectance bands
red = load_and_scale_band(red_path, roi)
nir = load_and_scale_band(nir_path, roi)

# CALCULATE NDVI --> ndvi = (nir - red) / (nir + red)
ndvi_den = nir + red
ndvi_den[ndvi_den == 0] = np.nan
ndvi = (nir - red) / ndvi_den
del ndvi_den, red, nir
print(f'Calculated NDVI for the current item: {s2_sr_items}') #TODO: update to handle multiple items per date


##############################
# LOAD/CALCULATE AND APPLY MASKS
# --- CLOUD mask (10m)
cloud_mask_path = item_path + '_cloudmask_10m.tif'
with rasterio.open(cloud_mask_path) as src_cloud:
    window = from_bounds(*roi, src_cloud.transform)
    cloud_mask = src_cloud.read(1, window=window)

# --- TERRAIN SHADOW mask (10m) #TODO

# ---- SNOW mask based on NDSI or SCL (20m, resampled to 10m)
if maskSnowWithNDSI is True:
    # Load green and SWIR bands only for snow masking based on NDSI, to save processing time and memory
    green = load_and_scale_band(green_path, roi)
    swir = load_scale_and_resample_20m_to_10m(swir_path, roi, target_transform, target_shape)
    # NDSI --> ndsi = (green - swir) / (green + swir)
    ndsi = green - swir # numerator
    ndsi_den = green + swir # denominator
    ndsi_den[ndsi_den == 0] = np.nan # avoid division by zero
    ndsi /= ndsi_den  # divide in-place
    del ndsi_den
    # Create snow mask based on NDSI
    snow_mask = np.zeros_like(ndsi, dtype=np.uint8)
    snow_mask[ndsi > threshold_ndsi] = 1  # 1 indicates snow
else:
    # Load SCL band only for snow masking based on SCL, to save processing time and memory
    scl_path = item_path + '_scl_20m.tif'
    # SCL classification values:
    # 0: No data, 1: Saturated or defective, 2: Dark area pixels, 3: Cloud shadows,
    # 4: Vegetation, 5: Bare soils, 6: Water, 7: Clouds low probability / unclassified,
    # 8: Clouds medium probability, 9: Clouds high probability, 10: Thin cirrus,
    # 11: Snow or ice
    scl = load_scale_and_resample_20m_to_10m(scl_path, roi, target_transform, target_shape,
                                        nodata=0, scale=1, offset=0) # no scaling for SCL
    # Create snow mask based on SCL
    snow_mask = np.zeros_like(scl, dtype=np.uint8)
    snow_mask[scl == 11] = 1  # 1 indicates snow

#TODO: add terrain shadow masking 
# Function to apply masks (clouds, snow, terrain shadow) to a specific band
def apply_masks(band, cloudmask=cloud_mask, snowmask=snow_mask,
                # illuminationmask=illumination_mask,  th_illumination=threshold_illumination, # TODO
                ):
    """
    Apply masks to a specific band.
    
    Parameters:
    -----------
    band : numpy.ndarray
        Input band to be masked 
    cloudmask : numpy.ndarray
        Cloud mask (0=Clear, 1=Thick Cloud, 2=Thin Cloud, 3=Cloud Shadow)
    snowmask : numpy.ndarray
        Snow mask (0=Clear, 1=Snow)
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
        print('- Applied cloud mask based the classification from OmniCloudMask')

    # Apply snow mask
    if snowmask is not None:
        snow_condition = snowmask != 0 
        masked_band[snow_condition] = np.nan
        if maskSnowWithNDSI is True:
            print(f'- Applied snow mask based on NDSI with threshold {threshold_ndsi}')
        else:
            print(f'- Applied snow mask based on SCL class 11 (snow/ice)')
        
    # TODO: Apply terrain shadow mask
    # if illuminationmask is not None:
    #     shadow_condition = illuminationmask > th_illumination
    #     masked_band[shadow_condition] = np.nan
    #     print(f'- Applied terrain shadow mask and removed areas of insufficient illumination (threshold: {threshold_illumination})')
    
    return masked_band

# Apply masks to NDVI
ndvi_masked = apply_masks(ndvi)
del ndvi, cloud_mask, snow_mask #, terrain_shadow_mask, illumination_mask # free memory of original arrays as soon as possible

##############################
# INPUT DATA: REFERENCE NDVI
# Load or compute long-term NDVI statistics for climate reference period (1991-2020)
s3_path_ndvi_ref = 's3://' + s3_bucket_satromo + s3_path_key_ndvi_ref + 'NDVI_Stats_DOY' + doy_str + '.tif'

with rasterio.open(s3_path_ndvi_ref) as src_ref:
    # Define window from ROI
    window = from_bounds(*roi, src_ref.transform)

# Function to resample 30m reference NDVI to match current NDVI resolution
def load_scale_and_resample_ndvi_reference(filepath, roi, target_transform, target_shape, band_num,
                                      nodata=ref_ndvi_nodata, scale=ref_ndvi_scale_factor, offset=ref_ndvi_offset):
    """
    Load a raster band and resample to target grid.

    Parameters:
    -----------
    filepath : str
        Path to the raster file
    roi : tuple
        Bounding box (minx, miny, maxx, maxy) for windowed reading
    target_transform : affine.Affine
        Target transform for resampling
    target_shape : tuple
        Target shape (height, width) for resampling
    band_num : int
        Band number to read from the raster file
    
    Returns:
    --------
    numpy.ndarray
        Resampled band
    """
    from rasterio.warp import reproject
    
    with rasterio.open(filepath) as src:
        window = from_bounds(*roi, src.transform)
        data = src.read(band_num, window=window, out_dtype=np.float32)
        src_transform = src.window_transform(window)
    
    resampled = np.empty(target_shape, dtype=np.float32)
    reproject(
        source=data,
        destination=resampled,
        src_transform=src_transform,
        src_crs=src.crs,
        dst_transform=target_transform,
        dst_crs=src.crs,
        resampling=Resampling.nearest
    )
    del data  # free memory of the original data array as soon as possible
    
    # Apply scaling and offset
    nodata_mask = resampled == nodata # Create nodata mask before modifying data
    # Apply scaling in-place on the full array — no temporary copy
    resampled += offset
    resampled *= scale
    
    # Restore nodata pixels to NaN
    resampled[nodata_mask] = np.nan

    return resampled
print('Loaded reference NDVI statistics for current day of year')

# Read relevant bands based on the chosen method
if workWithPercentiles is True:
    ndvi_ref_min = load_scale_and_resample_ndvi_reference(s3_path_ndvi_ref, roi, target_transform, target_shape, band_num=6)  # 5th percentile
    ndvi_ref_max = load_scale_and_resample_ndvi_reference(s3_path_ndvi_ref, roi, target_transform, target_shape, band_num=7)  # 95th percentile
    # Define confidence interval method
    CI_method = '5th_and_95th_percentile'
    print('- Using percentiles for VCI calculation')
else:
    ndvi_ref_min = load_scale_and_resample_ndvi_reference(s3_path_ndvi_ref, roi, target_transform, target_shape, band_num=1)  # minimum
    ndvi_ref_max = load_scale_and_resample_ndvi_reference(s3_path_ndvi_ref, roi, target_transform, target_shape, band_num=2)  # maximum
    CI_method = 'min_and_max'
    print('- Using min and max for VCI calculation')

##############################
# CALCULATE VCI
# VCI = 100 * (NDVI - NDVI_min) / (NDVI_max - NDVI_min)
vci_den = ndvi_ref_max - ndvi_ref_min # denominator
vci_den[vci_den == 0] = np.nan # avoid division by zero
vci = ndvi_masked - ndvi_ref_min  # numerator, reuse ndvi name or new var
vci /= vci_den # divide in-place
del ndvi_masked, ndvi_ref_min, ndvi_ref_max, vci_den
vci *= 100  # scale in-place
print('Calculated VCI')

############################################################
# INPUT DATA: TEMPERATURE
# Load surface downwelling longwave radiation (SDL) and surface outgoing longwave radiation (SOL) data for the specific date

# TODO: update to handle operational data since begining of 2026 or older data stored locally
if current_date < datetime(2024, 1 ,1):
    sdl_path = f'{stac_swisstopo_prod}{lst_collection_id}/MSG2004-2023/msg.SDL.H_ch02.lonlat_{year}{month}01000000.nc'
    sol_path = f'{stac_swisstopo_prod}{lst_collection_id}/MSG2004-2023/msg.SOL.H_ch02.lonlat_{year}{month}01000000.nc'
else:
    sdl_path = f'{stac_swisstopo_prod}{lst_collection_id}/msg.SDL.H_ch02.lonlat_{year}{month}{day}000000.nc'
    sol_path = f'{stac_swisstopo_prod}{lst_collection_id}/msg.SOL.H_ch02.lonlat_{year}{month}{day}000000.nc'

ds_sdl = xr.open_dataset(sdl_path, engine='h5netcdf')
ds_sol = xr.open_dataset(sol_path, engine='h5netcdf')

##############################
# CALCULATE LST
# Function to calculate LST from radiance
def calc_LST_for_date(ds_sol, ds_sdl, date, aggregation='hour', hour=None):
    """
    Calculate LST for a specific date with flexible aggregation options.
    
    Args:
        ds_sol: xarray Dataset with SOL data (already loaded)
        ds_sdl: xarray Dataset with SDL data (already loaded)
        date: date string in format 'YYYY-MM-DD'
        aggregation: 'max', 'mean', or 'hour' (default: 'hour')
        hour: Specific hour (0-23) when aggregation='hour' (e.g., 11 for 11am)
    
    Returns:
        xarray Dataset with calculated LST
    """
    # Convert date string to datetime
    target_date = datetime.strptime(date, '%Y-%m-%d')

    # Define time range for the full day
    start_time = target_date
    end_time = target_date + timedelta(days=1) - timedelta(seconds=1)

    # Filter data for the specific date
    sol_filtered = ds_sol.sel(time=slice(start_time, end_time))
    sdl_filtered = ds_sdl.sel(time=slice(start_time, end_time))

    # Check if we have data for the target date
    if len(sol_filtered.time) == 0 or len(sdl_filtered.time) == 0:
        print(f"No data found for {target_date.strftime('%Y-%m-%d')}")
        return None
    
    # Merge datasets
    ds = xr.merge([sol_filtered, sdl_filtered], compat='override')

    # Calculate LST
    Boltzmann = 5.670374419e-8
    Emissivity = 0.98
    ds['LST'] = ((ds['SOL']-(1-Emissivity)*ds['SDL'])/Boltzmann/(Emissivity))**(1/4)

    # Apply aggregation
    if aggregation == 'mean':
        lst_aggregated = ds['LST'].mean(dim='time')
        var_name = 'LST_mean'
    elif aggregation == 'max':
        lst_aggregated = ds['LST'].max(dim='time')
        var_name = 'LST_max'
    elif aggregation == 'hour':
        if hour is None:
            raise ValueError("hour must be specified when aggregation='hour'")
        if not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        
        # Filter for specific hour
        target_hour = target_date.replace(hour=hour, minute=0, second=0)
        ds_hour = ds.sel(time=target_hour, method='nearest')
        lst_aggregated = ds_hour['LST']
        var_name = f'LST_hour{hour:02d}'
    else:
        raise ValueError("aggregation must be 'max', 'mean', or 'hour'")
        

    # Create output dataset
    ds_output = xr.Dataset(
        data_vars={
            var_name: (('lat', 'lon'), lst_aggregated.values)
        },
        coords={
            'time': [target_date],
            'lat': ds.lat,
            'lon': ds.lon
        }
    )
    
    return ds_output

ds_11am = calc_LST_for_date(ds_sol, ds_sdl, current_date_str, aggregation='hour', hour=11)

# Function to resample LST data from lat/lon grid to match Sentinel-2 10m grid in EPSG:2056
def resample_lst_to_s2_grid(ds_lst, var_name, target_transform, target_shape, target_crs='EPSG:2056'):
    """
    Resample LST data from lat/lon grid to match Sentinel-2 10m grid in EPSG:2056.
    
    Parameters:
    -----------
    ds_lst : xarray.Dataset
        LST dataset with lat/lon coordinates
    var_name : str
        Name of the LST variable to resample (e.g., 'LST_mean', 'LST_max', 'LST_hour11')
    target_transform : affine.Affine
        Target transform from Sentinel-2 10m grid
    target_shape : tuple
        Target shape (height, width) from Sentinel-2 10m grid
    target_crs : str
        Target CRS (default: 'EPSG:2056')
    
    Returns:
    --------
    numpy.ndarray
        Resampled LST array on 10m grid
    """
    # Extract LST data
    lst_data = ds_lst[var_name].values
    
    # Get lat/lon coordinates
    lats = ds_lst.lat.values
    lons = ds_lst.lon.values
    
    # Determine if coordinates are ascending or descending
    lat_ascending = lats[1] > lats[0] if len(lats) > 1 else False
    lon_ascending = lons[1] > lons[0] if len(lons) > 1 else False
    
    # Calculate pixel resolution (always positive)
    lat_res = abs(lats[1] - lats[0]) if len(lats) > 1 else abs(lats[-1] - lats[-2])
    lon_res = abs(lons[1] - lons[0]) if len(lons) > 1 else abs(lons[-1] - lons[-2])
    
    # Get the top-left corner coordinates
    # For latitude: if descending (typical), use first value; if ascending, use last value
    # For longitude: if ascending (typical), use first value; if descending, use last value
    top_lat = lats[0] if not lat_ascending else lats[-1]
    left_lon = lons[0] if lon_ascending else lons[-1]
    
    # Create affine transform for source (LST in lat/lon)
    # The transform should point to the top-left corner and use negative lat_res
    src_transform = Affine.translation(left_lon - lon_res/2, top_lat + lat_res/2) * Affine.scale(lon_res, -lat_res)
    
    # Flip data if needed to match standard rasterio orientation (top-to-bottom, left-to-right)
    if not lat_ascending:
        # Data is already top-to-bottom, just ensure it's correct
        lst_data_oriented = lst_data
    else:
        # Flip vertically to go from bottom-to-top to top-to-bottom
        lst_data_oriented = np.flipud(lst_data)
    
    if not lon_ascending:
        # Flip horizontally to go from right-to-left to left-to-right
        lst_data_oriented = np.fliplr(lst_data_oriented)
    
    # Prepare output array
    lst_resampled = np.empty(target_shape, dtype=np.float32)
    
    # Reproject from EPSG:4326 (lat/lon) to EPSG:2056 (Swiss grid)
    reproject(
        source=lst_data_oriented.astype(np.float32),
        destination=lst_resampled,
        src_transform=src_transform,
        src_crs='EPSG:4326',
        dst_transform=target_transform,
        dst_crs=target_crs,
        resampling=Resampling.nearest,
        src_nodata=np.nan,
        dst_nodata=np.nan
    )
    
    return lst_resampled

# Use it after calculating LST
lst_11am_10m = resample_lst_to_s2_grid(ds_11am, 'LST_hour11', target_transform, target_shape)

# Extract the data arrays and convert from Kelvin to Celsius
lst_11am = lst_11am_10m - 273.15
print(f'Calculated LST (using the aggregation method "{lst_aggregation}")')

##############################
# INPUT DATA: REFERENCE LST
# Load or compute long-term LST statistics for climate reference period (1991-2020)
s3_path_lst_ref = f's3://{s3_bucket_satromo}{s3_path_key_lst_ref }LST_statistics_DOY{doy_str}{lst_ref_file}.nc'
ds_lst_ref = xr.open_dataset(s3_path_lst_ref, engine='h5netcdf', storage_options={'anon': True})
print('Loaded reference LST statistics for current day of year')

# Read relevant bands based on the chosen method
if workWithPercentiles is True:
    lst_ref_10m_min = resample_lst_to_s2_grid(ds_lst_ref, f'LST_{lst_aggregation}_p05', target_transform, target_shape)  # 5th percentile
    lst_ref_10m_max = resample_lst_to_s2_grid(ds_lst_ref, f'LST_{lst_aggregation}_p95', target_transform, target_shape)  # 95th percentile
    print('- Using percentiles for TCI calculation')
else:
    lst_ref_10m_min = resample_lst_to_s2_grid(ds_lst_ref, f'LST_{lst_aggregation}_min', target_transform, target_shape)  # minimum
    lst_ref_10m_max = resample_lst_to_s2_grid(ds_lst_ref, f'LST_{lst_aggregation}_max', target_transform, target_shape)  # maximum
    print('- Using min and max for TCI calculation')

##############################
# CALCULATE TCI
# TCI = 100 * (LST_max - LST) / (LST_max - LST_min)
tci_den = lst_ref_10m_max - lst_ref_10m_min
tci_den[tci_den == 0] = np.nan
tci = lst_ref_10m_max - lst_11am
tci /= tci_den
del lst_11am, lst_ref_10m_min, lst_ref_10m_max, tci_den
tci *= 100
print('Calculated TCI')

############################################################
# CALCULATE VHI
# VHI = a*VCI + (1-a)*TCI
vhi = vci * alpha
vhi += tci * (1 - alpha)
del vci, tci
print(f'Calculated VHI for {current_date_str}')

##############################
# APPLY VEGETATION MASK
s3_path_vegetation_mask = f's3://{s3_bucket_satromo}data/MASKS/Vegetation/wald_lebensraumkarte20220316_epsg2056.tif'

# --- quick fix to handle different resolutions and extents of vegetation mask ---
# 
# TODO check if resampling is necessary with the new vegetation masks
#
# This should run with the new vegetation masks, that matches the S2 grid:
# with rasterio.open(s3_path_vegetation_mask) as src_veg:
#     window = from_bounds(*roi, src_veg.transform)
#     vegetation_mask = src_veg.read(1, window=window)
#
# This is the quick fix:
with rasterio.open(s3_path_vegetation_mask) as src_veg:
    window = from_bounds(*roi, src_veg.transform)
    data_veg = src_veg.read(1, window=window)
    src_transform_veg = src_veg.window_transform(window)
    src_crs_veg = src_veg.crs

# Resample to match target grid (same as all other layers)
vegetation_mask = np.empty(target_shape, dtype=np.float32)
reproject(
    source=data_veg.astype(np.float32),
    destination=vegetation_mask,
    src_transform=src_transform_veg,
    src_crs=src_crs_veg,
    dst_transform=target_transform,
    dst_crs=src_crs_veg,
    resampling=Resampling.nearest,
    src_nodata=0,
    dst_nodata=0
)
vegetation_mask = vegetation_mask.astype(np.uint8)
# ---

# Apply vegetation mask to VHI (set non-vegetated areas to no_data value)
vhi_masked = vhi.copy()
vhi_masked[vegetation_mask == 0] = no_data

##############################
# GENERATE METADATA
# main_functions/main_utils.py. function metadata_add_entry
# https://github.com/swisstopo/topo-satromo-v2/blob/49fcc1545b609823602c5c5dc43c845912013f1e/main_functions/main_utils.py#L833

##############################
# EXPORT VHI
# Save to file with metadata

# print("********* finished processing {} *********".format(product_name))

##############################
# PLOTS
# Define VHI color bins and colors
from matplotlib.colors import ListedColormap, BoundaryNorm
vhi_bins = [0, 10, 20, 30, 40, 50, 60, 100, 110, 111]  # boundaries for each class
vhi_colors = [
    '#b56a29',  # [0,9]
    '#ce8540',  # (10,19]
    '#f5cd85',  # (20,29]
    '#fff5ba',  # (30,39]
    '#cbffca',  # (40,49]
    '#52bd9f',  # (50,59]
    '#0470b0',  # (60,100]
    '#b3b6b7',  # [110] (missing data)
    '#ffffff'   # placeholder for values > 110
]
# Create custom colormap for VHI
vhi_cmap = ListedColormap(vhi_colors)
vhi_norm = BoundaryNorm(vhi_bins, vhi_cmap.N)

# Simple plot of VHI
plt.figure(figsize=(10, 8))
plt.imshow(vhi_masked, cmap=vhi_cmap, norm=vhi_norm, interpolation='nearest') #, vmin=0, vmax=100
plt.colorbar()
plt.show()

# Plot NDVI, VCI, TCI and VHI side by side
# fig, axes = plt.subplots(2, 2, figsize=(14, 12))
# # NDVI
# im0 = axes[0, 0].imshow(ndvi, cmap='RdYlGn', vmin=-1, vmax=1)
# axes[0, 0].set_title('NDVI', fontsize=12, fontweight='bold')
# plt.colorbar(im0, ax=axes[0, 0], label='NDVI')
# # LST Mean (convert to Celsius)
# im1 = axes[0, 1].imshow(vci, cmap='RdYlBu_r', vmin=0, vmax=100)
# axes[0, 1].set_title('VCI', fontsize=12, fontweight='bold')
# plt.colorbar(im1, ax=axes[0, 1], label='VCI')
# # LST Max
# im2 = axes[1, 0].imshow(tci, cmap='RdYlBu_r', vmin=0, vmax=100)
# axes[1, 0].set_title('TCI', fontsize=12, fontweight='bold')
# plt.colorbar(im2, ax=axes[1, 0], label='TCI')
# # LST 11am
# im3 = axes[1, 1].imshow(vhi_masked, cmap=vhi_cmap, norm=vhi_norm, interpolation='nearest')
# axes[1, 1].set_title('VHI', fontsize=12, fontweight='bold')
# cbar3 = plt.colorbar(im3, ax=axes[1, 1], boundaries=vhi_bins, 
#                      ticks=[4.5, 14.5, 24.5, 34.5, 44.5, 54.5, 80, 110])
# cbar3.ax.set_yticklabels(['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-100', '110'], fontsize=9)
# cbar3.set_label('VHI')
# plt.tight_layout()
# plt.show()

# # Not-so-simple plot
# import matplotlib.pyplot as plt

# # Get coordinate arrays
# lats = ds_mean.lat.values
# lons = ds_mean.lon.values

# # Create side-by-side visualization with proper geographic extent
# fig, axes = plt.subplots(1, 3, figsize=(18, 5))

# # Common colormap settings
# vmin = np.nanmin([lst_mean, lst_max, lst_11am])
# vmax = np.nanmax([lst_mean, lst_max, lst_11am])

# # Define extent for imshow: [left, right, bottom, top]
# extent = [lons.min(), lons.max(), lats.min(), lats.max()]

# # Plot LST Mean
# im1 = axes[0].imshow(lst_mean, cmap='RdYlBu_r', vmin=vmin, vmax=vmax, 
#                      extent=extent, origin='lower', aspect='auto')
# axes[0].set_title(f'LST Mean - 2023-05-02', fontsize=12, fontweight='bold')
# axes[0].set_xlabel('Longitude (°)')
# axes[0].set_ylabel('Latitude (°)')
# plt.colorbar(im1, ax=axes[0], label='Temperature (°C)')

# # Plot LST Max
# im2 = axes[1].imshow(lst_max, cmap='RdYlBu_r', vmin=vmin, vmax=vmax,
#                      extent=extent, origin='lower', aspect='auto')
# axes[1].set_title(f'LST Max - 2023-05-02', fontsize=12, fontweight='bold')
# axes[1].set_xlabel('Longitude (°)')
# axes[1].set_ylabel('Latitude (°)')
# plt.colorbar(im2, ax=axes[1], label='Temperature (°C)')

# # Plot LST 11am
# im3 = axes[2].imshow(lst_11am, cmap='RdYlBu_r', vmin=vmin, vmax=vmax,
#                      extent=extent, origin='lower', aspect='auto')
# axes[2].set_title(f'LST 11:00 AM - 2023-05-02', fontsize=12, fontweight='bold')
# axes[2].set_xlabel('Longitude (°)')
# axes[2].set_ylabel('Latitude (°)')
# plt.colorbar(im3, ax=axes[2], label='Temperature (°C)')

# plt.tight_layout()
# plt.show()