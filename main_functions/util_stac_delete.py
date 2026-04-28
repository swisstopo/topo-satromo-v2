"""
STAC Asset and Item Deletion Script (Interactive Version)

This script deletes assets and items from a STAC (SpatioTemporal Asset Catalog) API.
It provides an interactive menu to either:
1. Delete all items from a specific collection
2. Delete a specific item by its ID

The script first deletes all assets associated with an item and then deletes the item itself
if all its assets were successfully deleted.

Author: David Oesch
Date: 2025-02-18
Modified: 2026-01-13
License: MIT License

Usage:
    python util_stac_delete.py

Dependencies:
    - pystac_client
    - requests
    - logging
    - json
    - urllib.parse

Functions:
    - load_credentials(config_path: str) -> tuple
    - setup_stac_client(url: str) -> pystac_client.Client
    - get_swisseo_collections(client: pystac_client.Client, collection_del: str) -> Generator
    - list_collections(client: pystac_client.Client) -> List[str]
    - get_collection_items_assets(collection) -> List[Dict]
    - get_single_item_assets(client: pystac_client.Client, collection_id: str, item_id: str) -> List[Dict]
    - delete_asset(base_url: str, collection_id: str, item_id: str, asset_key: str, auth: tuple) -> bool
    - delete_item(base_url: str, collection_id: str, item_id: str, auth: tuple) -> bool
    - delete_items_and_assets(base_url: str, items_assets: List[Dict], auth: tuple) -> Dict[str, List[str]]
    - prompt_deletion_mode() -> str
    - prompt_collection_selection(client: pystac_client.Client) -> str
    - prompt_item_id() -> str
    - confirm_deletion(deletion_type: str, target: str, item_count: int) -> bool
    - main() -> Dict[str, List[str]]

Example:
    To run the script, simply execute:
    python util_stac_delete_interactive.py
"""

import pystac_client
from typing import Dict, List, Generator, Optional
import logging
import requests
from urllib.parse import urljoin
import json
from pathlib import Path

# Configuration
base_url = "https://sys-data.int.bgdi.ch/api/stac/v0.9/"
config_path = Path("secrets") / "stac_fsdi-int.json"

# base_url = "https://data.geo.admin.ch/api/stac/v0.9/"
# config_path = Path("secrets") / "stac_fsdi-prod.json"

def load_credentials(config_path: str) -> tuple:
    """
    Load FSDI credentials from config file

    Args:
        config_path (str): Path to the config file

    Returns:
        tuple: (username, password)
    """
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return (config['FSDI']['username'], config['FSDI']['password'])
    except Exception as e:
        logging.error(f"Error loading credentials: {str(e)}")
        raise


def setup_stac_client(url) -> pystac_client.Client:
    """
    Initialize and setup STAC client with required conformance

    Args:
        url (str): STAC API endpoint URL

    Returns:
        pystac_client.Client: Configured STAC client
    """
    client = pystac_client.Client.open(url)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")
    return client


def get_swisseo_collections(client: pystac_client.Client, collection_del: str) -> Generator:
    """
    Retrieve all SwissEO collections matching the given pattern

    Args:
        client (pystac_client.Client): STAC client
        collection_del (str): Collection pattern to match

    Returns:
        Generator: Generator of SwissEO collections
    """
    return (
        collection for collection in client.get_collections()
        if collection_del.lower() in collection.id.lower()
    )


def list_collections(client: pystac_client.Client) -> List[str]:
    """
    List all available collections

    Args:
        client (pystac_client.Client): STAC client

    Returns:
        List[str]: List of collection IDs
    """
    return [collection.id for collection in client.get_collections()]


def get_collection_items_assets(collection) -> List[Dict]:
    """
    Get all assets from all items in a collection

    Args:
        collection: STAC collection

    Returns:
        List[Dict]: List of dictionaries containing item ID and its assets
    """
    items_assets = []

    for item in collection.get_items():
        item_assets = {
            'item_id': item.id,
            'collection_id': collection.id,
            'assets': {}
        }

        for asset_key, asset in item.get_assets().items():
            item_assets['assets'][asset_key] = {
                'href': asset.href,
                'type': asset.media_type,
                'roles': asset.roles if hasattr(asset, 'roles') else []
            }

        items_assets.append(item_assets)

    return items_assets


def get_single_item_assets(client: pystac_client.Client, collection_id: str, item_id: str) -> Optional[List[Dict]]:
    """
    Get assets for a single specific item

    Args:
        client (pystac_client.Client): STAC client
        collection_id (str): Collection ID
        item_id (str): Item ID

    Returns:
        Optional[List[Dict]]: List containing a single dictionary with item and its assets, or None if not found
    """
    try:
        collection = client.get_collection(collection_id)
        item = collection.get_item(item_id)

        if item is None:
            return None

        item_assets = {
            'item_id': item.id,
            'collection_id': collection.id,
            'assets': {}
        }

        for asset_key, asset in item.get_assets().items():
            item_assets['assets'][asset_key] = {
                'href': asset.href,
                'type': asset.media_type,
                'roles': asset.roles if hasattr(asset, 'roles') else []
            }

        return [item_assets]

    except Exception as e:
        logging.error(f"Error retrieving item {item_id} from collection {collection_id}: {str(e)}")
        return None


def delete_asset(base_url: str, collection_id: str, item_id: str, asset_key: str, auth: tuple) -> bool:
    """
    Delete a specific asset from an item

    Args:
        base_url (str): Base URL of the STAC API
        collection_id (str): Collection ID
        item_id (str): Item ID
        asset_key (str): Asset key to delete
        auth (tuple): Authentication credentials (username, password)

    Returns:
        bool: True if deletion was successful, False otherwise
    """
    delete_url = urljoin(base_url, f"collections/{collection_id}/items/{item_id}/assets/{asset_key}")
    print(f"Deleting asset: {asset_key}")
    try:
        response = requests.delete(delete_url, auth=auth)
        return response.status_code in [200, 204]
    except Exception as e:
        logging.error(f"Error deleting asset {asset_key} from item {item_id}: {str(e)}")
        return False


def delete_item(base_url: str, collection_id: str, item_id: str, auth: tuple) -> bool:
    """
    Delete a specific item from a collection

    Args:
        base_url (str): Base URL of the STAC API
        collection_id (str): Collection ID
        item_id (str): Item ID
        auth (tuple): Authentication credentials (username, password)

    Returns:
        bool: True if deletion was successful, False otherwise
    """
    delete_url = urljoin(base_url, f"collections/{collection_id}/items/{item_id}")
    print(f"Deleting item: {item_id}")
    try:
        response = requests.delete(delete_url, auth=auth)
        return response.status_code in [200, 204]
    except Exception as e:
        logging.error(f"Error deleting item {item_id}: {str(e)}")
        return False


def delete_items_and_assets(base_url: str, items_assets: List[Dict], auth: tuple) -> Dict[str, List[str]]:
    """
    Delete all assets and items in the correct order

    Args:
        base_url (str): Base URL of the STAC API
        items_assets (List[Dict]): List of items and their assets to delete
        auth (tuple): Authentication credentials (username, password)

    Returns:
        Dict[str, List[str]]: Summary of successful and failed deletions
    """
    results = {
        'successful_asset_deletions': [],
        'failed_asset_deletions': [],
        'successful_item_deletions': [],
        'failed_item_deletions': []
    }

    for item in items_assets:
        item_id = item['item_id']
        collection_id = item['collection_id']

        # First delete all assets for this item
        all_assets_deleted = True
        for asset_key in item['assets'].keys():
            success = delete_asset(base_url, collection_id, item_id, asset_key, auth)
            if success:
                results['successful_asset_deletions'].append(f"{item_id}/{asset_key}")
            else:
                results['failed_asset_deletions'].append(f"{item_id}/{asset_key}")
                all_assets_deleted = False

        # Only delete the item if all its assets were successfully deleted
        if all_assets_deleted:
            success = delete_item(base_url, collection_id, item_id, auth)
            if success:
                results['successful_item_deletions'].append(item_id)
            else:
                results['failed_item_deletions'].append(item_id)
        else:
            results['failed_item_deletions'].append(item_id)
            logging.warning(f"Skipping item deletion for {item_id} due to failed asset deletions")

    return results


def get_date_range_items(client: pystac_client.Client, collection_id: str, start_date: str, end_date: str) -> List[Dict]:
    """
    Search for items within a specific date range in a collection
    """
    items_assets = []

    # Format: "YYYY-MM-DDTHH:MM:SSZ/YYYY-MM-DDTHH:MM:SSZ"
    datetime_range = f"{start_date}/{end_date}"

    search = client.search(
        collections=[collection_id],
        datetime=datetime_range
    )

    for item in search.item_collection():
        item_assets = {
            'item_id': item.id,
            'collection_id': collection_id,
            'assets': {}
        }
        for asset_key, asset in item.get_assets().items():
            item_assets['assets'][asset_key] = {
                'href': asset.href,
                'type': asset.media_type,
                'roles': asset.roles if hasattr(asset, 'roles') else []
            }
        items_assets.append(item_assets)

    return items_assets

def prompt_date_range() -> tuple:
    """Prompt user for start and end dates"""
    print("\nEnter dates in YYYY-MM-DD format (or ISO 8601):")
    start = input("Start Date (e.g., 2023-01-01): ").strip()
    end = input("End Date   (e.g., 2023-12-31): ").strip()
    return start, end

def prompt_deletion_mode() -> str:
    print("\n" + "="*60)
    print("STAC DELETION TOOL")
    print("="*60)
    print("\nSelect deletion mode:")
    print("  1. Delete all items from a collection")
    print("  2. Delete a specific item")
    print("  3. Delete items in a collection by DATE RANGE") # Added Choice 3
    print()

    while True:
        choice = input("Enter your choice (1, 2, or 3): ").strip()
        if choice == '1': return 'collection'
        elif choice == '2': return 'item'
        elif choice == '3': return 'date_range'
        else: print("Invalid choice. Please enter 1, 2, or 3.")


def prompt_collection_selection(client: pystac_client.Client) -> str:
    """
    Prompt user to select or enter a collection ID

    Args:
        client (pystac_client.Client): STAC client

    Returns:
        str: Collection ID or pattern
    """
    print("\n" + "-"*60)
    print("COLLECTION SELECTION")
    print("-"*60)
    print("\nFetching available collections...")

    try:
        collections = list_collections(client)
        print(f"\nFound {len(collections)} collections:")
        for i, col_id in enumerate(collections, 1):
            print(f"  {i}. {col_id}")
    except Exception as e:
        logging.warning(f"Could not list collections: {str(e)}")
        collections = []

    print("\nOptions:")
    print("  - Enter a collection ID or pattern (e.g., 'ch.swisstopo.swisseo_s2-sr_v200')")
    if collections:
        print("  - Enter a number to select from the list above")
    print()

    while True:
        choice = input("Enter collection ID/pattern or number: ").strip()

        if not choice:
            print("Please enter a valid collection ID or number.")
            continue

        # Check if it's a number
        if choice.isdigit() and collections:
            idx = int(choice) - 1
            if 0 <= idx < len(collections):
                return collections[idx]
            else:
                print(f"Invalid number. Please enter a number between 1 and {len(collections)}.")
        else:
            return choice


def prompt_item_id() -> tuple:
    """
    Prompt user to enter collection ID and item ID

    Returns:
        tuple: (collection_id, item_id)
    """
    print("\n" + "-"*60)
    print("ITEM SELECTION")
    print("-"*60)
    print()

    collection_id = input("Enter collection ID: ").strip()
    while not collection_id:
        print("Collection ID cannot be empty.")
        collection_id = input("Enter collection ID: ").strip()

    item_id = input("Enter item ID: ").strip()
    while not item_id:
        print("Item ID cannot be empty.")
        item_id = input("Enter item ID: ").strip()

    return collection_id, item_id


def confirm_deletion(deletion_type: str, target: str, item_count: int) -> bool:
    """
    Ask user to confirm deletion

    Args:
        deletion_type (str): 'collection' or 'item'
        target (str): Collection ID/pattern or item ID
        item_count (int): Number of items that will be deleted

    Returns:
        bool: True if user confirms, False otherwise
    """
    print("\n" + "="*60)
    print("DELETION CONFIRMATION")
    print("="*60)

    if deletion_type == 'collection':
        print(f"\nYou are about to delete ALL items from collection(s) matching: {target} on {base_url}")
        print(f"Total items to be deleted: {item_count}")
    else:
        print(f"\nYou are about to delete item: {target} on {base_url}")
        print(f"Number of assets in this item: {item_count}")

    print("\n⚠️  WARNING: This action cannot be undone! ⚠️")
    print()

    confirmation = input('Type "I AGREE" (exactly) to proceed with deletion: ')

    return confirmation == "I AGREE"


def main():
    """
    Main function to orchestrate the deletion process

    Returns:
        Dict[str, List[str]]: Summary of deletion results
    """
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)



    try:
        auth = load_credentials(config_path)
        client = setup_stac_client(base_url)
        mode = prompt_deletion_mode()

        all_assets = []
        target_description = ""

        if mode == 'collection':
            collection_pattern = prompt_collection_selection(client)
            target_description = f"ALL items in collection(s) matching '{collection_pattern}'"
            for collection in get_swisseo_collections(client, collection_pattern):
                all_assets.extend(get_collection_items_assets(collection))

        elif mode == 'item':
            collection_id, item_id = prompt_item_id()
            target_description = f"item '{item_id}'"
            item_assets = get_single_item_assets(client, collection_id, item_id)
            if item_assets: all_assets = item_assets

        elif mode == 'date_range':
            collection_id = prompt_collection_selection(client)
            start_dt, end_dt = prompt_date_range()
            target_description = f"items in '{collection_id}' between {start_dt} and {end_dt}"

            logger.info(f"Searching for items in {collection_id} from {start_dt} to {end_dt}...")
            all_assets = get_date_range_items(client, collection_id, start_dt, end_dt)

        # Validation and Confirmation
        if not all_assets:
            print(f"\n❌ No items found for the specified criteria.")
            return None

        if not confirm_deletion(mode, target_description, len(all_assets)):
            print("\n❌ Deletion cancelled by user")
            return None

        # Execution
        print("\n" + "="*60 + "\nSTARTING DELETION PROCESS\n" + "="*60)
        results = delete_items_and_assets(base_url, all_assets, auth)

        # Log summary
        print("\n" + "="*60)
        print("DELETION SUMMARY")
        print("="*60)
        print(f"Successfully deleted assets: {len(results['successful_asset_deletions'])}")
        print(f"Failed asset deletions: {len(results['failed_asset_deletions'])}")
        print(f"Successfully deleted items: {len(results['successful_item_deletions'])}")
        print(f"Failed item deletions: {len(results['failed_item_deletions'])}")

        if results['failed_asset_deletions']:
            print("\n⚠️  Failed asset deletions:")
            for asset in results['failed_asset_deletions']:
                print(f"  - {asset}")

        if results['failed_item_deletions']:
            print("\n⚠️  Failed item deletions:")
            for item in results['failed_item_deletions']:
                print(f"  - {item}")

        return results

    except KeyboardInterrupt:
        print("\n\n❌ Operation cancelled by user (Ctrl+C)")
        return None
    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}")
        raise


if __name__ == "__main__":
    results = main()
    print("\n✅ Script execution completed")