"""
Utility functions for coregistration of satellite imagery.

This module provides utility functions for handling geospatial data and file operations
in a cross-platform compatible way (Linux and Windows). GDAL operations are performed
through subprocess calls instead of Python bindings for better cross-environment compatibility.
"""
# General python libraries/modules
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any

import numpy as np

# Specific SATROMO libraries/modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config
from main_functions import main_mosaicing
from main_functions import main_utils

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("coreg")


def resolve_os_path(path: Union[str, Path], is_file: bool = False) -> Path:
    """
    Resolves paths that might have been created on a different OS.
    Handles mixed path separators and different drive notation.

    Args:
        path: The path to resolve
        is_file: Whether the path is a file (True) or directory (False)

    Returns:
        Resolved Path object
    """
    path_str = str(path)

    # Handle Windows paths on Linux (e.g., C:/path/to/file)
    if sys.platform != 'win32' and re.match(r'^[A-Za-z]:', path_str):
        # Convert Windows drive letter to a Linux-friendly path
        path_str = path_str.replace(':', '')
        path_str = f"/mnt/{path_str[0].lower()}/{path_str[2:]}"

    # Handle Linux paths on Windows (e.g., /mnt/c/path/to/file)
    elif sys.platform == 'win32' and re.match(r'^/mnt/[a-z]/', path_str):
        # Convert Linux /mnt/c/... to Windows C:/...
        drive_letter = path_str[5].upper()
        path_str = f"{drive_letter}:{path_str[6:]}"

    # Create Path object and resolve
    path = Path(path_str)

    # Create parent directories if needed
    if is_file:
        main_utils.ensure_directory(path.parent)
    else:
        main_utils.ensure_directory(path)

    return path


def equalize_extents(reference_path: Union[str, Path], target_path: Union[str, Path],
                    output_path: Optional[Union[str, Path]] = None) -> str:
    """
    Equalize the extents of two rasters, cropping the reference to match the target.

    Args:
        reference_path: Path to the reference raster
        target_path: Path to the target raster
        output_path: Optional path for the output raster (default: auto-generated)

    Returns:
        Path to the equalized reference raster

    Raises:
        ValueError: If the rasters cannot be equalized
    """
    reference_path = main_utils.ensure_path(reference_path)
    target_path = main_utils.ensure_path(target_path)

    if output_path is None:
        # Create output path next to reference file with '_masked' suffix
        output_path = reference_path.with_name(f"{reference_path.stem}_masked{reference_path.suffix}")
    else:
        output_path = main_utils.ensure_path(output_path)

    # Get extents and dimensions of target
    target_info = main_utils.get_raster_info(target_path)
    reference_info = main_utils.get_raster_info(reference_path)

    minx_target, miny_target, maxx_target, maxy_target = target_info["extent"]
    width_target, height_target = target_info["width"], target_info["height"]

    minx_ref, miny_ref, maxx_ref, maxy_ref = reference_info["extent"]
    width_ref, height_ref = reference_info["width"], reference_info["height"]

    # Skip if already matching
    if (minx_target == minx_ref and maxx_target == maxx_ref and
        miny_target == miny_ref and maxy_target == maxy_ref and
        width_target == width_ref and height_target == height_ref):
        logger.info("Reference and target extents already match")
        return str(reference_path)

    # Get pixel spacing
    pixel_x, pixel_y = target_info["pixel_width"], target_info["pixel_height"]

    # Crop reference to match target using gdalwarp
    logger.info(f"Cropping reference raster to match target extent: {minx_target}, {miny_target}, {maxx_target}, {maxy_target}")

    command = [
        "gdalwarp",
        "-overwrite",
        "-te", str(minx_target), str(miny_target), str(maxx_target), str(maxy_target),
        "-tr", str(pixel_x), str(pixel_y),
        "-tap",  # Target aligned pixels
        "-r", "near",  # Resampling method
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "-co", "NUM_THREADS=ALL_CPUS",
        str(reference_path),
        str(output_path)
    ]

    success, _, stderr = main_utils.run_gdal_command(command)

    if not success:
        raise ValueError(f"Failed to equalize extents: {stderr}")

    return str(output_path)


def create_binary_mask(source_path: Union[str, Path], output_path: Union[str, Path],
                      threshold: float, greater_than: bool = True) -> str:
    """
    Create a binary mask from a raster based on a threshold.

    Args:
        source_path: Path to the source raster
        output_path: Path for the output mask
        threshold: Threshold value
        greater_than: If True, mask where values >= threshold; if False, mask where values <= threshold

    Returns:
        Path to the binary mask

    Raises:
        ValueError: If the mask cannot be created
    """
    source_path = main_utils.ensure_path(source_path)
    output_path = main_utils.ensure_path(output_path)
    main_utils.ensure_directory(output_path.parent)

    logger.info(f"Creating binary mask with threshold {threshold} (greater_than={greater_than})")

    # Create expression for gdal_calc
    if greater_than:
        calc_expr = f"A>={threshold}"
    else:
        calc_expr = f"A<={threshold}"

    # Use gdal_calc to create binary mask
    command = [
        "gdal_calc.py",
        "--overwrite",
        "-A", str(source_path),
        f"--outfile={output_path}",
        f"--calc={calc_expr}",
        "--type=Byte",
        "--NoDataValue=0",
        "--co", "COMPRESS=DEFLATE",
        "--co", "PREDICTOR=2",
        "--co", "NUM_THREADS=ALL_CPUS",
        "--quiet"
    ]

    success, _, stderr = main_utils.run_gdal_command(command)

    if not success:
        raise ValueError(f"Failed to create binary mask: {stderr}")

    return str(output_path)


def reproject_to_CH1903(input_path: Union[str, Path],
                          output_path: Union[str, Path],
                          resolution: Optional[Tuple[float, float]] = None,
                          resampling: str = "near") -> str:
    """
    Reproject a raster from UTM32N (EPSG:32632) to CH1903+ (EPSG:2056).

    Args:
        input_path: Path to the input raster file
        output_path: Path for the output reprojected raster
        resolution: Optional tuple of (x_resolution, y_resolution) in meters
                   If None, preserves the resolution of the original raster
        resampling: Resampling algorithm to use (near, bilinear, cubic, cubicspline,
                   lanczos, average, mode, max, min, med, q1, q3, sum)

    Returns:
        Path to the reprojected output raster

    Raises:
        ValueError: If reprojection fails
    """
    input_path = main_utils.ensure_path(input_path)
    output_path = main_utils.ensure_path(output_path)
    main_utils.ensure_directory(output_path.parent)

    logger.info(f"Reprojecting from UTM32N to CH1903+: {input_path}")

    # Build gdalwarp command
    command = [
        "gdalwarp",
        "-overwrite",
        "-t_srs", "EPSG:2056",   # CH1903+
        "-r", resampling,
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "-co", "NUM_THREADS=ALL_CPUS",
        "-co", "BIGTIFF=YES"
    ]

    # Add resolution if specified
    if resolution:
        command.extend(["-tr", str(resolution[0]), str(resolution[1])])

    # Add input and output paths
    command.extend([str(input_path), str(output_path)])

    # Run the command
    success, _, stderr = main_utils.run_gdal_command(command)

    if not success:
        raise ValueError(f"Failed to reproject from UTM32N to CH1903+: {stderr}")

    return str(output_path)


def reproject_to_UTM32N(input_path: Union[str, Path],
                           output_path: Union[str, Path],
                           resolution: Optional[Tuple[float, float]] = None,
                           resampling: str = "near") -> str:
    """
    Reproject a raster to UTM32N (EPSG:32632).

    Args:
        input_path: Path to the single band input raster file
        output_path: Path for the output reprojected raster
        resolution: Optional tuple of (x_resolution, y_resolution) in meters
                   If None, preserves the resolution of the original raster
        resampling: Resampling algorithm to use (near, bilinear, cubic, cubicspline,
                   lanczos, average, mode, max, min, med, q1, q3, sum)

    Returns:
        No data value used for reprojected file

    Raises:
        ValueError: If reprojection fails
    """
    input_path = main_utils.ensure_path(input_path)
    output_path = main_utils.ensure_path(output_path)
    main_utils.ensure_directory(output_path.parent)

    logger.info(f"Reprojecting from UTM31N to UTM32N: {input_path}")



    # Build gdalwarp command
    command = [
        "gdalwarp",
        "-overwrite",
        "-t_srs", "EPSG:32632",  # UTM32N
        # "-r", resampling,
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "-co", "NUM_THREADS=ALL_CPUS",
        "-co", "BIGTIFF=YES",
        "-co", "QUALITY=100",  # Use lossless compression for JP2
        "-co", "REVERSIBLE=YES",  # Ensure lossless wavelet transform
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-wo", "UNIFIED_SRC_NODATA=YES",
        "-tap", # Corner coordinates are integer divisible by the cell resolution
    ]

    # Add resolution if specified
    if resolution:
        command.extend(["-tr", str(resolution[0]), str(resolution[1])])

    # Add noData value
    file_in_info = main_utils.get_raster_info(input_path)
    src_no_data = file_in_info['bands'][0]['no_data_value']
    if src_no_data is None: # If there is no noData value set -> only set the output
        src_no_data = 0
        command.extend(["-srcnodata", str(src_no_data)])
        command.extend(["-dstnodata", str(src_no_data)])
    else: # If there is a noData value set -> set the output to the input
        command.extend(["-srcnodata", str(src_no_data)])
        command.extend(["-dstnodata", str(src_no_data)])

    # Ensure output data type is identical to input
    command.extend(['-ot', file_in_info['bands'][0]['data_type']])

    # Add input and output paths
    command.extend([str(input_path), str(output_path)])

    # Run the command
    success, _, stderr = main_utils.run_gdal_command(command)

    if not success:
        raise ValueError(f"Failed to reproject from CH1903+ to UTM32N: {stderr}")

    return src_no_data


def reproject_tiles_to_UTM32N(acquisition_date: str, orbit_nr: int):
    """
    Reproject all S2 and CS+ tiles in UTM31N of a single date/orbit combination to UTM32N (EPSG:32632).

    Args:
        acquisition_date: Date string in the format yyyymmdd of the date of interest
        orbit_nr: Orbit number as an integer

    Returns:
        NoData value used for reprojected files
    """

    data_folder = config.PRODUCT_S2_LEVEL_2A["copernicus_collection"]
    noData_value = None # Assuring a return even if no reprojection was needed
    s2_tiles = glob.glob(os.path.join(data_folder, f'R{orbit_nr:03d}', acquisition_date, '*T31*.jp2'))
    for file_in in s2_tiles:
        info = main_utils.get_raster_info(file_in)
        file_out = file_in.replace('T31', 'T32')
        noData_value = reproject_to_UTM32N(file_in, file_out, resolution=[info['pixel_width'], info['pixel_height']])

    return noData_value


def reproject_coregistered_mosaics_to_CH1903(date_str: str, orbit_nr: int):
    """
    Reproject a vrt mosaic in UTM32N of a single date/orbit combination to CH1903+ (EPSG:2056).
    Reprojection uses oversampling step to diminish aliasing effects along straight borders.

    Args:
        date_str: Date string in the format yyyymmdd of the date of interest
        orbit_nr: Orbit number as an integer
    """

    data_folder = config.PRODUCT_S2_LEVEL_2A["copernicus_collection"]
    for file_in in glob.glob(os.path.join(data_folder, f'R{orbit_nr:03d}', date_str, 'AROSICS_output','*.tif')):
        pass