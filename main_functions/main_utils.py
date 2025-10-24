import configuration as config
from pydrive.auth import GoogleAuth
from oauth2client.service_account import ServiceAccountCredentials
import boto3
import requests
import ee
import datetime
import csv
import os
import json
import pandas as pd
import dateutil
from google.cloud import storage
from typing import Dict, List, Optional, Tuple, Union, Any
from pathlib import Path
import subprocess
import logging
import re
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
    if os.path.exists(config.GOOGLE_SECRETS):
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

        # Read the service account key file
        with open(config.GOOGLE_SECRETS, "r") as f:
            data = json.load(f)

        # Authenticate with Google using the service account key file
        gauth = GoogleAuth()
        gauth.service_account_file = config.GOOGLE_SECRETS
        gauth.service_account_email = data["client_email"]
        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
            gauth.service_account_file, scopes=scopes
        )
        # Load AWS credentials from JSON
        with open(config.S3_SECRETS, "r") as f:
            aws_creds = json.load(f)

        # Load COPERNICUS credentials from JSON
        with open(config.COPERNICUS_SECRETS, "r") as f:
            copernicus_creds = json.load(f)

    else:
        # Run other code using secrets from GitHub Action
        # This script is running on GitHub
        gauth = GoogleAuth()
        google_client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
        google_client_secret = json.loads(google_client_secret)
        gauth.service_account_email = google_client_secret["client_email"]
        google_client_secret_str = json.dumps(google_client_secret)

        # Write the JSON string to a temporary key file
        gauth.service_account_file = "keyfile.json"
        with open(gauth.service_account_file, "w") as f:
            f.write(google_client_secret_str)

        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
            gauth.service_account_file, scopes=scopes
        )
        # Write S3
        s3_secrets_str = os.environ.get('S3_SECRETS')
        aws_creds = json.loads(s3_secrets_str)

        # copernicus S3
        copernicus_secrets_str = os.environ.get('COPERNICUS_S3_SECRETS')
        copernicus_creds = json.loads(copernicus_secrets_str)

    # Create the GCS client
    global storage_client
    storage_client = storage.Client.from_service_account_json(
            gauth.service_account_file)

    # Initialize Google Earth Engine
    credentials = ee.ServiceAccountCredentials(
        gauth.service_account_email, gauth.service_account_file
    )
    ee.Initialize(credentials)

    # Test if GEE initialization is successful
    image = ee.Image("NASA/NASADEM_HGT/001")
    title = image.get("title").getInfo()

    if title != "NASADEM: NASA NASADEM Digital Elevation 30m":
        print("GEE initialization FAILED")

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


def get_github_info():
    """
    Retrieves GitHub repository information and generates a GitHub link based on the latest commit.

    Returns:
        A dictionary containing the GitHub link. If the request fails or no commit hash is available, the link will be None.
    """
    # Enter your GitHub repository information
    owner = config.GITHUB_OWNER
    repo = config.GITHUB_REPO

    # Make a GET request to the GitHub API to retrieve information about the repository
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/main")

    github_info = {}

    if response.status_code == 200:
        # Extract the commit hash from the response
        commit_hash = response.json()["sha"]

        # Generate the GitHub link
        github_link = f"https://github.com/{owner}/{repo}/commit/{commit_hash}"
        github_info["GithubLink"] = github_link

    else:
        github_info["GithubLink"] = None

    # Make a GET request to the GitHub API to retrieve information about the repository releases
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest")

    if response.status_code == 200:
        # Extract the release version from the response
        release_version = response.json()["tag_name"]
    else:
        release_version = "0.0.0"

    github_info["ReleaseVersion"] = release_version

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


def maskOutside(image, aoi):
    """
    Masks the areas outside the specified region of interest (AOI) in an image.

    Args:
        image: The image to be masked.
        aoi: The region of interest (AOI) to keep in the image.

    Returns:
        The image with the areas outside the AOI masked.
    """
    # Create a constant image with a value of 1, clip it to the AOI, and use it as a mask
    # add .not() after mask() to mask inside
    mask = ee.Image.constant(1).clip(aoi).mask()

    # Apply the mask to the image
    return image.updateMask(mask)

# Function to analyse the number of sceneds first and last day


def get_collection_info(collection):
    """
    Retrieves information about an image collection.

    Args:
        collection: The image collection to retrieve information from.

    Returns:
        A tuple containing the first date, last date, and total number of images in the collection.
        Returns (None, None, 0) for empty collections.
    """
    # Sort the collection by date in ascending order

    sorted_collection = collection.sort('system:time_start')

    # Get the first and last image from the sorted collection
    first_image = sorted_collection.first()
    last_image = sorted_collection.sort('system:time_start', False).first()

    try:
        # Get the count of images in the collection
        image_count = collection.size().getInfo()
        # Get the dates of the first and last image
        first_date = ee.Date(first_image.get('system:time_start')).format('YYYY-MM-dd').getInfo()
        last_date = ee.Date(last_image.get('system:time_start')).format('YYYY-MM-dd').getInfo()
    except ee.EEException:
        image_count = 0
        # Handle cases where date information might be missing
        first_date = None
        last_date = None

    # Return the first date, last date, and total number of scenes
    return first_date, last_date, image_count


def get_quadrants(roi):
    """
    Divide a region of interest into quadrants.

    Parameters:
    roi (ee.Geometry): Region of interest.

    Returns:
    dict: Dictionary with the quadrants (quadrant1, quadrant2, quadrant3, quadrant4).
    """
    # Calculate the bounding box of the region
    bounds = roi.bounds()

    # Get the coordinates of the bounding box

    bbox = bounds.coordinates().getInfo()[0]

    # Extract the coordinates
    min_x, min_y = bbox[0]
    max_x, max_y = bbox[2]

    # Calculate the midpoints
    mid_x = (min_x + max_x) / 2
    mid_y = (min_y + max_y) / 2

    # Define the quadrants
    quadrant1 = ee.Geometry.Rectangle(min_x, min_y, mid_x, mid_y)
    quadrant2 = ee.Geometry.Rectangle(mid_x, min_y, max_x, mid_y)
    quadrant3 = ee.Geometry.Rectangle(min_x, mid_y, mid_x, max_y)
    quadrant4 = ee.Geometry.Rectangle(mid_x, mid_y, max_x, max_y)

    return {
        "quadrant1": quadrant1,
        "quadrant2": quadrant2,
        "quadrant3": quadrant3,
        "quadrant4": quadrant4
    }


def start_export(image, scale, description, region, filename_prefix, crs):
    """
    Starts an export task to export an image to Google Drive or Google Cloud Storage


    Args:
        image: The image to be exported.
        scale: The scale of the exported image.
        description: The description of the export task.
        region: The region of interest (ROI) to export.
        filename_prefix: The prefix to be used for the exported file.
        crs: The coordinate reference system (CRS) of the exported image.
        GCS=False : If set to true, an GCS will be used for output

    Returns:
        None
    """

    # Export in GEE
    # TODO Getting S2_mosaic.projection() makes no sense, it will always be a computed image, with 1 degree scale and EPSG 4326, unless manually reprojected.
    #  Use projection() from one of the original images instead, e.g., S2_collection.first().projection(), *after the aoi/date filters but before mapping any transformation function* then
    #  work with the corresponding CrsTtransform derived from it  crs:'EPSG:32632',   crsTransform: '[10,0,0,0,10,0]'

    if config.GDRIVE_TYPE == "GCS":
        # print("GCS export")
        task = ee.batch.Export.image.toCloudStorage(
            image=image,
            description=description,
            scale=scale,
            region=region,
            fileNamePrefix=filename_prefix,
            maxPixels=1e13,
            crs=crs,
            fileFormat="GeoTIFF",
            bucket=config.GCLOUD_BUCKET
        )
    else:
        # print("Drive export")
        task = ee.batch.Export.image.toDrive(
            image=image,
            description=description,
            scale=scale,
            region=region,
            fileNamePrefix=filename_prefix,
            maxPixels=1e13,
            crs=crs,
            fileFormat="GeoTIFF"
        )
    # OPTION Export in GEE with UTM32
    # for images covering that UTM zone this will be the best, but for the neighbouring UTM zones, images will be reprojected. So, for mosaics for larger areas spanning multiple UTM zones maybe some alternative projection is more convenient.
    # task = ee.batch.Export.image.toDrive(
    #    image=image,
    #    description=description,
    #    #scale=scale,
    #    "region=region,"
    #    fileNamePrefix=filename_prefix,
    #    maxPixels=1e13,
    #    crs = 'EPSG:32632',
    #    crsTransform = '[10,0,300000,0,-10,5200020]',
    #    fileFormat ="GeoTIFF"
    # )

    # OPTION: only reproject but without scale use this code, based on https://developers.google.com/earth-engine/guides/exporting#setting_scal
    # projection = image.projection().getInfo()
    # task = ee.batch.Export.image.toDrive(
    #     image=image,
    #     description=description,
    #     "region "= "region",
    #     fileNamePrefix=filename_prefix,
    #     crs=crs,
    #     maxPixels=1e13,
    #     fileFormat = "GeoTIFF",
    #     crsTransform = projection['transform']
    # )

    task.start()

    # Get Task ID
    task_id = task.status()["id"]
    print("Exporting  with Task ID:", task_id +
          f" file {filename_prefix} to {config.GDRIVE_TYPE}...")

    # Save Task ID and filename to a text file
    header = ["Task ID", "Filename"]
    data = [task_id, filename_prefix]

    # Check if the file already exists
    file_exists = os.path.isfile(config.GEE_RUNNING_TASKS)

    with open(config.GEE_RUNNING_TASKS, "a", newline="") as f:
        writer = csv.writer(f)

        # Write the header if the file is newly created
        if not file_exists:
            writer.writerow(header)

        # Write the data
        writer.writerow(data)


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


def prepare_export(roi, productitem, productasset, productname, scale, image, sensor_stats, current_date_str):
    """
    Prepare the export of the image by splitting it into quadrants and starting the export tasks.
    It also generates product status information, updates the product status file,
    and writes the product description to a CSV file.

    Args:
        roi (ee.Geometry): Region of interest for the export.
        productitem (str): Timestamp of assets YYYYMMDThhmmss, "YYYYMMDDT235959" for a day
        productasset (str): Base filename for the exported files.
        productname (str): Product name of the exported files.
        scale (str): Scalenumber in [m] of the exported file
        image (ee.Image): Image to be exported.
        sensor_stats (list): List containing sensor statistics.
        current_date_str (str): Current date in string format.

    Returns:
        None
    """

    # Get current Processor Version from GitHub
    processor_version = get_github_info()

    # Define the quadrants to split into 4 regions
    quadrants = get_quadrants(roi)

    for quadrant_name, quadrant in quadrants.items():
        # Create filename for each quadrant
        filename_q = productasset + quadrant_name
        # Start the export for each quadrant

        start_export(image, int(scale),
                     productasset, quadrant, filename_q, config.OUTPUT_CRS)

    # Generate product status information
    product_status = {
        'Product': productname,
        'LastSceneDate': sensor_stats[1],
        'RunDate': current_date_str,
        'Status': "RUNNING"
    }

    # Update the product status file
    update_product_status_file(product_status, config.LAST_PRODUCT_UPDATES)

    # Get Product info from config
    product = get_product_from_techname(productname)

    # Update the product  file
    header = ["Product", "Item", "Asset", "DateFirstScene", "DateLastScene",
              "NumberOfScenes", "DateItemGeneration", "ProcessorHashLink", "ProcessorReleaseVersion", "GeocatID"]
    data = [productname, productitem, productasset, str(sensor_stats[0]), str(
        sensor_stats[1]), str(sensor_stats[2]), current_date_str, processor_version["GithubLink"], processor_version["ReleaseVersion"], product['geocat_id']]

    # Create swisstopo_data dictionary
    swisstopo_data = {"header": header, "data": data}

    # Create swisstopo_data dictionary with uppercase keys
    swisstopo_data = {key.upper(): value for key, value in zip(header, data)}

    # Adding extracting image info
    image_info = ee.Image(image).getInfo()

    # Convert keys to uppercase and add prefix
    image_info_gee = {"GEE_" + key.upper(): value for key,
                      value in image_info.items()}

    # Add swisstopo_data to image_info_gee
    image_info_gee["SWISSTOPO"] = swisstopo_data

    # Export the dictionary as JSON
    with open(os.path.join(config.PROCESSING_DIR, productasset + "_metadata.json"), 'w') as json_file:
        json.dump(image_info_gee, json_file)

    return None


def get_collection_info_landsat(collection):
    """
    Retrieves information about an image collection for the line of Landsat satellites

    Args:
        collection: The landsat image collection to retrieve information from.

    Returns:
        A tuple containing the first date, last date, and total number of images in the collection.
        Returns (None, None, 0) for empty collections.
    """
    # Sort the collection by date in ascending order
    index_list = collection.aggregate_array('system:index')

    dates_list = [dateutil.parser.parse(i.split('_')[-1]) for i in index_list.getInfo()]

    # Get the first and last image and size of image collection
    image_count = len(dates_list) if len(dates_list)>0 else 0
    first_date = min(dates_list) if image_count>0 else None
    last_date = max(dates_list) if image_count>0 else None

    # Return the first date, last date, and total number of scenes
    return first_date, last_date, image_count


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