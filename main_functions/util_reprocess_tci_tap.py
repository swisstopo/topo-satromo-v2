"""
reprocess_tci_tap.py  --  Re-publish TCI assets with -tap alignment fix.

Downloads every "True color image - 10m" asset from
ch.swisstopo.swisseo_s2-sr_v200 between DATE_FROM and DATE_TO,
re-runs gdalwarp with -tap (no cutline), and uploads the result
back to STAC via publish_to_stac.

Usage:
    python main_functions/util_reprocess_tci_tap.py --secrets path/to/secrets.json

Author: swisstopo
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests
import pystac_client

# ---------------------------------------------------------------------------
# Allow imports from the project root (one level up from main_functions/)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import configuration as config                           # noqa: E402
from main_publish_stac_fsdi import publish_to_stac       # noqa: E402

# ---------------------------------------------------------------------------
# Constants  --  adjust if needed
# ---------------------------------------------------------------------------
STAGING = "sys-data.int.bgdi.ch"
#STAGING = "data.geo.admin.ch"
STAC_BASE_URL = f"https://{STAGING}/api/stac/v0.9/"
COLLECTION_ID = "ch.swisstopo.swisseo_s2-sr_v200"
ASSET_TITLE   = "True color image - 10m"
GEOCAT_ID     = ""
DATE_FROM     = "2025-01-01T00:00:00Z"
DATE_TO       = "2026-03-31T23:59:59Z"
SECRETS_PATH  = r"/mnt/c/Users/Localadmin/Documents/SATROMO/topo-satromo-v2/topo-satromo-v2/secrets/stac_fsdi-int" \
".json"
WORK_DIR = r"/mnt/c/temp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_credentials(secrets_path: str) -> tuple:
    with open(secrets_path) as fh:
        cfg = json.load(fh)
    return cfg["FSDI"]["username"], cfg["FSDI"]["password"]


def download_file(href: str, dest: str, auth: tuple) -> None:
    with requests.get(href, auth=auth, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)


def reprocess_tci(input_path: str, output_path: str) -> bool:
    """Run gdalwarp with -tap; no cutline, no alpha band."""
    cmd = [
        "gdalwarp",
        "-of", "COG",
        "-co", "BIGTIFF=YES",
        "-co", "NUM_THREADS=ALL_CPUS",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
        "-co", "COMPRESS=JPEG",
        "-co", "QUALITY=85",
        "-dstalpha",
        "-tr", "10", "10",
        "-tap",
        "-overwrite",
        input_path,
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    gdalwarp error:\n{result.stderr}")
        return False
    return True


def item_id_to_raw_item(item_id: str) -> str:
    """
    Convert STAC item id '2025-01-07t101319'
    to publish_to_stac raw_item '2025-01-07T101319'.
    """
    return re.sub(r"(\d{4}-\d{2}-\d{2})t(\d{6})", r"\1T\2", item_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reprocess TCI assets: add -tap, re-upload to STAC."
    )
    parser.add_argument(
        "--secrets",
        default=SECRETS_PATH,
        help="Path to STAC credentials JSON. Defaults to SECRETS_PATH constant.",
    )
    args = parser.parse_args()

    secrets_path = str(Path(args.secrets).resolve())

    # Point config at the provided secrets file so publish_to_stac
    # enters run_type 2 (DEV/local) and reads the right credentials.
        # Patch config so publish_to_stac uses the correct INT endpoint
    config.FSDI_SECRETS        = secrets_path
    config.STAC_FSDI_SCHEME    = "https"
    config.STAC_FSDI_HOSTNAME  = STAGING
    config.STAC_FSDI_API       = "/api/stac/v0.9/"

    auth = load_credentials(secrets_path)

    # ------------------------------------------------------------------
    # Search STAC for all items in the date window
    # ------------------------------------------------------------------
    client = pystac_client.Client.open(STAC_BASE_URL)
    client.add_conforms_to("COLLECTIONS")
    client.add_conforms_to("ITEM_SEARCH")

    search = client.search(
        collections=[COLLECTION_ID],
        datetime=f"{DATE_FROM}/{DATE_TO}",
        max_items=None,
    )

    items = list(search.items())
    print(f"Found {len(items)} items between {DATE_FROM} and {DATE_TO}\n")

    work_dir = Path(WORK_DIR)
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Work directory: {work_dir}\n")

    original_cwd = os.getcwd()

    for idx, item in enumerate(items, 1):

        # Find the TCI asset in this item
        tci_asset = None
        for _, asset in item.get_assets().items():
            if getattr(asset, "title", None) == ASSET_TITLE:
                tci_asset = asset
                break

        if tci_asset is None:
            print(f"[{idx}/{len(items)}] {item.id}: no TCI asset, skipping")
            continue

        filename    = os.path.basename(tci_asset.href)   # e.g. ..._tci_10m.tif
        dl_path     = str(work_dir / filename)
        proc_path   = str(work_dir / ("tap_" + filename))
        final_path  = str(work_dir / filename)            # overwrite download

        print(f"[{idx}/{len(items)}] {item.id}")

        # 1. Download
        print(f"  Downloading ...")
        try:
            download_file(tci_asset.href, dl_path, auth)
        except Exception as exc:
            print(f"  Download failed: {exc}")
            continue

        # 2. Reprocess
        print(f"  Reprocessing with -tap ...")
        if not reprocess_tci(dl_path, proc_path):
            print(f"  Reprocessing failed, skipping upload.")
            for p in (dl_path, proc_path):
                Path(p).unlink(missing_ok=True)
            continue

        # Replace original download with processed file
        os.remove(dl_path)
        os.rename(proc_path, final_path)

        # 3. Upload  --  publish_to_stac expects to run from the directory
        #    that contains the asset file.
        os.chdir(work_dir)
        print(f"  Uploading ...")
        try:
            publish_to_stac(
                raw_asset=filename,
                raw_item=item_id_to_raw_item(item.id),
                collection=COLLECTION_ID,
                geocat_id=GEOCAT_ID,
                current=None,
                asset_title=ASSET_TITLE,
            )
        except Exception as exc:
            print(f"  Upload failed: {exc}")
        finally:
            os.chdir(original_cwd)

        # 4. Clean up processed file (publish_to_stac renames it back)
        Path(final_path).unlink(missing_ok=True)
        # Also clean up the lowercase-renamed copy publish_to_stac may leave
        Path(work_dir / filename.lower()).unlink(missing_ok=True)

        print(f"  Done.")

    print(f"\nFinished. Processed {len(items)} items.")


if __name__ == "__main__":
    main()