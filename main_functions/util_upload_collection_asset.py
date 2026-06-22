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
    python util_upload_collection_asset.py [dev_config.py|prod_config.py]

    # Upload a specific file:
    python util_upload_collection_asset.py prod_config.py \\
        --file path/to/myfile.csv \\
        --collection ch.swisstopo.swisseo_s2-sr_v200 \\
        --title "My Title" \\
        --stac-name myfile.csv
"""

import argparse
import importlib
import json
import os
import sys

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


def _get_fsdi_credentials(cfg):
    """Return (user, password) for FSDI STAC API.

    Uses main_utils.determine_run_type() to decide whether to read from the
    local secrets file (run_type 2 = dev) or from environment variables
    (run_type 1 = prod/GitHub), following the same pattern as initialize_gee().
    """
    from main_functions import main_utils
    main_utils.determine_run_type()
    if main_utils.run_type == 2:
        with open(cfg.FSDI_SECRETS, "r") as fh:
            data = json.load(fh)
        user = os.environ.get("STAC_USER", data["FSDI"]["username"])
        password = os.environ.get("STAC_PASSWORD", data["FSDI"]["password"])
        print(f"  Auth: local secrets ({cfg.FSDI_SECRETS})")
    else:
        user = os.environ["FSDI_STAC_USER"]
        password = os.environ["FSDI_STAC_PASSWORD"]
        print("  Auth: environment variables (PROD)")
    return user, password


def _register_asset(asset_url, stac_asset_name, asset_title, mime_type, user, password):
    """PUT asset metadata to register/update the asset slot in STAC."""
    payload = {"id": stac_asset_name, "title": asset_title, "type": mime_type}
    resp = requests.put(url=asset_url, json=payload, auth=(user, password))
    if resp.status_code in (200, 201):
        print(f"  Registered: {asset_url}")
        return True
    print(f"  ERROR registering (HTTP {resp.status_code}): {resp.text}")
    return False


def _multipart_upload(uploads_url, local_file, user, password):
    """Multipart-upload local_file to the given STAC uploads URL.

    Delegates to StacMultipartUploader from main_multipart_upload_via_api.
    That class builds an item-level URL internally, so we override
    uploads_url after construction — all methods use self.uploads_url,
    so the correct collection-level endpoint is used throughout.
    """
    from main_functions import main_multipart_upload_via_api as _mpu

    old_argv = sys.argv
    # StacMultipartUploader reads its config from sys.argv via get_args().
    # Use placeholder values for collection/item/asset since we override the URL.
    sys.argv = [
        "util_upload_collection_asset.py",
        "int",          # env (any valid choice; URL is overridden below)
        "placeholder",  # collection
        "placeholder",  # item
        "placeholder",  # asset
        local_file,
        "--username", user,
        "--password", password,
        "--force",
    ]
    try:
        uploader = _mpu.StacMultipartUploader()
        uploader.uploads_url = uploads_url   # override with collection-level URL
        uploader.credentials = (user, password)
        uploader.upload_file()
        return True
    except SystemExit:
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False
    finally:
        sys.argv = old_argv


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

    user, password = _get_fsdi_credentials(config)

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
        default="dev_config.py",
        help="Config module name: dev_config.py (default) or prod_config.py",
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

    user, password = _get_fsdi_credentials(cfg)

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
