#!/usr/bin/python3
"""
Upload collection-level STAC assets.

As a library function (from step1_processor_s2_sr.py or any other script):

    from main_functions import util_upload_collection_asset
    util_upload_collection_asset.publish_collection_asset(
        local_file="acquisitionplan.csv",
        collection=config.PRODUCT_S2_LEVEL_2A["product_name"],
        asset_title="Collection AcquisitionPlan",
        stac_asset_name="acquisitionplan.csv",   # optional, defaults to basename
    )

As a CLI script:

    # Upload default asset list (acquisitionplan.csv + step0_empty_assets.csv):
    python util_upload_collection_asset.py [dev_config|prod_config]

    # Upload a specific file:
    python util_upload_collection_asset.py prod_config \\
        --file path/to/myfile.csv \\
        --collection ch.swisstopo.swisseo_s2-sr_v200 \\
        --title "My Title" \\
        --stac-name myfile.csv
"""

import argparse
import hashlib
import importlib
import json
import os
import sys
from base64 import b64encode
from hashlib import md5

import multihash
import requests


# ---------------------------------------------------------------------------
# Default asset list (used by CLI when no --file is given)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_SCRIPT_DIR, "..")

DEFAULT_ASSETS = [
    {
        "local_file": os.path.join(_REPO_ROOT,"tools", "acquisitionplan.csv"),
        "asset_title": "Collection AcquisitionPlan",
        "stac_asset_name": "acquisitionplan.csv",
    },
    {
        "local_file": os.path.join(_REPO_ROOT, "tools", "step0_empty_assets.csv"),
        "asset_title": "Collection DatesWithNoImagery",
        "stac_asset_name": "dates_with_no_imagery.csv",
    },
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config(config_name):
    """Import configuration module by name (dev_config / prod_config)."""
    config_name = config_name.replace(".py", "")
    sys.path.insert(0, _REPO_ROOT)
    return importlib.import_module(f"configuration.{config_name}")


def _get_credentials(config):
    """Return (user, password) from local secrets file or env vars."""
    if os.path.exists(config.FSDI_SECRETS):
        with open(config.FSDI_SECRETS, "r") as fh:
            data = json.load(fh)
        user = os.environ.get("STAC_USER", data["FSDI"]["username"])
        password = os.environ.get("STAC_PASSWORD", data["FSDI"]["password"])
        print(f"  Auth: local secrets ({config.FSDI_SECRETS})")
    else:
        user = os.environ["FSDI_STAC_USER"]
        password = os.environ["FSDI_STAC_PASSWORD"]
        print("  Auth: environment variables (PROD)")
    return user, password


def _b64_md5(data):
    return b64encode(md5(data).digest()).decode("utf-8")


def _register_asset(asset_url, stac_asset_name, asset_title, mime_type, user, password):
    """PUT asset metadata to register/update the asset slot in STAC."""
    payload = {"id": stac_asset_name, "title": asset_title, "type": mime_type}
    resp = requests.put(url=asset_url, json=payload, auth=(user, password))
    if resp.status_code in (200, 201):
        print(f"  Registered: {asset_url}")
        return True
    print(f"  ERROR registering (HTTP {resp.status_code}): {resp.text}")
    return False


def _multipart_upload(uploads_url, local_file, user, password, part_size_mb=250):
    """Multipart-upload local_file to the given STAC uploads URL."""
    file_size = os.path.getsize(local_file)
    part_size = min(part_size_mb * 1024 ** 2, file_size)

    sha256 = hashlib.sha256()
    md5_parts = []
    print("  Calculating checksums ...")
    with open(local_file, "rb") as fh:
        while True:
            chunk = fh.read(part_size)
            if not chunk:
                break
            sha256.update(chunk)
            md5_parts.append({"part_number": len(md5_parts) + 1, "md5": _b64_md5(chunk)})

    checksum_multihash = multihash.to_hex_string(
        multihash.encode(sha256.digest(), "sha2-256")
    )

    payload = {
        "number_parts": len(md5_parts),
        "md5_parts": md5_parts,
        "checksum:multihash": checksum_multihash,
        "update_interval": 30,
    }

    resp = requests.post(uploads_url, json=payload, auth=(user, password))

    if resp.status_code == 409 and "already in progress" in resp.text:
        print("  Upload in progress — aborting previous ...")
        in_prog = requests.get(uploads_url, params={"status": "in-progress"}, auth=(user, password))
        if in_prog.status_code == 200:
            for u in in_prog.json().get("uploads", []):
                requests.post(f"{uploads_url}/{u['upload_id']}/abort", auth=(user, password))
        resp = requests.post(uploads_url, json=payload, auth=(user, password))

    if resp.status_code != 201:
        print(f"  ERROR starting upload (HTTP {resp.status_code}): {resp.text}")
        return False

    upload_id = resp.json()["upload_id"]
    upload_urls = resp.json()["urls"]
    print(f"  Uploading: {len(upload_urls)} part(s)")

    etags = []
    with open(local_file, "rb") as fh:
        for url_entry in upload_urls:
            chunk = fh.read(part_size)
            part_num = url_entry["part"]
            r = requests.put(
                url_entry["url"],
                data=chunk,
                headers={"Content-MD5": md5_parts[part_num - 1]["md5"]},
            )
            if r.status_code != 200:
                print(f"  ERROR part {part_num} (HTTP {r.status_code})")
                requests.post(f"{uploads_url}/{upload_id}/abort", auth=(user, password))
                return False
            etags.append({"etag": r.headers["ETag"], "part_number": part_num})
            print(f"  Part {part_num}/{len(upload_urls)} done")

    resp = requests.post(
        f"{uploads_url}/{upload_id}/complete",
        json={"parts": etags},
        auth=(user, password),
    )
    if resp.status_code == 200 and resp.json().get("status") == "completed":
        print("  Upload completed.")
        return True
    print(f"  ERROR completing (HTTP {resp.status_code}): {resp.text}")
    return False


def _mime_for(local_file):
    ext = os.path.splitext(local_file)[1].lower()
    return {
        ".csv": "text/csv",
        ".json": "application/json",
        ".geojson": "application/geo+json",
        ".parquet": "application/vnd.apache.parquet",
        ".gpkg": "application/geopackage+sqlite3",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def publish_collection_asset(local_file, collection, asset_title, stac_asset_name=None):
    """
    Upload local_file as a collection-level asset on STAC.

    Args:
        local_file (str): Path to the local file to upload.
        collection (str): STAC collection ID
                          (e.g. config.PRODUCT_S2_LEVEL_2A["product_name"]).
        asset_title (str): Human-readable title shown in STAC.
        stac_asset_name (str): Asset key / filename on STAC.
                               Defaults to the lowercased basename of local_file.

    Returns:
        bool: True on success, False on any error.
    """
    import configuration as config  # uses whichever config the caller already loaded

    if stac_asset_name is None:
        stac_asset_name = os.path.basename(local_file).lower()

    print(f"\n[publish_collection_asset] {stac_asset_name} -> {collection}")

    if not os.path.isfile(local_file):
        print(f"  ERROR: file not found: {local_file}")
        return False

    user, password = _get_credentials(config)

    stac_base = f"{config.STAC_FSDI_SCHEME}://{config.STAC_FSDI_HOSTNAME}{config.STAC_FSDI_API}"
    asset_url = f"{stac_base}collections/{collection}/assets/{stac_asset_name}"
    uploads_url = f"{asset_url}/uploads"

    if not _register_asset(asset_url, stac_asset_name, asset_title, _mime_for(local_file), user, password):
        return False

    if not _multipart_upload(uploads_url, local_file, user, password):
        return False

    print(f"  Done: {asset_url}")
    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Upload collection-level STAC assets."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="dev_config",
        help="Config module name: dev_config (default) or prod_config",
    )
    parser.add_argument("--file", help="Local file to upload (overrides default list)")
    parser.add_argument("--collection", help="STAC collection ID (required with --file)")
    parser.add_argument("--title", help="Asset title on STAC (required with --file)")
    parser.add_argument(
        "--stac-name",
        dest="stac_name",
        help="Asset key/filename on STAC (default: lowercased basename of --file)",
    )
    args = parser.parse_args()

    cfg = _load_config(args.config)

    stac_base = f"{cfg.STAC_FSDI_SCHEME}://{cfg.STAC_FSDI_HOSTNAME}{cfg.STAC_FSDI_API}"
    print(f"Config    : {args.config}")
    print(f"STAC base : {stac_base}")

    user, password = _get_credentials(cfg)

    if args.file:
        # Single-file mode
        if not args.collection or not args.title:
            parser.error("--collection and --title are required when --file is given")
        stac_asset_name = args.stac_name or os.path.basename(args.file).lower()
        local_file = args.file
        collection = args.collection
        asset_title = args.title

        print(f"\n--- {asset_title} ({stac_asset_name}) ---")
        if not os.path.isfile(local_file):
            print(f"  ERROR: file not found: {local_file}")
            sys.exit(1)

        asset_url = f"{stac_base}collections/{collection}/assets/{stac_asset_name}"
        if not _register_asset(asset_url, stac_asset_name, asset_title, _mime_for(local_file), user, password):
            sys.exit(1)
        if not _multipart_upload(asset_url + "/uploads", local_file, user, password):
            sys.exit(1)
        print(f"  Done: {asset_url}")

    else:
        # Default list mode
        collection = cfg.PRODUCT_S2_LEVEL_2A["product_name"]
        print(f"Collection: {collection}")

        for asset in DEFAULT_ASSETS:
            local_file = asset["local_file"]
            asset_title = asset["asset_title"]
            stac_asset_name = asset["stac_asset_name"]

            print(f"\n--- {asset_title} ({stac_asset_name}) ---")

            if not os.path.isfile(local_file):
                print(f"  ERROR: file not found: {local_file}")
                continue

            asset_url = f"{stac_base}collections/{collection}/assets/{stac_asset_name}"
            if not _register_asset(asset_url, stac_asset_name, asset_title, _mime_for(local_file), user, password):
                continue
            if not _multipart_upload(asset_url + "/uploads", local_file, user, password):
                continue
            print(f"  Done: {asset_url}")


if __name__ == "__main__":
    _cli()
