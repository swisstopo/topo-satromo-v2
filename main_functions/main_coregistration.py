"""
Coregistration functionality for satellite imagery.

This module provides functions for coregistering satellite imagery using the AROSICS library.
It handles coregistration and shift calculation for pre-mosaiced Sentinel-2 imagery.
"""
# General python libraries/modules
import glob
import logging
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any
import re

import numpy as np
from arosics import COREG_LOCAL, DeShifter
from osgeo import gdal, gdalconst
from scipy.interpolate import interp2d




# Specific SATROMO libraries/modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from configuration import dev_config as config
from main_functions import main_reprojection, main_mosaicing
from main_functions import main_utils

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("coreg")

def resample_raster(
    src_filename: Union[str, Path],
    match_filename: Union[str, Path],
    dst_filename: Union[str, Path]
) -> str:
    """
    Resample a raster to match the resolution and extent of another raster.

    Args:
        src_filename: The path to the source raster file.
        match_filename: The path to the raster file to match.
        dst_filename: The path to the output raster file.

    Returns:
        Path to the resampled raster.

    Raises:
        RuntimeError: If resampling fails.
    """
    src_filename = main_utils.ensure_path(src_filename)
    match_filename = main_utils.ensure_path(match_filename)
    dst_filename = main_utils.ensure_path(dst_filename)
    main_utils.ensure_directory(dst_filename.parent)

    logger.info(f"Resampling {os.path.basename(src_filename)} to match {os.path.basename(match_filename)}")

    try:
        # Get information from the match file
        match_ds = gdal.Open(str(match_filename), gdalconst.GA_ReadOnly)
        if match_ds is None:
            raise ValueError(f"Could not open match raster: {match_filename}")

        match_geotrans = match_ds.GetGeoTransform()
        wide = match_ds.RasterXSize
        high = match_ds.RasterYSize
        match_ds = None

        # Create a temporary file to change the resolution
        temp_filename = os.path.join(os.path.dirname(dst_filename), "temp_resample.tif")
        res = match_geotrans[1]

        # First step: Change resolution
        command = [
            "gdalwarp",
            "-overwrite",
            "-q",
            "-tr", str(res), str(res),
            "-tap",
            "-co", "COMPRESS=DEFLATE",
            "-co", "PREDICTOR=2",
            str(src_filename),
            temp_filename
        ]

        success, _, stderr = main_utils.run_gdal_command(command)
        if not success:
            logger.error(f"Failed to resample raster (step 1): {stderr}")
            raise RuntimeError(f"Failed to resample raster (step 1): {stderr}")

        # Second step: Match extent
        command = [
            "gdalwarp",
            "-overwrite",
            "-q",
            "-te", str(match_geotrans[0]),
            str(match_geotrans[3] - high * res),
            str(match_geotrans[0] + wide * res),
            str(match_geotrans[3]),
            "-dstnodata", "0",
            "-co", "COMPRESS=DEFLATE",
            "-co", "PREDICTOR=2",
            temp_filename,
            str(dst_filename)
        ]

        success, _, stderr = main_utils.run_gdal_command(command)
        if not success:
            logger.error(f"Failed to resample raster (step 2): {stderr}")
            raise RuntimeError(f"Failed to resample raster (step 2): {stderr}")

        # Remove the temporary file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

        return str(dst_filename)

    except Exception as e:
        logger.error(f"Error resampling raster {src_filename}: {str(e)}")
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        raise


def create_binary_cloud_mask(
    cloud_file: Union[str, Path],
    output_file: Union[str, Path],
    cloudfree_class: int
) -> str:
    """
    Create a binary cloud mask from a cloud probability file.

    Args:
        cloud_file: Path to the cloud probability file.
        output_file: Path to the output binary mask file.
        cloudfree_class: Class number for cloudfree pixels.

    Returns:
        Path to the created binary mask.

    Raises:
        RuntimeError: If mask creation fails.
    """
    cloud_file = main_utils.ensure_path(cloud_file)
    output_file = main_utils.ensure_path(output_file)
    main_utils.ensure_directory(output_file.parent)

    logger.info(f"Creating binary cloud mask with classes ≠{cloudfree_class} as clouds ({cloudfree_class}: cloudfree, ≠{cloudfree_class}: cloudy)")

    # Use gdal_calc to create binary mask
    command = [
        'gdal_calc.py',
        '-A', str(cloud_file),
        '--overwrite',
        f'--outfile={output_file}',
        f'--calc="A!={cloudfree_class}"',
        '--type=Byte',
        '--NoDataValue=None',
        '--co', 'COMPRESS=DEFLATE',
        '--co', 'PREDICTOR=2',
        '--co', 'NUM_THREADS=ALL_CPUS',
        '--quiet'
    ]

    success, _, stderr = main_utils.run_gdal_command(command)
    if not success:
        logger.error(f"Failed to create cloud mask: {stderr}")
        raise RuntimeError(f"Failed to create cloud mask: {stderr}")

    return str(output_file)


def coregister_S2(
    acquisition_date: Union[str, datetime],
    orbit_nr: int,
    cloudfree_class: Optional[int] = None
) -> tuple[bool, str, dict]:
    """
    Coregister a pre-mosaiced Sentinel-2 image using AROSICS.

    Args:
        acquisition_date: Acquisition date as string (YYYYMMDD) or datetime object.
        orbit_nr: Relative orbit number.
        cloud_threshold: Cloud threshold percentage. If None, uses value from config.

    Returns:
        Path to the output 10m coreg file.

    Raises:
        RuntimeError: If coregistration fails.
        FileNotFoundError: If required files are not found.
    """
    # Convert acquisition_date to string if it's a datetime
    if isinstance(acquisition_date, datetime):
        acquisition_date_str = acquisition_date.strftime("%Y%m%d")
    else:
        acquisition_date_str = str(acquisition_date)

    # Get cloud threshold from config if not provided
    if cloudfree_class is None:
        cloudfree_class = config.AROSICS_CONFIG['omnicloud_cloudfree_class']

    # Set up paths
    base_path = main_utils.ensure_path(config.PRODUCT_S2_LEVEL_2A["copernicus_collection"])
    data_dir = os.path.join(base_path, f"R{orbit_nr:03}", acquisition_date_str)
    data_dir = main_utils.ensure_directory(data_dir)

    logger.info(f"Coregistering Sentinel-2 data for date {acquisition_date_str}, orbit {orbit_nr}")

    # Find multiband files
    multiband_mosaic_10m_pattern = os.path.join(data_dir, config.AROSICS_CONFIG['multiband_mosaic_pattern_10m'])
    singleband_mosaic_10m_pattern = os.path.join(data_dir, config.AROSICS_CONFIG['singleband_mosaic_pattern_10m'])
    omnicloud_10m_pattern = os.path.join(data_dir, f"{config.AROSICS_CONFIG['singleband_mosaic_pattern']}_omnicloud.tif")

    # Find required files for coregistration
    mosaic_10m = glob.glob(singleband_mosaic_10m_pattern.replace('.vrt', '_clip.vrt'))
    omnicloud_10m = glob.glob(omnicloud_10m_pattern.replace('.tif', '_clip.vrt'))


    # File availability checks
    if not mosaic_10m: # If not available
        raise FileNotFoundError(f"No multiband mosaic found matching {multiband_mosaic_10m_pattern}")
    elif len(mosaic_10m)>1: # If multiple
        raise ValueError(f"Found multiple (i.e. {len(mosaic_10m)}) files matching the pattern '{multiband_mosaic_10m_pattern}'")

    if not omnicloud_10m:
        pass # Remove once CS+ is implemented
        #raise FileNotFoundError(f"No CSPlus data found matching {csplus_10m_pattern}")
    elif len(omnicloud_10m)>1: # If multiple
        raise ValueError(f"Found multiple (i.e. {len(omnicloud_10m)}) files matching the pattern '{omnicloud_10m_pattern}'")

    # Use the first file found
    mosaic_10m = mosaic_10m[0]
    try: # Remove once CS+ is implemented
        omnicloud_10m = omnicloud_10m[0]
    except: # Remove once CS+ is implemented
        pass # Remove once CS+ is implemented

    # Extract the date-time for the mosaic filename
    # Try to extract time from the first filename
    match = re.search(r'(\d{8}T\d{6})', str(mosaic_10m))
    if match:
        date_obj = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    else:
        # Use acquisition date with default time if no time found
        date_obj = datetime.strptime(acquisition_date_str, "%Y%m%d")

    # Format for output filename
    formatted_time = date_obj.strftime("%Y-%m-%dt%H%M%S")

    logger.info(f"Using multiband mosaic file: {os.path.basename(mosaic_10m)}")
    if omnicloud_10m:
        logger.info(f"Using OmniCloudMask file: {os.path.basename(omnicloud_10m)}")
    else:
        logger.warning("No cloud mask file found - proceeding without cloud masking")

    # Set coregistration parameters from config
    max_points = config.AROSICS_CONFIG['max_points']

    # Get target image dimensions
    try:
        x_size, y_size = main_utils.get_extent_and_dimensions(mosaic_10m)[-2:]
    except Exception as e:
        logger.error(f"Failed to get dimensions for {mosaic_10m}: {str(e)}")
        raise

    # Calculate grid resolution based on maximum points and image dimensions
    grid_res = round((((x_size * y_size) / max_points) ** .5) / 5) * 5

    # Set output file names and folder
    out_name = f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{formatted_time}_registration.pickle"

    # Output to topmost level (parent of main_functions)
    script_dir = Path(__file__).parent  # main_functions folder
    out_folder = script_dir.parent       # parent of main_functions
    out_folder = main_utils.ensure_directory(out_folder)

    # Check if output files already exist
    if (os.path.exists(os.path.join(out_folder, out_name)) and
        os.path.exists(os.path.join(out_folder, out_name.replace(".tif", ".pickle")))):
        logger.info(f"Image already coregistered --> Skipping")
        return False, os.path.exists(os.path.join(out_folder, out_name.replace(".tif", ".pickle")))

    # Process cloud mask if available
    cloud_mask_path = None
    if omnicloud_10m:
        # Create binary cloud mask
        cloud_bin_file = omnicloud_10m.replace('.vrt', '_bin.tif')
        try:
            cloud_mask_path = create_binary_cloud_mask(omnicloud_10m, cloud_bin_file, cloudfree_class)

            # Resample cloud mask to match multiband mosaic resolution
            gsd_multiband = main_utils.get_pixel_spacing(mosaic_10m)
            gsd_cld = main_utils.get_pixel_spacing(cloud_mask_path)

            if gsd_multiband != gsd_cld:
                logger.info(f"Resampling cloud mask to match multiband mosaic resolution")
                resampled_path = cloud_mask_path.replace(f'-{gsd_cld[0]:0.0f}m_', f'-{gsd_multiband[0]:0.0f}m_')
                cloud_mask_path = resample_raster(cloud_mask_path, mosaic_10m, resampled_path)
        except Exception as e:
            logger.error(f"Failed to process cloud mask: {str(e)}")
            cloud_mask_path = None

    # Set reference image path from config or use default
    reference_path = config.AROSICS_CONFIG['reference_image']

    # Print title
    title_str = f'{os.path.basename(mosaic_10m)} | {grid_res}x{grid_res}px'
    logger.info('=' * len(title_str))
    logger.info(title_str)
    logger.info('-' * len(title_str))

    try:
        # Set number of CPU threads
        num_cpus = max(os.cpu_count() - 1, 1)

        # Define coregistration arguments from config
        window_size = config.AROSICS_CONFIG['window_size']
        max_iter = config.AROSICS_CONFIG['max_iter']
        max_shift = config.AROSICS_CONFIG['max_shift']
        reference_band = config.AROSICS_CONFIG['reference_band'] # Band to be used from the reference image
        shift_band = next(i for i, v in enumerate(config.SENTINEL2_BAND_CONFIG[10]) if v == 'B04') + 1 # Band to be used from the image to be shifted, +1 because 1-based
        output_options = config.AROSICS_CONFIG['output_options']

        kwargs = {
            'path_out': f'{out_name}',
            'projectDir': out_folder,
            'q': False,
            'nodata': (0, 0),
            'mask_baddata_tgt': cloud_mask_path,
            'out_crea_options': output_options,
            # 'CPUs': num_cpus,
            'CPUs': 32,
            'progress': True,
            'fmt_out': 'GTIFF',
            'r_b4match': reference_band,
            's_b4match': shift_band,
            'window_size': tuple(window_size),
            'max_iter': max_iter,
            'max_shift': max_shift,
            'grid_res': grid_res,
        }

        # Perform coregistration
        logger.info(f"Running coregistration with grid resolution {grid_res}x{grid_res}px for Ref Band #{reference_band}/Tgt Band #{shift_band}")

        #mosaic_10m = '/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-mosaic_20250619T101559_B04_10m.tif'
        #mosaic_10m = mosaic_10m.replace('.vrt','.tif')
        #kwargs['path_out'] = os.path.basename(mosaic_10m).replace('.tif', f'{config.AROSICS_CONFIG["coreg_file_suffix"]}.tif')

        del kwargs['s_b4match']
        CRL = COREG_LOCAL(config.AROSICS_CONFIG['reference_image'].replace(".tif", "_clip.vrt"), mosaic_10m, **kwargs)
        #test = CRL.correct_shifts()
        #deshift_image(im_target='/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-multiband_20250619T101559_10m.vrt', coreg_info=CRL.coreg_info, path_out='/home/localadmin/Downloads/S2_Test/test_multi_10m.tif', fmt_out='GTIFF', CPUs=64, nodata=0)
        #deshift_image(im_target='/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-multiband_20250619T101559_20m.vrt', coreg_info=CRL.coreg_info, path_out='/home/localadmin/Downloads/S2_Test/test_multi_20m.tif', fmt_out='GTIFF', CPUs=64, nodata=0)
        #deshift_image(im_target='/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-multiband_20250619T101559_60m.vrt', coreg_info=CRL.coreg_info, path_out='/home/localadmin/Downloads/S2_Test/test_multi_60m.tif', fmt_out='GTIFF', CPUs=64, nodata=0)
        # Save coregistration info to pickle file
        pickle_path = os.path.join(out_folder, out_name)
        coreg_info_to_pickle(CRL.coreg_info, pickle_path) # Also correcting shifts
        logger.info(f"Saved coregistration info to {pickle_path}")

        if len(CRL.coreg_info['GCPList']) != 0:  # If there are GCPs, so not totally cloud covered
            logger.info(f"Found {len(CRL.coreg_info['GCPList'])} valid GCPs")
            #CRL.correct_shifts()
            #deshift_image(im_target='/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-mosaic_20250619T101559_B01_20m.vrt', pickle_path=pickle_path, path_out='/home/localadmin/Downloads/S2_Test/test_multi_20m.tif', fmt_out='GTIFF', CPUs=64, nodata=0)
            #deshift_image(im_target='/home/localadmin/Downloads/S2_Test/R065/20250619/S2-L2A-mosaic_20250619T101559_B09_60m.vrt', pickle_path=pickle_path, path_out='/home/localadmin/Downloads/S2_Test/test_multi_60m.tif', fmt_out='GTIFF', CPUs=64, nodata=0)

            # Save tie points to shapefile
            #shapefile_path = os.path.join(out_folder, out_name.replace('_clip', '').replace(".tif", ".shp"))
            #CRL.tiepoint_grid.to_PointShapefile(path_out=shapefile_path)
            #logger.info(f"Saved tie points to {shapefile_path}")
            success = True
        else:
            logger.warning("No valid GCPs found - area may be totally cloud covered")
            success = False

        logger.info('=' * len(title_str))

        return success, pickle_path

    except Exception as e:
        logger.error(f"Coregistration failed: {str(e)}")
        raise RuntimeError(f"Coregistration failed: {str(e)}")


def coreg_info_to_pickle(coreg_info: Dict[str, Any], file_path: Union[str, Path]) -> None:
    """
    Dump coregistration information to a pickle file.

    Args:
        coreg_info: Coregistration information dictionary.
        file_path: Path to save the pickle file.

    Returns:
        None
    """
    file_path = main_utils.ensure_path(file_path)
    main_utils.ensure_directory(file_path.parent)

    logger.info(f"Saving coregistration info to {file_path}")

    try:
        # Make a deep copy to avoid modifying the original
        coreg_info_dump = coreg_info.copy()

        # Convert GCP objects to tuples of their attributes to make them picklable
        coreg_info_dump['GCPList'] = [
            (gcp.GCPX, gcp.GCPY, gcp.GCPZ, gcp.GCPPixel, gcp.GCPLine)
            for gcp in coreg_info_dump['GCPList']
        ]

        with open(file_path, 'wb') as outF:
            pickle.dump(coreg_info_dump, outF)

        logger.info(f"Successfully saved coregistration info to {file_path}")
    except Exception as e:
        logger.error(f"Error saving coregistration info: {str(e)}")
        raise


def coreg_info_from_pickle(file_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load coregistration information from a pickle file.

    Args:
        file_path: Path to the pickle file.

    Returns:
        Dictionary containing coregistration information.

    Raises:
        FileNotFoundError: If file doesn't exist.
        ValueError: If file format is invalid.
    """
    file_path = main_utils.ensure_path(file_path)

    logger.info(f"Loading coregistration info from {file_path}")

    try:
        # Load the pickled data
        with open(file_path, 'rb') as inF:
            coreg_info_new = pickle.load(inF)

        # Convert the tuples back to GDAL GCP objects
        coreg_info_new['GCPList'] = [gdal.GCP(*attrs) for attrs in coreg_info_new['GCPList']]

        logger.info(f"Successfully loaded coregistration info with {len(coreg_info_new['GCPList'])} GCPs")
        return coreg_info_new
    except FileNotFoundError:
        logger.error(f"Coregistration info file not found: {file_path}")
        raise
    except Exception as e:
        logger.error(f"Error loading coregistration info: {str(e)}")
        raise ValueError(f"Invalid coregistration info file: {str(e)}")


def deshift_image(
    im_target: Union[str, Path],
    pickle_path = Union[str, Path],
    **kwargs
) -> None:
    """
    Deshift an image using coregistration information.

    Args:
        im_target: Path of the image to be de-shifted.
        coreg_info: The results of the co-registration (from COREG.coreg_info or COREG_LOCAL.coreg_info).
        **kwargs: Additional keyword arguments for DeShifter.DESHIFTER.

    Keyword Args:
        path_out (str): Output path for coregistered results.
        fmt_out (str): Raster file format (default: 'ENVI').
        out_crea_options (list): GDAL creation options.
        band2process (int): Band index to process (starts with 1).
        nodata (float): No data value.
        out_gsd (float): Output pixel size.
        align_grids (bool): Align input grid to reference.
        match_gsd (bool): Match input pixel size to reference.
        target_xyGrid (list): Custom output grid [[x_min,x_max], [y_min,y_max]].
        min_points_local_corr (int): Min points for local correction (default: 5).
        resamp_alg (str): Resampling algorithm.
        cliptoextent (bool): Clip to actual bounds.
        clipextent (list): Manual clipping extent [xmin,ymin,xmax,ymax].
        CPUs (int): Number of CPUs to use.
        progress (bool): Show progress.
        v (bool): Verbose mode.
        q (bool): Quiet mode.

    Returns:
        None

    Raises:
        ValueError: If image cannot be opened or processed.
    """
    # Example of using pickle for coregistration:
    #
    # Step 1: Save coregistration info to pickle file
    # coreg_info_to_pickle(CRL.coreg_info, '/path/to/coreg_info.pkl')
    #
    # Step 2: Load coregistration info from pickle file later
    # coreg_info_reloaded = coreg_info_from_pickle('/path/to/coreg_info.pkl')
    #
    # Step 3: Use reloaded info to deshift image
    # deshift_image(image_to_deshift, coreg_info_reloaded, path_out='/path/to/output.tif')

    im_target = main_utils.ensure_path(im_target)

    logger.info(f"Deshifting image: {im_target}")

    coreg_info = coreg_info_from_pickle(pickle_path)

    try:
        # GSD is typically the absolute value of the pixel size in the x-direction
        ref_gsd_x = coreg_info['updated map info means'][5]  # Pixel size in the x-direction
        ref_gsd_y = coreg_info['updated map info means'][6]  # Pixel size in the y-direction

        # Extract target image GSD
        tgt_gsd_x, tgt_gsd_y = main_utils.get_pixel_spacing(im_target)

        # Recalculate GCPPixel and GCPLine for each GCP in the list
        logger.info(f"Adjusting GCPs for target GSD: {tgt_gsd_x} x {tgt_gsd_y}")
        for gcp in coreg_info['GCPList']:
            gcp.GCPPixel = gcp.GCPPixel * (ref_gsd_x / tgt_gsd_x)
            gcp.GCPLine = gcp.GCPLine * (ref_gsd_y / tgt_gsd_y)

        # Ensure output directory exists if path_out is specified
        if 'path_out' in kwargs:
            main_utils.ensure_directory(Path(kwargs['path_out']).parent)

        # Default parameters if not specified based on https://github.com/geostandards-ch/cog-best-practices#lossless-raster
        default_params = {
            'fmt_out': 'COG',
            'out_crea_options': ['COMPRESS=DEFLATE', 'PREDICTOR=2', 'NUM_THREADS=ALL_CPUS', 'BIGTIFF=YES'],
            'progress': True,
            'out_gsd': (tgt_gsd_x, tgt_gsd_y),
            'resamp_alg': 'nearest',
        }

        # Update with defaults if not in kwargs
        for key, value in default_params.items():
            if key not in kwargs:
                kwargs[key] = tuple(value) if isinstance(value, tuple) else value # Making sure that tuples are preserved

        logger.info(f"Running DeShifter with parameters: {kwargs}")
        DeShifter.DESHIFTER(im2shift=str(im_target), coreg_results=coreg_info, **kwargs).correct_shifts()
        logger.info(f"Successfully deshifted image")

    except Exception as e:
        logger.error(f"Error deshifting image: {str(e)}")
        raise


def deshift_files(
    acquisition_date: Union[str, datetime],
    orbit_nr: int,
    pickle_path: Union[str, Path],
    **kwargs
) -> List[str]:
    """
    Deshift all files for a given acquisition using coregistration info.

    Args:
        acquisition_date: Acquisition date as string (YYYYMMDD) or datetime object.
        orbit_nr: Relative orbit number.
        pickle_path: Path to the pickle file containing coregistration info.
        **kwargs: Additional arguments for deshift_image (e.g., CPUs, fmt_out).

    Returns:
        List of output file paths.
    """
    # Convert acquisition_date to string if it's a datetime
    if isinstance(acquisition_date, datetime):
        acquisition_date_str = acquisition_date.strftime("%Y%m%d")
    else:
        acquisition_date_str = str(acquisition_date)

    # Find all files to deshift
    base_path = f"{config.PRODUCT_S2_LEVEL_2A['copernicus_collection']}/R{orbit_nr:03}/{acquisition_date_str}"

    files_to_deshift = []
    files_to_deshift += glob.glob(f"{base_path}/{config.AROSICS_CONFIG['singleband_mosaic_pattern']}{acquisition_date_str}*_*_*m_clip.vrt")
    files_to_deshift += glob.glob(f"{base_path}/{config.AROSICS_CONFIG['singleband_mosaic_pattern']}{acquisition_date_str}*_omnicloud_clip.vrt")
    #files_to_deshift += glob.glob(f"{base_path}/{config.AROSICS_CONFIG['cloudprob_mosaic_pattern'].replace('.vrt', '_clip.vrt')}")
    #files_to_deshift += glob.glob(f"{base_path}/{config.AROSICS_CONFIG['cloudprob_mosaic_pattern'].replace('.vrt', '_clip_bin.tif')}")

    # Extract datetime from pickle filename
    pickle_basename = os.path.basename(pickle_path)
    datetime_match = re.search(r'(\d{4}-\d{2}-\d{2}t\d{6})', pickle_basename)
    if not datetime_match:
        raise ValueError(f"Could not extract datetime from pickle filename: {pickle_basename}")
    formatted_time = datetime_match.group(1)

    # Get topmost directory for output
    script_dir = Path(__file__).parent  # main_functions folder
    topmost_dir = script_dir.parent      # parent of main_functions

    output_paths = []

    for file in files_to_deshift:
        logger.info(f"Processing: {os.path.basename(file)}")

        # Get nodata value
        info = main_utils.get_raster_info(file)
        nodata = info["bands"][0]["no_data_value"]

        # Extract band name and GSD from filename
        # Pattern matches any band name (B02, B8A, AOT, SCL, TCI, etc.) followed by resolution
        match = re.search(r'_([A-Z0-9]+)_(\d+)m', os.path.basename(file))
        if match:
            band_name = match.group(1).lower()
            gsd = match.group(2)
            suffix = f"{band_name}_{gsd}m"
        else:
            # Fallback for files without band info (like cloud masks or omnicloud)
            #if '_cloud_' in os.path.basename(file): # CS+
            #    band_name = 'cloudmask'
            #    suffix = f"{band_name}_10m"
            if '_omnicloud_' in os.path.basename(file): #Omnicloud
                band_name = 'cloudmask'
                suffix = f"{band_name}_10m"
            else:
                logger.info(f"Unknown file in list for deshifting: {os.path.basename(file)}. Skipping.")
                continue

        # Build output path at topmost level
        output_filename = f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{formatted_time}_{suffix}.tif"
        output_path = topmost_dir / output_filename

        # Deshift the image
        deshift_image(
            im_target=file,
            pickle_path=pickle_path,
            path_out=str(output_path),
            nodata=nodata,
            **kwargs
        )

        output_paths.append(str(output_path))

    logger.info(f"Deshifted {len(output_paths)} files")


    return output_paths


def main():
    """
    Main entry point for command-line usage.

    Example usage:
    python main_coregistration.py 20220715 42

    This will coregister Sentinel-2 data from July 15, 2022, orbit 42
    using the cloud threshold from the config file.
    """
    import argparse

    parser = argparse.ArgumentParser(description='Coregister Sentinel-2 imagery.')
    parser.add_argument('acquisition_date', type=str, help='Acquisition date (YYYYMMDD)')
    parser.add_argument('orbit_nr', type=int, help='Relative orbit number')
    parser.add_argument('cloud_threshold', type=int, help='Cloud threshold percentage')

    args = parser.parse_args()

    try:
        # Run coregistration
        result = coregister_S2(
            acquisition_date=args.acquisition_date,
            orbit_nr=args.orbit_nr,
            cloud_threshold=args.cloud_threshold
        )

        logger.info(f"Coregistration completed successfully. Output: {result}")
        return 0
    except Exception as e:
        logger.error(f"Error during coregistration: {str(e)}")
        return 1