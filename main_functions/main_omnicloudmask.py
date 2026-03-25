import numpy as np
import sys, os
import rasterio
from rasterio.crs import CRS
from pathlib import Path
from omnicloudmask import predict_from_array
import re
from datetime import datetime
import torch
import subprocess
import platform
from main_functions import main_utils

# Save original sys.argv before importing configuration
original_argv = sys.argv.copy()

# Temporarily clear sys.argv so configuration doesn't try to parse omnicloudmask args
sys.argv = [sys.argv[0]]  # Keep only script name

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config

sys.argv = original_argv


def get_band_resolution(band_name):
    """
    Get the resolution (GSD) for a specific band from config

    Args:
        band_name: Band name (e.g., 'B03', 'B04', 'B08')

    Returns:
        Resolution in meters
    """
    for resolution, bands in config.SENTINEL2_BAND_CONFIG.items():
        if band_name in bands:
            return resolution
    raise ValueError(f"Band {band_name} not found in SENTINEL2_BAND_CONFIG")

def find_band_file(scene_folder, acquisition_date, band_name):
    """
    Find a specific band VRT file in the scene folder

    Args:
        scene_folder: Path to scene folder (e.g., SENTINEL-2/R108/20250423)
        acquisition_date: Acquisition date (e.g., '20250423')
        band_name: Band name (e.g., 'B03', 'B04', 'B08')

    Returns:
        Path to band file or None
    """
    scene_folder = Path(scene_folder)

    # Get resolution from config
    resolution = get_band_resolution(band_name)

    # Get mosaic pattern from config and build full pattern
    # Pattern from config: 'S2-L2A-mosaic_*'
    # Full pattern: S2-L2A-mosaic_{acquisitiondate}T*_{bandname}_{gsd}m.vrt
    mosaic_base = config.AROSICS_CONFIG['singleband_mosaic_pattern'].replace('*', '')
    pattern = f"{mosaic_base}{acquisition_date}T*_{band_name}_{resolution}m.vrt"

    matches = list(scene_folder.glob(pattern))

    if matches:
        return matches[0]
    return None

def generate_cloud_mask_for_scene(orbit_nr, acquisition_date, output_dir, noData_value=0, **kwargs):
    """
    Generate cloud mask for a specific scene from VRT mosaics

    Args:
        orbit_nr: Orbit number (e.g., 'R108' or '108')
        acquisition_date: Acquisition date (e.g., '20250423' or '2025-04-23')
        noData_value: Value within input scenes that specifies no data region. Defaults to 0.
        output_dir: Directory to save cloud masks
        **kwargs: Additional arguments for predict_from_array (e.g., batch_size, inference_dtype)

    Returns:
        Cloud mask array
    """
    # Normalize orbit number (ensure it starts with 'R')
    if not orbit_nr.startswith('R'):
        orbit_nr = f"R{int(orbit_nr):03d}"

    # Normalize date (remove dashes if present)
    acquisition_date = acquisition_date.replace('-', '')

    # Construct scene path
    data_folder = Path(config.PRODUCT_S2_LEVEL_2A["copernicus_collection"])
    scene_folder = data_folder / orbit_nr / acquisition_date

    if not scene_folder.exists():
        raise ValueError(f"Scene folder does not exist: {scene_folder}")

    # OmniCloudMask requires B03, B04, B08
    required_bands = ['B03', 'B04', 'B08']

    # Verify these bands are in the config
    for band in required_bands:
        try:
            get_band_resolution(band)
        except ValueError:
            raise ValueError(f"Required band {band} not found in SENTINEL2_BAND_CONFIG")

    print(f"\n{'='*60}")
    print(f"Orbit: {orbit_nr}, Date: {acquisition_date}")
    print(f"Scene folder: {scene_folder}")
    print(f"{'='*60}")

    # Find band files (VRT mosaics)
    band_files = {}
    for band in required_bands:
        band_file = find_band_file(scene_folder, acquisition_date, band)
        if band_file is None:
            resolution = get_band_resolution(band)
            raise ValueError(f"Could not find {band} band ({resolution}m VRT mosaic) in {scene_folder}")
        band_files[band] = band_file
        print(f"  {band}: {band_file.name}")

    # Extract the date-time for the mosaic filename
    # Try to extract time from the first filename
    match = re.search(r'(\d{4}\d{2}\d{2}T\d{6})', str(band_files['B04']))
    if match:
        time_str = match.group(1)
    else:
        # Use acquisition date with default time if no time found
        date_obj = datetime.strptime(acquisition_date, "%Y%m%d")
        time_str = date_obj.strftime("%Y-%m-%dT000000")

    # Read bands in order: Red, Green, NIR (as required by OmniCloudMask)
    with rasterio.open(band_files['B04']) as src:  # Red
        red = src.read(1)
        profile = src.profile.copy()
        print(f"Image size: {red.shape}")

    with rasterio.open(band_files['B03']) as src:  # Green
        green = src.read(1)

    with rasterio.open(band_files['B08']) as src:  # NIR
        nir = src.read(1)

    # Stack as (3, height, width) - Red, Green, NIR
    input_array = np.stack([red, green, nir])

    # Your modified code
    noData_value = 0  # Your actual noData value

    gpu_available, gpu_status = main_utils.check_gpu_availability()

    #for testing purposes, we can force GPU availability to False to test CPU fallback
    #gpu_available = False
    #gpu_status = "GPU availability forced to False for testing CPU fallback"

    # Check for GPU availability
    if gpu_available:
        print(gpu_status)
        default_kwargs = {
            'batch_size': 1,
            'inference_dtype': 'bf16',
            'mosaic_device': 'cpu',
            'patch_size': 1000,
            'patch_overlap': 300,
            'no_data_value': noData_value,
            'apply_no_data_mask': True
        }
    else:
        #CPU settings only for testing see https://github.com/swisstopo/topo-satromo-v2/issues/22
        print(f"\n{'='*60}")
        print(gpu_status)
        print("NOT FOR OPERATIONAL USE, JUST FOR TESTING PURPOSES!")
        print(f"\n{'='*60}")
        default_kwargs = {
            'batch_size': 1,  # Reduced to 1 for large images
            'inference_device': 'cpu',
            'inference_dtype': 'fp32',  # Use bfloat16 for memory efficiency
            'mosaic_device': 'cpu',  # Offload patch mosaicking to CPU to save GPU memory
            'patch_size': 512,  #  patch size
            'patch_overlap': 64,  #  overlap
            'no_data_value': noData_value,
            'apply_no_data_mask': True
        }

    default_kwargs.update(kwargs)

    # Generate cloud mask
    print("Generating cloud mask (this may take a while for large mosaics)...")
    print(f"Settings: noData={noData_value}, batch_size={default_kwargs['batch_size']}, mosaic_device={default_kwargs['mosaic_device']}")
    pred_mask = predict_from_array(input_array, **default_kwargs)

    # Squeeze to remove extra dimensions (from (1, 1, H, W) to (H, W))
    pred_mask = pred_mask.squeeze()

    # Write OmicloudMask method info to Metadata
    dt = datetime.strptime(time_str, '%Y%m%dT%H%M%S')
    formatted_time = dt.strftime('%Y-%m-%dt%H%M%S')
    main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{formatted_time}_metadata.json","PROPERTIES","OMNICLOUD_METHOD",gpu_status)

    # Create output directory structure
    output_dir = Path(output_dir)
    output_scene_dir = output_dir / orbit_nr / acquisition_date
    output_scene_dir.mkdir(parents=True, exist_ok=True)

    # Output filename
    output_path = output_scene_dir / f"{config.AROSICS_CONFIG['singleband_mosaic_pattern'].replace('*', '')}{time_str}_omnicloud.tif"

    # Update profile for output with explicit EPSG:32632
    profile.update(
        driver='GTiff',
        nodata=None,
        dtype=rasterio.uint8,
        count=1,
        compress='lzw',
        crs=CRS.from_epsg(32632)  # UTM Zone 32N
    )

    # Save result
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(pred_mask.astype(np.uint8), 1)

    print(f"Cloud mask saved to: {output_path}")
    print(f"CRS: EPSG:32632 (UTM Zone 32N)")
    print("Classes: 0=Clear, 1=Thick Cloud, 2=Thin Cloud, 3=Cloud Shadow")

    return pred_mask


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--orbit', '-o', required=True)
    parser.add_argument('--date', '-d', type=str, required=True)
    parser.add_argument('--output-dir', default='cloud_masks')

    args = parser.parse_args()

    generate_cloud_mask_for_scene(
        orbit_nr=args.orbit,
        acquisition_date=args.date,
        output_dir=args.output_dir,
    )