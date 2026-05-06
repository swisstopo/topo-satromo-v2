"""
util_terrain_backfill.py
========================
Backfill missing terrain mask assets in the STAC catalogue.

For a given year, this script scans all items in the SwissEO Sentinel-2 SR
collection and checks whether each item already has a terrain mask asset
(title: "Terrain mask - 10m"). If the asset is missing, the script:

  1. Reads ORBIT_NR and the acquisition datetime from the item metadata JSON.
  2. Calls main_terrain_parallel() to generate the terrain mask GeoTIFF.
  3. Publishes the result to STAC via publish_to_stac().

Usage (run from project root):
    python util_terrain_backfill.py dev_config.py --year 2025
    python util_terrain_backfill.py dev_config.py --year 2025 --dry-run

The first positional argument is the configuration file (same convention as
all other scripts in this project). It is consumed by configuration/__init__.py.

The script follows the same STAC connection pattern as util_stac_delete.py
(credentials from secrets/stac_fsdi-int.json, pystac_client).

Configuration:
    BASE_URL        : STAC API endpoint
    CONFIG_PATH     : Path to FSDI credentials JSON
    COLLECTION_ID   : STAC collection to scan
    GEOCAT_ID       : Geocat ID for terrain mask asset
    ASSET_TITLE     : Asset title used to detect existing terrain mask assets
    ASSET_NAME_TPL  : Asset filename template (formatted with timedate)

Dependencies:
    pystac_client, requests, json, logging, pathlib
    main_functions.main_terrain_parallel
    main_functions.main_publish_stac_fsdi
"""

import sys
import os
import json
import logging
import argparse
import requests
from pathlib import Path
from urllib.parse import urljoin
import subprocess

import pystac_client

# ---------------------------------------------------------------------------
# Path setup
# This script lives in the project root and is run from there:
#   python util_terrain_backfill.py dev_config.py --year 2025
#
# The configuration package (configuration/__init__.py) reads sys.argv[1]
# as the config filename. We consume --year and --dry-run ourselves via
# argparse BEFORE configuration is imported, so that sys.argv still contains
# the config filename at position 1 when configuration/__init__.py runs.
#
# sys.argv expected by configuration/__init__.py:
#   sys.argv[0] = script name   (ignored)
#   sys.argv[1] = config file   e.g. "dev_config.py"
# ---------------------------------------------------------------------------
# __file__ ist main_functions/util_terrain_backfill.py
# Projektwurzel ist eine Ebene höher
# Ganz oben nach den Standard-Imports, VOR dem configuration-Import:
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR  = os.path.dirname(THIS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, THIS_DIR)

# Default-Argumente fuer VSCode-Start (kein Terminal-Aufruf)
if len(sys.argv) == 1:
    sys.argv = [sys.argv[0], "dev_config.py", "--year", "2026", "--dry-run"]

_original_argv = sys.argv[:]
sys.argv = sys.argv[:2]

import configuration as config  # noqa: F401

sys.argv = _original_argv

from main_terrain_parallel import create_terrain_mask
from main_publish_stac_fsdi import publish_to_stac

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# STAC endpoint (switch to prod by uncommenting the second pair)
BASE_URL    = "https://sys-data.int.bgdi.ch/api/stac/v0.9/"
CONFIG_PATH = Path("secrets") / "stac_fsdi-int.json"

# BASE_URL    = "https://data.geo.admin.ch/api/stac/v0.9/"
# CONFIG_PATH = Path("secrets") / "stac_fsdi-prod.json"

# STAC collection to scan
COLLECTION_ID = "ch.swisstopo.swisseo_s2-sr_v200"

#current  – ignorieren
CURRENT_ITEM_ID = "swisseo_s2-sr_v200"

# Geocat ID for terrain mask asset (update as needed)
GEOCAT_ID = "a4bc1c7a-3e2f-4d95-9d86-a1a0b09b11a7"

# Asset identification
ASSET_TITLE    = "Terrain mask - 10m"
ASSET_NAME_TPL = "swisseo_s2-sr_v200_mosaic_{timedate}_terrainmask_10m.tif"

# ---------------------------------------------------------------------------
# Credential and STAC helpers (same pattern as util_stac_delete.py)
# ---------------------------------------------------------------------------

def load_credentials(config_path):
    """
    Load FSDI credentials from a JSON config file.

    Args:
        config_path : Path to JSON file with structure {"FSDI": {"username": ..., "password": ...}}

    Returns:
        (username, password) tuple
    """
    with open(config_path, "r") as f:
        cfg = json.load(f)
    return (cfg["FSDI"]["username"], cfg["FSDI"]["password"])


def setup_stac_client(url):
    """
    Initialise pystac_client with required conformance declarations.

    Args:
        url : STAC API root URL

    Returns:
        pystac_client.Client
    """
    client = pystac_client.Client.open(url)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")
    return client


# ---------------------------------------------------------------------------
# Item inspection helpers
# ---------------------------------------------------------------------------

def item_has_terrain_mask(item):
    """
    Check whether a STAC item already has a terrain mask asset.

    Detection is based on the asset title ("Terrain mask - 10m").
    Falls back to checking the asset key/href for the expected filename pattern.

    Args:
        item : pystac.Item

    Returns:
        True if terrain mask asset exists, False otherwise.
    """
    for asset_key, asset in item.get_assets().items():
        # Primary check: asset title
        title = getattr(asset, "title", None) or asset.extra_fields.get("title", "")
        if title == ASSET_TITLE:
            return True
        # Fallback: filename pattern in href
        href = asset.href or ""
        if "terrainmask_10m" in href.lower():
            return True
    return False


def read_metadata_json(item, auth):
    """
    Download and parse the metadata JSON asset of a STAC item.

    Looks for an asset whose href ends with '_metadata.json'.

    Args:
        item : pystac.Item
        auth : (username, password) tuple for HTTP Basic Auth

    Returns:
        dict with parsed JSON content, or None if not found / download failed.
    """
    for asset_key, asset in item.get_assets().items():
        href = asset.href or ""
        if href.endswith("_metadata.json"):
            try:
                response = requests.get(href, auth=auth, timeout=30)
                if response.status_code == 200:
                    return response.json()
                else:
                    logging.warning(
                        f"Could not download metadata for {item.id}: "
                        f"HTTP {response.status_code}"
                    )
            except Exception as exc:
                logging.warning(f"Error downloading metadata for {item.id}: {exc}")
            return None

    logging.warning(f"No metadata JSON asset found for item {item.id}")
    return None


def extract_orbit_nr(metadata):
    """
    Extract ORBIT_NR from the PROPERTIES section of a metadata JSON.

    Args:
        metadata : dict as returned by read_metadata_json()

    Returns:
        ORBIT_NR as string (e.g. "22"), or None if not found.
    """
    try:
        return str(metadata["PROPERTIES"]["ORBIT_NR"])
    except (KeyError, TypeError):
        return None


def extract_timedate(item_id):
    """
    Extract the acquisition timedate string from a STAC item ID.

    Item IDs follow the pattern:
        swisseo_s2-sr_v200_mosaic_YYYY-MM-DDtHHMMSS_...
    Example:
        swisseo_s2-sr_v200_mosaic_2025-06-01t101041_cloudmask_10m

    Args:
        item_id : STAC item ID string

    Returns:
        timedate string in format YYYY-MM-DDtHHMMSS (e.g. "2025-06-01t101041"),
        or None if not parseable.
    """
    parts = item_id.split("_")
    for part in parts:
        # Match YYYY-MM-DDtHHMMSS pattern
        if len(part) >= 15 and "t" in part and part[:4].isdigit():
            return part
    return None

# ---------------------------------------------------------------------------
# Terrain-Prozess Wrapper (muss auf Modulebene sein fuer mp.Process pickle)
# ---------------------------------------------------------------------------

def _run_terrain(orbit, timedate, output_filename, queue):
    """
    Wrapper fuer create_terrain_mask in einem isolierten Prozess.
    Gibt Resultat via multiprocessing.Queue zurueck.
    Muss auf Modulebene definiert sein damit mp.Process sie pickeln kann.
    """
    try:
        success = create_terrain_mask(orbit, timedate, output_filename)
        queue.put(success)
    except Exception as exc:
        logging.error(f"create_terrain_mask in subprocess failed: {exc}", exc_info=True)
        queue.put(False)
# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------

def process_year(year, dry_run=False):
    """
    Scan all STAC items for a given year and backfill missing terrain masks.

    For each item in the collection that falls within the requested year and
    does not yet have a terrain mask asset, this function:
      1. Downloads and parses the metadata JSON to retrieve ORBIT_NR.
      2. Calls main_terrain_parallel() to generate the terrain GeoTIFF.
      3. Calls publish_to_stac() to upload the result.

    Args:
        year    : int, e.g. 2025
        dry_run : if True, log what would be done but do not generate or upload anything.

    Returns:
        dict with keys:
            processed   : list of item IDs successfully processed
            skipped     : list of item IDs that already had a terrain mask
            failed      : list of item IDs where processing failed
    """
    results = {"processed": [], "skipped": [], "failed": []}

    # --- Connect to STAC ---
    auth   = load_credentials(CONFIG_PATH)
    client = setup_stac_client(BASE_URL)

    # --- Search items for the requested year ---
    start_dt = f"{year}-01-01T00:00:00Z"
    end_dt   = f"{year}-12-31T23:59:59Z"
    logging.info(f"Searching {COLLECTION_ID} for year {year} ({start_dt} / {end_dt})")

    search = client.search(
        collections=[COLLECTION_ID],
        datetime=f"{start_dt}/{end_dt}",
    )

    items = list(search.item_collection())
    logging.info(f"Found {len(items)} items for year {year}")

    for item in items:
        item_id = item.id

        # current  – ignorieren
        if item_id == CURRENT_ITEM_ID:
            logging.info(f"  SKIP: current item '{item_id}' ignoriert")
            continue
        logging.info(f"--- Processing item: {item_id}")

        # --- Check if terrain mask already exists ---
        if item_has_terrain_mask(item):
            logging.info(f"  SKIP: terrain mask already present")
            results["skipped"].append(item_id)
            continue

        # --- Extract timedate from item ID ---
        timedate = extract_timedate(item_id)
        if not timedate:
            logging.warning(f"  FAIL: cannot extract timedate from item ID '{item_id}'")
            results["failed"].append(item_id)
            continue

        # --- Download and parse metadata JSON ---
        metadata = read_metadata_json(item, auth)
        if not metadata:
            logging.warning(f"  FAIL: could not read metadata JSON for {item_id}")
            results["failed"].append(item_id)
            continue

        # --- Extract ORBIT_NR ---
        orbit_nr = extract_orbit_nr(metadata)
        if not orbit_nr:
            logging.warning(f"  FAIL: ORBIT_NR not found in metadata for {item_id}")
            results["failed"].append(item_id)
            continue

        # --- Build output filename ---
        output_filename = ASSET_NAME_TPL.format(timedate=timedate)
        print(f"Working on: orbit_nr={orbit_nr}, timedate={timedate}, output_filename={output_filename}")
        logging.info(
            f"  orbit={orbit_nr}, timedate={timedate}, output={output_filename}"
        )

        if dry_run:
            logging.info(f"  DRY-RUN: would call main_terrain_parallel and publish_to_stac")
            results["processed"].append(item_id)
            continue

        # --- Generate terrain mask ---
        # subprocess.run startet einen voellig isolierten Python-Prozess.
        # Kein gemeinsamer Speicher mit Embree/HORAYZON -> kein "scene not committed".
        # Die interne Parallelisierung (n_proc Worker) in create_terrain_mask
        # bleibt vollstaendig erhalten.

        python_exe  = sys.executable
        script_path = os.path.join(THIS_DIR, "main_terrain_parallel.py")

        cmd = [
            python_exe,
            script_path,
            "--orbit",   orbit_nr,
            "--timedate", timedate,
            "--output",  output_filename,
        ]
        logging.info(f"  Starte Terrain-Prozess: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                check=False,          # Fehler selbst behandeln
                capture_output=False, # Output direkt in Konsole
                cwd=ROOT_DIR,         # Arbeitsverzeichnis = Projektwurzel
            )
            success = (result.returncode == 0)
        except Exception as exc:
            logging.error(f"  FAIL: subprocess.run raised: {exc}", exc_info=True)
            results["failed"].append(item_id)
            continue

        if not success:
            logging.error(f"  FAIL: create_terrain_mask returned False for {item_id}")
            results["failed"].append(item_id)
            continue

        if success is not True :
            logging.error(f"  FAIL: create_terrain_mask raised exception: {orbit_nr}, {timedate}, {output_filename}")
            results["failed"].append(item_id)
            continue
        else:

            logging.info(f"  Terrain mask generated: {output_filename}")

            publish_to_stac(
                    raw_asset=output_filename,
                    raw_item=item_id,
                    collection=COLLECTION_ID,
                    geocat_id=GEOCAT_ID,
                    current=None,
                    asset_title=ASSET_TITLE,
                )
            logging.info(f"  Published to STAC: {output_filename}")
            results["processed"].append(item_id)
        # --- Clean up local file after successful upload ---
        if os.path.isfile(output_filename):
            os.remove(output_filename)
            logging.info(f"  Local file removed: {output_filename}")

    return results
    #     # --- Generate terrain mask ---

    #     success = create_terrain_mask(orbit_nr, timedate, output_filename,sequential=True )
    #         #breakpoint()  # Debug: Nach create_terrain_mask zurückkehren, um Ausgabe zu prüfen
    #     if success is not True :
    #         logging.error(f"  FAIL: create_terrain_mask raised exception: {orbit_nr}, {timedate}, {output_filename}")
    #         results["failed"].append(item_id)
    #         continue
    #     else:

    #         logging.info(f"  Terrain mask generated: {output_filename}")

    #         publish_to_stac(
    #                 raw_asset=output_filename,
    #                 raw_item=item_id,
    #                 collection=COLLECTION_ID,
    #                 geocat_id=GEOCAT_ID,
    #                 current=None,
    #                 asset_title=ASSET_TITLE,
    #             )
    #         logging.info(f"  Published to STAC: {output_filename}")
    #         results["processed"].append(item_id)

    #     # --- Clean up local file after successful upload ---
    #     if os.path.isfile(output_filename):
    #         os.remove(output_filename)
    #         logging.info(f"  Local file removed: {output_filename}")

    # return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    """
    Parse command-line arguments.

    sys.argv layout expected by configuration/__init__.py:
        sys.argv[0] : script name
        sys.argv[1] : config filename, e.g. dev_config.py

    Our own arguments (--year, --dry-run, --loglevel) are parsed separately
    so they do not interfere with the configuration package's sys.argv parsing.
    """
    parser = argparse.ArgumentParser(
        description="Backfill missing terrain mask assets in the STAC catalogue.",
        # Do not parse sys.argv[1] as our own arg (it belongs to configuration)
        # We use parse_known_args to be safe.
    )
    parser.add_argument(
        "config_file",
        type=str,
        help="Configuration file to load, e.g. dev_config.py (same as other scripts)",
    )
    parser.add_argument(
        "--year", "-y",
        type=int,
        required=True,
        help="Year to process, e.g. 2025",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        default=False,
        help="Log what would be done without generating or uploading anything.",
    )
    parser.add_argument(
        "--loglevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )
    return parser.parse_args()


if __name__ == "__main__":

    # Default-Werte fuer direkten Start in VSCode (ohne Kommandozeilenargumente)
    # Entspricht: python util_terrain_backfill.py dev_config.py --year 2026 --dry-run


    print(f"DEBUG sys.argv at start: {sys.argv}")

    if "--year" not in sys.argv and "-y" not in sys.argv:
        sys.argv = [sys.argv[0], "dev_config.py", "--year", "2026"]

    print(f"DEBUG sys.argv after default: {sys.argv}")

    args = parse_args()
    args.dry_run = False   # ← VSCode-Override entfernen wenn produktiv

    print(f"DEBUG args.dry_run: {args.dry_run}")
    print(f"DEBUG args.year: {args.year}")

    logging.basicConfig(
        level=getattr(logging, args.loglevel),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    logging.info("=" * 60)
    logging.info("util_terrain_backfill start")
    logging.info(f"  Config  : {args.config_file}")
    logging.info(f"  Year    : {args.year}")
    logging.info(f"  Dry-run : {args.dry_run}")
    logging.info(f"  STAC    : {BASE_URL}")
    logging.info("=" * 60)

    results = process_year(year=args.year, dry_run=args.dry_run)
    print(results)

    logging.info("=" * 60)
    logging.info("SUMMARY")
    logging.info(f"  Processed : {len(results['processed'])}")
    logging.info(f"  Skipped   : {len(results['skipped'])}")
    logging.info(f"  Failed    : {len(results['failed'])}")
    if results["failed"]:
        logging.warning("Failed items:")
        for item_id in results["failed"]:
            logging.warning(f"  - {item_id}")
    logging.info("=" * 60)