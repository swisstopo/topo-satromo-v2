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
from main_utils import run_gdal_command, ensure_directory, ensure_path

logger = logging.getLogger(__name__)

def create_sentinel2_band_mosaic(
    acquisition_date: Union[str, datetime],
    orbit_nr: int,
    band_name: str,
    ground_sampling_distance: Optional[int] = None,
    noData_value: Optional[int] = 0,
    output_dir: Optional[Union[str, Path]] = None
) -> Dict[str, Any]:
    """
    Create a mosaic from Sentinel-2 tiles for a specific orbit and date.
    
    Args:
        acquisition_date: Acquisition date (string YYYYMMDD or datetime)
        orbit_nr: Relative orbit number
        band_name: Band designation used in the filename to be mosaiced
        ground_sampling_distance: ground sampling distance of the band of interest
        noData_value: NoData value to be used while mosaicing (default: 0)
        output_dir: Directory for the output mosaic (default: base_path/orbit/date)
        
    Returns:
        Dictionary with paths to the created VRTs and processing details
        
    Raises:
        ValueError: If no B04 or cloud mask files are found
    """
    # Convert paths to Path objects
    base_path = ensure_path(config.AROSICS_CONFIG['data_folder'])
    
    # Convert acquisition_date to string if it's a datetime
    if isinstance(acquisition_date, datetime):
        acquisition_date_str = acquisition_date.strftime("%Y%m%d")
    else:
        acquisition_date_str = str(acquisition_date)
    
    # Set up orbit path
    data_dir = os.path.join(base_path,f"R{orbit_nr:03d}",acquisition_date_str)
    
    # Set output directory if not provided
    if output_dir is None:
        output_dir = data_dir
    else:
        output_dir = ensure_path(output_dir)
    
    # Ensure output directory exists
    ensure_directory(output_dir)
    
    logger.info(f"Creating mosaic for orbit {orbit_nr} date {acquisition_date_str}")
    
    # Extract acquisition date in ISO format for filenames
    time_str = None

    # Extract ground sampling distance of band
    if not ground_sampling_distance:
        files_temp = list(glob.glob(os.path.join(data_dir, f"T32*_{band_name}_*m.jp2")))
    
        if not files_temp:
            raise ValueError(f"No files found matching pattern T32*_{band_name}_*m.jp2 in {data_dir}")
    
        # Extract the GSD values from all files
        gsd_values = []
        for file_path in files_temp:
            # Extract the part between last underscore and 'm.jp2'
            match = re.search(r'_(\d+)m\.jp2$', file_path)
            if match:
                gsd_values.append(int(match.group(1)))
            else:
                logger.warning(f"Could not extract GSD from filename: {file_path}")
        
        # Verify all GSD values are identical
        if not gsd_values:
            raise ValueError(f"Could not extract GSD from any filename in {data_dir}")
        
        if len(set(gsd_values)) != 1:
            raise ValueError(f"Found inconsistent ground sampling distances: {set(gsd_values)}")
    
        # Use the common GSD value
        ground_sampling_distance = gsd_values[0]
        logger.info(f"Using ground sampling distance of {ground_sampling_distance}m from filenames")

    
    # Find all B04 and cloud mask files for the orbit and date
    band_files = list(glob.glob(os.path.join(data_dir, f"T32*_{band_name}_{ground_sampling_distance}m.jp2")))
    #cloud_files = list(data_dir.glob("*_MSK_CLDPRB_20m*.jp2"))
    
    if not band_files:
        error_msg = f"No files found for band {band_name} @ {ground_sampling_distance} m GSD for orbit {orbit_nr} on date {acquisition_date_str}"
        logger.warning(error_msg)
        raise ValueError(error_msg)
    
    #if not cloud_files:
    #    error_msg = f"No cloud mask files found for orbit {orbit_nr} on date {acquisition_date_str}"
    #    logger.warning(error_msg)
    #    raise ValueError(error_msg)
    
    # Extract the date-time for the mosaic filename
    if not time_str:
        # Try to extract time from the first filename
        match = re.search(r'(\d{4}\d{2}\d{2}T\d{6})', band_files[0])
        if match:
            time_str = match.group(1)
        else:
            # Use acquisition date with default time if no time found
            date_obj = datetime.strptime(acquisition_date_str, "%Y%m%d")
            time_str = date_obj.strftime("%Y-%m-%dT000000")
    
    logger.info(f"Creating mosaic for band {band_name} @ {ground_sampling_distance} m GSD for orbit {orbit_nr} date {time_str}")
    
    # Define output VRT paths
    vrt_path = os.path.join(output_dir, f"S2-L2A-mosaic_{time_str}_{band_name}_{ground_sampling_distance}m.vrt")
    #cloud_vrt_path = output_dir / f"S2-L2A-mosaic_{time_str}_cloud-20m.vrt"
    
    # Create VRTs
    logger.info(f"Creating B04 mosaic VRT with {len(band_files)} files")
    success = build_vrt(band_files, vrt_path, noData_value)
    
    if not success:
        raise ValueError(f"Failed to create band mosaic VRT")
    
    #logger.info(f"Creating cloud mosaic VRT with {len(cloud_utm_files)} files")
    #success = build_vrt(cloud_utm_files, cloud_vrt_path, allow_projection_difference=True, srcnodata=0)
    
    if not success:
        raise ValueError(f"Failed to create cloud mosaic VRT")
    
    return {
        "success": True,
        "band_vrt": str(vrt_path),
        #"cloud_vrt": str(cloud_vrt_path),
        "time_str": time_str,
        "band_files_count": len(band_files),
        #"cloud_files_count": len(cloud_utm_files)
    }


def build_vrt(input_files: List[Path], output_vrt: Path, 
             allow_projection_difference: bool = False,
             noData_value: Optional[int] = 0) -> bool:
    """
    Build a VRT from a list of input files.
    
    Args:
        input_files: List of input file paths
        output_vrt: Output VRT file path
        allow_projection_difference: Whether to allow projection differences
        noData_value: Source nodata value (default: 0)
        
    Returns:
        True if successful, False otherwise
    """
    command = ["gdalbuildvrt"]
    
    if allow_projection_difference:
        command.append("-allow_projection_difference")
    
    if noData_value is not None:
        command.extend(["-srcnodata", str(noData_value)])
    
    command.append(str(output_vrt))
    command.extend([str(f) for f in input_files])
    
    success, _, stderr = run_gdal_command(command)
    
    if not success:
        logger.error(f"Failed to build VRT: {stderr}")
        return False
        
    return True

def create_multiband_raster(
    acquisition_date: Union[str, datetime],
    orbit_nr: int,
    gsd_dict: Dict[int, List[str]],
    output_dir: Optional[Union[str, Path]] = None,
    noData_value: Optional[int] = 0
) -> Dict[str, Any]:
    """
    Create multiband rasters from mosaiced Sentinel-2 bands, grouped by ground sampling distance.
    
    Args:
        acquisition_date: Acquisition date (string YYYYMMDD or datetime)
        orbit_nr: Relative orbit number
        gsd_dict: Dictionary mapping ground sampling distances to ordered lists of band names
                  Example: {10: ["B02", "B03", "B04", "B08"], 20: ["B05", "B06", "B07", "B8A", "B11", "B12"]}
        output_dir: Directory for the output multiband raster (default: base_path/orbit/date)
        noData_value: NoData value to be used for the output raster (default: 0)
        
    Returns:
        Dictionary with paths to the created multiband rasters and processing details
        
    Raises:
        ValueError: If no VRT files are found for specified bands
    """
    # Convert paths to Path objects
    base_path = ensure_path(config.AROSICS_CONFIG['data_folder'])
    
    # Convert acquisition_date to string if it's a datetime
    if isinstance(acquisition_date, datetime):
        acquisition_date_str = acquisition_date.strftime("%Y%m%d")
    else:
        acquisition_date_str = str(acquisition_date)
    
    # Set up orbit path
    data_dir = os.path.join(base_path, f"R{orbit_nr:03d}", acquisition_date_str)
    
    # Set output directory if not provided
    if output_dir is None:
        output_dir = data_dir
    else:
        output_dir = ensure_path(data_dir)
    
    # Ensure output directory exists
    ensure_directory(output_dir)
    
    logger.info(f"Creating multiband rasters for orbit {orbit_nr} date {acquisition_date_str}")
    
    # Dictionary to store results
    results = {
        "success": True,
        "multiband_rasters": {},
        "time_str": None
    }
    
    # Process each ground sampling distance separately
    for gsd, band_list in gsd_dict.items():
        logger.info(f"Processing {len(band_list)} bands with {gsd}m ground sampling distance")
        
        # List to store VRT files for this GSD
        vrt_files = []
        time_str = None
        
        # Create or locate VRT for each band
        for band_name in band_list:
            try:
                # First try to find existing VRT
                vrt_pattern = os.path.join(data_dir, f"S2-L2A-mosaic_*_{band_name}_{gsd}m.vrt")
                existing_vrts = list(glob.glob(vrt_pattern))
                
                if existing_vrts:
                    vrt_path = existing_vrts[0]
                    logger.info(f"Found existing VRT for {band_name}: {vrt_path}")
                    
                    # Extract time string if not already set
                    if not time_str:
                        match = re.search(r'S2-L2A-mosaic_(\d{4}\d{2}\d{2}T\d{6})_', vrt_path)
                        if match:
                            time_str = match.group(1)
                else:
                    # Create mosaic if VRT doesn't exist
                    logger.info(f"Creating mosaic for {band_name} at {gsd}m resolution")
                    mosaic_result = create_sentinel2_band_mosaic(
                        acquisition_date=acquisition_date,
                        orbit_nr=orbit_nr,
                        band_name=band_name,
                        ground_sampling_distance=gsd,
                        noData_value=noData_value,
                        output_dir=output_dir
                    )
                    
                    vrt_path = mosaic_result["band_vrt"]
                    
                    # Set time string if not already set
                    if not time_str:
                        time_str = mosaic_result["time_str"]
                
                vrt_files.append(vrt_path)
                
            except ValueError as e:
                logger.warning(f"Failed to process band {band_name}: {str(e)}")
                results["success"] = False
        
        # Update the global time_str if it's not set yet
        if not results["time_str"] and time_str:
            results["time_str"] = time_str
        
        # Skip if no VRTs were found/created for this GSD
        if not vrt_files:
            logger.warning(f"No VRTs found or created for {gsd}m resolution bands")
            continue
        
        # Create the multiband output filename (VRT)
        output_filename = f"S2-L2A-multiband_{time_str}_{gsd}m.vrt"
        output_path = os.path.join(output_dir, output_filename)
        
        # Build a list of band descriptions for GDAL
        band_descriptions = []
        for i, vrt in enumerate(vrt_files):
            band_name = band_list[i]
            band_descriptions.append(f"-b 1 -dstband {i+1}")
        
        # Construct the GDAL command to build a VRT with multiple bands
        output_path = os.path.join(output_dir, f"S2-L2A-multiband_{time_str}_{gsd}m.vrt")
        
        gdal_command = [
            "gdalbuildvrt",
            "-separate",
            "-resolution", "highest",
            "-a_srs", "EPSG:32632",  # UTM zone 32N - you may need to adjust this or make it dynamic
            "-vrtnodata", str(noData_value),
            output_path
        ]
        
        # Add input files
        gdal_command.extend(vrt_files)
        
        # Run the GDAL command
        logger.info(f"Running: {' '.join(gdal_command)}")
        success, stdout, stderr = run_gdal_command(gdal_command)
        
        if success:
            logger.info(f"Successfully created multiband raster: {output_path}")
            
            # Create a separate log file with band information
            band_info_file = os.path.splitext(output_path)[0] + "_bands.txt"
            with open(band_info_file, 'w') as f:
                f.write(f"Multiband raster: {output_path}\n")
                f.write(f"Created on: {datetime.now().isoformat()}\n\n")
                f.write(f"Band order:\n")
                for i, band_name in enumerate(band_list):
                    f.write(f"Band {i+1}: {band_name}\n")
            
            # Add to results
            results["multiband_rasters"][gsd] = {
                "path": output_path,
                "band_order": band_list,
                "band_count": len(band_list)
            }
        else:
            logger.error(f"Failed to create multiband raster for {gsd}m resolution")
            logger.error(f"Error: {stderr}")
            results["success"] = False
    
    return results


def create_sentinel2_multiband_by_config(
    acquisition_date: Union[str, datetime],
    orbit_nr: int,
    output_dir: Optional[Union[str, Path]] = None,
    noData_value: Optional[int] = 0
) -> Dict[str, Any]:
    """
    Create multiband rasters for Sentinel-2 data based on band configurations in the config file.
    
    Args:
        acquisition_date: Acquisition date (string YYYYMMDD or datetime)
        orbit_nr: Relative orbit number
        output_dir: Directory for the output rasters (default: base_path/orbit/date)
        noData_value: NoData value for output rasters (default: 0)
        
    Returns:
        Dictionary with processing results
    """
    # Get band configuration from config
    band_config = config.SENTINEL2_BAND_CONFIG
    
    # Create multiband rasters according to config
    results = create_multiband_raster(
        acquisition_date=acquisition_date,
        orbit_nr=orbit_nr,
        gsd_dict=band_config,
        output_dir=output_dir,
        noData_value=noData_value
    )
    
    return results