"""
STAC Asset Downloader by Asset Title

This script downloads all STAC assets matching a given asset title
from one or more collections.

Author: David Oesch
Date: 2026-01-19
License: MIT

Usage:
    python util_stac_download_by_title.py
"""

import json
import logging
import os
from pathlib import Path
from typing import List

import requests
import pystac_client


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

STAC_BASE_URL = "https://sys-data.int.bgdi.ch/api/stac/v0.9/"
CONFIG_PATH = r"C:\temp\satromo-dev\secrets\stac_fsdi-int.json"

ASSET_TITLE_TO_DOWNLOAD = "Cloud mask - 10m"

OUTPUT_BASE_DIR = r"C:\temp\stac_cloud_masksv_17_CPU"
OUTPUT_BASE_DIR = r"\\tsclient\M\Transfer\oed\stac_cloud_masksv_17_CPU"


# ---------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------

def load_credentials(config_path: str) -> tuple:
    with open(config_path, "r") as f:
        cfg = json.load(f)
    return cfg["FSDI"]["username"], cfg["FSDI"]["password"]


def setup_stac_client(url: str) -> pystac_client.Client:
    client = pystac_client.Client.open(url)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")
    return client


def list_collections(client: pystac_client.Client) -> List[str]:
    return [c.id for c in client.get_collections()]


def download_asset(asset_href: str, target_dir: str, auth: tuple) -> bool:
    try:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        filename = os.path.basename(asset_href)
        output_path = os.path.join(target_dir, filename)

        with requests.get(asset_href, auth=auth, stream=True) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        return True

    except Exception as e:
        logging.error(f"Download failed: {asset_href} | {str(e)}")
        return False


# ---------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------

def main():

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    auth = load_credentials(CONFIG_PATH)
    client = setup_stac_client(STAC_BASE_URL)

    print("\nAvailable collections:\n")
    collections = list_collections(client)
    for idx, col in enumerate(collections, 1):
        print(f"{idx}. {col}")

    selection = input(
        "\nEnter collection ID or pattern (substring match): "
    ).strip()

    matching_collections = [
        c for c in client.get_collections()
        if selection.lower() in c.id.lower()
    ]

    if not matching_collections:
        print("\nNo matching collections found.")
        return

    total_downloaded = 0
    total_failed = 0

    for collection in matching_collections:
        logging.info(f"Processing collection: {collection.id}")

        for item in collection.get_items():

            for asset_key, asset in item.get_assets().items():

                asset_title = getattr(asset, "title", None)

                if asset_title != ASSET_TITLE_TO_DOWNLOAD:
                    continue

                target_dir = os.path.join(
                    OUTPUT_BASE_DIR,
                    collection.id,
                    item.id
                )

                success = download_asset(
                    asset_href=asset.href,
                    target_dir=target_dir,
                    auth=auth
                )

                if success:
                    total_downloaded += 1
                    logging.info(
                        f"Downloaded: {collection.id}/{item.id}/{asset_key}"
                    )
                else:
                    total_failed += 1

    print("\n" + "=" * 60)
    print("DOWNLOAD SUMMARY")
    print("=" * 60)
    print(f"Asset title: {ASSET_TITLE_TO_DOWNLOAD}")
    print(f"Successfully downloaded: {total_downloaded}")
    print(f"Failed downloads: {total_failed}")


if __name__ == "__main__":
    main()
