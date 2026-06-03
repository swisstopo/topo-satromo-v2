import multiprocessing
import torch
import subprocess
import numpy as np
from datetime import datetime
import configuration as config
from main_functions import main_utils, main_publish_stac_fsdi, main_coregistration, main_reprojection, main_mosaicing,main_thumbnails,main_create_rgb,main_cloudpercentage,main_omnicloudmask,main_terrain_module,main_terrain_parallel
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


from step0_processors.step0_utils import write_asset_as_empty

# Processing pipeline for daily Sentinel-2 L2A surface reflectance (sr) mosaics over Switzerland

##############################
# INTRODUCTION
# This script provides a tool to preprocess Sentinel-2 L2A surface reflectance (sr) data over Switzerland.
# It performs automated downloads from Copernicus Data Space, organizes files into orbit groups,
# integrates CloudScore+ data, and prepares data for further processing.
#

##############################
# CONTENT
# The switches enable / disable the execution of individual steps in this script

# This script includes the following steps:
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

    ##############################
    # SWITCHES
    # The switches enable / disable the execution of individual steps in this script

    # options': True, False - defines if we store the original data to S3 as backup
    s3_backup = False # backup copernicus tiles data to S3
    gpu_check = config.GPU_ENFORCEMENT # AROSICS : on Prod we use True to enforce GPU usage, on Dev we use False to allow CPU fallback (only for testing purposes)

    ##############################
    # TIME
    # define a date or use the current date:

    # start_date = datetime.strptime(day_to_process, '%Y-%m-%d')
    # end_date = start_date + timedelta(days=1)

    ##############################
    # SPACE
    # Official swisstopo boundaries
    # source: https:#www.swisstopo.admin.ch/de/geodata/landscape/boundaries3d.html#download
    # Simplified version for faster processing
    aoi_CH_simplified = os.path.join("assets", "swissboundary_simplified_4326.json")

    ##############################
    # REFERENCE DATA

    # # TERRAIN SHADOW - based on a very precise digital surface  model in a 10 m resolution
    # # source: LIDAR, Provided by GANDOR
    # # processing: TODO
    # terrain_shadow_collection = TODO

    ##############################
    # SATELLITE DATA

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
                            return

        # Print download statistics
        return dl_stats


    dl_stats=copernicus_download(main_utils.copernicus_s3.Bucket(copernicus_bucket), search_result=search_result, target="temp")


    # Check if we have a failed download
    if dl_stats is None or dl_stats[1] != 0:
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

            gdal_merge_cmd = shutil.which("gdal_merge.py") or shutil.which("gdal_merge") or "gdal_merge.py"
            command = [
                gdal_merge_cmd,
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
        orbit_num_int = int(orbit_num)
        if (orbit_num_int in [8, 22] and cloudcover > 85.0) or (orbit_num_int in [108, 65] and cloudcover > 95.0):
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
            # Build per-class probability masks (initially 0/1) for resampling for categorical (SCL) DATA
            # The name comes from digital circuit design. https://en.wikipedia.org/wiki/One-hot
            # At any given time, exactly one bit is "hot" (set to 1) and all others are 0.
            # Applied to SCL data: for a pixel with class 4, band 4 = 1 and all other 11 bands = 0.
            # Without one-hot encoding, bilinear interpolation between SCL class 2 (dark vegetation)
            # and class 8 (cloud medium probability) would produce a meaningless average of 5.
            # =========================================================================
            is_scl = "_scl_" in str(input_path.name).lower()

            if is_scl:
                print("\n=== SCL Data Detected: Applying Consistent 3-Step Process with One-Hot Encoding ===")

                # -------------------------------------------------------------------------
                def calculate_chunk_rows(image_width, num_bands=1, dtype_bytes=1, ram_fraction=0.10):
                    """
                    Berechnet chunk_rows basierend auf verfuegbarem System-RAM.

                    Args:
                        image_width:  Bildbreite in Pixeln
                        num_bands:    Anzahl Baender (1 fuer normales Bild, 12 fuer One-Hot)
                        dtype_bytes:  Bytes pro Pixel (uint8=1, float32=4)
                        ram_fraction: Anteil des verfuegbaren RAM nutzen (0.10 = 10%)

                    Returns:
                        int: chunk_rows, mindestens 100, hoechstens 5000
                    """
                    # Verfuegbaren RAM lesen
                    try:
                        # Linux: /proc/meminfo
                        with open("/proc/meminfo", "r") as f:
                            for line in f:
                                if line.startswith("MemAvailable:"):
                                    available_bytes = int(line.split()[1]) * 1024  # kB -> Bytes
                                    break
                    except FileNotFoundError:
                        # Windows/Mac Fallback
                        try:
                            import psutil
                            available_bytes = psutil.virtual_memory().available
                        except ImportError:
                            available_bytes = 4 * 1024 ** 3  # 4 GB konservativer Fallback
                            print(f"  RAM-Erkennung nicht moeglich, verwende 4 GB als Fallback")

                    # Nutzbaren RAM berechnen
                    usable_bytes  = available_bytes * ram_fraction
                    bytes_per_row = image_width * num_bands * dtype_bytes
                    chunk_rows    = int(usable_bytes / bytes_per_row)

                    # Grenzen setzen: mindestens 100, hoechstens 5000
                    chunk_rows = max(100, min(chunk_rows, 5000))

                    available_gb = available_bytes / 1024 ** 3
                    usable_gb    = usable_bytes    / 1024 ** 3
                    print(f"  RAM verfuegbar:                    {available_gb:.1f} GB")
                    print(f"  RAM fuer diesen Schritt ({ram_fraction*100:.0f}%):   {usable_gb:.1f} GB")
                    print(f"  Bildbreite:                        {image_width} Pixel")
                    print(f"  Baender x Bytes pro Pixel:         {num_bands} x {dtype_bytes}")
                    print(f"  chunk_rows:                        {chunk_rows}")

                    return chunk_rows

                # -------------------------------------------------------------------------
                def write_onehot_chunked(input_file, output_file, num_classes=12, chunk_rows=1000):
                    """
                    Liest das SCL-Bild zeilenweise und schreibt num_classes One-Hot-Baender.
                    RAM-Verbrauch pro Chunk: chunk_rows * Bildbreite * num_classes * 1 Byte

                    Args:
                        input_file:  Pfad zum geclippten + oversampleten SCL-Bild (1 Band, uint8)
                        output_file: Pfad zur One-Hot-Ausgabedatei (12 Baender, uint8)
                        num_classes: Anzahl SCL-Klassen (Standard: 12 fuer Sentinel-2)
                        chunk_rows:  Anzahl Zeilen pro Verarbeitungsblock

                    Returns:
                        str: original_dtype des Eingabebildes (fuer spaeteren Argmax-Schritt)
                    """
                    with rasterio.open(input_file) as src:
                        width          = src.width
                        height         = src.height
                        original_dtype = src.dtypes[0]
                        meta           = src.meta.copy()
                        meta.update(
                            count=num_classes,
                            dtype='uint8',
                            nodata=None,
                            compress='deflate',
                            tiled=True,
                            blockxsize=512,
                            blockysize=512
                        )

                        with rasterio.open(output_file, 'w', **meta) as dst:
                            for row_start in range(0, height, chunk_rows):
                                row_end = min(row_start + chunk_rows, height)

                                window = rasterio.windows.Window(
                                    col_off=0,
                                    row_off=row_start,
                                    width=width,
                                    height=row_end - row_start
                                )

                                # Nur diesen Zeilenblock lesen: (row_count, width) uint8
                                data_chunk = src.read(1, window=window)

                                # Jede Klasse einzeln kodieren und sofort schreiben
                                for class_idx in range(num_classes):
                                    band_data = (data_chunk == class_idx).astype('uint8')
                                    dst.write(band_data, class_idx + 1, window=window)

                                pct = (row_end / height) * 100
                                print(f"  One-Hot Encoding: {pct:.0f}%  ({row_end}/{height} Zeilen)", end='\r')

                    print(f"\n✓ Step A: One-Hot Datei erstellt ({num_classes} Baender): {output_file}")
                    return original_dtype

                # -------------------------------------------------------------------------
                def write_argmax_chunked(input_file, output_file, original_dtype, nodata_value, chunk_rows=1000):
                    """
                    Liest das 12-Band Float32-Bild zeilenweise und schreibt das Argmax-Ergebnis.
                    RAM-Verbrauch pro Chunk: chunk_rows * Bildbreite * 12 * 4 Byte (Float32)

                    Args:
                        input_file:     Pfad zum downgesampleten 12-Band Float32-Bild
                        output_file:    Pfad zur Argmax-Ausgabedatei (1 Band, original_dtype)
                        original_dtype: Ziel-Datentyp (uint8 fuer SCL)
                        nodata_value:   NoData-Wert
                        chunk_rows:     Anzahl Zeilen pro Verarbeitungsblock
                    """
                    with rasterio.open(input_file) as src:
                        width  = src.width
                        height = src.height
                        meta   = src.meta.copy()
                        meta.update(count=1, dtype=original_dtype)
                        if nodata_value is not None:
                            meta.update(nodata=nodata_value)

                        with rasterio.open(output_file, 'w', **meta) as dst:
                            for row_start in range(0, height, chunk_rows):
                                row_end = min(row_start + chunk_rows, height)

                                window = rasterio.windows.Window(
                                    col_off=0,
                                    row_off=row_start,
                                    width=width,
                                    height=row_end - row_start
                                )

                                # Alle 12 Baender fuer diesen Block: (12, row_count, width) float32
                                block = src.read(window=window)

                                # Klasse mit hoechstem Anteil gewinnt
                                result = np.argmax(block, axis=0).astype(original_dtype)
                                dst.write(result, 1, window=window)

                                pct = (row_end / height) * 100
                                print(f"  Argmax: {pct:.0f}%  ({row_end}/{height} Zeilen)", end='\r')

                    print(f"\n✓ Step B: Argmax Datei erstellt: {output_file}")

                # -------------------------------------------------------------------------

                try:
                    # Temp-Dateipfade
                    temp_1_os       = input_path.parent / f"{input_path.stem}_temp1_os.tif"
                    onehot_init     = input_path.parent / f"{input_path.stem}_onehot_init.tif"
                    temp_2_rp       = input_path.parent / f"{input_path.stem}_temp2_rp.tif"
                    temp_3_ds       = input_path.parent / f"{input_path.stem}_temp3_ds.tif"
                    recombined_file = input_path.parent / f"{input_path.stem}_recombined.tif"

                    temp_files_to_clean.extend([temp_1_os, onehot_init, temp_2_rp, temp_3_ds, recombined_file])

                    NUM_CLASSES = 12  # Sentinel-2 SCL Klassen 0-11

                    # --- Step 1: Einzelnes SCL-Band oversampeln + clippen (nearest, guenstig) ---
                    print(f"\n--- SCL Step 1: Clipping + oversampling einzelnes SCL-Band auf {intermediate_res}m (near) ---")
                    cmd_os = [
                        "gdalwarp", "-cutline", str(clipfile), "-of", "GTiff",
                        "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-co", "COMPRESS=DEFLATE",
                        "-tr", str(intermediate_res), str(intermediate_res),
                        "-r", "near", "-ot", "Byte", "-overwrite",
                        str(input_tif), str(temp_1_os)
                    ]
                    subprocess.run(cmd_os, check=True, capture_output=True)
                    print(f"✓ Step 1: Einzelnes SCL-Band geclippt + overgesampelt auf {intermediate_res}m")

                    # --- Step A: One-Hot Kodierung zeilenweise (RAM-schonend) ---
                    print(f"\n--- SCL Step A: One-Hot Encoding {NUM_CLASSES} Klassen (chunked, RAM-schonend) ---")

                    # Bildbreite fuer RAM-Berechnung auslesen
                    with rasterio.open(temp_1_os) as src:
                        img_width_a = src.width

                    # RAM-Verbrauch pro Zeile: Bildbreite * 12 Baender * 1 Byte (uint8)
                    chunk_rows_a = calculate_chunk_rows(
                        image_width=img_width_a,
                        num_bands=NUM_CLASSES,
                        dtype_bytes=1,           # uint8
                        ram_fraction=0.10
                    )

                    original_dtype = write_onehot_chunked(
                        input_file=str(temp_1_os),
                        output_file=str(onehot_init),
                        num_classes=NUM_CLASSES,
                        chunk_rows=chunk_rows_a
                    )

                    # --- Step 2: 12 Baender nach Ziel-CRS umprojizieren (bilinear, intermediate res) ---
                    # -ot Float32 erforderlich: bilinear erzeugt Bruchwerte 0.0-1.0
                    print(f"\n--- SCL Step 2: Reprojizierung {NUM_CLASSES} Baender nach EPSG:{epsg} @ {intermediate_res}m (bilinear) ---")
                    cmd_rp = [
                        "gdalwarp", "-t_srs", f"EPSG:{epsg}", "-of", "GTiff",
                        "-co", "TILED=YES", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-to", "ALLOW_BALLPARK=NO", "-to", "ONLY_BEST=YES",
                        "-co", "COMPRESS=DEFLATE",
                        "-tr", str(intermediate_res), str(intermediate_res),
                        "-r", "bilinear", "-ot", "Float32", "-overwrite",
                        str(onehot_init), str(temp_2_rp)
                    ]
                    subprocess.run(cmd_rp, check=True, capture_output=True)
                    print(f"✓ Step 2: Reprojiziert nach EPSG:{epsg} @ {intermediate_res}m")

                    # onehot_init sofort loeschen (Disk freigeben)
                    if onehot_init.exists():
                        onehot_init.unlink()
                        print(f"  Zwischendatei geloescht: {onehot_init.name}")

                    # --- Step 3: Auf finale Aufloesung runtersampeln (bilinear) ---
                    print(f"\n--- SCL Step 3: Downsampling {NUM_CLASSES} Baender auf {resolution}m (bilinear) ---")
                    cmd_ds = [
                        "gdalwarp", "-of", "GTiff", "-co", "BIGTIFF=YES",
                        "-co", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
                        "-co", "COMPRESS=DEFLATE",
                        "-tr", str(resolution), str(resolution), "-tap",
                        "-r", "bilinear", "-ot", "Float32", "-overwrite",
                        str(temp_2_rp), str(temp_3_ds)
                    ]
                    subprocess.run(cmd_ds, check=True, capture_output=True)
                    print(f"✓ Step 3: Downgesampelt auf {resolution}m")

                    # temp_2_rp sofort loeschen (Disk freigeben)
                    if temp_2_rp.exists():
                        temp_2_rp.unlink()
                        print(f"  Zwischendatei geloescht: {temp_2_rp.name}")

                    # --- Step B: Argmax Rekombination zeilenweise (RAM-schonend) ---
                    print(f"\n--- SCL Step B: Argmax Rekombination -> {original_dtype} (chunked) ---")

                    # Bildbreite fuer RAM-Berechnung auslesen
                    with rasterio.open(temp_3_ds) as src:
                        img_width_b = src.width

                    # RAM-Verbrauch pro Zeile: Bildbreite * 12 Baender * 4 Byte (float32)
                    chunk_rows_b = calculate_chunk_rows(
                        image_width=img_width_b,
                        num_bands=NUM_CLASSES,
                        dtype_bytes=4,           # float32
                        ram_fraction=0.10
                    )

                    write_argmax_chunked(
                        input_file=str(temp_3_ds),
                        output_file=str(recombined_file),
                        original_dtype=original_dtype,
                        nodata_value=nodata_value,
                        chunk_rows=chunk_rows_b
                    )

                    # --- Step C: Finale COG-Konvertierung ---
                    print(f"\n--- SCL Step C: Konvertierung zu finalem COG ---")
                    cmd_cog = [
                        "gdalwarp", "-of", "COG", "-co", "BIGTIFF=YES",
                        "-co", "COMPRESS=DEFLATE",
                        "-co", "PREDICTOR=2", "-co", "NUM_THREADS=ALL_CPUS",
                        "--config", "GDAL_NUM_THREADS", "ALL_CPUS"
                    ]
                    if nodata_value is not None:
                        cmd_cog.extend(["-srcnodata", str(nodata_value), "-dstnodata", str(nodata_value)])

                    cmd_cog.extend([str(recombined_file), str(input_tif), "-overwrite"])
                    subprocess.run(cmd_cog, check=True, capture_output=True)
                    print(f"✓ Finales SCL COG erstellt: {input_tif}")

                except Exception as e:
                    print(f"\n Fehler waehrend SCL-Verarbeitung: {e}")
                    raise e

                finally:
                    # Alle verbleibenden Temp-Dateien aufraumen
                    for f in temp_files_to_clean:
                        if f.exists():
                            print(f"Bereinigung: {f}")
                            f.unlink()

                # SCL-Verarbeitung abgeschlossen
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
        # Terrainshadowmask and incidence angle calculation: pass orbit ans date time and outputfilename
        #     
        terrain_result = main_terrain_parallel.create_terrain_mask(orbit_num,timestamp,f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_terrainmask_10m.tif")                     
        
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

            # Since we generate Terrain manually, add it in the file list
            file_list.append({
                'timestamp': timestamp,
                'band': 'TERRAINMASK',
                'resolution': 10,
                'filename': f"{config.PRODUCT_S2_LEVEL_2A['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_terrainmask_10m.tif"
            })

            # TCI last, resolution descending (bigger first), bands Z to A, so it is alphabetically in STAC
            file_list.sort(key=lambda x: (x['band'] == 'TCI', x['resolution'], x['band']), reverse=True)

            for file_info in file_list:
                band = file_info['band']
                filename = file_info['filename']

                # Get band title using config
                band_names = config.PRODUCT_S2_LEVEL_2A['band_names']

                # Since TCI and TERRAINMASK are generated manually, title generated manually as well
                if band == 'TCI':
                    band_title = "True color image - 10m"
                elif band == 'TERRAINMASK':
                    band_title = "Terrain mask - 10m"
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
            print("Newest dataset detected: updating CURRENT")
            filename_current = re.sub(r'\d{4}-\d{2}-\d{2}t\d{6}', 'current', filename)
            # Rename the file
            os.rename(filename, filename_current)
            main_publish_stac_fsdi.publish_to_stac(filename_current,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],asset_title="Metadata", current=True)
            os.rename(filename_current, filename)

        # Upload Thumbnail
        filename=thumbnail
        main_publish_stac_fsdi.publish_to_stac(filename,timestamp,config.PRODUCT_S2_LEVEL_2A['product_name'],config.PRODUCT_S2_LEVEL_2A['geocat_id'],None,asset_title="Thumbnail")
        if is_current == True:
            print("Newest dataset detected: updating CURRENT")
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
