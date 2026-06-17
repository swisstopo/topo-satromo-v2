#!/usr/bin/python3
"""
Upload acquisitionplan.csv as a collection-level STAC asset.

Collection-level assets live at:
  collections/{collection}/assets/{asset}
not under any item — analogous to how lubis-luftbilder_schraegaufnahmen_2056.gpkg
is stored at the collection root.

Usage:
    python util_upload_collection_asset.py [dev_config|prod_config]
    (default: dev_config)
"""

import hashlib
import importlib
import json
import os
import platform
import sys
from base64 import b64encode
from datetime import datetime
from hashlib import md5

import multihash
import requests

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

config_name = sys.argv[1].replace(".py", "") if len(sys.argv) > 1 else "dev_config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
config = importlib.import_module(f"configuration.{config_name}")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

COLLECTION = config.PRODUCT_S2_LEVEL_2A["product_name"]

ASSETS = [
    {
        "filename": "acquisitionplan.csv",
        "path": os.path.join(os.path.dirname(__file__), "..", "acquisitionplan.csv"),
        "title": "Collection AcquisitionPlan",
    },
    {
        "filename": "step0_empty_assets.csv",
        "path": os.path.join(os.path.dirname(__file__), "..", "tools", "step0_empty_assets.csv"),
        "title": "Collection DatesWithNoImagery",
    },
]

STAC_BASE = f"{config.STAC_FSDI_SCHEME}://{config.STAC_FSDI_HOSTNAME}{config.STAC_FSDI_API}"
ENV = "int" if ".int." in config.STAC_FSDI_HOSTNAME else "prod"

PART_SIZE_MB = 250

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials():
    if os.path.exists(config.FSDI_SECRETS):
        with open(config.FSDI_SECRETS, "r") as fh:
            data = json.load(fh)
        user = os.environ.get("STAC_USER", data["FSDI"]["username"])
        password = os.environ.get("STAC_PASSWORD", data["FSDI"]["password"])
        print(f"  Auth: local secrets file ({config.FSDI_SECRETS})")
    else:
        user = os.environ["FSDI_STAC_USER"]
        password = os.environ["FSDI_STAC_PASSWORD"]
        print("  Auth: environment variables (PROD)")
    return user, password


# ---------------------------------------------------------------------------
# Asset registration (PUT metadata)
# ---------------------------------------------------------------------------

def register_collection_asset(asset_path_url, asset_name, title, user, password):
    """PUT the asset metadata JSON to register the asset in the collection."""
    payload = {
        "id": asset_name,
        "title": title,
        "type": "text/csv",
    }
    response = requests.put(
        url=asset_path_url,
        json=payload,
        auth=(user, password),
    )
    if response.status_code in (200, 201):
        print(f"  Asset registered: {asset_path_url}")
        return True
    else:
        print(f"  ERROR registering asset (HTTP {response.status_code}): {response.text}")
        return False


# ---------------------------------------------------------------------------
# Multipart upload (collection-level)
# ---------------------------------------------------------------------------

def b64_md5(data):
    return b64encode(md5(data).digest()).decode("utf-8")


def multipart_upload_collection_asset(uploads_url, filepath, user, password):
    """Perform a STAC multipart upload for a collection-level asset."""
    file_size = os.path.getsize(filepath)
    part_size = min(PART_SIZE_MB * 1024 ** 2, file_size)

    # --- generate hashes ---
    sha256 = hashlib.sha256()
    md5_parts = []
    print("  Calculating checksums ...")
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(part_size)
            if not chunk:
                break
            sha256.update(chunk)
            md5_parts.append({"part_number": len(md5_parts) + 1, "md5": b64_md5(chunk)})

    checksum_multihash = multihash.to_hex_string(
        multihash.encode(sha256.digest(), "sha2-256")
    )

    # --- start multipart upload ---
    payload = {
        "number_parts": len(md5_parts),
        "md5_parts": md5_parts,
        "checksum:multihash": checksum_multihash,
        "update_interval": 30,
    }
    resp = requests.post(uploads_url, json=payload, auth=(user, password))
    if resp.status_code == 409 and "already in progress" in resp.text:
        print("  Upload already in progress — aborting previous and retrying ...")
        # abort previous
        in_prog = requests.get(uploads_url, params={"status": "in-progress"}, auth=(user, password))
        if in_prog.status_code == 200:
            for u in in_prog.json().get("uploads", []):
                requests.post(f"{uploads_url}/{u['upload_id']}/abort", auth=(user, password))
        resp = requests.post(uploads_url, json=payload, auth=(user, password))

    if resp.status_code != 201:
        print(f"  ERROR starting multipart upload (HTTP {resp.status_code}): {resp.text}")
        return False

    upload_id = resp.json()["upload_id"]
    upload_urls = resp.json()["urls"]
    print(f"  Upload started: {len(upload_urls)} part(s)")

    # --- upload parts ---
    etags = []
    with open(filepath, "rb") as fh:
        for url_entry in upload_urls:
            chunk = fh.read(part_size)
            part_num = url_entry["part"]
            r = requests.put(
                url_entry["url"],
                data=chunk,
                headers={"Content-MD5": md5_parts[part_num - 1]["md5"]},
            )
            if r.status_code != 200:
                print(f"  ERROR uploading part {part_num} (HTTP {r.status_code})")
                requests.post(f"{uploads_url}/{upload_id}/abort", auth=(user, password))
                return False
            etags.append({"etag": r.headers["ETag"], "part_number": part_num})
            print(f"  Part {part_num}/{len(upload_urls)} uploaded")

    # --- complete upload ---
    resp = requests.post(
        f"{uploads_url}/{upload_id}/complete",
        json={"parts": etags},
        auth=(user, password),
    )
    if resp.status_code == 200 and resp.json().get("status") == "completed":
        print("  Multipart upload completed.")
        return True
    else:
        print(f"  ERROR completing upload (HTTP {resp.status_code}): {resp.text}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Config : {config_name}")
    print(f"Collection: {COLLECTION}")
    print(f"STAC base : {STAC_BASE}")

    user, password = get_credentials()

    for asset in ASSETS:
        asset_name = asset["filename"].lower()
        asset_path = asset["path"]
        asset_title = asset["title"]

        print(f"\n--- {asset_title} ({asset_name}) ---")

        if not os.path.isfile(asset_path):
            print(f"  ERROR: file not found: {asset_path}")
            continue

        asset_path_url = f"{STAC_BASE}collections/{COLLECTION}/assets/{asset_name}"
        uploads_url = f"{asset_path_url}/uploads"

        print("  Registering asset metadata ...")
        if not register_collection_asset(asset_path_url, asset_name, asset_title, user, password):
            continue

        print("  Uploading data ...")
        if not multipart_upload_collection_asset(uploads_url, asset_path, user, password):
            continue

        print(f"  Done: {asset_path_url}")


if __name__ == "__main__":
    main()
