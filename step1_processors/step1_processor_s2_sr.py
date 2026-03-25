<<<<<<< HEAD
import multiprocessing
import torch
import subprocess
import numpy as np
from datetime import datetime
import configuration as config
from main_functions import main_utils, main_publish_stac_fsdi, main_coregistration, main_reprojection, main_mosaicing,main_thumbnails,main_create_rgb,main_cloudpercentage,main_omnicloudmask
from collections import defaultdict
import requests
import os
import json
import time
from pathlib import Path
import glob
import shutil
import re
import rasterio
import glob
import socket
import geopandas as gpd
from importlib.metadata import version


=======
import numpy as np
from datetime import datetime, timedelta
from main_functions import main_utils
>>>>>>> main
from step0_processors.step0_utils import write_asset_as_empty

# Processing pipeline for daily Sentinel-2 L2A surface reflectance (sr) mosaics over Switzerland

##############################
# INTRODUCTION
# This script provides a tool to preprocess Sentinel-2 L2A surface reflectance (sr) data over Switzerland.
<<<<<<< HEAD
# It performs automated downloads from Copernicus Data Space, organizes files into orbit groups,
# integrates CloudScore+ data, and prepares data for further processing.
=======
# It can mask clouds and cloud shadows, detect terrain shadows, mosaic images from the same image swath,
# co-register images to the Sentinel-2 Global Reference Image, and export the results.
>>>>>>> main
#

##############################
# CONTENT
# The switches enable / disable the execution of individual steps in this script

# This script includes the following steps:
<<<<<<< HEAD
# 1. Search for available Sentinel-2 L2A scenes via STAC API
# 2. Download matching scenes from Copernicus Data Space
# 3. Optional backup to S3
# 4. Organize files by orbit and date
# 5. Integrate corresponding CloudScore+ data
# 6. Generate and update metadata files
# 7. [TODO] Terrain shadow masking
# 8. Co-registration with AROSICS
# 9. STAC catalog generation
#
# The script processes one mosaic image per day with automated quality checks and error handling.


def process_product_s2_sr(day_to_process: str, collection: str) -> None:

=======
# 1. Download Data
# 2. Masking clouds and cloud shadows
# 2. Detecting terrain shadows
# 3. Mosaicing of images from the same day (=same orbital track) over Switzerland
# 4. Registering the S2 Mosaic to the Sentinel-2 global reference image
# 5. Exporting spectral bands, additional layers and relevant properties
#
# The script is set up to export one mosaic image per day.


def process_product_s2_sr(day_to_process: str, collection: str) -> None:
>>>>>>> main
    ##############################
    # SWITCHES
    # The switches enable / disable the execution of individual steps in this script

<<<<<<< HEAD
    # options': True, False - defines if we store the original data to S3 as backup
    s3_backup = False # backup copernicus tiles data to S3
    gpu_check = True# Check if a GPU system is available for processing, if not write to empty asset list and skip processing
=======
    # options': True, False - defines if individual clouds and cloud shadows are masked
    cloudMasking = True
    # options: True, False - defines if the CloudScore+ dataset should be used (if False': s2cloudless)
    cloudScorePlus = True
    # options: True, False - defines if a cast shadow mask is applied
    terrainShadowDetection = False
    # options: True, False - defines if a cast shadow mask is applied from the precalculated mask
    terrainShadowDetectionPrecalculated = True
    # options': True, False - defines if individual scenes get mosaiced to an image swath
    swathMosaic = True
    # options': True, False - defines if the coregistration is applied
    coRegistration = True
    # options': True, False - defines if the coregistration is applied
    coRegistrationPrecalculated = False

    # Export switches
    # options': True, 'False - defines if 10-m-bands are exported': 'B2','B3','B4','B8'
    export10mBands = True
    # options': True, 'False - defines if 20-m-bands are exported':  select from 'B5','B6','B7','B8A','B11','B12'below
    export20mBands = True
    # options': True, 'False - defines if 60-m-bands are exported': 'B1','B9','B10'
    # export60mBands = False  # NOTEJS: ununsed, export function commented in the script below
    # options': True, 'False - defines if registration layers are exported': 'reg_dx','reg_dy', 'reg_confidence'
    exportRegLayers = True
    # options': True, 'False - defines if masks are exported': 'terrainShadowMask','cloudAndCloudShadowMask'
    exportMasks = True
    # options': True, 'False - defines if S2 cloud probability layer is exported': 'cloudProbability'
    exportS2cloud = True
>>>>>>> main

    ##############################
    # TIME
    # define a date or use the current date:

<<<<<<< HEAD
    # start_date = datetime.strptime(day_to_process, '%Y-%m-%d')
    # end_date = start_date + timedelta(days=1)
=======
    start_date = datetime.strptime(day_to_process, '%Y-%m-%d')
    end_date = start_date + timedelta(days=1)
>>>>>>> main

    ##############################
    # SPACE
    # Official swisstopo boundaries
    # source: https:#www.swisstopo.admin.ch/de/geodata/landscape/boundaries3d.html#download
<<<<<<< HEAD
    # Simplified version for faster processing
    aoi_CH_simplified = os.path.join("assets", "swissboundary_simplified_4326.json")
=======
    # processing: reprojected in QGIS to epsg32632
    aoi_CH = ee.FeatureCollection(
        "projects/satromo-prod/assets/res/swissBOUNDARIES3D_1_5_TLM_LANDESGEBIET_dissolve_epsg32632").geometry()
    aoi_CH_simplified = ee.FeatureCollection(
        "projects/satromo-prod/assets/res/CH_boundaries_buffer_5000m_epsg32632").geometry()
>>>>>>> main

    ##############################
    # REFERENCE DATA

<<<<<<< HEAD
    # # TERRAIN SHADOW - based on a very precise digital surface  model in a 10 m resolution
    # # source: LIDAR, Provided by GANDOR
    # # processing: TODO
    # terrain_shadow_collection = TODO
=======
    # # Sentinel-2 Global Reference Image (contains the red spectral band in 10 m resolution))
    # # source: https:#s2gri.csgroup.space
    # # processing: GDAL merge and warp (reproject) to epsg32632
    # S2_gri = ee.Image("projects/satromo-prod/assets/res/S2_GRI_CH_epsg32632")

    # # swissSURFACE3D- very precise digital Surface model in a 10 m resolution
    # # source: https://www.swisstopo.admin.ch/de/hoehenmodell-swisssurface3d (inside CH) and the area at "Meiringen" and outside CH was filled with https://www.swisstopo.admin.ch/de/geodata/height/alti3d.html#download
    # # source: https://www.swisstopo.admin.ch/de/hoehenmodell-swissaltiregio
    # # processing: by F. Gandor in FME
    # DEM_sa3d = ee.Image(
    #     "projects/satromo-prod/assets/res/SS3DR_SA3DRegio_10m_20kmBuffer_epsg32632")

    # # SRTM 30 - digital elevation model (slope and aspect) used for the atmospheric correction in sen2cor in a 30 m resolution
    # # source: https://developers.google.com/earth-engine/datasets/catalog/USGS_SRTMGL1_003
    # # processing: ee.Terrain.slope(DEM) and ee.Terrain.aspect(DEM) converted to radians
    # slope = ee.Image('projects/satromo-prod/assets/res/SRTM30m_slope_radians_epsg32632')
    # aspect = ee.Image('projects/satromo-prod/assets/res/SRTM30m_aspect_radians_epsg32632')

    # # Terrain - very precise digital surface  model in a 10 m resolution
    # # source: https://code.earthengine.google.com/ccfa64fe9827c93e2986e693983332e2
    # # processing: The shadow masks are  combined into a single image with multiple bands as asset per DOY.
    # terrain_shadow_collection = "projects/satromo-prod/assets/col/TERRAINSHADOW_SWISS/"

    # # DX DY - Precalculated DX DY shifts
    # # source: https://github.com/SARcycle/AROSICS/
    # # processing: The DX DY are  combined into a single image with multiple bands as asset per DATE.
    # dxdy_collection = "projects/satromo-432405/assets/COL_S2_SR_DXDY"
>>>>>>> main

    ##############################
    # SATELLITE DATA

<<<<<<< HEAD
    # # Local Copernicus STAC Collection
    copernicus_collection = config.PRODUCT_S2_LEVEL_2A["copernicus_collection"]# Local Copernicus STAC Collection
    # # Copernicus Baseline Version greater than
    baseline_version = "04.00"  # Baseline Version greater than !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    # # Copernicus Processing Level
    processing_level = "L2A"
    # # Copernicus Bucket
    copernicus_bucket = "eodata"

    # # Coregistration results
    s3_coreg_path = f"data/SENTINEL-2/COREGISTRATION/"

    ##############################
    #IMAGE SEARCH

    def copernicus_image_search(date, copernicus_collection, aoi, processing_level, baseline_version):
        """
        Searches for Sentinel-2 satellite images from a STAC API based on the specified date, collection, area of interest (AOI),
        processing level, and baseline version.
        Args:
            date (str): The date for which to search images, in 'YYYY-MM-DD' format.
            collection (str): The STAC collection name to filter images (e.g., 'sentinel-2-l2a').
            aoi (str): Path to a GeoJSON file defining the area of interest.
            processing_level (str): The processing level to filter images (e.g., 'LEVEL2A').
            baseline_version (str): Minimum processor version; only images with a higher version are returned.
        Returns:
            list: A list of STAC items (dicts) matching the search criteria, filtered by processing level, baseline version,
                and deduplicated to keep only the newest satellite per (date, orbit) group while preserving all tiles.
        Raises:
            requests.exceptions.HTTPError: If the STAC API request fails.
            Exception: For other errors such as file reading or JSON parsing issues.
        """
        # STAC Access point
        search_url = "https://stac.dataspace.copernicus.eu/v1/search"
        #search_url = "https://catalogue.dataspace.copernicus.eu/stac/search" #old endpoint dead on 17.11.2025

        with open(aoi, 'r') as f:
            geojson_data = json.load(f)
        geometry = geojson_data['geometries'][0]

        # Build the query body for SENTINEL2 filter for switzerland and LEVEL2A
        query_body = {
            "collections": [copernicus_collection],
            "intersects": geometry,
            "datetime": f"{date}T00:00:00Z/{date}T23:59:59Z",
            "limit": 100
        }

        try:
            response = requests.post(search_url, json=query_body)
            response.raise_for_status()

            result = response.json()
            items = result.get('features', [])
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error: {e}")
            print(f"Response status: {response.status_code}")
            print(f"Response text: {response.text}")
            raise
        except Exception as e:
            print(f"Error: {e}")
            raise

        # Filter for processing level / baseline version
        search_result = [
            item for item in items
            if item['properties'].get('processing:version', '00.00') > baseline_version
        ]

        # --- Deduplicate: keep all tiles but only from the newest satellite per (date, orbit) ---
        # First pass: determine the winning satellite per (date, orbit)
        best_sat_per_orbit = {}
        for item in search_result:
            props    = item['properties']
            date_str = props['datetime'][:10]       # '2026-02-07T...' -> '2026-02-07'
            orbit    = props['sat:relative_orbit']
            sat      = item['id'].split('_')[0]     # 'S2A_MSIL2A_...' -> 'S2A'

            group = (date_str, orbit)
            if group not in best_sat_per_orbit or sat > best_sat_per_orbit[group]:
                best_sat_per_orbit[group] = sat

        # Second pass: keep all tiles from the winning satellite, drop the rest
        culled_per_orbit = defaultdict(set)
        winners = []
        for item in search_result:
            props    = item['properties']
            date_str = props['datetime'][:10]
            orbit    = props['sat:relative_orbit']
            sat      = item['id'].split('_')[0]
            group    = (date_str, orbit)

            if sat == best_sat_per_orbit[group]:
                winners.append(item)
            else:
                culled_per_orbit[group].add(sat)

        if culled_per_orbit:
            summary = '; '.join(
                f"{best_sat_per_orbit[g]} kept over {', '.join(sorted(sats))} (orbit {g[1]})"
                for g, sats in culled_per_orbit.items()
            )
            n_culled = sum(len(sats) for sats in culled_per_orbit.values())
            print(f'\t\t- {n_culled} sensor(s) culled due to multiple sensors '
                f'for same date and orbit ({summary})')

        search_result = winners

        return search_result

    # Perform the scene search
    search_result = copernicus_image_search(date=day_to_process, copernicus_collection =copernicus_collection,  aoi=aoi_CH_simplified, processing_level=processing_level, baseline_version=baseline_version)

    # Check if we have data at all
    if len(search_result) == 0:
        write_asset_as_empty(collection, day_to_process, 'No candidate scene')
        return

    # TODO check if already in  stac,  check if online is a new processor / baseline

    ##############################
    # TILE Completness check

    # in the List Search_result we check if we have all tiles for each orbit, if realiveOrbitnUmber is  8 ist ahs to be < 4 unqieue tileID, if realiveOrbitnUmber is  108 ist ahs to be < 11 unqieue tileID
    orbit_to_tiles = defaultdict(set)
    for item in search_result:
        orbit_num = item['properties']['sat:relative_orbit']
        grid_code = item['properties']['grid:code']  # 'MGRS-32TLT'
        tile_id = grid_code.split('-')[1]
        orbit_to_tiles[orbit_num].add(tile_id)
    # Define expected tile counts for specific orbits
    expected_tile_counts = {8: 4, 108: 11, 65: 11, 22: 4}  # Add more orbits and their expected counts as needed
    # Filter orbits based on expected tile counts
    valid_orbits = {orbit for orbit, tiles in orbit_to_tiles.items()
                    if orbit not in expected_tile_counts or len(tiles) >= expected_tile_counts[orbit]}
    # Filter non orbits based on expected tile counts
    non_valid_orbits = {orbit for orbit, tiles in orbit_to_tiles.items()
                if orbit in expected_tile_counts and len(tiles) < expected_tile_counts[orbit]}
    # Filter search_result to include only items from valid orbits
    search_result = [item for item in search_result if item['properties']['sat:relative_orbit'] in valid_orbits]

    # If no valid orbits remain, write an empty asset and return
    if len(search_result) == 0:
        write_asset_as_empty(collection, day_to_process, 'Tile upload incomplete')
        return
    # If we have at least one valid orbit remain, write an empty asset entry
    if len(non_valid_orbits) > 0:
        write_asset_as_empty(collection, day_to_process, f'Tile upload incomplete: {sorted(non_valid_orbits)}')
        # continue processing the valid orbits

    ##############################
    # SYSTEM CHECK
    # Check if we have a system with GPU available for processing. If not we write to empty asset list that data is ready but we can not process it with the current system. This information will then be processed by the next run of the processing pipeline: A) read the empty asset list B) check if data is ready but not processed , remove it from the empty asset list C) process the data on a system with GPU
    if gpu_check is True:

        gpu_available, gpu_status = main_utils.check_gpu_availability()

        if gpu_available is not True:
            print(gpu_status)
            write_asset_as_empty(collection, day_to_process, 'Tiles ready awaiting GPU system run')
            return


    ##############################
    # IMAGE DOWNLOAD

    # Download the data from copernicus

    def copernicus_download(bucket, search_result: list, target: str = "") -> list:
        """
        Downloads files from an S3 bucket based on STAC search results from the new Copernicus endpoint.

        Args:
            bucket: boto3 Resource bucket object representing the S3 bucket.
            search_result (list): List of search result dictionaries containing asset information (STAC Items).
            target (str, optional): Local directory to store downloaded files. Defaults to the current directory.

        Returns:
            list: Download statistics as [success_count, failure_count].

        Raises:
            FileNotFoundError: If no files are found for a given product prefix.
        """

        # Initialize download statistics
        dl_stats = [0, 0]  # 0: success, 1: failed

        # Define which file we want to download based on the Band configs
        # NOTE: This line assumes 'config' is properly imported and defined.
        # target_endings = [f'{band}_{res}m.jp2' for res, bands in config.SENTINEL2_BAND_CONFIG.items() for band in bands]

        # Using a placeholder for target_endings if config isn't available:
        target_endings = ['.jp2']

        # Create the target dir
        os.makedirs(target, exist_ok=True)

        print(f"Downloading {len(search_result)} tiles from {bucket.name}...")

        # Loop over the search results
        for i, item in enumerate(search_result):

            # --- START OF MODIFIED SECTION: Derive the S3 Product Prefix ---

            # 1. Use a reliable asset (e.g., 'AOT_10m') to get the S3 HREF.
            try:
                sample_href = item['assets']['AOT_10m']['href']
            except KeyError:
                print(f"Skipping item {item['id']}: Missing expected 'AOT_10m' asset key.")
                dl_stats[1] += 1
                continue

            # 2. Use regex to extract the S3 object key prefix (everything after the bucket name, up to .SAFE/)
            # Example HREF: s3://bucket-name/eodata/.../PRODUCT.SAFE/GRANULE/...
            # We need the prefix: eodata/.../PRODUCT.SAFE/
            match = re.search(r's3:\/\/[^\/]+\/(.*\.SAFE\/)', sample_href)

            if match:
                # 'product' is the S3 object key prefix (e.g., 'eodata/Sentinel-2/.../PRODUCT.SAFE/')
                product = match.group(1)
            else:
                print(f"Skipping item {item['id']}: Could not find .SAFE directory pattern in asset HREF.")
                dl_stats[1] += 1
                continue

            # --- END OF MODIFIED SECTION ---

            # Use the extracted S3 object key prefix to filter objects
            files = bucket.objects.filter(Prefix=product)

            if not list(files):
                raise FileNotFoundError(f"Could not find any files for S3 Prefix: {product}. Check bucket contents.")

            # The rest of the download and retry logic is unchanged:
            for file in files:
                if os.path.isdir(file.key):
                    continue

                # Filter for only files with target endings
                if not any(file.key.endswith(ending) for ending in target_endings):
                    continue

                # Retry logic for each file
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        target_path = os.path.join(target, file.key)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        bucket.download_file(file.key, target_path)
                        #print(f"Downloaded: {file.key}")
                        dl_stats[0] += 1
                        break  # Success, exit retry loop

                    except Exception as e:
                        print(f"Attempt {attempt + 1} failed for {file.key}: {str(e)}")

                        if attempt < max_retries - 1:  # Don't wait after last attempt
                            print(f"Retrying in 30 seconds...")
                            time.sleep(30)
                        else:
                            print(f"Failed to download {file.key} after {max_retries} attempts")
                            dl_stats[1] += 1

        # Print download statistics
        return dl_stats


    dl_stats=copernicus_download(main_utils.copernicus_s3.Bucket(copernicus_bucket), search_result=search_result, target="temp")


    # Check if we have a failed download
    if dl_stats[1] != 0:
        write_asset_as_empty(collection, day_to_process, 'Tile download incomplete')
        return


    # TODO check if atile is mostly no data : meaning that a granule is missing and we have to wait until a second or athird granule is here.

    ##############################
    # Backup data to S3


    def upload_directory_with_progress(local_directory, bucket_name, s3_prefix=""):
        """
        Upload directory with progress tracking and better error handling using main_utils.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        uploaded_files = []
        failed_files = []
        local_path = Path(local_directory)

        if not local_path.exists() or not local_path.is_dir():
            raise ValueError(f"Invalid directory: {local_directory}")

        # Get all files to upload
        all_files = [f for f in local_path.rglob('*') if f.is_file()]
        total_files = len(all_files)

        print(f"Found {total_files} files to upload...")

        def upload_single_file(file_path):
            relative_path = file_path.relative_to(local_path)
            s3_key = str(relative_path).replace(os.sep, '/')
            if s3_prefix:
                s3_key = f"{s3_prefix.rstrip('/')}/{s3_key}"

            try:
                main_utils.s3.upload_file(str(file_path), bucket_name, s3_key)
                return {"success": True, "file": s3_key, "local_path": str(file_path)}
            except Exception as e:
                return {"success": False, "file": s3_key, "local_path": str(file_path), "error": str(e)}

        # Upload files concurrently
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_file = {executor.submit(upload_single_file, f): f for f in all_files}

            completed = 0
            for future in as_completed(future_to_file):
                result = future.result()
                completed += 1

                if result["success"]:
                    uploaded_files.append(result["file"])
                    # print(f"[{completed}/{total_files}] ✓ {result['file']}")
                else:
                    failed_files.append(result)
                    print(f"[{completed}/{total_files}] ✗ {result['file']} - {result['error']}")

        print(f"\nUpload complete! {len(uploaded_files)} successful, {len(failed_files)} failed")

        if failed_files:
            print("\nFailed uploads:")
            for failed in failed_files:
                print(f"  {failed['file']}: {failed['error']}")

        return {"uploaded": uploaded_files, "failed": failed_files}

    if s3_backup is True:
        ul_stats = upload_directory_with_progress(local_directory="temp", bucket_name=config.S3_BUCKET_NAME, s3_prefix=f"{config.S3_BUCKET_PATH}/")

        if ul_stats['failed']:
            write_asset_as_empty(collection, day_to_process, 'S3 upload incomplete')
            return

    ##############################
    # Move data to Sentinel-2/ORBIT(R000)/JJJJMMDD

    def parse_copernicus_folder_name(folder_name):
        """
        Parse Copernicus folder name to extract orbit and date information.
        Returns tuple (orbit, date) or (None, None) if parsing fails.
        """
        # Remove .SAFE extension if present
        name = folder_name.replace('.SAFE', '')

        # Pattern: MMM_MSIXXX_YYYYMMDDHHMMSS_Nxxyy_ROOO_Txxxxx_<Product Discriminator>
        pattern = r'(\w+)_(\w+)_(\d{8})T\d{6}_N\d{4}_R(\d{3})_T\w{5}_(.+)'
        match = re.match(pattern, name)

        if match:
            date = match.group(3)  # YYYYMMDD
            orbit = f"R{match.group(4)}"  # ROOO format
            return orbit, date
        else:
            print(f"Warning: Could not parse folder name: {folder_name}")
            return None, None

    def merge_jp2_with_gdal_merge(existing_file, new_file, output_file):
        """Merge two JP2 files using gdal_merge with NoData=0 handling."""
        # Two Step appproach 1. Driver Limitations (The "Random Access" Problem) and 2. Performance and CPU Usage
        try:
            print(f"    Merging duplicate files: {os.path.basename(existing_file)}")

            temp_tif = output_file.replace('.jp2', '_merged.tif')

            command = [
                "gdal_merge",
                "-o", temp_tif,
                "-n", "0",
                "-a_nodata", "0",
                "-init", "0",
                existing_file,
                new_file
            ]

            result = subprocess.run(command, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                print(f"    Error in gdal_merge: {result.stderr}")
                if os.path.exists(temp_tif):
                    os.remove(temp_tif)
                return False

            translate_cmd = [
                'gdal_translate',
                '-of', 'JP2OpenJPEG',
                '-co', 'QUALITY=100',
                '-a_nodata', '0',
                temp_tif,
                output_file
            ]

            result = subprocess.run(translate_cmd, capture_output=True, text=True, timeout=300)

            if os.path.exists(temp_tif):
                os.remove(temp_tif)

            if result.returncode != 0:
                print(f"    Error converting to JP2: {result.stderr}")
                return False

            print(f"    Successfully merged files with NoData handling")
            return True

        except Exception as e:
            print(f"    Error in merge process: {e}")
            return False


    def move_copernicus_data(temp_folder, collection_folder):
        """
        Process Copernicus data folders and copy relevant JP2 files.

        Args:
            temp_folder (str): Path to the temp folder containing Copernicus folders
            collection_folder (str): Base path for the collection output folder
        """

        # Define the file endings we're looking for based on the config
        target_endings = [f'{band}_{res}m.jp2'
                        for res, bands in config.SENTINEL2_BAND_CONFIG.items()
                        for band in bands]
        # Find all subdirectories in temp folder that match Sentinel-2 naming pattern
        pattern = f"{temp_folder}/**/*.SAFE"
        sentinel_folders = glob.glob(pattern, recursive=True)

        if not sentinel_folders:
            print(f"No Sentinel-2 folders found in {temp_folder}")
            return 1

        print(f"Found {len(sentinel_folders)} Sentinel-2 folders")

        for folder_path in sentinel_folders:
            if not os.path.isdir(folder_path):
                continue

            folder_name = os.path.basename(folder_path)
            print(f"\nProcessing folder: {folder_name}")

             # Parse folder name to extract orbit and date
            orbit, date = parse_copernicus_folder_name(folder_name)
            if orbit is None or date is None:
                print(f"Skipping folder {folder_name} - could not parse name")
                continue

            print(f"  Orbit: {orbit}, Date: {date}")
             # Create output directory
            output_dir = os.path.join(collection_folder, orbit, date)
            os.makedirs(output_dir, exist_ok=True)
            print(f"  Output directory: {output_dir}")

            # Find all T*.jp2 files in the folder (including subdirectories)
            jp2_pattern = os.path.join(folder_path, "**", "T*.jp2")
            jp2_files = glob.glob(jp2_pattern, recursive=True)

            copied_count = 0
            merged_count = 0
            skipped_count = 0

            for jp2_file in jp2_files:
                file_name = os.path.basename(jp2_file)
                # Check if file ends with any of our target endings
                for ending in target_endings:
                    if file_name.endswith(ending):
                        try:
                            destination = os.path.join(output_dir, file_name)

                            if os.path.exists(destination):
                                print(f"    Granule duplicated detected: {file_name}")
                                temp_merged = destination + '.merged_temp.jp2'

                                if merge_jp2_with_gdal_merge(destination, jp2_file, temp_merged):
                                    backup_file = destination + '.backup'
                                    shutil.move(destination, backup_file)
                                    shutil.move(temp_merged, destination)
                                    os.remove(backup_file)
                                    merged_count += 1
                                else:
                                    print(f"    Merge failed, keeping original file")
                                    if os.path.exists(temp_merged):
                                        os.remove(temp_merged)
                                    skipped_count += 1
                            else:
                                shutil.move(jp2_file, destination)
                                copied_count += 1

                            break # Found a match, no need to check other endings

                        except Exception as e:
                            print(f"    Error processing {file_name}: {e}")
                            return 0

            print(f"  Files copied: {copied_count}, merged: {merged_count}, skipped: {skipped_count}")

        return 1

    move_stats= move_copernicus_data("temp", copernicus_collection)

    # Check if we have a failed move
    if move_stats == 0:
        write_asset_as_empty(collection, day_to_process, 'Data download incomplete')
        return

    # Delete temporary folder and all its contents in an OS-agnostic way
    shutil.rmtree("temp")

    ##############################
    # Generate metadata file and store it local

    def export_orbits_to_json_files(grouped_results, earliest_datetimes, output_dir="./"):
        """
        Export each orbit group to a separate JSON file with naming format:
        swisseo_s2-sr_v200_mosaic_{earliest_timestamp}_metadata.json

        Args:
            grouped_results: Dictionary from group_search_results_by_orbit function
            earliest_datetimes: Dictionary from get_earliest_datetime_per_orbit function
            output_dir: Directory to save the files (default: current directory)

        Returns:
            list: List of created file paths
        """
        created_files = []

        # Ensure output directory exists
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        for orbit_num, orbit_data in grouped_results.items():
            # Get the earliest timestamp for this orbit
            earliest_timestamp = earliest_datetimes.get(orbit_num)

            if earliest_timestamp:
                # Create filename with format: swisseo_s2-sr_v100_mosaic_{timestamp}_metadata.json
                filename = f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{earliest_timestamp}_metadata.json"
                file_path = os.path.join(output_dir, filename)

                # Write the orbit data to JSON file
                with open(file_path, 'w') as json_file:
                    json.dump(orbit_data, json_file, indent=2)

                created_files.append(file_path)
                print(f"Created: {filename}")
            else:
                print(f"Warning: No timestamp found for orbit {orbit_num}, skipping...")

        return created_files

    def get_earliest_datetime_per_orbit(grouped_results):
        """
        Find the earliest datetime for each orbit group by comparing all datetime entries.
        Args:
            grouped_results: Dictionary from group_search_results_by_orbit function
        Returns:
            dict: Dictionary with orbit numbers as keys and earliest datetime in YYYY-MM-DDthhmmss format
        """
        earliest_per_orbit = {}
        for orbit_num, orbit_data in grouped_results.items():
            earliest_datetime = None
            # Check all granules in this orbit
            granules = orbit_data["SOURCE"]["GRANULES"]
            for granule_id, properties in granules.items():
                if 'datetime' in properties:
                    current_datetime = properties['datetime']
                    if earliest_datetime is None or current_datetime < earliest_datetime:
                        earliest_datetime = current_datetime

            # Convert to desired format YYYY-MM-DDthhmmss
            if earliest_datetime:
                # Parse the ISO datetime string
                dt = datetime.fromisoformat(earliest_datetime.replace('Z', '+00:00'))
                # Format as YYYY-MM-DDthhmmss
                formatted_datetime = dt.strftime('%Y-%m-%dt%H%M%S')
                earliest_per_orbit[orbit_num] = formatted_datetime
            else:
                earliest_per_orbit[orbit_num] = None

        return earliest_per_orbit

    def group_search_results_by_orbit(search_result):
        """
        Groups search results by relativeOrbitNumber and creates JSON structure
        with SOURCE information.

        Args:
            search_result: List of dictionaries with 'id' and 'properties' keys

        Returns:
            dict: Dictionary with orbit numbers as keys, containing grouped data
        """

        # Group results by orbit number
        orbit_groups = defaultdict(list)

        for item in search_result:
            orbit_num = item['properties']['sat:relative_orbit']
            orbit_groups[orbit_num].append(item)

        # Create the final JSON structure
        result = {}

        for orbit_num, items in orbit_groups.items():
            # Initialize the structure for this orbit
            orbit_data = {
                "SOURCE": {
                    "scene_count": len(items),
                    "GRANULES": {}
                }
            }

            # Add each item to GRANULES using its ID as the key
            for item in items:
                granule_id = item['id']
                orbit_data["SOURCE"]["GRANULES"][granule_id] = item['properties']

            result[str(orbit_num)] = orbit_data

        return result
    # Group the results
    grouped_results = group_search_results_by_orbit(search_result)

    # Get timestamp for each orbit
    orbit_timestamp = get_earliest_datetime_per_orbit(grouped_results)

    # Export to individual JSON files
    created_files = export_orbits_to_json_files(grouped_results, orbit_timestamp)


    ##############################
    # TODO TERRAINSHADOWMASK


    ##############################
    # COREGISTRATION AROSICS
    acquisition_date = main_utils.parse_date(day_to_process).strftime('%Y%m%d')
    orbit_nrs = [int(orbit) for orbit in grouped_results.keys()]

    for i in range(len(orbit_nrs)):

        orbit_nr = orbit_nrs[i]

        noData_value = main_reprojection.reproject_tiles_to_UTM32N(acquisition_date=acquisition_date, orbit_nr=orbit_nr)
        main_mosaicing.create_sentinel2_multiband_by_config(
            acquisition_date=acquisition_date,
            orbit_nr=orbit_nr,
            noData_value=noData_value
        )

        # # Creating cloud mask with omnicloudmask
        result=main_omnicloudmask.generate_cloud_mask_for_scene(orbit_nr=str(orbit_nr),acquisition_date=acquisition_date,output_dir=config.PRODUCT_S2_LEVEL_2A["copernicus_collection"])


        main_mosaicing.equalize_all_extents(acquisition_date=acquisition_date, orbit_nr=orbit_nr)
        success, pickle_path = main_coregistration.coregister_S2(acquisition_date=acquisition_date, orbit_nr=orbit_nr)

        # If coregistration was successful, proceed to deshift the files
        if success:
            main_coregistration.deshift_files(
                acquisition_date=acquisition_date,
                orbit_nr=orbit_nr,
                pickle_path=pickle_path,
                fmt_out='GTIFF',
                CPUs=os.cpu_count() #use all cpus
            )
        # Else, log the failure and continue to the next day
        else:
            write_asset_as_empty(collection, day_to_process, f'cloudy')
            pattern = f"*{day_to_process}*.*"
            # Clean up Files
            for file in Path(".").glob(pattern):
                print(f"Cleaning up: {file}")
                file.unlink()
            # Clean up Download folder
            if Path(copernicus_collection).exists():
                print(f"Cleaning up: {copernicus_collection}")
                shutil.rmtree(copernicus_collection)
            return

    ##############################
    # Clean up Download folder
    if Path(copernicus_collection).exists():
        print(f"Cleaning up: {copernicus_collection}")
        shutil.rmtree(copernicus_collection)

    ##############################
    # Loop over all orbits and process final steps

    def get_raster_properties(input_file):
        """
        Extract resolution, datatype, and nodata value from a raster file.

        Parameters:
        -----------
        input_file : str or Path
            Path to the input raster file

        Returns:
        --------
        dict : Dictionary containing:
            - 'resolution': int or None (maximum of x/y resolution in map units)
            - 'datatype': str or None (GDAL datatype string like 'Byte', 'Float32')
            - 'nodata': float/int or None (nodata value)
            - 'res_x': float or None (x resolution)
            - 'res_y': float or None (y resolution)
            - 'statistics': list of dict or None (band numbers)
        """
        try:
            with rasterio.open(input_file) as src:
                # Get metadata
                meta = src.meta

                # Get pixel size (resolution) - using absolute values
                original_res_x = abs(src.transform[0])
                original_res_y = abs(src.transform[4])
                resolution = int(max(original_res_x, original_res_y)) if original_res_x and original_res_y else None

                # Map rasterio dtype to GDAL dtype string
                dtype_map = {
                    'uint8': 'Byte',
                    'uint16': 'UInt16',
                    'int16': 'Int16',
                    'uint32': 'UInt32',
                    'int32': 'Int32',
                    'float32': 'Float32',
                    'float64': 'Float64'
                }

                # Get datatype from metadata
                rasterio_dtype = str(meta['dtype']) if 'dtype' in meta else None
                datatype = dtype_map.get(rasterio_dtype, None) if rasterio_dtype else None

                # Get nodata value
                nodata_value = meta.get('nodata', None)
                # Convert nodata to int for integer data types
                if nodata_value is not None and rasterio_dtype in ['uint8', 'uint16', 'int16', 'uint32', 'int32']:
                    nodata_value = int(nodata_value)

                # Get band information
                band_stats = []
                for band in range(1, meta['count'] + 1):
                    band_stats.append({'band': band})

                return {
                    'resolution': resolution,
                    'datatype': datatype,
                    'nodata': nodata_value,
                    'res_x': original_res_x,
                    'res_y': original_res_y,
                    'statistics': band_stats
                }

        except Exception as e:
            print(f"Error reading raster properties: {e}")
            return {
                'resolution': None,
                'datatype': None,
                'nodata': None,
                'res_x': None,
                'res_y': None,
                'statistics': None
            }

    for orbit_num, timestamp in orbit_timestamp.items():
        print(f"Processing orbit {orbit_num} of {timestamp} ...")


        ##############################
        # Calculate Cloud Percentage:

        # Wrap the string in Path() first
        buffer_path = Path(config.BUFFER)
        # Construct new filename with orbit number
        orbit_clipfile = buffer_path.with_name(f"{buffer_path.stem}_{orbit_num}{buffer_path.suffix}")
        cloudcover = main_cloudpercentage.cloudpercentage(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_cloudmask_10m.tif",orbit_clipfile)
        print(f"Cloud percentage for orbit {orbit_num} at {timestamp}: {cloudcover:.2f}%")

        # Check if we dont have to much cloudy data: if orbit_num is 8 or 22 and cloudcover >85%  or orbit_num is 108 or 65 and cloudcover >95% we write to empty asset and stop processing .
        if (orbit_num in [8,22] and cloudcover >85.0) or (orbit_num in [108,65] and cloudcover >95.0):
            print(f"Orbit {orbit_num} at {timestamp} is too cloudy ({cloudcover:.2f}%), skipping further processing.")
            write_asset_as_empty(collection, day_to_process, 'cloudy')
            return

        #METADATA add cloudcover
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","CLOUDPERCENTAGE",f"{cloudcover:.2f}")

        #METADATA add GCP
        coreg_info=main_coregistration.coreg_info_from_pickle(pickle_path)
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","GCP_COUNT",f"{len(coreg_info['GCPList'])}")

        #METADATA add COREG RMSE
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","COREG_MEAN_SHIFT_PX_X",f"{coreg_info['mean_shifts_px']['x']:.2f}")
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","COREG_MEAN_SHIFT_PX_Y",f"{coreg_info['mean_shifts_px']['y']:.2f}")

        #METADATA add  ORBIT NR
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","ORBIT_NR",f"{orbit_num}")

        #METADATA add PROCESSING DATE
        processing_date = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","PROCESSING_DATE_UTC",processing_date)

        #METADATA add PROCESSING HOST information
        hostname = socket.gethostname()
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","PROCESSING_HOSTNAME",hostname)

        #METADATA add SOFTWARE_ENVIRONMENT gdal version and arosics version and omnicoudlmask version
        gdal_version = main_utils.run_gdal_command(["gdalinfo", "--version"])
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","GDAL_VERSION",gdal_version[1])
        try:
            arosics_version = version("arosics")
        except:
            arosics_version = "unknown"
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","AROSICS_VERSION",arosics_version)
        try:
            omnicloudmask_version = version("omnicloudmask")
        except:
            omnicloudmask_version = "unknown"
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","OMNICLOUDMASK_VERSION",omnicloudmask_version)

        #METADATA add SWISSTOPO_PROCESSOR VERSION
        processor_version = main_utils.get_github_info()
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","SWISSTOPO_PROCESSOR_VERSION",processor_version['GithubLink'])
        main_utils.metadata_add_entry(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json","PROPERTIES","SWISSTOPO_RELEASE_VERSION",processor_version['ReleaseVersion'])

        ##############################
        # Terrainshadowmask and incidence angle calculation: pass orbit ans date time and outputfilename
        #terrain_result = main_terrain.create_mask(
                            # f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_tci_10m.tif", config.PRODUCT_S2_LEVEL_2A['product_name'])

        ##############################
        # Clip Data to Switzerland and Reproject to CH1903LV95

        def clip_resample_to_cog(
            input_tif,
            clipfile,
            nodata_value=None,
            epsg=2056,
            lossy=False,
            quality=85,
            oversample_factor=5,
            orbit_nr=None
            ):
            """
            Clips, resamples and converts raster to COG format using multi-step oversampling.
            Process: 5x oversample (nearest) -> bilinear reproject -> 5x downsample (bilinear)
            As decided on 02.04.2025 with AGROSCOPE team.
            Uses only ONE temporary file to minimize disk usage.
            ONE-HOT ENCODING FOR CATEGORICAL (SCL) DATA: If the input file is identified as SCL (Scene Classification Layer), it applies a consistent 3-step process with one-hot encoding to ensure accurate class representation during resampling.

            Resolution and datatype are automatically detected from input file.

            Args:
                input_tif: Path to input raster (will be replaced with processed version)
                clipfile: Path to clip shapefile/geojson
                nodata_value: NoData value (optional - will be auto-detected if None)
                epsg: EPSG code for coordinate system
                lossy: True for JPEG compression, False for DEFLATE compression
                quality: JPEG quality (1-100), only relevant if lossy=True
                oversample_factor: Oversampling factor (default: 5)
                orbit_nr: Orbit number (for TCI masking/logging purposes)
            """

            # Read original resolution and datatype from input file
            props = get_raster_properties(input_tif)
            resolution = props['resolution']
            datatype = props['datatype']
            nodata_value = props['nodata']  # Get NoData from source

            print(f"Detected original resolution: {resolution}m")
            print(f"Using datatype: {datatype}")
            print(f"Using nodata value: {nodata_value}")

            input_path = Path(input_tif)
            intermediate_res = resolution / oversample_factor

            # List to track temp files for guaranteed cleanup
            temp_files_to_clean = []

            # =========================================================================
            # ONE-HOT ENCODING FOR CATEGORICAL (SCL) DATA
            # =========================================================================
            is_scl = "_scl_" in str(input_path.name).lower()

            if is_scl:
                print("\n=== SCL Data Detected: Applying Consistent 3-Step Process with One-Hot Encoding ===")

                try:
                    # Temp file definitions for SCL pipeline
                    onehot_init = input_path.parent / f"{input_path.stem}_onehot_init.tif"
                    temp_1_os = input_path.parent / f"{input_path.stem}_temp1_os.tif"
                    temp_2_rp = input_path.parent / f"{input_path.stem}_temp2_rp.tif"
                    temp_3_ds = input_path.parent / f"{input_path.stem}_temp3_ds.tif"
                    recombined_file = input_path.parent / f"{input_path.stem}_recombined.tif"

                    temp_files_to_clean.extend([onehot_init, temp_1_os, temp_2_rp, temp_3_ds, recombined_file])

                    # --- Step A: Split into 12 One-Hot Bands ---
                    with rasterio.open(input_tif) as src:
                        meta = src.meta.copy()
                        original_dtype = src.dtypes[0]
                        data = src.read(1)

                        num_classes = 12 # Sentinel-2 SCL uses classes 0 through 11
                        meta.update(count=num_classes, dtype='float32', nodata=None)

                        with rasterio.open(onehot_init, 'w', **meta) as dst:
                            for i in range(num_classes):
                                dst.write((data == i).astype('float32'), i + 1)
                    print("✓ Step A: One-hot encoded 12-band file created.")

                    # --- Step 1: Oversample (Nearest) ---
                    print(f"\n--- SCL Step 1: Oversampling 12 bands to {intermediate_res}m (near) ---")
                    cmd_os = [
                        "gdalwarp", "-cutline", str(clipfile), "-of", "GTiff",
                        "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-tr", str(intermediate_res), str(intermediate_res),
                        "-r", "near", "-ot", "Float32", "-overwrite",
                        str(onehot_init), str(temp_1_os)
                    ]
                    subprocess.run(cmd_os, check=True, capture_output=True)

                    # --- Step 2: Reproject (Bilinear) ---
                    print(f"\n--- SCL Step 2: Reprojecting 12 bands to EPSG:{epsg} (bilinear) ---")
                    cmd_rp = [
                        "gdalwarp", "-t_srs", f"EPSG:{epsg}", "-of", "GTiff",
                        "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-to", "ALLOW_BALLPARK=NO", "-to", "ONLY_BEST=YES",
                        "-tr", str(intermediate_res), str(intermediate_res),
                        "-r", "bilinear", "-ot", "Float32", "-overwrite",
                        str(temp_1_os), str(temp_2_rp)
                    ]
                    subprocess.run(cmd_rp, check=True, capture_output=True)

                    # --- Step 3: Downsample (Bilinear) ---
                    print(f"\n--- SCL Step 3: Downsampling 12 bands to {resolution}m (bilinear) ---")
                    cmd_ds = [
                        "gdalwarp", "-of", "GTiff", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-tr", str(resolution), str(resolution), "-tap",
                        "-r", "bilinear", "-ot", "Float32", "-overwrite",
                        str(temp_2_rp), str(temp_3_ds)
                    ]
                    subprocess.run(cmd_ds, check=True, capture_output=True)

                    # --- Step B: Recombine using Argmax ---
                    print(f"\n--- SCL Step B: Recombining bands using Argmax ---")
                    with rasterio.open(temp_3_ds) as src:
                        meta = src.meta.copy()
                        warped_data = src.read()

                        # Find the class with the highest fraction per pixel
                        final_data = np.argmax(warped_data, axis=0).astype(original_dtype)

                        meta.update(count=1, dtype=original_dtype)
                        if nodata_value is not None:
                            meta.update(nodata=nodata_value)

                        with rasterio.open(recombined_file, 'w', **meta) as dst:
                            dst.write(final_data, 1)

                    # --- Step C: Final COG Conversion ---
                    print(f"\n--- SCL Step C: Converting to Final COG ---")
                    cmd_cog = [
                        "gdalwarp", "-of", "COG", "-co", "BIGTIFF=YES",
                        "-co", "COMPRESS=DEFLATE", # Always force lossless for SCL
                        "-co", "PREDICTOR=2", "-co", "NUM_THREADS=ALL_CPUS",
                        "--config", "GDAL_NUM_THREADS", "ALL_CPUS"
                    ]
                    if nodata_value is not None:
                        cmd_cog.extend(["-srcnodata", str(nodata_value), "-dstnodata", str(nodata_value)])

                    cmd_cog.extend([str(recombined_file), str(input_tif), "-overwrite"])
                    subprocess.run(cmd_cog, check=True, capture_output=True)

                    print(f"✓ Final SCL COG created: {input_tif}")

                except Exception as e:
                    print(f"\n✗ Error occurred during SCL processing: {e}")
                    raise e
                finally:
                    # Clean up all One-Hot temp files
                    for f in temp_files_to_clean:
                        if f.exists():
                            print(f"Cleaning up: {f}")
                            f.unlink()

                # Exit the function early since SCL processing is complete
                return
            # =========================================================================

            # =========================================================================
            # CONTINUOUS DATA (e.g., B02, B03, B04)
            # =========================================================================
            temp_file = input_path.parent / f"{input_path.stem}_temp{input_path.suffix}"

            try:
                # Calculate intermediate resolution
                print(f"\n=== Step 1: Clipping and oversampling to {intermediate_res}m with nearest neighbour (NO reprojection) ===")

                # Step 1: Clip and oversample with nearest neighbour (keep original projection)
                cmd_oversample = [
                    "gdalwarp", "-cutline", str(clipfile), "-of", "GTiff",
                    "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                    "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                    "-tr", str(intermediate_res), str(intermediate_res),
                    "-r", "near", "-ot", datatype, "-overwrite"
                ]

                if nodata_value is not None:
                    cmd_oversample.extend(["-srcnodata", str(nodata_value)])  # Treat this value as NoData in source
                    cmd_oversample.extend(["-dstnodata", str(nodata_value)])  # Set this value as NoData in output

                cmd_oversample.extend([str(input_tif), str(temp_file)])

                print(f"Command: {' '.join(cmd_oversample)}")
                result = subprocess.run(cmd_oversample, capture_output=True, text=True)

                if result.returncode != 0:
                    raise Exception(f"Oversampling failed: {result.stderr}")
                print(f"✓ Oversampled and clipped file created: {temp_file}")

                print(f"\n=== Step 2: Reprojecting to EPSG:{epsg} with bilinear at {intermediate_res}m ===")

                # Step 2: Reproject with bilinear (at oversampled resolution)
                cmd_reproject = [
                    "gdalwarp", "-t_srs", f"EPSG:{epsg}", "-of", "GTiff",
                    "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                    "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                    "-to", "ALLOW_BALLPARK=NO", "-to", "ONLY_BEST=YES",
                    "-tr", str(intermediate_res), str(intermediate_res),
                    "-r", "bilinear", "-ot", datatype, "-overwrite"
                ]

                if nodata_value is not None:
                    cmd_reproject.extend(["-srcnodata", str(nodata_value)])  # Treat this value as NoData in source
                    cmd_reproject.extend(["-dstnodata", str(nodata_value)])  # Set this value as NoData in outpu

                cmd_reproject.extend([str(temp_file), str(input_tif)])
                result = subprocess.run(cmd_reproject, capture_output=True, text=True)

                if result.returncode != 0:
                    print(f"Error: {result.stderr}")
                    raise Exception(f"Reprojection failed with code {result.returncode}")

                # Move result back to temp_file for next step
                shutil.move(str(input_tif), str(temp_file))
                print(f"✓ Reprojected file ready")

                # Step 3: Resample (downsample) with bilinear to final resolution and convert to COG
                print(f"\n=== Step 3: Resampling to {resolution}m with bilinear and COG conversion ===")

                props_reprojected = get_raster_properties(temp_file)
                nodata_value = props_reprojected['nodata']  # Get NoData from step 2 output
                print(f"Detected reprojected resolution: {props_reprojected['resolution']}m")
                print(f"Using datatype: {props_reprojected['datatype']}")
                print(f"Using nodata value: {nodata_value}")

                target_res = resolution


                cmd_downsample = [
                    "gdalwarp", "-of", "COG", "-co", "BIGTIFF=YES",
                    "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                    "-tr", str(target_res), str(target_res), "-tap",
                    "-r", "bilinear", "-ot", datatype, "-overwrite"
                ]

                if lossy:
                    print(f"Using JPEG compression with quality {quality}")
                    cmd_downsample.extend([
                        "-cutline", str(clipfile), "-crop_to_cutline", "-dstalpha",
                        "-co", "COMPRESS=JPEG", "-co", f"QUALITY={quality}", "-co", "PHOTOMETRIC=YCBCR"
                    ])
                else:
                    print(f"Using lossless DEFLATE compression")
                    cmd_downsample.extend(["-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2", "-co", "ZLEVEL=2"])

                    # For lossless, preserve NoData value
                    if nodata_value is not None:
                        cmd_downsample.extend(["-srcnodata", str(nodata_value), "-dstnodata", str(nodata_value)])

                cmd_downsample.extend([str(temp_file), str(input_tif)])

                print(f"Command: {' '.join(cmd_downsample)}")
                result = subprocess.run(cmd_downsample, capture_output=True, text=True)

                if result.returncode != 0:
                    print(f"Error: {result.stderr}")
                    raise Exception(f"Resampling failed with code {result.returncode}")

                print(f"✓ Final COG created: {input_tif}")

            except Exception as e:
                print(f"\n✗ Error occurred: {e}")
                raise e

            finally:
                # Clean up temp file
                if temp_file.exists():
                    print(f"Cleaning up: {temp_file}")
                    temp_file.unlink()

        ##############################
        # Clip Data to Switzerland and Reproject to CH1903LV95


        def parse_sentinel2_filename(filename):
            """Parse Sentinel-2 mosaic filename including cloudmask."""
            basename = os.path.basename(filename)

            if not basename.endswith('.tif'):
                return None

            name_without_ext = basename[:-4]
            parts = name_without_ext.split('_')

            if len(parts) < 7 or parts[3] != 'mosaic':
                return None

            timestamp = parts[4]
            band = parts[5].upper()
            resolution_str = parts[6]

            if not resolution_str.endswith('m'):
                return None

            try:
                resolution = int(resolution_str[:-1])
            except ValueError:
                return None

            # Validate: either in band_config or is CLOUDMASK
            all_bands = [b for bands in config.PRODUCT_S2_LEVEL_2A['band_config'].values() for b in bands]

            if band not in all_bands and band != 'CLOUDMASK':
                return None

            return {
                'timestamp': timestamp,
                'band': band,
                'resolution': resolution,
                'filename': filename
            }


        # get all .tif files in the current folder
        all_tifs = glob.glob("*.tif")

        # keep only those whose filename (without the directory) contains the timestamp ( if we have multiple  orbits in the same folder)
        tif_files = [f for f in all_tifs if timestamp in os.path.basename(f)]

        # Parse and group by timestamp
        files_by_timestamp = defaultdict(list)

        for tif_file in tif_files:
            parsed = parse_sentinel2_filename(tif_file)
            if parsed:
                files_by_timestamp[parsed['timestamp']].append(parsed)

        # Process files grouped by timestamp
        for timestamp, file_list in sorted(files_by_timestamp.items()):
            print(f"\n=== Processing timestamp: {timestamp} ===")

            # Sort by resolution and band
            file_list.sort(key=lambda x: (x['resolution'], x['band']))

            for file_info in file_list:
                band = file_info['band']
                filename = file_info['filename']

                # Get band title using config
                band_names = config.PRODUCT_S2_LEVEL_2A['band_names']
                band_title = band_names.get(band, band)

                # Set compression
                if band in ['TCI']:
                    lossy = True
                    quality = 85
                else:
                    lossy = False
                    quality = 100

                print(f"  Processing: {band} ({band_title}) - lossy={lossy}, quality={quality}")

                # Clip on BBOX of extent buffer to reduce file size for processing

                # Get bounds from GeoPackage
                gdf = gpd.read_file(orbit_clipfile)
                bounds_2056 = gdf.total_bounds  # in EPSG:2056

                # Transform bounds to EPSG:32632
                from shapely.geometry import box
                bbox_gdf = gpd.GeoDataFrame(
                    geometry=[box(*bounds_2056)],
                    crs='EPSG:2056'
                )
                bbox_utm = bbox_gdf.to_crs('EPSG:32632')
                bounds = bbox_utm.total_bounds  # Now in EPSG:32632

                # Temporary output filename
                temp_filename = str(filename) + ".tmp"

                cmd = [
                    'gdal_translate',
                    '-of', 'GTiff',  # Explicitly specify GeoTIFF format
                    '-projwin', str(bounds[0]), str(bounds[3]), str(bounds[2]), str(bounds[1]),
                    str(filename),
                    temp_filename
                ]

                # Run with error capture
                result = subprocess.run(cmd, capture_output=True, text=True)

                # Replace original with clipped version
                os.remove(filename)
                os.rename(temp_filename, filename)

                print(f"Original file  clipped to BBOX of : {filename}")

                #Clip, resample and convert to COG
                clip_resample_to_cog(
                    filename,
                    orbit_clipfile,
                    nodata_value=None,
                    epsg=2056,
                    lossy=lossy,
                    quality=quality,
                    oversample_factor=5,
                    orbit_nr=orbit_num
                )
        ##############################
        # Generate TCI

        buffer_path = Path(config.BUFFER)
        # Construct new filename with orbit number
        orbit_clipfile = buffer_path.with_name(f"{buffer_path.stem}_{orbit_num}{buffer_path.suffix}")
        # Generate TCI from B04,B03,B02
        main_create_rgb.create_enhanced_rgb(f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_b04_10m.tif", f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_b03_10m.tif", f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_b02_10m.tif", orbit_clipfile,f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_tci_10m.tif")

        ##############################
        # Generate Thumbnails
        # check if there is a need to create thumbnail , if yes create it

        thumbnail = main_thumbnails.create_thumbnail(
                            f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_tci_10m.tif", config.PRODUCT_S2_LEVEL_2A['product_name'])




        ##############################
        # Checkif current, if yes then rund upload below twice a day
        is_current = main_utils.extract_and_compare_datetime_from_url(f"{config.STAC_FSDI_SCHEME}://{config.STAC_FSDI_HOSTNAME}{config.STAC_FSDI_API}collections/{collection.split('/')[-1]}/items/{collection.split('/')[-1].replace('swisstopo.', '').replace('ch.', '')}",timestamp)

        ##############################
        # Upload to STAC
        # Process Sentinel files group§ed by timestamp
        for timestamp, file_list in sorted(files_by_timestamp.items()):
            print(f"\n=== Processing timestamp: {timestamp} ===")

            # Since we generate TCI Manually, add it in the file list
            file_list.append({
                'timestamp': timestamp,
                'band': 'TCI',
                'resolution': 10,
                'filename': f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_tci_10m.tif"
            })

            # TCI last, resolution descending (bigger first), bands Z to A, so it is alphabetically in STAC
            file_list.sort(key=lambda x: (x['band'] == 'TCI', x['resolution'], x['band']), reverse=True)

            for file_info in file_list:
                band = file_info['band']
                filename = file_info['filename']

                # Get band title using config
                band_names = config.PRODUCT_S2_LEVEL_2A['band_names']

                if band == 'TCI':
                    band_title = "True color image - 10m"
                else:
                    band_title = band_names.get(band, band)

                # STAC Upload
                main_publish_stac_fsdi.publish_to_stac(filename,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],None,asset_title=band_title)
                if is_current == True:
                    print("Newest dataset detected: updating CURRENT")
                    filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
                    # Rename the file
                    os.rename(filename, filename_current)
                    main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],asset_title=band_title, current=True)
                    os.rename(filename_current, filename)

        # Upload metadata file
        filename=f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json"
        main_publish_stac_fsdi.publish_to_stac(filename,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],None,asset_title="Metadata")
        if is_current == True:
            #print("Newest dataset detected: updating CURRENT")
            filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
            # Rename the file
            os.rename(filename, filename_current)
            main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],asset_title="Metadata", current=True)
            os.rename(filename_current, filename)

        # Upload Thumbnail
        filename=thumbnail
        main_publish_stac_fsdi.publish_to_stac(filename,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],None,asset_title="Thumbnail")
        if is_current == True:
            #print("Newest dataset detected: updating CURRENT")
            filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
            # Rename the file
            os.rename(filename, filename_current)
            main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],asset_title="Thumbnail", current=True)
            os.rename(filename_current, filename)

        # Clean up Thumbnailfile
        if Path(filename).exists():
                print(f"Cleaning up: {filename}")
                Path(filename).unlink()


        ##############################
        # Upload pickle to S3

        filename=f"swisseo_s2-sr_v200_mosaic_{timestamp}_registration.pickle"
        s3_key = os.path.join(s3_coreg_path, filename).replace("\\", "/")

        main_utils.s3.upload_file(f"swisseo_s2-sr_v200_mosaic_{timestamp}_registration.pickle", config.S3_BUCKET_NAME, s3_key)

        ##############################
        # TODO Upload to GEE

        ##############################
        # Cleaning up files of orbit
        pattern = f"*{timestamp}*.*"
        # Clean up pickle file
        for file in Path(".").glob(pattern):
            print(f"Cleaning up: {file}")
            file.unlink()






    print("end of function")
=======

    # - Query data with pystac, based on perimeter
    # - Download data all orbits with predefined bands
    # - unzip in folder per orbit























    # MULTIPLE ORBITS per day: For 2025 starting in March, ESA runs S2A and S2C in parallel resulting in multiple orbits per day

    # Sentinel-2
    S2_sr_orbits= ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filter(ee.Filter.bounds(aoi_CH)) \
        .filter(ee.Filter.date(start_date, end_date))

    # unique SENSING_ORBIT_NUMBER
    unique_orbits = S2_sr_orbits.aggregate_array('SENSING_ORBIT_NUMBER') \
        .distinct() \
        .getInfo()

    # For multiple orbits set a cloudy scene counter to zero
    cloudy_scene_counter = 0

    # Loop over all orbits
    for orbit in unique_orbits:

        # Print if unique_orbit has more than 1 element
        if len(unique_orbits) > 1:
            print(f"Processing orbit: {orbit} of {day_to_process}")


        # S2 CloudScore+
        S2_csp = ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED') \
            .filter(ee.Filter.bounds(aoi_CH)) \
            .filter(ee.Filter.date(start_date, end_date))

        # S2cloudless
        S2_clouds = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY') \
            .filter(ee.Filter.bounds(aoi_CH)) \
            .filter(ee.Filter.date(start_date, end_date))

        # Sentinel-2
        S2_sr = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filter(ee.Filter.bounds(aoi_CH)) \
            .filter(ee.Filter.date(start_date, end_date)) \
            .filter(ee.Filter.eq('SENSING_ORBIT_NUMBER', orbit)) \
            .linkCollection(S2_csp, ['cs', 'cs_cdf']) \
            .linkCollection(S2_clouds, ['probability'])

        # Is a scene available for this date -> Yes: continue / No: abort ('No candidate scene')
        image_list_size = S2_sr.size().getInfo()
        if image_list_size == 0:
            write_asset_as_empty(collection, day_to_process, 'No candidate scene')
            return

        # Are all tiles for the overpass available -> Yes: continue / No: abort ('Tile upload incomplete')
        SENSING_ORBIT_NUMBER = S2_sr.first().get('SENSING_ORBIT_NUMBER').getInfo()
        if image_list_size < 4 and SENSING_ORBIT_NUMBER == 8:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return  # exit if condition met
        if image_list_size < 11 and SENSING_ORBIT_NUMBER == 108:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return
        if image_list_size < 11 and SENSING_ORBIT_NUMBER == 65:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return
        if image_list_size < 4 and SENSING_ORBIT_NUMBER == 22:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return


        # Get image_list_size for the cloud probability dataset
        if cloudScorePlus is True:
            image_list_size_cloud = S2_sr.select('cs').size().getInfo()
        else:
            image_list_size_cloud = S2_sr.select('probability').size().getInfo()

        # Are CloudScore+ datasets for all tiles available -> Yes: continue / No: abort ('Cloud probability data missing')
        if image_list_size_cloud < 4 and SENSING_ORBIT_NUMBER == 8:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 11 and SENSING_ORBIT_NUMBER == 108:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 11 and SENSING_ORBIT_NUMBER == 65:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 4 and SENSING_ORBIT_NUMBER == 22:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return

        # image_list = S2_sr.toList(S2_sr.size())
        # for i in range(image_list_size):
        #     image = ee.Image(image_list.get(i))

        #     # EE asset ids for Sentinel-2 L2 assets have the following format: 20151128T002653_20151128T102149_T56MNN.
        #     #  Here the first numeric part represents the sensing date and time, the second numeric part represents the product generation date and time,
        #     #  and the final 6-character string is a unique granule identifier indicating its UTM grid reference
        #     image_id = image.id().getInfo()
        #     image_sensing_timestamp = image_id.split('_')[0]
        #     # first numeric part represents the sensing date, needs to be used in publisher
        #     print("generating json {} of {} ({})".format(
        #         i + 1, image_list_size, image_sensing_timestamp))

        #     # Generate the filename
        #     filename = config.PRODUCT_S2_LEVEL_2A['product_name'] + '_' + image_id
        #     # Export Image Properties into a json file
        #     file_name = filename + "_properties" + "_run" + \
        #         day_to_process.replace("-", "") + ".json"
        #     json_path = os.path.join(config.PROCESSING_DIR, file_name)
        #     with open(json_path, "w") as json_file:
        #         json.dump(image.getInfo(), json_file)

        ###########################
        # WATER MASK
        # The water mask is used to limit a buffering operation on the cast shadow mask.
        # Here, it helps to better distinguish between dark areas and water bodies.
        # This distinction is also used to limit the cloud shadow propagation.
        # EU-Hydro River Network Database 2006-2012 data is derived from this data source:
        # https:#land.copernicus.eu/en/products/eu-hydro/eu-hydro-river-network-database#download
        # processing: reprojected in QGIS to epsg32632

        # Lakes
        lakes = ee.FeatureCollection(
            "projects/satromo-prod/assets/res/CH_inlandWater")

        # vector-to-image conversion based on the area attribute
        lakes_img = lakes.reduceToImage(
            properties=['AREA'],
            reducer=ee.Reducer.first()
        )

        # Make a binary mask and clip to area of interest
        lakes_binary = lakes_img.gt(0).unmask().clip(aoi_CH_simplified)

        # Rivers
        rivers = ee.FeatureCollection(
            "projects/satromo-prod/assets/res/CH_RiverNet")

        # vector-to-image conversion based on the area attribute.
        rivers_img = rivers.reduceToImage(
            properties=['AREA_GEO'],
            reducer=ee.Reducer.first()
        )

        # Make a binary mask and clip to area of interest
        rivers_binary = rivers_img.gt(0).unmask().clip(aoi_CH_simplified)

        # combine both water masks
        water_binary = rivers_binary.Or(lakes_binary)

        ##############################
        # FUNCTIONS

        # This function detects clouds and cloud shadows, masks all spectral bands for them, and adds the mask as an additional layer
        # CloudScore+
        def maskCloudsAndShadowsCloudScorePlus(image):
            # Use 'cs' or 'cs_cdf'
            # cs: Pixel quality score based on spectral distance from a (theoretical) clear reference
            # cs_cdf: Value of the cumulative distribution function of possible cs values for the estimated cs value
            QA_BAND = 'cs_cdf'

            # invert the cloud score bands to represent cloudy with 1 and clear with 0
            # inherently CloudScore+ shows the clearness of a pixel, but we would like to look at cloudyness
            invertedImage = image.expression('1 - b("cs")', {'cs': image.select('cs')}).rename('cs') \
                .addBands(image.expression('1 - b("cs_cdf")', {'cs_cdf': image.select('cs_cdf')}).rename('cs_cdf'))

            # replace the cloud score bands with the inverted ones
            bandNames = image.bandNames()
            bandsToDelete = ['cs', 'cs_cdf']
            bandsToKeep = bandNames.filter(
                ee.Filter.inList('item', bandsToDelete).Not())

            # Replace 'cs' and 'cs_cdf' bands in the original 'image' with the inverted versions
            image = image \
                .select(bandsToKeep) \
                .addBands(invertedImage.select(['cs']).rename('cs')) \
                .addBands(invertedImage.select(['cs_cdf']).rename('cs_cdf'))

            # get the cloud probability

            # clouds = image.select(QA_BAND)
            # get the cloud probability casted to uint8 0-100
            clouds = image.select(QA_BAND).multiply(100).toUint8()

            # The threshold for masking; values between 0.50 and 0.35 generally work well.
            # Lower values will remove thin clouds, haze, cirrus & shadows.
            CLOUD_THRESHOLD = 40  # casted to 100 from 0.4
            CLOUDSHADOW_THRESHOLD = 20  # casted to 100 from 0.2

            # applying the maximum cloud probability threshold
            isNotCloud = clouds.lt(CLOUD_THRESHOLD)

            # get the solar position
            meanAzimuth = image.get('MEAN_SOLAR_AZIMUTH_ANGLE')
            meanZenith = image.get('MEAN_SOLAR_ZENITH_ANGLE')

            # define potential cloud shadow values
            cloudShadowMask = clouds.lt(CLOUD_THRESHOLD).And(
                clouds.gte(CLOUDSHADOW_THRESHOLD))

            # Project shadows from clouds. This step assumes we're working in a UTM projection.
            shadowAzimuth = ee.Number(90).subtract(ee.Number(meanAzimuth))
            # shadow distance is tied to the solar zenith angle (minimum shadowDistance is 30 pixel)
            shadowDistance = ee.Number(meanZenith).multiply(
                0.7).floor().int().max(30)

            # With the following algorithm, cloud shadows are projected.
            isCloud = isNotCloud.directionalDistanceTransform(
                shadowAzimuth, shadowDistance)
            isCloud = isCloud.reproject(
                crs=image.select('B2').projection(), scale=100)

            cloudShadow = isCloud.select('distance').mask()

            # combine projected Shadows & potential cloud shadow values
            cloudShadow = cloudShadow.And(cloudShadowMask)

            # combine mask for clouds and cloud shadows
            cloudAndCloudShadowMask = cloudShadow.Or(isNotCloud.Not())

            # Opening operation: individual pixels are deleted (localMin) and buffered (localMax) to also capture semi-transparent cloud edges
            cloudAndCloudShadowMask = cloudAndCloudShadowMask \
                .focalMin(50, 'circle', 'meters', 1, None) \
                .focalMax(100, 'circle', 'meters', 1, None)

            # mask spectral bands for clouds and cloudShadows
            # image_out = image.select(['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B11', 'B12']) \
            #     .updateMask(cloudAndCloudShadowMask.Not())  # NOTE: disabled because we want the clouds in the asset

            # adding the additional S2 L2A layers, S2 cloudProbability and cloudAndCloudShadowMask as additional bands
            image = image.addBands(clouds.rename(['cloudProbability'])) \
                .addBands(cloudAndCloudShadowMask.rename(['cloudAndCloudShadowMask']))

            return image.set({
                'cloud_detection_algorithm': 'CloudScore+',
                'cloud_mask_threshold': str(CLOUD_THRESHOLD) + ' / ' + str(CLOUDSHADOW_THRESHOLD)
            })

        # This function detects clouds and cloud shadows, masks all spectral bands for them, and adds the mask as an additional layer
        # S2cloudless
        def maskCloudsAndShadowsSTwoCloudless(image):
            # get the solar position
            meanAzimuth = image.get('MEAN_SOLAR_AZIMUTH_ANGLE')
            meanZenith = image.get('MEAN_SOLAR_ZENITH_ANGLE')

            # get the cloud probability
            clouds = image.select('probability')
            # the maximum cloud probability threshold is set at 50
            CLOUD_THRESHOLD = 50
            isNotCloud = clouds.lt(CLOUD_THRESHOLD)
            cloudMask = isNotCloud.Not()
            # Opening operation: individual pixels are deleted (localMin) and buffered (localMax) to also capture semi-transparent cloud edges
            cloudMask = cloudMask.focalMin(50, 'circle', 'meters', 1, None).focalMax(
                100, 'circle', 'meters', 1, None)

            # Find dark pixels but exclude lakes and rivers (otherwise projected shadows would cover large parts of water bodies)
            darkPixels = image.select(['B8', 'B11', 'B12']).reduce(
                ee.Reducer.sum()).lt(2500).subtract(water_binary).clamp(0, 1)

            # Project shadows from clouds. This step assumes we're working in a UTM projection.
            shadowAzimuth = ee.Number(90).subtract(ee.Number(meanAzimuth))
            # shadow distance is tied to the solar zenith angle (minimum shadowDistance is 30 pixel)
            shadowDistance = ee.Number(meanZenith).multiply(
                0.7).floor().int().max(30)

            # With the following algorithm, cloud shadows are projected.
            isCloud = cloudMask.directionalDistanceTransform(
                shadowAzimuth, shadowDistance)
            isCloud = isCloud.reproject(
                crs=image.select('B2').projection(), scale=100)

            cloudShadow = isCloud.select('distance').mask()

            # combine projectedShadows & darkPixel and buffer the cloud shadow
            cloudShadow = cloudShadow.And(darkPixels).focalMax(
                100, 'circle', 'meters', 1, None)

            # combined mask for clouds and cloud shadows
            cloudAndCloudShadowMask = cloudShadow.Or(cloudMask)

            # mask spectral bands for clouds and cloudShadows
            # image_out = image.select(['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B11', 'B12']) \
            #     .updateMask(cloudAndCloudShadowMask.Not())  # NOTE: disabled because we want the clouds in the asset

            # adding the additional S2 L2A layers, S2 cloudProbability and cloudAndCloudShadowMask as additional bands
            image = image.addBands(clouds.rename(['cloudProbability'])) \
                .addBands(cloudAndCloudShadowMask.rename(['cloudAndCloudShadowMask']))

            return image.set({
                'cloud_detection_algorithm': 's2cloudless',
                'cloud_mask_threshold': CLOUD_THRESHOLD         # threshold for cloud mask
            })

        # This function calculates and adds the illumination angle
        def addIlluminationAngel(image):
            # get the solar position
            meanAzimuth = image.get('MEAN_SOLAR_AZIMUTH_ANGLE')
            meanZenith = image.get('MEAN_SOLAR_ZENITH_ANGLE')

            # Create an empty image to apply the expression
            empty_image = ee.Image().float()

            # Calculate illumination angle
            illumination_cos = empty_image.expression(
                'cos(sz) * cos(ps) + sin(sz) * sin(ps) * cos(sa - pa)',
                {
                    'sz': ee.Number(meanZenith).multiply(np.pi).divide(180),  # Convert solar zenith to radians
                    'sa': ee.Number(meanAzimuth).multiply(np.pi).divide(180),  # Convert solar azimuth to radians
                    'ps': slope,
                    'pa': aspect
                }
            )
            # The result is the cosine of the illumination angle
            # To get the angle itself -> acos
            illumination_angle_r = illumination_cos.acos()
            illumination_angle = illumination_angle_r.multiply(180).divide(np.pi)

            # Round to full numbers, convert to int, and cap at 90
            illumination_angle = illumination_angle.round().toInt().clamp(0, 90).rename('terrainShadowMask')

            # add the additonal terrainShadow band
            image = image.addBands(illumination_angle)

            return image

        # This function detects and updates terrain shadows
        def addTerrainShadow(image):
            # get the solar position
            meanAzimuth = image.get('MEAN_SOLAR_AZIMUTH_ANGLE')
            meanZenith = image.get('MEAN_SOLAR_ZENITH_ANGLE')

            # Terrain shadow
            terrainShadow = ee.Terrain.hillShadow(
                DEM_sa3d, meanAzimuth, meanZenith, 100, True)
            terrainShadow = terrainShadow.Not() # invert the binaries

            # Update the existing terrainShadowMask band
            updatedMask = image.select('terrainShadowMask').where(terrainShadow, 100)

            # Replace the existing terrainShadowMask band
            image = image.addBands(updatedMask, ['terrainShadowMask'], True)

            return image

        # This updates terrain shadows from precalcuated terrain
        def addTerrainShadow_predefined(image, start_date, terrain_shadow_collection, S2_sr):

            # Define the day of year
            doy = ee.Date(start_date).getRelative('day', 'year').add(1)

            # Get the date string and create an ee.Date object
            date_string = ee.Date(start_date).format('YYYY-MM-dd').getInfo()
            midnight_date = ee.Date(date_string)

            # Get the ee.Date object in UNIX TIME
            midnight_unix = midnight_date.millis()

            # Load the terrain shadow image for the DOY
            terrain_shadow_asset = ee.Image(
                terrain_shadow_collection + str(doy.getInfo()))

            # Extract Unix time from the first image in the Sentinel-2 collection
            sysindex = S2_sr.first()
            index = sysindex.get('system:index').getInfo()

            date_time_part = ee.String(index).split('_').get(0)
            date_time_part_without_t = ee.String(date_time_part).replace('T', '')
            date = ee.Date.parse('yyyyMMddHHmmss', date_time_part_without_t)
            unix_time = ee.Number(date.millis()).subtract(midnight_unix)

            # Extract band names from the asset and remove the prefix "shadow_"
            band_names = terrain_shadow_asset.bandNames().map(
                lambda band_name: ee.String(band_name).replace('shadow_', ''))

            # Find the band with the smallest difference in Unix time
            def find_closest_band(current, previous):
                current_time = ee.Number.parse(current)
                previous_time = ee.Number.parse(previous)
                current_diff = current_time.subtract(unix_time).abs()
                previous_diff = previous_time.subtract(unix_time).abs()
                return ee.Algorithms.If(current_diff.lt(previous_diff), current, previous)

            closest_band_name = ee.String(band_names.iterate(
                find_closest_band, band_names.get(0)))

            band_image = terrain_shadow_asset.select(
                'shadow_' + closest_band_name.getInfo())

            # Update the existing terrainShadowMask band
            updatedMask = image.select('terrainShadowMask').where(band_image, 100)

            # Replace the existing terrainShadowMask band
            image = image.addBands(updatedMask, ['terrainShadowMask'], True)

            return image

        # This function adds the masked-pixel-percentage (clouds, cloud shadows, QA masks) as a property to each image
        def addMaskedPixelCount(image):
            # counter the umber of pixel that are masked by cloud or shadows
            image_mask = image.select('cloudAndCloudShadowMask').gt(
                0).Or(image.select('terrainShadowMask').gt(99))
            statsMasked = image_mask.reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=image.geometry().intersection(aoi_CH_simplified),
                scale=100,
                bestEffort=True,
                maxPixels=1e10,
                tileScale=4
            )
            dataPixels = statsMasked.getNumber('cloudAndCloudShadowMask')

            # get the total number of valid pixel
            image_mask = image.select('cloudAndCloudShadowMask').gte(0)
            statsAll = image_mask.unmask().reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=image.geometry().intersection(aoi_CH_simplified),
                scale=100,
                bestEffort=True,
                maxPixels=1e10,
                tileScale=4
            )
            allPixels = statsAll.getNumber('cloudAndCloudShadowMask')

            # Calculate the percentages and add the properties
            percMasked = (dataPixels.divide(allPixels)).multiply(
                1000).round().divide(10)
            percData = ee.Number(100).subtract(percMasked)

            return image.set({
                'percent_data': percData,  # percentage of unmasked pixel
                # masked pixels include clouds, cloud shadows and QA pixels
                'percent_masked': percMasked
            })

        # This function buffers (inward) the tile geometry by 500m
        # necessary because the CloudScore+ dataset has edge effects
        def clip_outermost_rows(image):
            img_geometry = image.geometry()  # Get the geometry of each image

            # Buffer the geometry inward by 500 meters
            buffered_geometry = img_geometry.buffer(-500)
            # Clip the image to the outer bounds
            return image.clip(buffered_geometry)

        # This function masks all bands to the same extent as the 20 m and 60 m bands

        def maskEdges(s2_img):
            return s2_img.updateMask(
                s2_img.select('B8A').mask().updateMask(s2_img.select('B9').mask()))

        # This function sets the date as an additional property to each image
        def set_date(img):
            date = img.date().format('YYYY-MM-dd')
            return img.set('date', date)

        ##############################
        # PROCESSING

        # Map the date and edges functions
        S2_sr = S2_sr.map(clip_outermost_rows) \
            .map(maskEdges) \
            .map(set_date)

        # SWITCH
        if cloudMasking is True:
            # apply the cloud mapping and masking functions
            if cloudScorePlus is True:
                print('--- Cloud and cloud shadow masking applied: CloudScore+ ---')
                S2_sr = ee.ImageCollection(
                    S2_sr).map(maskCloudsAndShadowsCloudScorePlus)
            else:
                print('--- Cloud and cloud shadow masking applied: s2cloudless ---')
                S2_sr = ee.ImageCollection(
                    S2_sr).map(maskCloudsAndShadowsSTwoCloudless)

        # Add the illumination angle as terrainShadowMask band
        S2_sr = S2_sr.map(addIlluminationAngel)

        # SWITCH
        if terrainShadowDetection is True:
            print('--- Terrain shadow detection applied ---')
            # apply the terrain shadow function
            S2_sr = S2_sr.map(addTerrainShadow)

        if terrainShadowDetectionPrecalculated is True:
            print('--- Terrain shadow from precalculated shadow applied  ---')
            # apply the terrain shadows
            S2_sr = S2_sr.map(lambda image: addTerrainShadow_predefined(
                image, start_date, terrain_shadow_collection, S2_sr))

        # MOSAIC
        # This step mosaics overlapping Sentinel-2 tiles acquired on the same day

        # 'distinct' removes duplicates from a collection based on a property.
        distinctDates_S2_sr = S2_sr.distinct('date').sort('date')

        # define the filter
        filter = ee.Filter.equals(leftField='date', rightField='date')

        # 'ee.Join.saveAll' Returns a join that pairs each element from the first collection with a group of matching elements from the second collection
        # the matching images are stored in a new property called 'date_match'
        join = ee.Join.saveAll('date_match')

        # 'apply' Joins to collections.
        joinCol_S2_sr = join.apply(distinctDates_S2_sr, S2_sr, filter)

        # This function mosaics image acquired on the same day (same image swath)
        def mosaic_collection(img):
            # create a collection of the date-matching images
            col = ee.ImageCollection.fromImages(img.get('date_match'))

            # extract collection properties to assign to the mosaic
            time_start = col.aggregate_min('system:time_start')
            time_end = col.aggregate_max('system:time_end')
            index_list = col.aggregate_array('system:index')
            index_list = index_list.join(',')
            scene_count = col.size()

            # get the unified geometry of the collection (outer boundary)
            col_geo = col.geometry().dissolve()

            # clip the mosaic to set a geometry to it

            mosaic = col.mosaic().clip(col_geo).copyProperties(img, ["system:time_start", "system:index", "date", "month",
                                                                    "SENSING_ORBIT_NUMBER", "PROCESSING_BASELINE",
                                                                    "SPACECRAFT_NAME", "MEAN_SOLAR_ZENITH_ANGLE",
                                                                    "MEAN_SOLAR_AZIMUTH_ANGLE", "cloud_detection_algorithm",
                                                                    "cloud_mask_threshold"])

            # Getting swisstopo Processor Version
            processor_version = main_utils.get_github_info()

            # Set TerrainShadow Properties
            if terrainShadowDetectionPrecalculated:
                terrainshadow_method = terrain_shadow_collection
            else:
                terrainshadow_method = 'ee.Terrain.hillShadow'

            # Set TerrainShadow Properties
            if coRegistrationPrecalculated:
                coreg_method = dxdy_collection
            else:
                coreg_method = 'GEE displacement'

            # set the extracted properties to the mosaic
            mosaic = mosaic.set('system:time_start', time_start) \
                .set('system:time_end', time_end) \
                .set('index_list', index_list) \
                .set('scene_count', scene_count) \
                .set('COREGISTRATION', coreg_method) \
                .set('TERRAIN_SHADOW', terrainshadow_method) \
                .set('SWISSTOPO_PROCESSOR', processor_version['GithubLink']) \
                .set('SWISSTOPO_RELEASE_VERSION', processor_version['ReleaseVersion'])

            # reset the projection to epsg:32632 as mosaic changes it to epsg:4326 (otherwise the registration fails)
            mosaic = ee.Image(mosaic).setDefaultProjection('epsg:32632', None, 10)

            return mosaic

        # SWITCH
        if swathMosaic is True:
            print('--- Image swath mosaicing applied ---')
            # apply the mosaicing function
            S2_sr = ee.ImageCollection(joinCol_S2_sr.map(
                mosaic_collection)).map(addMaskedPixelCount)
            # filter for data availability: "'percent_data', 2 " is 98% cloudfree. "'percent_data', 20 " is 80% cloudfree.
            S2_sr = S2_sr.filter(ee.Filter.gte('percent_data', 20))
            length_without_clouds = S2_sr.size().getInfo()
            if length_without_clouds == 0:
                # check if the first scene is cloudy increase the counter in this case. if we have two scenes with clouds assign cloudy
                if len(unique_orbits) > 1:
                    cloudy_scene_counter = cloudy_scene_counter+ 1
                    if cloudy_scene_counter == 1:
                        print(f"Orbit {SENSING_ORBIT_NUMBER} is cloudy")
                        continue
                    if cloudy_scene_counter == 2:
                        print(f"Orbit {SENSING_ORBIT_NUMBER} is cloudy")
                        write_asset_as_empty(collection, day_to_process, 'cloudy')
                        return
                else:
                    write_asset_as_empty(collection, day_to_process, 'cloudy')
                    return
            # This is the If condition the return just the line after the end the step0 script ends the process if 'percent_data' is greater.
            # It's after the mosaic because the threshold (80% here) is applied on the whole mosaic and not per scene:
            # we decide together for the whole swath if we want to process it or not.

            S2_sr = S2_sr.first()

        ##############################
        # REGISTER

        # This function co-registers Sentinel-2 images to the Sentinel-2 global reference image

        def S2regFunc(image):

            # Use bicubic resampling during registration.
            imageOrig = image.resample('bicubic')

            # Choose to register using only the 'R' band.
            imageRedBand = imageOrig.select('B4')

            # Determine the displacement by matching only the 'R' bands.
            displacement = imageRedBand.displacement(
                referenceImage=S2_gri,
                maxOffset=10,
                patchWidth=300,
                stiffness=8
            )

            # Extract relevant displacement parameters
            reg_dx = displacement.select('dx').rename('reg_dx')
            reg_dx = reg_dx.multiply(100).round().toInt16()
            reg_dy = displacement.select('dy').rename('reg_dy')
            reg_dy = reg_dy.multiply(100).round().toInt16()
            reg_confidence = displacement.select(
                'confidence').rename('reg_confidence')
            reg_confidence = reg_confidence.multiply(100).round().toUint8()

            # Compute image offset and direction.
            reg_offset = reg_dx.hypot(reg_dy).rename('reg_offset')
            reg_angle = reg_dx.atan2(reg_dy).rename('reg_offsetAngle')

            # Use the computed displacement to register all original bands.
            registered = image.displace(displacement) \
                .addBands(reg_dx) \
                .addBands(reg_dy) \
                .addBands(reg_confidence) \
                .addBands(reg_offset) \
                .addBands(reg_angle)

            return registered

        def S2regprecalcFunc(image, day, collection, orbit):
            # Load the collection
            dxdy_coll = ee.ImageCollection(collection)

            # Define the precise start and end timestamps for '2023-10-01'
            start_datetime = day+'T00:00:00'
            end_datetime = day+'T23:59:59'


            # Filter the collection by the precise date and time range and SENSING_ORBIT_NUMBER
            filtered_collection = dxdy_coll.filterDate(
                start_datetime, end_datetime).filter(ee.Filter.eq('SENSING_ORBIT_NUMBER', orbit))

            # Is a dx dy available for this date -> Yes: continue / No: abort ('No dx dy available')
            image_list_size = filtered_collection.size().getInfo()
            if image_list_size == 0:
                write_asset_as_empty(
                    collection, day, 'No dx dy available')
                return

            # Get the first image that meets the criteria
            dxdy = filtered_collection.first()

            # Check if the image exists
            if dxdy:
                # Get the image ID
                dxdy_id = dxdy.get('system:id').getInfo()
                print('-> dxdy ID:', dxdy_id)
            else:
                print('ERROR: No precalculated dxdy  found for the specified date.')

            # Extract relevant displacement parameters
            # Select the bands 'reg_dx' and 'reg_dy' and divide by 100
            displacement = dxdy.select(['reg_dx', 'reg_dy']).divide(100)

            # Extract relevant displacement parameters
            reg_dx = dxdy.select('reg_dx')
            reg_dy = dxdy.select('reg_dy')
            reg_confidence = dxdy.select(
                'reg_dy').rename('reg_confidence')
            # TODO This band is not needed change whole processing chain since now all are 0, till the export
            reg_confidence = reg_confidence.multiply(0).round().toUint8()

            # # Use bicubic resampling during registration.
            # imageOrig = image.resample('bicubic')

            # # Choose to register using only the 'R' band.
            # imageRedBand = imageOrig.select('B4')

            # # Determine the displacement by matching only the 'R' bands.
            # displacement = imageRedBand.displacement(
            #     referenceImage=S2_gri,
            #     maxOffset=10,
            #     patchWidth=300,
            #     stiffness=8
            # )

            # # Extract relevant displacement parameters
            # reg_dx = displacement.select('dx').rename('reg_dx')
            # reg_dx = reg_dx.multiply(100).round().toInt16()
            # reg_dy = displacement.select('dy').rename('reg_dy')
            # reg_dy = reg_dy.multiply(100).round().toInt16()
            # reg_confidence = displacement.select(
            #     'confidence').rename('reg_confidence')
            # reg_confidence = reg_confidence.multiply(100).round().toUint8()

            # Compute image offset and direction.
            reg_offset = reg_dx.hypot(reg_dy).rename('reg_offset')
            reg_angle = reg_dx.atan2(reg_dy).rename('reg_offsetAngle')

            # Use the computed displacement to register all original bands.
            registered = image.displace(displacement) \
                .addBands(reg_dx) \
                .addBands(reg_dy) \
                .addBands(reg_confidence) \
                .addBands(reg_offset) \
                .addBands(reg_angle)

            return registered

        # SWITCH
        if coRegistration is True:
            print('--- Image swath co-registration applied ---')
            # apply the registration function
            S2_sr = S2regFunc(S2_sr)
        if coRegistrationPrecalculated is True:
            print('--- Image swath co-registration from precalculated dx dy is applied ---')
            # apply the registration function

            S2_sr = S2regprecalcFunc(S2_sr, day_to_process, dxdy_collection,orbit)

        ##############################
        # EXPORT

        # extract the date and time (it is same time for all images in the mosaic)
        sensing_date = S2_sr.get('system:index').getInfo()[0:15]
        sensing_date_read = sensing_date[0:4] + '-' + \
            sensing_date[4:6] + '-' + sensing_date[6:15]

        # Add Source to fullfill Copernicus requirements:
        S2_sr = S2_sr.set(
            'DATA_SOURCE', "Contains modified Copernicus Sentinel data "+day_to_process[:4])

        # define the export aoi

        # # mask the zero values outside the satellite footprint
        # # Pixels are not zeros, return zeros
        # zeros = S2_sr.Not()
        # # Pixels are zeros, return ones
        # ones = zeros.Not()
        # # Vectorize the ones mask image
        # vectorized_ones = ones.reduceToVectors()

        # the full mosaic image geometry covers larger areas outside Switzerland that are not needed

        aoi_img = S2_sr.geometry()
        # therefore it is clipped with rectangle to keep the geometry simple
        # the alternative clip with aoi_CH would be computationally heavier
        aoi_exp = aoi_img.intersection(aoi_CH_simplified)  # alternativ': aoi_CH
        # aoi_exp = aoi_img.intersection(aoi_CH_simplified).intersection(
        #     vectorized_ones)  # alternativ': aoi_CH

        # SWITCH export
        if export10mBands is True:
            print('Launching export for 10m bands')
            # define the filenames
            fname_10m = 'S2-L2A_mosaic_' + sensing_date_read + '_bands-10m'
            band_list_10m = ['B2', 'B3', 'B4', 'B8']
            if exportMasks:
                band_list_10m.extend(
                    ['terrainShadowMask', 'cloudAndCloudShadowMask'])
            if exportRegLayers:
                band_list_10m.extend(['reg_dx', 'reg_dy', 'reg_confidence'])
            if exportS2cloud:
                band_list_10m.extend(['cloudProbability'])
            print('Band list: {}'.format(band_list_10m))
            # Export COG 10m bands
            task = ee.batch.Export.image.toAsset(
                image=S2_sr.select(band_list_10m).clip(
                    aoi_exp).set('pixel_size_meter', 10),
                scale=10,
                description=task_description + '_10m'+ 'Orbit: '+str(orbit),
                crs='EPSG:2056',
                region=aoi_exp,
                maxPixels=1e10,
                assetId=collection + '/' + fname_10m,
            )
            task.start()

        # SWITCH export
        if export20mBands is True:
            print('Launching export for 20m bands')
            # define the filenames
            fname_20m = 'S2-L2A_mosaic_' + sensing_date_read + '_bands-20m'
            band_list_20m = ['B8A', 'B11', 'B5']
            print('Band list: {}'.format(band_list_20m))
            # Export COG 20m bands
            task = ee.batch.Export.image.toAsset(
                image=S2_sr.select(band_list_20m).clip(
                    aoi_exp).set('pixel_size_meter', 20),
                scale=20,
                description=task_description + '_20m'+ 'Orbit: '+str(orbit),
                crs='EPSG:2056',
                region=aoi_exp,
                maxPixels=1e10,
                assetId=collection + '/' + fname_20m
            )
            task.start()

        """"
        # SWITCH export
        if export60mBands is True:
            print('Launching export for 60m bands')
            fname_60m = 'S2-L2A_Mosaic_' + sensing_date_read + '_Bands-60m'
            band_list_60m = ['B1', 'B9', 'B10']
            print('Band list: {}'.format(band_list_60m))
            task = ee.batch.Export.image.toAsset(
                image=S2_sr.select(band_list_60m).clip(aoi_exp),
                scale=60,
                description=task_description + '_60m',
                crs='EPSG:2056',
                region=aoi_exp,
                maxPixels=1e10,
                assetId=collection + '/' + fname_60m
            )
            task.start()
        """
>>>>>>> main
