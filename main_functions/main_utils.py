import configuration as config
import boto3
import requests
import csv
import os
import json
import pandas as pd
import torch
import platform
import dateutil
from typing import Dict, List, Optional, Tuple, Union, Any
from pathlib import Path
import subprocess
import logging
import re
from datetime import datetime, timedelta
from pystac_client import Client
import json
import time

import glob
import math
import shutil


logger = logging.getLogger(__name__)

def determine_run_type():
    """
    Determines the run type based on the existence of the SECRET on the local machine file.

    If the file `config.GOOGLE_SECRETS` exists, sets the run type to 2 (DEV) and prints a corresponding message.
    Otherwise, sets the run type to 1 (PROD) and prints a corresponding message.
    """
    global run_type
    if os.path.exists(config.FSDI_SECRETS):
        run_type = 2
        print("\nType 2 run PROCESSOR: We are on a local machine")
    else:
        run_type = 1
        print("\nType 1 run PROCESSOR: We are on GitHub")


def initialize_gee():
    """
    Initializes Google Earth Engine (GEE) and Google Drive based on the run type.

    If the run type is 2, initializes GEE and authenticates using the service account key file.
    If the run type is 1, initializes GEE and authenticates using secrets from GitHub Action.

    Prints a success or failure message after initializing GEE.

    Note: This function assumes the required credentials and scopes are properly set.

    Returns:
        None
    """
    # Set scopes for Google Drive
    scopes = ["https://www.googleapis.com/auth/drive"]

    if run_type == 2:
        # Initialize GEE and authenticate using the service account key file

        # # Read the service account key file
        # with open(config.GOOGLE_SECRETS, "r") as f:
        #     data = json.load(f)

        # # Authenticate with Google using the service account key file
        # gauth = GoogleAuth()
        # gauth.service_account_file = config.GOOGLE_SECRETS
        # gauth.service_account_email = data["client_email"]
        # gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
        #     gauth.service_account_file, scopes=scopes
        # )
        # Load AWS credentials from JSON
        with open(config.S3_SECRETS, "r") as f:
            aws_creds = json.load(f)

        # Load COPERNICUS credentials from JSON
        with open(config.COPERNICUS_SECRETS, "r") as f:
            copernicus_creds = json.load(f)

    else:
        # Run other code using secrets from GitHub Action
        # This script is running on GitHub
        # gauth = GoogleAuth()
        # google_client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
        # google_client_secret = json.loads(google_client_secret)
        # gauth.service_account_email = google_client_secret["client_email"]
        # google_client_secret_str = json.dumps(google_client_secret)

        # # Write the JSON string to a temporary key file
        # gauth.service_account_file = "keyfile.json"
        # with open(gauth.service_account_file, "w") as f:
        #     f.write(google_client_secret_str)

        # gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
        #     gauth.service_account_file, scopes=scopes
        # )
        # Write S3
        s3_secrets_str = os.environ.get('S3_SECRETS')
        aws_creds = json.loads(s3_secrets_str)

        # copernicus S3
        copernicus_secrets_str = os.environ.get('COPERNICUS_S3_SECRETS')
        copernicus_creds = json.loads(copernicus_secrets_str)

    # Create the GCS client
    # global storage_client
    # storage_client = storage.Client.from_service_account_json(
    #         gauth.service_account_file)

    # # Initialize Google Earth Engine
    # credentials = ee.ServiceAccountCredentials(
    #     gauth.service_account_email, gauth.service_account_file
    # )
    # ee.Initialize(credentials)

    # # Test if GEE initialization is successful
    # image = ee.Image("NASA/NASADEM_HGT/001")
    # title = image.get("title").getInfo()

    # if title != "NASADEM: NASA NASADEM Digital Elevation 30m":
    #     print("GEE initialization FAILED")

    # Initialize S3 client with credentials
    global s3
    try:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=aws_creds["aws_access_key_id"],
            aws_secret_access_key=aws_creds["aws_secret_access_key"],
            region_name=aws_creds["aws_region_name"],
        )

    except Exception as e:
        print(f"Warning: S3 initialization failed - {e}")


    # Initialize COPERNICUS S3 client with credentials
    global copernicus_s3
    try:
        session = boto3.session.Session()
        copernicus_s3 = boto3.resource(
            "s3",
            endpoint_url='https://eodata.dataspace.copernicus.eu',
            aws_access_key_id=copernicus_creds["access_key"],
            aws_secret_access_key=copernicus_creds["secret_key"],
            region_name='default'
        )

    except Exception as e:
        print(f"Warning: COPERNICUS S3 initialization failed - {e}")

def is_date_in_empty_asset_list(collection, check_date_str):
    """
    Check if a given date for a collection is in the empty asset list.

    Args:
    collection_basename (str): The basename of the collection.
    check_date_str (str): The date to check in string format.
    config (object): Configuration object containing EMPTY_ASSET_LIST path.

    Returns:
    bool: True if the date is found in the empty asset list, False otherwise.
    """
    try:
        collection_basename = os.path.basename(collection)
        # Read the empty asset list
        df = pd.read_csv(config.EMPTY_ASSET_LIST)

        # Filter the dataframe for the given collection and date
        df_selection = df[(df.collection == collection_basename) &
                          (df.date == check_date_str)]

        # Check if any rows match the criteria
        if len(df_selection) > 0:
            print(check_date_str+' is in empty_asset_list for '+collection)
            return True
        else:
            return False

    except Exception as e:
        print(f"Error checking empty asset list: {e}")
        return False  # Return False in case of any error to allow further processing


import time
import requests

def get_github_info():
    """
    Retrieves GitHub repository information and generates a GitHub link based on the latest commit.
    Retries up to 3 times with a 30-second delay on connection errors.
    Returns:
        A dictionary containing the GitHub link and release version.
        Falls back to None / "github could not be reached" if all attempts fail.
    """
    owner = config.GITHUB_OWNER
    repo = config.GITHUB_REPO

    MAX_RETRIES = 3
    RETRY_DELAY = 30  # seconds

    def get_with_retry(url):
        """Performs a GET request with retries on connection errors."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.get(url, timeout=15)
                return response
            except requests.exceptions.ConnectionError as e:
                if attempt < MAX_RETRIES:
                    print(f"  Connection error on attempt {attempt}/{MAX_RETRIES}: {e}")
                    print(f"  Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"  All {MAX_RETRIES} attempts failed for {url}: {e}")
                    return None

    github_info = {}

    # --- Commit hash ---
    response = get_with_retry(
        f"https://api.github.com/repos/{owner}/{repo}/commits/main"
    )
    if response is not None and response.status_code == 200:
        commit_hash = response.json()["sha"]
        github_info["GithubLink"] = f"https://github.com/{owner}/{repo}/commit/{commit_hash}"
    else:
        github_info["GithubLink"] = "github could not be reached"

    # --- Release version ---
    response = get_with_retry(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    )
    if response is not None and response.status_code == 200:
        github_info["ReleaseVersion"] = response.json()["tag_name"]
    else:
        github_info["ReleaseVersion"] = "github could not be reached"

    return github_info


def get_product_from_techname(techname):
    """
    This function searches for a dictionary in the 'config' module that contains
    'product_name' with a specified value and returns it.

    Parameters:
    techname (str): The value of 'product_name' to search for.
                    For example, 'ch.swisstopo.swisseo_s2-sr_v100'.

    Returns:
    dict: The dictionary that contains 'product_name' with the value of 'techname'.
          If no such dictionary is found, it returns None.
    """

    # Initialize the variable to None
    var = None

    # Iterate over all attributes in the config module
    for attr_name in dir(config):
        attr_value = getattr(config, attr_name)

        # Check if the attribute is a dictionary
        if isinstance(attr_value, dict):
            # Check if the dictionary contains 'product_name' with the desired value
            if attr_value.get('product_name') == techname:
                var = attr_value
                break  # Exit the loop once the dictionary is found

    return var


def addINDEX(image, bands, index_name):
    """
    Add an Index (eg NDVI) band to the image based on two bands.

    Args:
        image (ee.Image): Input image to add the index band.
        bands (dict): Dictionary containing band names for NIR and RED.
        index_name (str): Name of the index used as band name

    Returns:
        ee.Image: Image with the index band added.
    """

    # Extract the band names for NIR and RED from the input dictionary
    NIR = bands['NIR']
    RED = bands['RED']

    # Compute the index using the normalizedDifference() function and rename the band to "NDVI"
    index = image.normalizedDifference([NIR, RED]).rename(index_name)

    # Add the index band to the image using the addBands() function
    image_with_index = image.addBands(index)

    # Return the image with the NDVI band added
    return image_with_index






def check_product_status(product_name):
    """
    Check if the given product has a "Status" marked as complete

    Parameters:
    product_name (str): Name of the product to check.

    Returns:
    bool: True if "Status" has a value equal to 'complete'
    False otherwise
    """

    with open(config.LAST_PRODUCT_UPDATES, "r", newline="", encoding="utf-8") as f:
        dict_reader = csv.DictReader(f, delimiter=",")
        for row in dict_reader:
            if row["Product"] == product_name:
                return row['Status'] == 'complete'
    return False


def check_product_update(product_name, date_string):
    """
    Check if the given product has a newer "LastSceneDate" than the provided date.

    Parameters:
    product_name (str): Name of the product to check.
    date_string (str): Date in the format "YYYY-MM-DD" for comparison.

    Returns:
    bool: True if date_String has a newer Date than "LastSceneDate" stored in the product,
    True if the product is not found, False otherwise.
    """
    target_date = datetime.datetime.strptime(date_string, "%Y-%m-%d").date()

    with open(config.LAST_PRODUCT_UPDATES, "r", newline="", encoding="utf-8") as f:
        dict_reader = csv.DictReader(f, delimiter=",")
        for row in dict_reader:
            if row["Product"] == product_name:
                last_scene_date = datetime.datetime.strptime(
                    row["LastSceneDate"], "%Y-%m-%d").date()
                return last_scene_date < target_date
    return True


def update_product_status_file(input_dict, output_file):
    """
    Write a dictionary to a CSV file. If the file exists, the data is appended to it.
    If the file does not exist, a new file is created with a header. The function also
    updates the dictionary entry for the "Product" field.

    Args:
        input_dict (dict): Dictionary to be written to the file.
        output_file (str): Path of the output file.

    Returns:
        None
    """
    # Get the field names from the input dictionary
    fieldnames = list(input_dict.keys())

    if os.path.isfile(output_file):
        # If the file already exists, update the existing data or append new data
        with open(output_file, "r+", newline="", encoding="utf-8") as f:
            dict_reader = csv.DictReader(f, delimiter=",")
            lines = list(dict_reader)
            product_exists = False
            for i, line in enumerate(lines):
                if line["Product"] == input_dict["Product"]:
                    lines[i] = input_dict
                    product_exists = True
                    break
            if not product_exists:
                lines.append(input_dict)

            # Move the file pointer to the beginning
            f.seek(0)
            dict_writer = csv.DictWriter(
                f, fieldnames=fieldnames, delimiter=",", quotechar='"', lineterminator="\n"
            )
            dict_writer.writeheader()
            dict_writer.writerows(lines)

            # Truncate the file to remove any remaining data
            f.truncate()
    else:
        # If the file doesn't exist, create a new file and write the header and data
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            dict_writer = csv.DictWriter(
                f, fieldnames=fieldnames, delimiter=",", quotechar='"', lineterminator="\n"
            )
            dict_writer.writeheader()
            dict_writer.writerow(input_dict)

    # Return None
    return None


def ensure_path(path: Union[str, Path]) -> Path:
    """
    Ensures a path is a proper Path object with normalized separators for the current OS.

    Args:
        path: The path to normalize

    Returns:
        A normalized Path object
    """
    # Convert to Path object if it's a string
    if isinstance(path, str):
        path = Path(path)

    # Normalize path (handles different path separators)
    path = Path(os.path.normpath(str(path)))

    return path


def ensure_directory(path: Union[str, Path]) -> Path:
    """
    Ensures a directory exists, creating it if necessary.

    Args:
        path: Directory path

    Returns:
        Path object for the directory
    """
    path = ensure_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path

def run_gdal_command(command: List[str]) -> Tuple[bool, str, str]:
    """
    Run a GDAL command and capture its output.

    Args:
        command: List of command arguments

    Returns:
        Tuple of (success, stdout, stderr)
    """
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        return process.returncode == 0, process.stdout, process.stderr
    except Exception as e:
        return False, "", str(e)

def get_raster_info(raster_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Get basic information about a raster file using gdalinfo.

    Args:
        raster_path: Path to the raster file

    Returns:
        Dictionary with raster information (dimensions, extent, projection, etc.)

    Raises:
        ValueError: If the raster cannot be opened
    """
    raster_path = ensure_path(raster_path)

    # Run gdalinfo to get raster information
    command = ["gdalinfo", "-json", str(raster_path)]
    success, stdout, stderr = run_gdal_command(command)

    if not success:
        logger.error(f"Failed to get information for raster: {raster_path}")
        raise ValueError(f"Failed to open raster: {raster_path}")

    # Parse JSON output
    try:
        info = json.loads(stdout)

        # Extract basic information
        width = info["size"][0]
        height = info["size"][1]

        # Get geotransform
        geotransform = info["geoTransform"]
        minx = geotransform[0]
        maxy = geotransform[3]
        pixel_width = abs(geotransform[1])
        pixel_height = abs(geotransform[5])
        maxx = minx + pixel_width * width
        miny = maxy - pixel_height * height

        # Get projection and EPSG
        projection = info["coordinateSystem"]["wkt"]
        epsg = None
        if "EPSG" in info["coordinateSystem"].get("dataAxisToSRSAxisMapping", ""):
            epsg_match = re.search(r'EPSG:(\d+)', info["coordinateSystem"]["dataAxisToSRSAxisMapping"])
            if epsg_match:
                epsg = int(epsg_match.group(1))

        # Alternative method to get EPSG using projinfo
        if epsg is None:
            epsg_command = ["gdalsrsinfo", "-o", "epsg", str(raster_path)]
            epsg_success, epsg_stdout, _ = run_gdal_command(epsg_command)
            if epsg_success and "EPSG:" in epsg_stdout:
                epsg_match = re.search(r'EPSG:(\d+)', epsg_stdout)
                if epsg_match:
                    epsg = int(epsg_match.group(1))

        # Extract band information
        bands = []
        for i, band in enumerate(info["bands"], 1):
            bands.append({
                "index": i,
                "data_type": band.get("type", "Unknown"),
                "no_data_value": band.get("noDataValue", None)
            })

        return {
            "width": width,
            "height": height,
            "pixel_width": pixel_width,
            "pixel_height": pixel_height,
            "extent": (minx, miny, maxx, maxy),
            "projection": projection,
            "epsg": epsg,
            "geotransform": geotransform,
            "bands": bands
        }

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error parsing gdalinfo output: {e}")
        raise ValueError(f"Failed to parse raster information: {e}")


def get_pixel_spacing(raster_path: Union[str, Path]) -> Tuple[float, float]:
    """
    Get the pixel spacing (resolution) of a raster file.

    Args:
        raster_path: Path to the raster file

    Returns:
        Tuple of (x_resolution, y_resolution) in the raster's units

    Raises:
        ValueError: If the pixel spacing cannot be determined
    """
    try:
        info = get_raster_info(raster_path)
        return (info["pixel_width"], info["pixel_height"])
    except Exception as e:
        logger.error(f"Error getting pixel spacing: {e}")
        raise


def get_extent_and_dimensions(raster_path: Union[str, Path]) -> Tuple[float, float, float, float, int, int]:
    """
    Get the extent and dimensions of a raster file.

    Args:
        raster_path: Path to the raster file

    Returns:
        Tuple of (minx, maxx, miny, maxy, width, height)

    Raises:
        ValueError: If the extent and dimensions cannot be determined
    """
    try:
        info = get_raster_info(raster_path)
        minx, miny, maxx, maxy = info["extent"]
        return (minx, maxx, miny, maxy, info["width"], info["height"])
    except Exception as e:
        logger.error(f"Error getting extent and dimensions: {e}")
        raise


def parse_date(date_str: str) -> datetime:
    """
    Parse date string in various formats using dateutil.parser.

    This function can handle a wide variety of date formats automatically,
    including ISO formats, common regional formats, and timestamps.

    Args:
        date_str: Date string in virtually any common format

    Returns:
        Datetime object

    Raises:
        ValueError: If the date string cannot be parsed
    """
    try:
        from dateutil import parser
        return parser.parse(date_str)
    except (ImportError, ValueError) as e:
        # Fall back to manual parsing if dateutil is not available
        # or if the parser fails for some reason
        formats = [
            "%Y-%m-%d",
            "%Y%m%d",
            "%d.%m.%Y",
            "%Y/%m/%d",
            "%Y-%m-%dT%H%M%S"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        raise ValueError(f"Could not parse date: {date_str}")


def equalize_extents(
    common_extent: Tuple[float, float, float, float],
    im_target: Union[str, Path]
) -> str:
    """
    Clip the target image to a common extent that is aligned to a grid.
    The common extent should already be aligned to the coarsest GSD grid,
    ensuring compatibility with all finer resolution images.

    Args:
        common_extent: Tuple of (minx, miny, maxx, maxy) representing the common aligned extent.
        im_target: The path to the target raster file (will be clipped).

    Returns:
        Path to the clipped target as a VRT file.

    Raises:
        RuntimeError: If clipping fails or the target doesn't overlap with the common extent.
    """
    im_target = ensure_path(im_target)
    minx_common, miny_common, maxx_common, maxy_common = common_extent

    # Define output file name for target (same location, same name, but .vrt extension)
    output_target = im_target.with_name(im_target.stem + "_clip.vrt")

    logger.info(f"Clipping target image to common extent: ({minx_common}, {miny_common}, {maxx_common}, {maxy_common})")

    try:
        # Get extent of the target image
        minx_target, maxx_target, miny_target, maxy_target, _, _ = get_extent_and_dimensions(im_target)

        # Get the target's GSD
        gsd_x_target, gsd_y_target = get_pixel_spacing(im_target)

        # Check that target origin is aligned to multiples of its GSD
        tolerance = 1e-6
        if (abs(minx_target % gsd_x_target) > tolerance or
            abs(miny_target % gsd_y_target) > tolerance):
            raise RuntimeError(f"Target image origin ({minx_target}, {miny_target}) is not aligned to multiples of its GSD ({gsd_x_target}, {gsd_y_target})")

        # Verify that common extent is aligned to target's GSD
        # (This should always be true if common extent is aligned to coarsest GSD and target GSD divides into it)
        if (abs(minx_common % gsd_x_target) > tolerance or
            abs(miny_common % gsd_y_target) > tolerance or
            abs(maxx_common % gsd_x_target) > tolerance or
            abs(maxy_common % gsd_y_target) > tolerance):
            raise RuntimeError(
                f"Common extent ({minx_common}, {miny_common}, {maxx_common}, {maxy_common}) "
                f"is not aligned to target's GSD ({gsd_x_target}, {gsd_y_target}). "
                f"This should not happen if common extent is aligned to coarsest GSD."
            )

        # Check if there's overlap between target and common extent
        if (minx_common >= maxx_target or maxx_common <= minx_target or
            miny_common >= maxy_target or maxy_common <= miny_target):
            raise RuntimeError(
                f"Target image extent ({minx_target}, {miny_target}, {maxx_target}, {maxy_target}) "
                f"does not overlap with common extent ({minx_common}, {miny_common}, {maxx_common}, {maxy_common})"
            )

        # Crop the target image to the common extent
        command_target = [
            "gdalwarp",
            "-overwrite",
            "-of", "VRT",
            "-te", str(minx_common), str(miny_common), str(maxx_common), str(maxy_common),
            "-r", "near",
            str(im_target),
            str(output_target)
        ]

        success, _, stderr = run_gdal_command(command_target)
        if not success:
            logger.error(f"Failed to crop target image: {stderr}")
            raise RuntimeError(f"Failed to crop target image: {stderr}")

        logger.info(f"Successfully clipped {im_target.name} to common extent")
        return str(output_target)

    except Exception as e:
        logger.error(f"Error equalizing extents for {im_target}: {str(e)}")
        raise

def get_stac_items_for_date(
    stac_catalog_url: str,
    collection_id: str,
    target_date: datetime.date
) -> List[Dict[str, Any]]:
    """
    Query a swisstopo STAC v0.9 catalog for items from a specific collection on a specific date.

    Args:
        stac_catalog_url: Base URL of the STAC catalog (e.g., 'https://data.geo.admin.ch/api/stac/v0.9/')
        collection_id: Collection ID (e.g., 'ch.swisstopo.swisseo_s2-sr_v200')
        target_date: Date to query for

    Returns:
        List of STAC item dicts for the specified date
    """
    # Build the items endpoint URL (strip trailing slash to be safe)
    base = stac_catalog_url.rstrip("/")
    items_url = f"{base}/collections/{collection_id}/items"

    # Use midnight-to-midnight range with no microseconds
    start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_dt   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    datetime_str = f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    items = []
    params = {
        "datetime": datetime_str,
        "limit": 100,
    }

    while True:
        response = requests.get(items_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        for feature in data.get("features", []):
            items.append({
                "id":         feature.get("id"),
                "properties": feature.get("properties", {}),
                "assets":     feature.get("assets", {}),
                "geometry":   feature.get("geometry"),
                "bbox":       feature.get("bbox"),
                "datetime":   feature.get("properties", {}).get("datetime"),
            })

        # Handle pagination via next link
        next_href = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_href = link.get("href")
                break

        if next_href:
            # Next link already has params baked in — fetch directly
            response = requests.get(next_href, timeout=30)
            response.raise_for_status()
            data = response.json()
            params = {}  # params are now in the URL
            items_url = next_href
            # Re-enter loop cleanly
            for feature in data.get("features", []):
                items.append({
                    "id":         feature.get("id"),
                    "properties": feature.get("properties", {}),
                    "assets":     feature.get("assets", {}),
                    "geometry":   feature.get("geometry"),
                    "bbox":       feature.get("bbox"),
                    "datetime":   feature.get("properties", {}).get("datetime"),
                })
            break  # swisstopo v0.9 pagination is simple; adjust if needed
        else:
            break

    return items


def check_stac_collection_availability(
    stac_catalog_url: str,
    collection_id: str,
    target_date: datetime.date,
    temporal_coverage: int
) -> tuple[bool, List[datetime.date]]:
    """
    Check if STAC items are available for all dates in the temporal coverage period.
    Args:
        stac_catalog_url: Base URL of the STAC catalog
        collection_id: Collection ID to check
        target_date: End date of the temporal coverage
        temporal_coverage: Number of days to check backwards from target_date
    Returns:
        Tuple of (all_present: bool, missing_dates: List[datetime.date])
    """
    base = stac_catalog_url.rstrip("/")
    items_url = f"{base}/collections/{collection_id}/items"

    check_date = target_date - timedelta(days=temporal_coverage)
    all_present = True
    missing_dates = []

    while check_date <= target_date:
        start_dt = datetime.datetime(check_date.year, check_date.month, check_date.day, 0, 0, 0)
        end_dt   = datetime.datetime(check_date.year, check_date.month, check_date.day, 23, 59, 59)
        datetime_str = f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"

        response = requests.get(items_url, params={"datetime": datetime_str, "limit": 1}, timeout=30)
        response.raise_for_status()
        features = response.json().get("features", [])

        if not features:
            print(f"No STAC items found for date {check_date}")
            all_present = False
            missing_dates.append(check_date)
        else:
            print(f"Found {len(features)} STAC item(s) for date {check_date}")

        check_date += timedelta(days=1)

    return all_present, missing_dates


def extract_collection_id_from_url(stac_url: str, config_api_path: str = '/api/stac/v0.9/') -> tuple[str, str]:
    """
    Extract the base URL and collection ID from a STAC collection URL.

    Supports both formats:
    - Viewer URL: 'https://sys-data.int.bgdi.ch/#/collections/ch.swisstopo.swisseo_s2-sr_v200'
    - Viewer URL: 'https://data.geo.admin.ch/#/collections/ch.swisstopo.swisseo_s2-sr_v200'
    - API URL: 'https://sys-data.int.bgdi.ch/api/stac/v0.9/' (Integration)
    - API URL: 'https://data.geo.admin.ch/api/stac/v0.9/' (Production)

    For viewer URLs, automatically converts to the API endpoint using the base domain.

    Args:
        stac_url: Full STAC collection URL or just the collection ID
        config_api_path: API path from config (e.g., config.STAC_FSDI_API)

    Returns:
        Tuple of (api_base_url, collection_id)
    """
    # If it's just a collection ID without URL, need to know which environment
    if not stac_url.startswith('http'):
        raise ValueError(f"URL must include base domain. Got: {stac_url}")

    # Handle viewer URLs like https://sys-data.int.bgdi.ch/#/collections/...
    if '#/collections/' in stac_url:
        # Extract the base domain and collection ID
        base_domain = stac_url.split('#')[0].rstrip('/')
        collection_id = stac_url.split('#/collections/')[1].strip('/')

        # Build API URL using the same base domain + config API path
        api_url = base_domain + config_api_path
        if not api_url.endswith('/'):
            api_url += '/'

        return api_url, collection_id

    # Handle API URLs like https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/...
    if '/collections/' in stac_url:
        base_part = stac_url.split('/collections/')[0]
        collection_id = stac_url.split('/collections/')[1].strip('/')

        # Ensure base URL ends with /
        if not base_part.endswith('/'):
            base_part += '/'

        return base_part, collection_id

    # If no collection specified, assume it's just the base API URL
    if stac_url.endswith('/'):
        return stac_url, ''
    else:
        return stac_url + '/', ''

def metadata_add_entry(
    json_file: str,
    path: str,
    key: str,
    value: Any,
    separator: str = "\\"
) -> dict:
    """
    Fügt einen Eintrag in eine verschachtelte JSON-Struktur hinzu.

    Args:
        json_file: Pfad zur JSON-Datei
        path: Pfad zur Zielgruppe (z.B. "SOURCE\\PROPERTIES" oder "PROPERTIES")
        key: Name des hinzuzufügenden Eintrags
        value: Wert des Eintrags
        separator: Trennzeichen für den Pfad (Standard: "\\")

    Returns:
        dict: Aktualisierte JSON-Daten

    Beispiele:
        # Einfacher Pfad
        metadata_add_entry("data.json", "PROPERTIES", "CLOUDCOVER", 23.5)

        # Verschachtelter Pfad
        metadata_add_entry("data.json", "SOURCE\\PROPERTIES", "CLOUDCOVER", 23.5)

        # Mehrfach verschachtelt
        metadata_add_entry("data.json", "SOURCE\\PROPERTIES\\METADATA", "CLOUDCOVER", 23.5)
    """
    # JSON-Datei laden
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Pfad in Teile aufteilen
    path_parts = [p.strip() for p in path.split(separator) if p.strip()]

    # Verschachtelte Struktur durchlaufen/erstellen
    current_level = data
    for part in path_parts:
        if part not in current_level:
            current_level[part] = {}
        current_level = current_level[part]

    # Wert hinzufügen (überschreibt vorhandene Werte)
    current_level[key] = value

    # JSON-Datei speichern
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return data

def extract_and_compare_datetime_from_url(url, iso_string):
    """
    Extracts the datetime value from a given STAC ITEM JSON URL and compares it with a provided ISO string.
    Args:
        url (str): The URL to fetch JSON data from.
        iso_string (str): The datetime string in format 'YYYY-MM-DDtHHMMSS' for comparison.
    Returns:
        bool: True if the extracted datetime value is on the same day or newer than the provided ISO string; False otherwise.
    """
    response = requests.get(url)  # Fetch the JSON data from the URL
    if response.status_code == 404:
        return True
    if response.status_code == 200:
        data = response.json()  # Parse the JSON data
        # Extract the "datetime" value
        datetime_value = data['properties']['datetime']
        # Parse the datetime value from the JSON response
        extracted_datetime = datetime.strptime(
            datetime_value, '%Y-%m-%dT%H:%M:%SZ')
        # Parse the ISO string with format '2025-07-03t100711'
        iso_datetime = datetime.strptime(iso_string, '%Y-%m-%dt%H%M%S')
        # Extract dates from both datetime objects
        extracted_date = extracted_datetime.date()
        iso_date = iso_datetime.date()
        # Compare the dates
        return extracted_date <= iso_date
    else:
        print("Failed to fetch data from the URL:", response.status_code)
        return False

def check_gpu_availability():
    """
    Check if GPU is available and properly initialized.
    Returns: tuple (bool, str) - (is_available, status_message)
    """
    try:
        # First, try to check CUDA availability
        # This might trigger the warning/error you're seeing
        if torch.cuda.is_available():
            # Additional verification: try to get device count
            device_count = torch.cuda.device_count()
            if device_count > 0:
                # Try to actually access the device
                try:
                    device_name = torch.cuda.get_device_name(0)
                    return True, f"GPU detected: {device_name}"
                except Exception as e:
                    return False, f"GPU initialization failed: {str(e)}"
            else:
                return False, "GPU initialization failed: No CUDA devices found"
        else:
            # CUDA not available, do system-level check
            return verify_gpu_with_system_tools()
    except Exception as e:
        # Catch the CUDA initialization error
        error_msg = str(e)
        if "forward compatibility was attempted on non supported HW" in error_msg or \
        "CUDA" in error_msg or "cuda" in error_msg:
            return False, "GPU initialization failed: CUDA compatibility error"
        return False, f"GPU initialization failed: {error_msg}"

def verify_gpu_with_system_tools():
    """
    Verify GPU availability using system tools (nvidia-smi on Linux, nvidia-smi.exe on Windows)
    Returns: tuple (bool, str) - (is_available, status_message)
    """
    system = platform.system()

    try:
        if system == "Linux":
            # Check with nvidia-smi on Linux
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu_name = result.stdout.strip().split('\n')[0]
                return False, f"GPU hardware detected ({gpu_name}) but PyTorch CUDA not available"
            else:
                return False, "No GPU detected"

        elif system == "Windows":
            # Check with nvidia-smi on Windows (usually in System32 or NVIDIA folder)
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True  # Needed on Windows to find nvidia-smi in PATH
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu_name = result.stdout.strip().split('\n')[0]
                return False, f"GPU hardware detected ({gpu_name}) but PyTorch CUDA not available"
            else:
                return False, "No GPU detected"
        else:
            return False, f"Unsupported operating system: {system}"

    except FileNotFoundError:
        return False, "No GPU detected (nvidia-smi not found)"
    except subprocess.TimeoutExpired:
        return False, "GPU check timed out"
    except Exception as e:
        return False, f"GPU check failed: {str(e)}"