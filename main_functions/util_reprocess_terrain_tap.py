"""
util_reprocess_terrain_tap.py  --  Re-generate terrain mask assets for a date range.

Scans all items in ch.swisstopo.swisseo_s2-sr_v200 between DATE_FROM and DATE_TO,
reads ORBIT_NR from the item metadata JSON, calls main_terrain_parallel.py as an
isolated subprocess (required to avoid HORAYZON/Embree "scene not committed" issues),
and publishes the result back to STAC via publish_to_stac.

Unlike util_terrain_backfill.py this script always reprocesses every item in the
date range regardless of whether a terrain mask already exists.

Usage (run from project root):
    python main_functions/util_reprocess_terrain_tap.py --secrets path/to/secrets.json
    python main_functions/util_reprocess_terrain_tap.py --secrets path/to/secrets.json \\
        --date-from 2025-06-01T00:00:00Z --date-to 2025-09-30T23:59:59Z
    python main_functions/util_reprocess_terrain_tap.py --secrets path/to/secrets.json --dry-run

Author: swisstopo
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import requests
import pystac_client

# ---------------------------------------------------------------------------
# Allow imports from the project root (one level up from main_functions/)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(THIS_DIR))

import configuration as config                           # noqa: E402
from main_publish_stac_fsdi import publish_to_stac       # noqa: E402

# ---------------------------------------------------------------------------
# Constants  --  adjust if needed
# ---------------------------------------------------------------------------
STAGING       = "sys-data.int.bgdi.ch"
# STAGING     = "data.geo.admin.ch"
STAC_BASE_URL = f"https://{STAGING}/api/stac/v0.9/"
COLLECTION_ID = "ch.swisstopo.swisseo_s2-sr_v200"
ASSET_TITLE   = "Terrain mask - 10m"
GEOCAT_ID     = "a4bc1c7a-3e2f-4d95-9d86-a1a0b09b11a7"

DATE_FROM     = "2026-03-03T00:00:00Z"
DATE_TO       = "2026-03-03T23:59:59Z"

SECRETS_PATH  = r"D:\temp\github\topo-satromo-v2\secrets\stac_fsdi-int.json"

# Filename template for the generated terrain mask GeoTIFF
ASSET_NAME_TPL = "swisseo_s2-sr_v200_mosaic_{timedate}_terrainmask_10m.tif"

# The "current" item that only points to the latest asset -- skip it
CURRENT_ITEM_ID = "swisseo_s2-sr_v200"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_credentials(secrets_path: str) -> tuple:
    with open(secrets_path) as fh:
        cfg = json.load(fh)
    return cfg["FSDI"]["username"], cfg["FSDI"]["password"]


def read_metadata_json(item, auth: tuple):
    """
    Download and parse the metadata JSON asset of a STAC item.
    Returns a dict, or None if not found / download failed.
    """
    for _, asset in item.get_assets().items():
        href = asset.href or ""
        if href.endswith("_metadata.json"):
            try:
                response = requests.get(href, auth=auth, timeout=30)
                if response.status_code == 200:
                    return response.json()
                logging.warning(
                    f"  Metadata download failed for {item.id}: "
                    f"HTTP {response.status_code}"
                )
            except Exception as exc:
                logging.warning(f"  Metadata download error for {item.id}: {exc}")
            return None
    logging.warning(f"  No metadata JSON asset found for item {item.id}")
    return None


def extract_orbit_nr(metadata: dict):
    """Extract ORBIT_NR from the PROPERTIES section of a metadata JSON."""
    try:
        return str(metadata["PROPERTIES"]["ORBIT_NR"])
    except (KeyError, TypeError):
        return None


def extract_timedate(item_id: str):
    """
    Extract the acquisition timedate string from a STAC item ID.

    Item IDs follow the pattern:
        swisseo_s2-sr_v200_mosaic_YYYY-MM-DDtHHMMSS_...
    Returns the timedate token (e.g. "2025-06-01t101041"), or None.
    """
    for part in item_id.split("_"):
        if len(part) >= 15 and "t" in part and part[:4].isdigit():
            return part
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reprocess terrain mask assets for a date range and re-upload to STAC."
    )
    parser.add_argument(
        "--secrets",
        default=SECRETS_PATH,
        help="Path to STAC credentials JSON. Defaults to SECRETS_PATH constant.",
    )
    parser.add_argument(
        "--date-from",
        default=DATE_FROM,
        help=f"Start of date range (ISO 8601). Default: {DATE_FROM}",
    )
    parser.add_argument(
        "--date-to",
        default=DATE_TO,
        help=f"End of date range (ISO 8601). Default: {DATE_TO}",
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
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.loglevel),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    secrets_path = str(Path(args.secrets).resolve())

    # Patch config so publish_to_stac uses the correct endpoint and credentials
    config.FSDI_SECRETS       = secrets_path
    config.STAC_FSDI_SCHEME   = "https"
    config.STAC_FSDI_HOSTNAME = STAGING
    config.STAC_FSDI_API      = "/api/stac/v0.9/"
    config.DSM_FILE     = os.path.join("local_assets","DSM_10m_EPSG2056_CH_clipped_10km_extended_9999.tif")

    auth = load_credentials(secrets_path)

    # ------------------------------------------------------------------
    # Search STAC for all items in the date window
    # ------------------------------------------------------------------
    client = pystac_client.Client.open(STAC_BASE_URL)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")

    search = client.search(
        collections=[COLLECTION_ID],
        datetime=f"{args.date_from}/{args.date_to}",
        max_items=None,
    )

    items = list(search.items())
    logging.info(
        f"Found {len(items)} items between {args.date_from} and {args.date_to}"
    )

    original_cwd = os.getcwd()
    root_dir     = str(ROOT)

    results = {"processed": [], "skipped": [], "failed": []}

    for idx, item in enumerate(items, 1):
        item_id = item.id

        # Skip the "current" pointer item
        if item_id == CURRENT_ITEM_ID:
            logging.info(f"[{idx}/{len(items)}] SKIP current item '{item_id}'")
            results["skipped"].append(item_id)
            continue

        logging.info(f"[{idx}/{len(items)}] Processing: {item_id}")

        # --- Extract timedate from item ID ---
        timedate = extract_timedate(item_id)
        if not timedate:
            logging.warning(f"  FAIL: cannot extract timedate from item ID '{item_id}'")
            results["failed"].append(item_id)
            continue

        # --- Download and parse metadata JSON to get ORBIT_NR ---
        metadata = read_metadata_json(item, auth)
        if not metadata:
            logging.warning(f"  FAIL: could not read metadata JSON for {item_id}")
            results["failed"].append(item_id)
            continue

        orbit_nr = extract_orbit_nr(metadata)
        if not orbit_nr:
            logging.warning(f"  FAIL: ORBIT_NR not found in metadata for {item_id}")
            results["failed"].append(item_id)
            continue

        output_filename = ASSET_NAME_TPL.format(timedate=timedate)
        logging.info(
            f"  orbit={orbit_nr}, timedate={timedate}, output={output_filename}"
        )

        if args.dry_run:
            logging.info(
                f"  DRY-RUN: would call main_terrain_parallel and publish_to_stac"
            )
            results["processed"].append(item_id)
            continue

        # --- Generate terrain mask via isolated subprocess ---
        # We call _terrain_worker.py (a thin shim) instead of main_terrain_parallel.py
        # directly.  The shim stages sys.argv so configuration/__init__.py receives
        # a valid config filename at position 1, then calls create_terrain_mask().
        # Running in a subprocess also avoids the HORAYZON/Embree
        # "scene not committed" error that occurs in long-running loops.
        python_exe   = sys.executable
        worker_path  = str(THIS_DIR / "_terrain_worker.py")
        config_file  = "dev_config.py"   # passed as sys.argv[1] for configuration

        cmd = [
            python_exe,
            worker_path,
            config_file,
            "--orbit",    orbit_nr,
            "--timedate", timedate,
            "--output",   output_filename,
        ]
        logging.info(f"  Running: {' '.join(cmd)}")

        try:
            proc = subprocess_run(cmd, root_dir)
            success = (proc.returncode == 0)
        except Exception as exc:
            logging.error(f"  FAIL: subprocess raised: {exc}", exc_info=True)
            results["failed"].append(item_id)
            continue

        if not success:
            logging.error(
                f"  FAIL: main_terrain_parallel.py returned non-zero for {item_id}"
            )
            results["failed"].append(item_id)
            continue

        # --- Publish to STAC ---
        # publish_to_stac expects to run from the directory containing the asset.
        os.chdir(root_dir)
        try:
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
        except Exception as exc:
            logging.error(f"  FAIL: publish_to_stac raised: {exc}", exc_info=True)
            results["failed"].append(item_id)
        finally:
            os.chdir(original_cwd)

        # --- Clean up local file after successful upload ---
        output_path = Path(root_dir) / output_filename
        if output_path.is_file():
            output_path.unlink()
            logging.info(f"  Local file removed: {output_filename}")

        logging.info(f"  Done.")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logging.info("=" * 60)
    logging.info("SUMMARY")
    logging.info(f"  Processed : {len(results['processed'])}")
    logging.info(f"  Skipped   : {len(results['skipped'])}")
    logging.info(f"  Failed    : {len(results['failed'])}")
    if results["failed"]:
        logging.warning("Failed items:")
        for fid in results["failed"]:
            logging.warning(f"  - {fid}")
    logging.info("=" * 60)


# ---------------------------------------------------------------------------
# Thin subprocess wrapper (kept separate so it can be patched in tests)
# ---------------------------------------------------------------------------

def subprocess_run(cmd: list, cwd: str):
    """Run *cmd* as a subprocess with output forwarded to the console."""
    import subprocess
    return subprocess.run(
        cmd,
        check=False,          # Errors are handled by the caller
        capture_output=False, # Forward stdout/stderr directly to console
        cwd=cwd,
    )


if __name__ == "__main__":
    # Default arguments for direct VSCode start (no terminal invocation)
    if len(sys.argv) == 1:
        sys.argv += [
            "--secrets", SECRETS_PATH,
            "--date-from", DATE_FROM,
            "--date-to",   DATE_TO,
            # "--dry-run",
        ]
    main()
