import numpy as np
from datetime import datetime, timedelta
import configuration as config
from main_functions import main_utils
from collections import defaultdict
import requests
import os
import json
import time
from pathlib import Path
import glob
import shutil
import re

from step0_processors.step0_utils import write_asset_as_empty

# Processing pipeline for daily Sentinel-2 L2A surface reflectance (sr) mosaics over Switzerland

##############################
# INTRODUCTION
# This script provides a tool to preprocess Sentinel-2 L2A surface reflectance (sr) data over Switzerland.
# It can mask clouds and cloud shadows, detect terrain shadows, mosaic images from the same image swath,
# co-register images to the Sentinel-2 Global Reference Image, and export the results.
#

##############################
# CONTENT
# The switches enable / disable the execution of individual steps in this script

# This script includes the following steps:
# 1. Download Data
# 2. Masking clouds and cloud shadows
# 2. Detecting terrain shadows
# 3. Mosaicing of images from the same day (=same orbital track) over Switzerland
# 4. Registering the S2 Mosaic to the Sentinel-2 global reference image
# 5. Exporting spectral bands, additional layers and relevant properties
#
# The script is set up to export one mosaic image per day.


def process_product_s2_sr(day_to_process: str, collection: str) -> None:

    ##############################
    # SWITCHES
    # The switches enable / disable the execution of individual steps in this script

    # options': True, False - defines if we store the original data to S3 as backup
    s3_backup = False
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

    ##############################
    # TIME
    # define a date or use the current date:

    # start_date = datetime.strptime(day_to_process, '%Y-%m-%d')
    # end_date = start_date + timedelta(days=1)

    ##############################
    # SPACE
    # Official swisstopo boundaries
    # source: https:#www.swisstopo.admin.ch/de/geodata/landscape/boundaries3d.html#download
    # processing: reprojected in QGIS to epsg32632
    #aoi_CH = ee.FeatureCollection(
    #    "projects/satromo-prod/assets/res/swissBOUNDARIES3D_1_5_TLM_LANDESGEBIET_dissolve_epsg32632").geometry()
    aoi_CH_simplified = os.path.join("assets", "swissboundary_simplified_4326.json")

    ##############################
    # REFERENCE DATA

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

    ##############################
    # SATELLITE DATA


    # - Query data with pystac, based on perimeter
    # - Download data all orbits with predefined bands
    # - unzip in folder per orbit

    # # Copernicus Collection
    copernicus_collection = "SENTINEL-2"

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
        search_url = "https://catalogue.dataspace.copernicus.eu/stac/search"

        with open(aoi, 'r') as f:
            geojson_data = json.load(f)
        geometry = geojson_data['features'][0]['geometry']


        # Build the query body for SENTINEL2 filter for switzerland and LEVEL2A
        query_body = {
            "filter-lang": "cql2-json",
            "filter": {
                "op": "and",
                "args": [
                    {
                        "op": "=",
                        "args": [
                            {"property": "collection"},
                            copernicus_collection
                        ]
                    },
                    {
                        "op": "s_intersects",
                        "args": [
                            {"property": "geometry"},
                            geometry
                        ]
                    },
                    {
                        "op": "t_intersects",
                        "args": [
                            {"property": "datetime"},
                            {
                                "interval": [
                                    f"{date}T00:00:00Z",
                                    f"{date}T23:59:59Z"
                                ]
                            }
                        ]
                    }
                ]
            },
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

        # remove baseline version and filter for  processing level
        search_result = [item for item in items
                    if ("_"+processing_level+"_" in item['properties'].get('sourceProduct', '') and
                        item['properties']['processorVersion'] > baseline_version)]

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
        orbit_num = item['properties']['relativeOrbitNumber']
        tile_id = item['properties']['tileId']
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
    search_result = [item for item in search_result if item['properties']['relativeOrbitNumber'] in valid_orbits]

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

    def copernicus_download(bucket, search_result: list, target: str = "") -> None:
        """
        Downloads files from an S3 bucket based on search results.
        Iterates over the provided search results, determines the corresponding product prefix,
        and downloads all matching files from the S3 bucket to the specified local target directory.
        Implements retry logic for each file download and prints progress and statistics.
            bucket: boto3 Resource bucket object representing the S3 bucket.
            search_result (list): List of search result dictionaries containing asset information.
            target (str, optional): Local directory to store downloaded files. Should end with a '/'.
                Defaults to the current directory.
        Returns:
            list: Download statistics as [success_count, failure_count].
        Raises:
            FileNotFoundError: If no files are found for a given product.
        """

        # Initialize download statistics
        dl_stats = [0, 0]  # 0: success, 1: failed

        # Define which file we want to download based on the Band configs
        target_endings = [f'{band}_{res}m.jp2' for res, bands in config.SENTINEL2_BAND_CONFIG.items() for band in bands]

        # Create the target dir
        os.makedirs(target, exist_ok=True)

        print(f"Downloading {len(search_result)} tiles from {bucket}...")
        # Loop over the search results
        for i, item in enumerate(search_result):
            #print(f"Downloading tile {i+1} of {len(search_result)} ...")
            product_all = item['assets']['PRODUCT']['alternate']['s3']['href']+"/"
            product = product_all.lstrip("/").split("/", 1)[1]
            files = bucket.objects.filter(Prefix=product)

            if not list(files):
                raise FileNotFoundError(f"Could not find any files for {product}")
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

    breakpoint()
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
        swisseo_s2-sr_v100_mosaic_{earliest_timestamp}_metadata.json

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
                filename = f"swisseo_s2-sr_v100_mosaic_{earliest_timestamp}_metadata.json"
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
            orbit_num = item['properties']['relativeOrbitNumber']
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
        metadata_filename = f"swisseo_s2-sr_v100_mosaic_{timestamp}_metadata.json"
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

    breakpoint()

    ##############################
    # TODO COREGISTRATION AROSICS

    ##############################
    # TODO TERRAINSHADOWMASK

    ##############################
    # TODO Upload to STAC

    """
    breakpoint()
    files = bucket.objects.filter(Prefix=product)
    if not list(files):
        raise FileNotFoundError(f"Could not find any files for {product}")
    for file in files:
        os.makedirs(os.path.dirname(file.key), exist_ok=True)
        if not os.path.isdir(file.key):
            bucket.download_file(file.key, f"{target}{file.key}")

    # path to the product to download
    download(s3_copernicus.Bucket("eodata"), "Sentinel-1/SAR/SLC/2019/10/13/S1B_IW_SLC__1SDV_20191013T155948_20191013T160015_018459_022C6B_13A2.SAFE/")

    # Check items completion based on orbits
    # download via https://documentation.dataspace.copernicus.eu/APIs/S3.html
    # Filter for 4.00 greater












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

        # """
        # # SWITCH export
        # if export60mBands is True:
        #     print('Launching export for 60m bands')
        #     fname_60m = 'S2-L2A_Mosaic_' + sensing_date_read + '_Bands-60m'
        #     band_list_60m = ['B1', 'B9', 'B10']
        #     print('Band list: {}'.format(band_list_60m))
        #     task = ee.batch.Export.image.toAsset(
        #         image=S2_sr.select(band_list_60m).clip(aoi_exp),
        #         scale=60,
        #         description=task_description + '_60m',
        #         crs='EPSG:2056',
        #         region=aoi_exp,
        #         maxPixels=1e10,
        #         assetId=collection + '/' + fname_60m
        #     )
        #     task.start()
        # """
