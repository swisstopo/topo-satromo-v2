import rasterio
import xarray as xr
import rioxarray
from pystac_client import Client
import os
import numpy as np
import configuration as config
from datetime import datetime, timedelta
import rasterio
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from rasterio.warp import reproject, Resampling, transform_bounds
from affine import Affine
from main_functions import main_utils, main_publish_stac_fsdi, main_extract_warnregions, main_thumbnails

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
def process_product_vhi(
    day_to_process: str, 
    collection: str, 
    roi: tuple[float, float, float, float] | None = None
    ) -> str:
    """
    Process Vegetation Health Index for a given day.
    
    Args:
        day_to_process: Date string (e.g., 'YYYY-MM-DD')
        collection: Name of the data collection to process
        roi: Optional bounding box as (min_x, min_y, max_x, max_y) in EPSG:2056.
            If None, processes all available data.
    """
    product_name = config.PRODUCT_VHI['product_name']
    print("********* processing {} *********".format(product_name))

    ##############################
    # SWITCHES
    # Enable/disable execution of individual steps

    workWithPercentiles = True
    # options: True, False - defines if the p05 and p95 percentiles of the reference data sets are used,
    # otherwise the min and max will be used (False)
  
    ##############################
    # CONFIGURATION / PARAMETERS
    # Paths
    stac_swisstopo, s2_sr_collection_id = config.PRODUCT_VHI['step0_collection'].split('#/collections/')
    stac_swisstopo_version = 'api/stac/v0.9/'
    lst_aggregation = '11am' # options: 'mean', 'max', '11am'
    lst_ref_file = f'_MSG_ch02_2004-2020_7days_{lst_aggregation}' # MSG (2004-2020) / MFG (1991-2003)
    warnregions = 'assets/warnregionen_vhi_2056.shp'

    # Constants
    s2_nodata = 0 # NoData value in swissEO S2-SR products
    s2_scale_factor = 0.0001 # Scale factor for reflectance values in swissEO S2-SR products
    s2_offset = -0.1 # Offset for reflectance values in swissEO S2-SR products
    ref_ndvi_nodata = 255 # NoData value in reference NDVI statistics
    ref_ndvi_scale_factor = 0.01 # Scale factor for reference NDVI statistics
    ref_ndvi_offset = -100 # Offset for reference NDVI statistics
    alpha = 0.5 # Weighting factor for VHI calculation (0.5 means equal weight for VCI and TCI)
    threshold_ndsi = 0 # values equal or above indicate snow
    threshold_illumination = 70 # values equal or above indicate insufficient illumination angles [°degrees]

    # Environments
    os.environ['AWS_NO_SIGN_REQUEST'] = 'YES' # to access public S3 buckets without credentials

    # ROI (if not provided)
    bbox_ch = (2480400, 1059000, 2839000, 1302500) # bounding box for Switzerland with a ~5km buffer

    ##############################
    # TIME
    current_date_str = day_to_process
    current_date = datetime.strptime(current_date_str, '%Y-%m-%d')

    doy = current_date.timetuple().tm_yday
    doy_str = f'{doy:03d}' # zero-padded three-digit day of year

    year = current_date_str[:4]
    month = current_date_str[5:7]
    day = current_date_str[8:10]

    # Create timestamp for export filename (YYYY-MM-DDt235959)
    timestamp = f'{current_date_str}t235959'

    # Time window
    d = int(config.PRODUCT_VHI['temporal_coverage'])-1
    start_date = current_date - timedelta(days=d)
    end_date = current_date + timedelta(days=1) # add one day to include the current date in the window

    ############################################################
    # INPUT DATA: REFLECTANCE, NDVI CALCULATION AND MASKS
    client = Client.open(stac_swisstopo + stac_swisstopo_version) # connect to STAC API
    client.add_conforms_to('COLLECTIONS') # due to the implementation of the swisstopo STAC API, we need to add conformance classes
    client.add_conforms_to('ITEM_SEARCH')
    s2_sr_collection = client.get_collection(s2_sr_collection_id)

    # Generate all date strings in the time window
    date_strings = [
        (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        for i in range((end_date - start_date).days)
    ]

    def get_window_and_transform(src, roi):
        """
        Return (window, transform) for a given rasterio source and ROI.
        If roi is None, returns (None, src.transform) covering the full raster.
        If roi does not overlap the source, returns (None, None) as a signal to skip.
        """
        if roi is None:
            return None, src.transform
        
        window = from_bounds(*roi, src.transform)
        if window.width <= 0 or window.height <= 0:
            return None, None  # no overlap — caller should return NaN array
        
        return window, src.window_transform(window)

    # Function to load a band and apply offset and scale factors, also preserving nodata values
    def load_and_scale_band(filepath, roi, target_transform, target_shape,
                            nodata=s2_nodata, scale=s2_scale_factor, offset=s2_offset):
        """
        Load a raster band and apply scaling and offset, preserving nodata values.
        Reprojection to target grid to guarantee matching output shape and nodata handling.
        
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
            window, src_transform = get_window_and_transform(src, roi)
            if src_transform is None:  # no overlap
                return np.full(target_shape, fill_value=np.nan, dtype=np.float32)
            data = src.read(1, window=window, out_dtype=np.float32)
            src_crs = src.crs 
        
        # Reproject onto the fixed 10m reference grid
        scaled = np.full(target_shape, fill_value=np.nan, dtype=np.float32)
        reproject(
            source=data,
            destination=scaled,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=target_transform,
            dst_crs=src_crs,
            resampling=Resampling.nearest,
            src_nodata=nodata,
            dst_nodata=np.nan
        )
        del data

        nodata_mask = scaled == nodata # Create nodata mask before modifying data
        # Apply scaling in-place on the full array — no temporary copy
        scaled *= scale
        scaled += offset
        
        # Restore nodata pixels to NaN
        scaled[nodata_mask] = np.nan
        
        return scaled

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
            window, src_transform = get_window_and_transform(src, roi)
            if src_transform is None:
                return np.full(target_shape, fill_value=np.nan, dtype=np.float32)
            data_20m = src.read(1, window=window, out_dtype=np.float32)
            src_crs = src.crs  
        
        # Resample to 10m grid (start with nan filled array to guarantee matching output shape and nodata handling)
        data_10m = np.full(target_shape, fill_value=np.nan, dtype=np.float32)
        reproject(
            source=data_20m,
            destination=data_10m,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=target_transform,
            dst_crs=src_crs,
            resampling=Resampling.nearest,
            src_nodata=nodata,
            dst_nodata=np.nan
        )
        del data_20m  # free memory of the original 20m data array as soon as possible

        # Apply scaling and offset
        nodata_mask = data_10m == nodata # Create nodata mask before modifying data
        # Apply scaling in-place on the full array — no temporary copy
        data_10m *= scale
        data_10m += offset
        
        # Restore nodata pixels to NaN
        data_10m[nodata_mask] = np.nan

        return data_10m

    # Function to apply masks (clouds, snow, terrain shadow) to a specific band
    def apply_masks(band, cloudmask, snowmask, illumination_mask, th_illumination=threshold_illumination):
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
        illumination_mask : numpy.ndarray
            Illumination angles (in degrees) for insufficient illumination and terrain shadow detection
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
            cloud_mask_condition = (cloudmask != 0)
            masked_band[cloud_mask_condition] = np.nan

        # Apply snow mask
        if snowmask is not None:
            snow_condition = (snowmask != 0)
            masked_band[snow_condition] = np.nan
            
        # Apply terrain shadow mask
        if illumination_mask is not None:
            shadow_condition = illumination_mask > th_illumination
            masked_band[shadow_condition] = np.nan
        
        return masked_band

    # Retrieve all S2-SR items in the collection and filter them by the date window
    s2_sr_items = []

    s2_sr_items = sorted(
        [item for item in s2_sr_collection.get_all_items()
        if any(date_str in item.id for date_str in date_strings)],
        key=lambda item: item.id  # sort by ID which starts with date
    )

    # Sort items newest-first so we can fill forward with the most recent valid value
    s2_sr_items_sorted = sorted(s2_sr_items, key=lambda item: item.id, reverse=True)

    def item_covers_roi(item, roi, roi_crs='EPSG:2056'):
        """Check if a STAC item's bbox intersects the ROI."""
        if roi is None:
            return True  # if no ROI is set, all items are considered valid
        
        # Transform ROI to WGS84 to match STAC bbox
        roi_wgs84 = transform_bounds(roi_crs, 'EPSG:4326', *roi)
        # STAC bbox is [west, south, east, north]
        item_bbox = item.bbox
        
        # Check for intersection
        no_overlap = (
            roi_wgs84[0] > item_bbox[2] or  # roi west > item east
            roi_wgs84[2] < item_bbox[0] or  # roi east < item west
            roi_wgs84[1] > item_bbox[3] or  # roi south > item north
            roi_wgs84[3] < item_bbox[1]     # roi north < item south
        )
        return not no_overlap

    # Filter to only items that cover the ROI
    s2_sr_items_sorted = [item for item in s2_sr_items_sorted if item_covers_roi(item, roi)]

    if len(s2_sr_items_sorted) == 0:
        raise ValueError(f'No S2-SR items found for the time window {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} and the specified ROI.')
    else:
        print(f'Found {len(s2_sr_items_sorted)} items in time window {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} and the specified ROI.')
        print(f'Starting the VHI calculation for {current_date_str}')

    # NDVI calculation and combining them to always take the newest value per pixel

    # Resolve ROI and establish 10m grid ONCE before the loop
    first_item = s2_sr_items_sorted[0]
    first_item_path = stac_swisstopo + s2_sr_collection_id + '/' + first_item.id + '/swisseo_s2-sr_v200_mosaic_' + first_item.id
    first_red_path = first_item_path + '_b04_10m.tif'

    if roi is None:
        roi = bbox_ch

    with rasterio.open(first_red_path) as src:
        window_10m = from_bounds(*roi, src.transform)
        target_transform = src.window_transform(window_10m)
        target_shape = (int(window_10m.height), int(window_10m.width))

    ndvi_combined = None

    NDVI_index_list = []
    for item in s2_sr_items_sorted:
    # Get file paths for required bands
        item_path = stac_swisstopo + s2_sr_collection_id + '/' + item.id + '/swisseo_s2-sr_v200_mosaic_' + item.id
        red_path = item_path + '_b04_10m.tif'
        nir_path = item_path + '_b08_10m.tif'

        # Load 10 m bands and apply offset and scale factor to reflectance bands
        red = load_and_scale_band(red_path, roi, target_transform, target_shape)
        nir = load_and_scale_band(nir_path, roi, target_transform, target_shape)

        # CALCULATE NDVI --> ndvi = (nir - red) / (nir + red)
        ndvi_den = nir + red
        ndvi_den[ndvi_den == 0] = np.nan
        ndvi = (nir - red) / ndvi_den
        del ndvi_den, red, nir
        import matplotlib.pyplot as plt

        # LOAD/CALCULATE AND APPLY MASKS
        # --- CLOUD mask (10m)
        cloud_mask_path = item_path + '_cloudmask_10m.tif'
        with rasterio.open(cloud_mask_path) as src_cloud:
            window, src_transform = get_window_and_transform(src_cloud, roi)
            if src_transform is None:
                cloud_mask = np.full(target_shape, dtype=np.uint8)  # treat as cloud-free if no overlap
            else:
                data = src_cloud.read(1, window=window)
                src_crs = src_cloud.crs
                cloud_mask_f = np.full(target_shape, fill_value=1, dtype=np.float32)
                reproject(
                    source=data.astype(np.float32),
                    destination=cloud_mask_f,
                    src_transform=src_transform,
                    src_crs=src_crs,
                    dst_transform=target_transform,
                    dst_crs=src_crs,
                    resampling=Resampling.nearest,
                    src_nodata=0,
                    dst_nodata=0
                )
                cloud_mask = cloud_mask_f.astype(np.uint8)

        # --- TERRAIN SHADOW and low ILLUMINATION mask (10m)
        illumination_mask_path = item_path + '_terrainmask_10m.tif'
        with rasterio.open(illumination_mask_path) as src_illumination:
            window, src_transform = get_window_and_transform(src_illumination, roi)
            if src_transform is None:
                illumination_mask = np.full(target_shape, dtype=np.uint8)  # treat as no shadow if no overlap
            else:
                data = src_illumination.read(1, window=window)
                src_crs = src_illumination.crs
                illumination_mask_f = np.full(target_shape, fill_value=1, dtype=np.float32)
                reproject(
                    source=data.astype(np.float32),
                    destination=illumination_mask_f,
                    src_transform=src_transform,
                    src_crs=src_crs,
                    dst_transform=target_transform,
                    dst_crs=src_crs,
                    resampling=Resampling.nearest,
                    src_nodata=0,
                    dst_nodata=0
                )
                illumination_mask = illumination_mask_f.astype(np.uint8)

        # ---- SNOW mask based on NDSI and SCL (20m, resampled to 10m)
        # First, calculate NDSI-based snow mask from green and SWIR bands
        green_path = item_path + '_b03_10m.tif'
        swir_path = item_path + '_b11_20m.tif'
        # Load green and SWIR bands only for snow masking based on NDSI, to save processing time and memory
        green = load_and_scale_band(green_path, roi, target_transform, target_shape)
        swir = load_scale_and_resample_20m_to_10m(swir_path, roi, target_transform, target_shape)
        # NDSI --> ndsi = (green - swir) / (green + swir)
        ndsi = green - swir # numerator
        ndsi_den = green + swir # denominator
        ndsi_den[ndsi_den == 0] = np.nan # avoid division by zero
        ndsi /= ndsi_den  # divide in-place
        del green, swir, ndsi_den
        # Create snow mask based on NDSI
        snow_mask = np.zeros_like(ndsi, dtype=np.uint8)
        snow_mask[ndsi > threshold_ndsi] = 1  # 1 indicates snow

        # Load SCL band for additional snow masking based on SCL
        scl_path = item_path + '_scl_20m.tif'
        # SCL classification values:
        # 0: No data, 1: Saturated or defective, 2: Dark area pixels, 3: Cloud shadows,
        # 4: Vegetation, 5: Bare soils, 6: Water, 7: Clouds low probability / unclassified,
        # 8: Clouds medium probability, 9: Clouds high probability, 10: Thin cirrus,
        # 11: Snow or ice
        scl = load_scale_and_resample_20m_to_10m(scl_path, roi, target_transform, target_shape,
                                            nodata=0, scale=1, offset=0) # no scaling for SCL
        
        # Add to snow mask based on SCL classification
        snow_mask[scl == 11] = 1  # 1 indicates snow
        del scl, ndsi

        # Apply masks to NDVI
        ndvi_masked = apply_masks(ndvi, cloud_mask, snow_mask, illumination_mask)
        del ndvi, cloud_mask, snow_mask, illumination_mask

        # Combine: fill gaps in combined NDVI with values from this (older) item
        if ndvi_combined is None:
            ndvi_combined = ndvi_masked
        else:
            # Only fill pixels that are still NaN in the combined array
            fill_mask = np.isnan(ndvi_combined)
            ndvi_combined[fill_mask] = ndvi_masked[fill_mask]
        del ndvi_masked

        # Track this item as used
        NDVI_index_list.append(item.id)

        # Stop early if no NaN pixels remain
        if not np.any(np.isnan(ndvi_combined)):
            print(f'All pixels filled after {item.id}, stopping early')
            break

    NDVI_index_list_str = ','.join(NDVI_index_list)
    NDVI_scene_count = len(NDVI_index_list)
    print(f'Calculated and combined masked NDVI from {NDVI_scene_count} items, newest value takes priority') 

    ##############################
    # INPUT DATA: REFERENCE NDVI
    # Load or compute long-term NDVI statistics for climate reference period (1991-2020)
    s3_path_ndvi_ref = f'{config.PRODUCT_VHI['NDVI_reference_data']}NDVI_Stats_DOY{doy_str}.tif'

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
            src_crs = src.crs
        
        resampled = np.empty(target_shape, dtype=np.float32)
        reproject(
            source=data,
            destination=resampled,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=target_transform,
            dst_crs=src_crs,
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
    vci = ndvi_combined - ndvi_ref_min  # numerator, reuse ndvi name or new var
    vci /= vci_den # divide in-place
    del ndvi_combined, ndvi_ref_min, ndvi_ref_max, vci_den
    vci *= 100  # scale in-place
    print('Calculated VCI')

    ############################################################
    # INPUT DATA: TEMPERATURE
    # Load surface downwelling longwave radiation (SDL) and surface outgoing longwave radiation (SOL) data for the specific date
    # TODO: update elif part to include Feb 2026 after delivery from MCH
    if current_date < datetime(2024, 1, 1):
        sdl_path = f'{config.PRODUCT_VHI['LST_current_data']}/MSG2004-2023/msg.SDL.H_ch02.lonlat_{year}{month}01000000.nc'
        sol_path = f'{config.PRODUCT_VHI['LST_current_data']}/MSG2004-2023/msg.SOL.H_ch02.lonlat_{year}{month}01000000.nc'
    elif current_date >= datetime(2024, 1, 1) and current_date < datetime(2026, 4, 1):
        sdl_path = f'{config.PRODUCT_VHI['LST_current_data']}/MSG2024-2026/msg.SDL.H_ch02.lonlat_{year}{month}01000000.nc'
        sol_path = f'{config.PRODUCT_VHI['LST_current_data']}/MSG2024-2026/msg.SOL.H_ch02.lonlat_{year}{month}01000000.nc'
    else:
        sdl_path = f'{config.PRODUCT_VHI['LST_current_data']}/msg.SDL.H_ch02.lonlat_{year}{month}{day}000000.nc'
        sol_path = f'{config.PRODUCT_VHI['LST_current_data']}/msg.SOL.H_ch02.lonlat_{year}{month}{day}000000.nc'

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

    LST_index_list = f'MSG_METEOSWISS_ALLSKY_mosaic_{current_date_str}T11000000_bands-1721m'
    LST_scene_count = 1

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
    s3_path_lst_ref = f'{config.PRODUCT_VHI['LST_reference_data']}LST_statistics_DOY{doy_str}{lst_ref_file}.nc'
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

    # Forcing data range (to [0 100]), ...
    vhi = np.clip(vhi, 0, 100)
    # ... adding missing data value for when one of the datasets is unavailable, ...
    vhi = np.where(np.isnan(vhi), config.PRODUCT_VHI['missing_data'], vhi)
    # ... and converting the data type (to UINT8).
    vhi = vhi.astype(np.uint8)
    # Converting from NumPy array to xarray DataArray for easier handling and exporting
    height, width = target_shape
    xs = np.array([target_transform.c + (col + 0.5) * target_transform.a for col in range(width)])
    ys = np.array([target_transform.f + (row + 0.5) * target_transform.e for row in range(height)])
    vhi = xr.DataArray(vhi, dims=('y', 'x'), coords={'y': ys, 'x': xs})
    vhi = vhi.rio.write_crs(src_crs)

    ##############################
    # SET METADATA
    # Getting swisstopo Processor Version
    processor_version = main_utils.get_github_info()
    
    vhi.attrs.update({
        'doy': doy,
        'alpha': alpha,
        'temporal_coverage': config.PRODUCT_VHI['temporal_coverage'],
        'missing_data': config.PRODUCT_VHI['missing_data'],
        'no_data':config.PRODUCT_VHI['no_data'],
        'SWISSTOPO_PROCESSOR': processor_version['GithubLink'],
        'SWISSTOPO_RELEASE_VERSION': processor_version['ReleaseVersion'],
        'collection': collection,
        'system:time_start': start_date,
        'system:time_end': (end_date - timedelta(seconds=1)), 
        'NDVI_reference_data': config.PRODUCT_VHI['NDVI_reference_data'],
        'NDVI_index_list': NDVI_index_list_str,
        'NDVI_scene_count': NDVI_scene_count,
        'LST_reference_data': config.PRODUCT_VHI['LST_reference_data'],
        'LST_index_list': LST_index_list,
        'LST_scene_count': LST_scene_count,
        'VCI_and_TCI_calculated_with': CI_method,
        'pixel_size_meter': config.PRODUCT_VHI['spatial_scale_export'],
    })

    ##############################
    # APPLY VEGETATION MASK
    s3_path_forest_mask = f's3://s3-topo-satromo-prod/data/MASKS/Vegetation/wald_lebensraumkarte20220316_epsg2056.tif'
    s3_path_vegetation_mask = f's3://s3-topo-satromo-prod/data/MASKS/Vegetation/trans_mask_2056.tif'

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
    # Forest mask
    with rasterio.open(s3_path_forest_mask) as src_veg:
        window = from_bounds(*roi, src_veg.transform)
        data_veg = src_veg.read(1, window=window)
        src_transform_veg = src_veg.window_transform(window)
        src_crs_veg = src_veg.crs

    # Resample to match target grid (same as all other layers)
    forest_mask = np.empty(target_shape, dtype=np.float32)
    reproject(
        source=data_veg.astype(np.float32),
        destination=forest_mask,
        src_transform=src_transform_veg,
        src_crs=src_crs_veg,
        dst_transform=target_transform,
        dst_crs=src_crs_veg,
        resampling=Resampling.nearest,
        src_nodata=0,
        dst_nodata=0
    )
    forest_mask = forest_mask.astype(np.uint8)
    # ---
    # Vegetation mask
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

    # Apply vegetation mask to VHI
    vhi_forest = vhi.where(forest_mask != 0, other=config.PRODUCT_VHI['no_data'])
    vhi_vegetation = vhi.where(vegetation_mask != 0, other=config.PRODUCT_VHI['no_data'])

    # Saving locally as COGTIFF
    filename_forest = product_name.replace('ch.swisstopo.','') + \
            '_mosaic_' + timestamp + '_forest-10m'
    vhi_forest.rio.write_nodata(config.PRODUCT_VHI['no_data'], inplace=True) # ensure nodata value is set in the output file
    vhi_forest.rio.to_raster(
        raster_path=f'{filename_forest}.tif',
        driver='COG',
        COMPRESS='DEFLATE',
        BIGTIFF='YES',
        NUM_THREADS='ALL_CPUS',
        ADD_ALPHA='NO',
        OVERVIEW_RESAMPLING='NEAREST', # to ensure overview pixels to be actual values from the original raster and not interpolated values
    )

    filename_vegetation = product_name.replace('ch.swisstopo.','') + \
            '_mosaic_' + timestamp + '_vegetation-10m'
    vhi_vegetation.rio.write_nodata(config.PRODUCT_VHI['no_data'], inplace=True) # ensure nodata value is set in the output file
    vhi_vegetation.rio.to_raster(
        raster_path=f'{filename_vegetation}.tif',
        driver='COG',
        COMPRESS='DEFLATE',
        BIGTIFF='YES',
        NUM_THREADS='ALL_CPUS',
        ADD_ALPHA='NO',
        OVERVIEW_RESAMPLING='NEAREST',
    )

    ##############################
    # WARNREGIONS
    warnformats = ['.csv', '.geojson', '.parquet']
    dateISO8601 = f'{current_date_str}T235959Z'

    # Create warnregions for forest areas
    warnregionfilename_forest = f'{filename_forest}_warnregions'
    main_extract_warnregions.export(
        f'{filename_forest}.tif',
        warnregions,
        warnregionfilename_forest,
        dateISO8601,
        config.PRODUCT_VHI['missing_data'],
        config.PRODUCT_VHI['no_data'],
        config.PRODUCT_VHI['scaling_factor'],
        'forest'
    )
    print(f'Created warnregions for forest areas')

    # Create warnregions for vegetation areas
    warnregionfilename_vegetation = f'{filename_vegetation}_warnregions'
    main_extract_warnregions.export(
        f'{filename_vegetation}.tif',
        warnregions,
        warnregionfilename_vegetation,
        dateISO8601,
        config.PRODUCT_VHI['missing_data'],
        config.PRODUCT_VHI['no_data'],
        config.PRODUCT_VHI['scaling_factor'],
        'vegetation'
    )
    print(f'Created warnregions for vegetation areas')

    ##############################
    # METADATA FILE AND THUMBNAIL
    #TODO

    # Create thumbnail
    filename_thumbnail = main_thumbnails.create_thumbnail(f'{filename_vegetation}.tif', config.PRODUCT_VHI['product_name'])

    ##############################
    # EXPORT VHI

    # Create forest file list for export
    file_list_forest = []

    # Add forest-masked VHI to file list
    file_list_forest.append({
        'timestamp': timestamp,
        'band': 'FOREST-10M',
        'resolution': '10m',
        'filename': f'{filename_forest}.tif',
    })

    # Add warnregions for forest areas to file list
    for fmt in warnformats:
        file_list_forest.append({
            'timestamp': timestamp,
            'band': f'FOREST-WARNREGIONS{fmt.replace(".", "-").upper()}',
            'resolution': None,
            'filename': warnregionfilename_forest + fmt,
        })

    # Create vegetationfile list for export
    file_list_vegetation = []

    # Add vegetation-masked VHI to file list
    file_list_vegetation.append({
        'timestamp': timestamp,
        'band': 'VEGETATION-10M',
        'resolution': '10m',
        'filename': f'{filename_vegetation}.tif',
    })
    
    # Add warnregions for vegetation areas to file list
    for fmt in warnformats:
        file_list_vegetation.append({
            'timestamp': timestamp,
            'band': f'VEGETATION-WARNREGIONS{fmt.replace(".", "-").upper()}',
            'resolution': None,
            'filename': warnregionfilename_vegetation + fmt
        })

    # Export forest-masked VHI and warnregions for forest areas
    for file_info in file_list_forest:
        band = file_info['band']
        filename = file_info['filename']

        # STAC Upload
        main_publish_stac_fsdi.publish_to_stac(filename, timestamp, config.PRODUCT_VHI['product_name'], 
                                               config.PRODUCT_VHI['geocat_id_forest'], None, asset_title=band)
        # if is_current == True:
        #     print("Newest dataset detected: updating CURRENT")
        #     filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
        #     # Rename the file
        #     os.rename(filename, filename_current)
        #     main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_VHI['product_name'],config.PRODUCT_VHI['geocat_id_forest'],asset_title=band, current=True)
        #     os.rename(filename_current, filename)

    # Export forest-masked VHI and warnregions for vegetation areas
    for file_info in file_list_vegetation:
        band = file_info['band']
        filename = file_info['filename']

        # STAC Upload
        main_publish_stac_fsdi.publish_to_stac(filename, timestamp, config.PRODUCT_VHI['product_name'], 
                                               config.PRODUCT_VHI['geocat_id_forest'], None, asset_title=band)
        # if is_current == True:
        #     print("Newest dataset detected: updating CURRENT")
        #     filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
        #     # Rename the file
        #     os.rename(filename, filename_current)
        #     main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_VHI['product_name'],config.PRODUCT_VHI['geocat_id_forest'],asset_title=band, current=True)
        #     os.rename(filename_current, filename)

    # Upload metadata file
    # filename=f"{config.PRODUCT_VHI['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json"
    # main_publish_stac_fsdi.publish_to_stac(filename,timestamp,config.PRODUCT_VHI['product_name'],config.PRODUCT_VHI['geocat_id'],None,asset_title="Metadata")
    # if is_current == True:
    #     print("Newest dataset detected: updating CURRENT")
    #     filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
    #     # Rename the file
    #     os.rename(filename, filename_current)
    #     main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_VHI['product_name'],config.PRODUCT_VHI['geocat_id'],asset_title="Metadata", current=True)
    #     os.rename(filename_current, filename)

    # Upload Thumbnail
    main_publish_stac_fsdi.publish_to_stac(filename_thumbnail, timestamp, config.PRODUCT_VHI['product_name'], 
                                           config.PRODUCT_VHI['geocat_id_forest'], None, asset_title="Thumbnail")
    # if is_current == True:
    #     print("Newest dataset detected: updating CURRENT")
    #     filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
    #     # Rename the file
    #     os.rename(filename, filename_current)
    #     main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_VHI['product_name'],config.PRODUCT_VHI['geocat_id'],asset_title="Thumbnail", current=True)
    #     os.rename(filename_current, filename)

    # # Clean up Thumbnailfile
    # if Path(filename).exists():
    #         print(f"Cleaning up: {filename}")
    #         Path(filename).unlink()

    # print(f'********* finished processing {product_name} *********')


    

    ##############################
    # PLOTS
    # Define VHI color bins and colors
    # import matplotlib.pyplot as plt
    # from matplotlib.colors import ListedColormap, BoundaryNorm
    # vhi_bins = [0, 10, 20, 30, 40, 50, 60, 100, 110, 111]  # boundaries for each class
    # vhi_colors = [
    #     '#b56a29',  # [0,9]
    #     '#ce8540',  # (10,19]
    #     '#f5cd85',  # (20,29]
    #     '#fff5ba',  # (30,39]
    #     '#cbffca',  # (40,49]
    #     '#52bd9f',  # (50,59]
    #     '#0470b0',  # (60,100]
    #     '#b3b6b7',  # [110] (missing data)
    #     '#ffffff'   # placeholder for values > 110
    # ]
    # # Create custom colormap for VHI
    # vhi_cmap = ListedColormap(vhi_colors)
    # vhi_norm = BoundaryNorm(vhi_bins, vhi_cmap.N)

    # # -----
    # # Simple plot of VHI
    # plt.figure(figsize=(10, 8))
    # plt.imshow(vhi_vegetation, cmap=vhi_cmap, norm=vhi_norm, interpolation='nearest') #, vmin=0, vmax=100
    # plt.colorbar()
    # plt.show()

    # return f"VHI: Successfully processed {day_to_process}."