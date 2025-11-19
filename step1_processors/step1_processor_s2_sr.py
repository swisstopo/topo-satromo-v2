import subprocess
import numpy as np
from datetime import datetime
import configuration as config
from main_functions import main_utils, main_publish_stac_fsdi, main_coregistration, main_reprojection, main_mosaicing
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
# 8. [TODO] Co-registration with AROSICS
# 9. [TODO] STAC catalog generation
#
# The script processes one mosaic image per day with automated quality checks and error handling.


def process_product_s2_sr(day_to_process: str, collection: str) -> None:

    ##############################
    # SWITCHES
    # The switches enable / disable the execution of individual steps in this script

    # options': True, False - defines if we store the original data to S3 as backup
    s3_backup = False

    ##############################
    # TIME
    # define a date or use the current date:

    # start_date = datetime.strptime(day_to_process, '%Y-%m-%d')
    # end_date = start_date + timedelta(days=1)

    ##############################
    # SPACE
    # Official swisstopo boundaries
    # source: https:#www.swisstopo.admin.ch/de/geodata/landscape/boundaries3d.html#download
    # TODO use the full resolution for final processing
    #aoi_CH = FULLRES

    # Simplified version for faster processing
    aoi_CH_simplified = os.path.join("assets", "swissboundary_simplified_4326.json")

    ##############################
    # REFERENCE DATA

    # # SPOT swissimage Reference Image (contains the red spectral band in 10 m resolution))
    # # source: TODO
    # # processing: TODO
    #ref_data = TODO

    # # TERRAIN SHADOW - based on a very precise digital surface  model in a 10 m resolution
    # # source: LIDAR, Provided by GANDOR
    # # processing: TODO
    # terrain_shadow_collection = TODO

    ##############################
    # SATELLITE DATA

    # # Copernicus Collection
    copernicus_collection = "sentinel-2-l2a"
    # # Baseline Version greater than
    baseline_version = "04.00"
    # # Processing Level
    processing_level = "L2A"
    # # Bucket
    copernicus_bucket = "eodata"

    ##############################
    # Test if corresponing Cloudscope+ data is in  empty asset list
    no_csplus=main_utils.is_date_in_empty_asset_list(config.PRODUCT_S2_LEVEL_2A['step0_collection'], day_to_process)

    if no_csplus:
        if main_utils.is_date_in_empty_asset_list(config.PRODUCT_S2_LEVEL_2A['image_collection'], day_to_process) is False:
            write_asset_as_empty(config.PRODUCT_S2_LEVEL_2A['image_collection'], day_to_process, 'No CloudScore+ data available')
        return


    ##############################
    #IMAGE SEARCH


    def copernicus_image_search(date, copernicus_collection , aoi, processing_level, baseline_version):

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
            list: A list of STAC items (dicts) matching the search criteria, filtered by processing level and baseline version.
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
        "limit": 1000
        }
        #print("Sending POST request to STAC API...")
        #print(f"Query: {json.dumps(query_body, indent=2)}")

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

        # filter for  processing level
        search_result = [
            item for item in items
            if item['properties'].get('processing:version', '00.00') > baseline_version
        ]

        return search_result

    # Perform the scene search
    search_result = copernicus_image_search(date=day_to_process, copernicus_collection =copernicus_collection,  aoi=aoi_CH_simplified, processing_level=processing_level, baseline_version=baseline_version)

    # Check if we have data at all
    if len(search_result) == 0:
        write_asset_as_empty(collection, day_to_process, 'No candidate scene')
        return

    # TODO check if already in  stac and check if online is a new processor / baseline

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


    # TODO check if tile is mostly no data and in case of multiple identical tileIDs verify if their merge is not full of no data: to solve this with A B and C: Download all tiles, check with a 60m band if the orbit covers Switzerland and no area is "empty": meaning that the area in the orbit intersection with Switzerland has no/little no data

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

    def move_copernicus_data(temp_folder, collection_folder):
        """
        Process Copernicus data folders and copy relevant JP2 files.

        Args:
            temp_folder (str): Path to the temp folder containing Copernicus folders
            collection_folder (str): Base path for the collection output folder
        """

        # Define the file endings we're looking for based on the config
        target_endings = [f'{band}_{res}m.jp2' for res, bands in config.SENTINEL2_BAND_CONFIG.items() for band in bands]

        # Find all subdirectories in temp folder that match Sentinel-2 naming pattern
        pattern = f"{temp_folder}/**/*.SAFE"
        sentinel_folders = glob.glob(pattern, recursive=True)

        if not sentinel_folders:
            print(f"No Sentinel-2 folders found in {temp_folder}")
            return

        # print(f"Found {len(sentinel_folders)} Sentinel-2 folders")

        for folder_path in sentinel_folders:
            if not os.path.isdir(folder_path):
                continue

            folder_name = os.path.basename(folder_path)
            # print(f"\nProcessing folder: {folder_name}")

            # Parse folder name to extract orbit and date
            orbit, date = parse_copernicus_folder_name(folder_name)

            if orbit is None or date is None:
                print(f"Skipping folder {folder_name} - could not parse name")
                continue

            # print(f"  Orbit: {orbit}, Date: {date}")

            # Create output directory
            output_dir = os.path.join(collection_folder, orbit, date)
            os.makedirs(output_dir, exist_ok=True)
            # print(f"  Output directory: {output_dir}")

            # Find all T*.jp2 files in the folder (including subdirectories)
            jp2_pattern = os.path.join(folder_path, "**", "T*.jp2")
            jp2_files = glob.glob(jp2_pattern, recursive=True)

            copied_count = 0

            for jp2_file in jp2_files:
                file_name = os.path.basename(jp2_file)

                # Check if file ends with any of our target endings
                for ending in target_endings:
                    if file_name.endswith(ending):
                        try:
                            destination = os.path.join(output_dir, file_name)
                            shutil.move(jp2_file, destination)
                            # print(f"    Moved: {file_name}")
                            copied_count += 1
                            break  # Found a match, no need to check other endings
                        except Exception as e:
                            print(f"    Error copying {file_name}: {e}")
                            return 0

            #print(f"  Total files copied: {copied_count}")
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
                filename = f"swisseo_s2-sr_v200_mosaic_{earliest_timestamp}_metadata.json"
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
    # Copy corresponding CS+ to local
    # TODO maybe to reduce cost: Check if the URL exists before copying with requests and not with boto: this is to reduce the costs of s3 access. keep in mind that we want in this case access the CF distribution on int and not on prod

    # S3 config
    s3_path_info = config.PRODUCT_S2_LEVEL_CSPLUS['step0_collection']
    path_parts = s3_path_info[5:].split('/', 1)
    bucket_name = path_parts[0]
    s3_prefix = path_parts[1] if len(path_parts) > 1 else ""

    # Pattern to match JP2 filename format: T31TGM_20250423T104041_AOT_60m.jp2
    jp2_pattern = r'^([A-Z0-9]{6})_(\d{8}T\d{6})_.*\.jp2$'

    # CloudScore+ pattern: both .tif and _metadata.json files
    def make_cloudscore_pattern(timestamp, tile_id):
        return rf'.*{re.escape(timestamp)}_.*_{re.escape(tile_id)}_.*(_metadata\.json|\.tif)$'

    # Find JP2 directories and extract tile info
    tile_to_directory_map = {}

    # Print
    print(f"Downloading {bucket_name} CloudScore+ files")
    for root, dirs, files in os.walk(copernicus_collection):
        jp2_files = [f for f in files if f.lower().endswith('.jp2')]

        if jp2_files:
            #print(f"Processing directory: {root}")

            # Extract tile info from first matching JP2 file per tile
            for filename in jp2_files:
                match = re.match(jp2_pattern, filename, re.IGNORECASE)
                if match:
                    tile_id = match.group(1)
                    timestamp = match.group(2)
                    tile_key = (tile_id, timestamp)

                    if tile_key not in tile_to_directory_map:
                        tile_to_directory_map[tile_key] = root
                        # print(f"Found tile {tile_id}, timestamp {timestamp}")

    # Process each tile and download CloudScore+ files
    if tile_to_directory_map:
        # print(f"\nProcessing {len(tile_to_directory_map)} unique tile/timestamp combinations")

        for (tile_id, timestamp), source_directory in tile_to_directory_map.items():
            # print(f"\nSearching CloudScore+ for {tile_id}, {timestamp} -> {source_directory}")

            # Search S3 for matching files
            paginator = main_utils.s3.get_paginator('list_objects_v2')
            pattern = make_cloudscore_pattern(timestamp, tile_id)
            matching_files = []

            try:
                for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_prefix):
                    if 'Contents' in page:
                        for obj in page['Contents']:
                            filename = os.path.basename(obj['Key'])
                            if re.search(pattern, filename, re.IGNORECASE):
                                matching_files.append(obj['Key'])
            except Exception as e:
                print(f"Error searching S3: {e}")
                write_asset_as_empty(collection, day_to_process, 'Error searching CloudsScore tile')
                return

            # Download matching files
            if matching_files:
                #print(f"Downloading {len(matching_files)} CloudScore+ files")
                os.makedirs(source_directory, exist_ok=True)

                for key in matching_files:
                    try:
                        filename = os.path.basename(key)
                        local_path = os.path.join(source_directory, filename)
                        main_utils.s3.download_file(bucket_name, key, local_path)
                        #print(f"Downloaded {filename}")
                    except Exception as e:
                        print(f"Error downloading {key}: {e}")
                        write_asset_as_empty(collection, day_to_process, 'Error downloading CloudsScore tile')
                        return
            else:
                print(f"No CloudScore+ files found")
                write_asset_as_empty(collection, day_to_process, 'missing CloudsScore tile')
                return
    else:
        print("No valid JP2 files found")
        write_asset_as_empty(collection, day_to_process, 'No valid local tiles files found')
        return

    ##############################
    # Add CS+ metadata to each orbit's metadata JSON file

    for orbit_num, timestamp in orbit_timestamp.items():
        ts=timestamp.replace('-', '')[:8]
        orbit_dir = os.path.join(copernicus_collection, f"R{int(orbit_num):03d}", ts)
        metadata_filename = f"swisseo_s2-sr_v200_mosaic_{timestamp}_metadata.json"
        metadata_path =  metadata_filename

        # Find all _metadata.json files for CloudScore+ in the orbit directory
        csplus_metadata_files = []
        if os.path.exists(orbit_dir):
            csplus_metadata_files = [os.path.join(orbit_dir, f)
                                   for f in os.listdir(orbit_dir)
                                   if f.endswith("_metadata.json")]

        # Build the SOURCE_CLOUDSCOREPLUS structure
        source_csplus = {"GRANULES": {}}
        for cs_file in csplus_metadata_files:
            try:
                with open(cs_file, "r") as f:
                    csplus_data = json.load(f)
                # Extract granule ID from filename without _metadata.json and path
                granule_id = os.path.basename(cs_file).split("_metadata.json")[0]
                source_csplus["GRANULES"][granule_id] = csplus_data
            except Exception as e:
                print(f"Error reading CS+ metadata {cs_file}: {e}")

        # Update the orbit metadata with CloudScore+ data
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r") as f:
                    orbit_metadata = json.load(f)
                orbit_metadata["SOURCE_CLOUDSCOREPLUS"] = source_csplus
                with open(metadata_path, "w") as f:
                    json.dump(orbit_metadata, f, indent=2)
            except Exception as e:
                print(f"Error updating metadata file {metadata_path}: {e}")




    ##############################
    # TODO TERRAINSHADOWMASK

    breakpoint()
    ##############################
    # TODO COREGISTRATION AROSICS
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

        # Creating cloud mask with omnicloudmask
        # Build the argument list
        cmd = [
            config.AROSICS_CONFIG['omnicloudmask_venv_path'],
            config.AROSICS_CONFIG['omnicloudmask_script_path'],
            "--orbit", str(orbit_nr),
            "--date", acquisition_date,
            "--output-dir", copernicus_collection
        ]

        # Run the command
        result = subprocess.run(cmd, check=True)

        main_mosaicing.create_sentinel2_cloud_mosaic(acquisition_date=acquisition_date, orbit_nr=orbit_nr)
        main_mosaicing.equalize_all_extents(acquisition_date=acquisition_date, orbit_nr=orbit_nr)
        success, pickle_path = main_coregistration.coregister_S2(acquisition_date=acquisition_date, orbit_nr=orbit_nr)

        if success:
            main_coregistration.deshift_files(
                acquisition_date=acquisition_date,
                orbit_nr=orbit_nr,
                pickle_path=pickle_path,
                fmt_out='GTIFF',
                CPUs=64
            )

    ##############################
    # Move results to intermediate_data folder

    # shutil.move is FUBAR in case of nested folders with the same name
    # shutil.move(copernicus_collection, "/mnt/c/Users/Localadmin/Documents/SATROMO/intermediate_data/")

    source = copernicus_collection
    dest = "/mnt/c/Users/Localadmin/Documents/SATROMO/intermediate_data/SENTINEL-2"

    os.makedirs(dest, exist_ok=True)

    for item in os.listdir(source):
        src_path = os.path.join(source, item)
        dst_path = os.path.join(dest, item)

        if os.path.exists(dst_path):
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                shutil.rmtree(src_path)
            else:
                os.replace(src_path, dst_path)
        else:
            shutil.move(src_path, dst_path)

    os.rmdir(source)

    ##############################
    # TODO Upload pickle to S3

    ##############################
    # TODO Update METADATA json with
    # - coregistration status
    # - cloudshadow percentage

    ##############################
    # TODO Clip Data to Switzerland and Reproeject to CH1903LV95



    def clip_resample_to_cog(
        input_tif,
        clipfile,
        nodata_value=None,
        epsg=2056,
        lossy=False,
        quality=85,
        oversample_factor=5
        ):
        """
        Clips, resamples and converts raster to COG format using multi-step oversampling.
        Process: 5x oversample (nearest) -> bilinear reproject -> 5x downsample (bilinear)
        As decided on 02.04.2025 with AGROSCOPE team.
        Uses only ONE temporary file to minimize disk usage.

        Resolution and datatype are automatically detected from input file.

        Args:
            input_tif: Path to input raster (will be replaced with processed version)
            clipfile: Path to clip shapefile/geojson
            nodata_value: NoData value (optional)
            datatype: GDAL data type (e.g. Float32, Int16, Byte) - auto-detected if None
            epsg: EPSG code for coordinate system
            lossy: True for JPEG compression, False for DEFLATE compression
            quality: JPEG quality (1-100), only relevant if lossy=True
            oversample_factor: Oversampling factor (default: 5)
        """

        # Read original resolution and datatype from input file
        with rasterio.open(input_tif) as src:
            # Get pixel size (resolution) - using absolute values
            original_res_x = abs(src.transform[0])
            original_res_y = abs(src.transform[4])
            resolution = int(max(original_res_x, original_res_y))  # Use the larger value

            # Get datatype if not specified
            #if datatype is None:
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
            rasterio_dtype = str(src.dtypes[0])
            datatype = dtype_map.get(rasterio_dtype, 'Float32')

        print(f"Detected original resolution: {resolution}m")
        print(f"Using datatype: {datatype}")

        # Create single temp file path
        input_path = Path(input_tif)
        temp_file = input_path.parent / f"{input_path.stem}_temp{input_path.suffix}"

        try:
            # Calculate intermediate resolution
            intermediate_res = resolution / oversample_factor

            print(f"\n=== Step 1: Clipping and oversampling to {intermediate_res}m with nearest neighbour (NO reprojection) ===")

            # Step 1: Clip and oversample with nearest neighbour (keep original projection)
            cmd_oversample = [
                "gdalwarp",
                "-cutline", str(clipfile),
                "-crop_to_cutline",
                "-of", "GTiff",
                "-co", "TILED=YES",
                "-co", "BIGTIFF=YES",
                "-co", "COMPRESS=DEFLATE",
                "-co", "NUM_THREADS=ALL_CPUS",
                "-tr", str(intermediate_res), str(intermediate_res),
                "-r", "near",
                "-ot", datatype,
                "-overwrite"
            ]

            if nodata_value is not None:
                cmd_oversample.extend(["-dstnodata", str(nodata_value)])

            cmd_oversample.extend([str(input_tif), str(temp_file)])

            print(f"Command: {' '.join(cmd_oversample)}")
            result = subprocess.run(cmd_oversample, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Error: {result.stderr}")
                raise Exception(f"Oversampling failed with code {result.returncode}")

            print(f"✓ Oversampled and clipped file created: {temp_file}")

            # Step 2: Reproject with bilinear (at oversampled resolution)
            print(f"\n=== Step 2: Reprojecting to EPSG:{epsg} with bilinear at {intermediate_res}m ===")

            cmd_reproject = [
                "gdalwarp",
                "-t_srs", f"EPSG:{epsg}",
                "-of", "GTiff",
                "-co", "TILED=YES",
                "-co", "BIGTIFF=YES",
                "-co", "COMPRESS=DEFLATE",
                "-co", "NUM_THREADS=ALL_CPUS",
                "-tr", str(intermediate_res), str(intermediate_res),
                "-r", "bilinear",
                "-ot", datatype,
                "-overwrite"
            ]

            if nodata_value is not None:
                cmd_reproject.extend(["-dstnodata", str(nodata_value)])

            # Use temp_file as both input and output (via intermediate step)
            cmd_reproject.extend([str(temp_file), str(input_tif)])

            print(f"Command: {' '.join(cmd_reproject)}")
            result = subprocess.run(cmd_reproject, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Error: {result.stderr}")
                raise Exception(f"Reprojection failed with code {result.returncode}")

            # Move result back to temp_file for next step
            shutil.move(str(input_tif), str(temp_file))
            print(f"✓ Reprojected file ready")

            # Step 3: Resample (downsample) with bilinear to final resolution and convert to COG
            print(f"\n=== Step 3: Resampling to {resolution}m with bilinear and COG conversion ===")

            # Calculate output size based on resolution change
            # gdal_translate uses -outsize percentage or pixel dimensions
            outsize_percent = int((intermediate_res / resolution) * 100)

            cmd_downsample = [
                "gdal_translate",
                "-of", "COG",
                "-co", "NUM_THREADS=ALL_CPUS",
                "-co", "BIGTIFF=YES",
                "-outsize", f"{outsize_percent}%", f"{outsize_percent}%",
                "-r", "bilinear",
                "-ot", datatype
            ]

            # Compression options
            if lossy:
                print(f"Using JPEG compression with quality {quality}")
                cmd_downsample.extend([
                    "-co", "COMPRESS=JPEG",
                    "-co", f"QUALITY={quality}"
                ])
            else:
                print(f"Using lossless DEFLATE compression")
                cmd_downsample.extend([
                    "-co", "COMPRESS=DEFLATE",
                    "-co", "PREDICTOR=2",
                    "-co", "ZLEVEL=2"
                ])

            if nodata_value is not None:
                cmd_downsample.extend(["-a_nodata", str(nodata_value)])

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
    # TODO Clip Data to Switzerland and Reproeject to CH1903LV95

    clip_resample_to_cog("swisseo_s2-sr_v200_mosaic_2025-06-01t101041_tci_10m.tif",os.path.join("assets","swissboundary_buffer_5000m.gpkg"),nodata_value=None,epsg=2056,lossy=True,quality=85,oversample_factor=5)

    print("end of function")
    ##############################
    # TODO Upload to STAC
    #upload the  file swisseo_s2-sr_v200_mosaic_2025-06-10t103641_cloudprobability-10.tif to STAC: collection swisseo_s2-sr_v200 raw_asset is swisseo_s2-sr_v200_mosaic_2025-06-10t103641_cloudprobability-10.tif raw_item is 2025-06-10t103641 colelction is swisseo_s2-sr_v200 geocat_id is 6e8f3f3e-1d4e-11ee-be56-0242ac120002, current is none
    main_publish_stac_fsdi.publish_to_stac("swisseo_s2-sr_v200_mosaic_2025-06-01t101041_scl_20m.tif",
        "2025-06-01t101041",
        "ch.swisstopo.swisseo_s2-sr_v200",
        config.PRODUCT_S2_LEVEL_2A['geocat_id'],
        None
    )

    ##############################
    # TODO Upload to GEE

    print("end of function")
