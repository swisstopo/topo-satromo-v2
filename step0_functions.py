import os
import pandas as pd
import configuration as config
import csv
from datetime import datetime, timedelta
from main_functions import main_utils
from step0_processors import *
from step1_processors import *

def write_file(input_dict, output_file):
    """
    Write a dictionary to a CSV file. If the file exists, the data is appended
    to it. If the file does not exist, a new file is created with a header.

    Parameters:
    input_dict (dict): Dictionary to be written to file.
    output_file (str): Path of the output file.

    Returns:
    None
    """
    append_or_write = "a" if os.path.isfile(output_file) else "w"
    with open(output_file, append_or_write, encoding="utf-8", newline='') as f:
        dict_writer = csv.DictWriter(f, fieldnames=list(input_dict.keys()),
                                     delimiter=",", quotechar='"',
                                     lineterminator="\n")
        if append_or_write == "w":
            dict_writer.writeheader()
        dict_writer.writerow(input_dict)
    return

def step0_main(step0_product_dict, current_date_str):
    collections_ready = list()

    # Determine which collections are ready for processing
    # We check every step0 collection independently
    # The collection is ready if all assets are present for the interval [date-temporal_coverage; date]
    for step0_collection, (products, temporal_coverage, base_collection) in step0_product_dict.items():
        temporal_coverage -= 1

        ok = step0_check_collection(
            step0_collection, temporal_coverage, current_date_str)
        if ok:
            collections_ready.append(step0_collection)

    return collections_ready


def step0_check_collection(collection, temporal_coverage, current_date_str):
    """
    Check if assets are available for all dates in the temporal coverage period.

    Supports three types of collections:
    1. S3 paths (s3://...)
    2. STAC catalog URLs (https://...)


    Args:
        collection: Path/URL to the collection
        temporal_coverage: Number of days to check backwards
        current_date_str: Current date as string (format: 'YYYY-MM-DD')

    Returns:
        bool: True if all assets are present, False otherwise
    """
    target_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()

    # Check if the collection is stored on S3
    if collection.startswith("s3://"):
        # Parse the S3 bucket and prefix from the collection path
        s3_path = collection.replace("s3://", "")
        bucket_name = s3_path.split("/")[0]
        prefix = "/".join(s3_path.split("/")[1:])

        # Initialize S3
        main_utils.initialize_gee()

        # Use paginator to handle more than 1000 objects
        paginator = main_utils.s3.get_paginator('list_objects_v2')
        date_str = target_date.strftime('%Y%m%dT')

        # Paginate through all results and filter
        assets = []
        for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
            if 'Contents' in page:
                assets.extend([
                    obj['Key']
                    for obj in page['Contents']
                    if date_str in obj['Key'] and obj['Key'].endswith('.tif')
                ])

    # Check if the collection is a STAC catalog URL
    else:
        # Extract base URL and collection ID from the STAC URL
        try:
            # Use config.STAC_FSDI_API
            api_path = getattr(config, 'STAC_FSDI_API')
            stac_catalog_url, collection_id = main_utils.extract_collection_id_from_url(collection, api_path)
            print(f"Querying STAC catalog: {stac_catalog_url}")
            print(f"Collection ID: {collection_id}")
        except ValueError as e:
            print(f"Error parsing STAC URL: {e}")
            return False

        # Query STAC for items on the target date
        # We'll collect all items across the temporal coverage period
        check_date = target_date - timedelta(days=temporal_coverage)
        end_date = target_date

        assets = []
        while check_date <= end_date:
            daily_items = main_utils.get_stac_items_for_date(stac_catalog_url, collection_id, check_date)
            assets.extend(daily_items)
            check_date += timedelta(days=1)

    # For Earth Engine collections

    # Asset cleaning (only for Earth Engine assets with 'properties' and 'date')
    if 'cleaning_older_than' in config.step0[collection]:
        cleaning_target_date = target_date + \
            timedelta(days=-1 * config.step0[collection]['cleaning_older_than'])

        for asset in assets:
            # Check if asset has the expected structure
            if 'properties' in asset and 'date' in asset['properties']:
                date = asset['properties']['date']
                date_as_datetime = datetime.strptime(date, '%Y-%m-%d')

                if date_as_datetime.date() < cleaning_target_date:
                    print(f'Remove asset {date}')
                    print('XXX Actual asset deletion is not activated. Uncomment the code to do so XXXX')
                    # ee.data.deleteAsset(assetId=asset['id'])  # TODO: uncomment to actually delete

    # Check that asset is present for every date of the temporal coverage
    check_date = target_date - timedelta(days=temporal_coverage)
    end_date = target_date
    all_present = True
    has_any_real_data = False  # Track if we found at least one date with actual data

    while check_date <= end_date:
        asset_status = check_if_asset_prepared(
            collection, assets, check_date)

        # asset_status returns: 'ready', 'empty', or 'not_ready'
        if asset_status == 'not_ready':
            print(f'Asset not yet available for date {check_date}')
            all_present = False
        elif asset_status == 'ready':
            has_any_real_data = True

        check_date += timedelta(days=1)

    # Only mark collection as ready if:
    # 1. All dates are accounted for (either ready or empty)
    # 2. At least one date has actual data (not all dates are empty)
    return all_present and has_any_real_data


def check_if_asset_prepared(collection, assets, check_date):
    """
    Check if an asset is prepared for a given date.

    Supports two collection types:
    1. S3 collections (CLOUD_SCORE_PLUS and others starting with s3://)
    2. STAC collections (starting with http:// or https://)


    Args:
        collection: Collection path/URL
        assets: List of assets
        check_date: Date to check


    Returns:
        str: 'ready' if asset exists, 'empty' if in empty list, 'not_ready' if needs generation
    """
    # 1. we start by checking the state of the task
    #    (we start by that to fill the completed_tasks.csv if needed)
    # 2. if not running, check if the asset is already available
    # 3. if not in the available asset list, check if in empty_asset_list
    # 4. if not in running tasks, start task (if empty, write the empty_asset_list)

    check_date_str = check_date.strftime('%Y-%m-%d')
    print('checking date {}'.format(check_date))
    collection_basename = os.path.basename(collection)

    # For STAC collections, extract the collection ID as basename
    if collection.startswith("http://") or collection.startswith("https://"):
        if '#/collections/' in collection:
            collection_basename = collection.split('#/collections/')[1].strip('/')
        elif '/collections/' in collection:
            collection_basename = collection.split('/collections/')[1].strip('/')
        else:
            collection_basename = "STAC_COLLECTION"


    # 1. check if in asset list
    # Handle STAC collections
    if collection.startswith("http://") or collection.startswith("https://"):
        for asset in assets:
            if 'datetime' in asset:
                # Parse the datetime from the STAC item
                asset_datetime = asset['datetime']
                if isinstance(asset_datetime, str):
                    # Handle ISO format strings
                    asset_date = datetime.fromisoformat(
                        asset_datetime.replace('Z', '+00:00')
                    ).date()
                else:
                    asset_date = asset_datetime.date()

                if asset_date == check_date:
                    print('Collection {} READY for date {}'.format(
                        collection, check_date_str))
                    return 'ready'
            elif 'properties' in asset and 'datetime' in asset['properties']:
                # Alternative: datetime in properties
                asset_datetime = asset['properties']['datetime']
                if isinstance(asset_datetime, str):
                    asset_date = datetime.fromisoformat(
                        asset_datetime.replace('Z', '+00:00')
                    ).date()
                else:
                    asset_date = asset_datetime.date()

                if asset_date == check_date:
                    print('Collection {} READY for date {}'.format(
                        collection, check_date_str))
                    return 'ready'
        print('Item not found in STAC collection, continuing...')

    # Handle S3 collections (CLOUD_SCORE_PLUS)
    elif collection_basename == "CLOUD_SCORE_PLUS" or collection.startswith("s3://"):
        for asset in assets:
            if check_date.strftime('%Y%m%dT') in asset:
                print('Collection {} READY for date {}'.format(
                    collection, check_date_str))
                return 'ready'
        print('Asset not found in S3 collection, continuing...')



    # 2. if not in asset list check if in empty_asset_list
    df = pd.read_csv(config.EMPTY_ASSET_LIST)
    df_selection = df[(df.collection == collection_basename)
                      & (df.date == check_date_str)]
    if len(df_selection) > 0:
        print('Date found in empty_asset_list, skipping date (no source data available)')
        return 'empty'  # Return 'empty' status - date is accounted for but has no data

    # 3. Start asset generation if not found and not for STAC collections
    # # (STAC collections are read-only, we don't generate assets for them)
    # if collection.startswith("http://") or collection.startswith("https://"):
    #     print('STAC collection is read-only, cannot generate assets')
    #     return 'not_ready'

    print('Starting asset generation for {} / {}'.format(collection, check_date_str))
    generate_single_date_function = eval(
        config.step0[collection]['step0_function'])
    generate_single_date_function(check_date_str, collection)
    return 'not_ready'


def get_step0_dict():
    """
    This function is used to extract the step0 information from the config object and store it in a dictionary.
    The dictionary has the collection names as keys and the product names and temporal coverages as values
    """
    step0_dict = dict()
    for entry in dir(config):
        entry_value = getattr(config, entry)
        if not isinstance(entry_value, dict):
            continue
        if 'step0_collection' not in entry_value:
            continue
        temporal_coverage = int(entry_value['temporal_coverage'])
        collection_name = entry_value['step0_collection']
        base_collection = entry_value['image_collection']
        if collection_name not in step0_dict:
            step0_dict[collection_name] = [
                [entry, ], temporal_coverage, base_collection]
        else:
            if base_collection != step0_dict[collection_name][2]:
                raise BrokenPipeError(
                    'Inconsistent base collection in configuration file')

            temporal_coverage = max(
                step0_dict[collection_name][1], temporal_coverage)
            step0_dict[collection_name][0].append(entry)
            step0_dict[collection_name][1] = temporal_coverage

    return step0_dict