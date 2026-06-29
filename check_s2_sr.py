#!/usr/bin/env python3
"""
check_s2_sr.py - Lightweight Sentinel-2 data availability check.

Queries the Copernicus STAC API to determine whether new S2 L2A tiles are
available for Switzerland, then marks qualifying dates as ready for GPU
processing in tools/step0_empty_assets.csv.

Two categories of dates are checked on each run:
  1. The target date (today, or the date passed as argv[1])
  2. Historical entries in the CSV where remark is "No candidate scene",
     starts with "Tile upload incomplete" (e.g. "Tile upload incomplete: [22]"),
     or is "Tile download incomplete" (download failed during processing),
     and collection matches COLLECTION_NAME, within the lookback window.

At the end, sets the GitHub Actions output 'needs-processing=true' if the
CSV contains any "Tiles ready awaiting GPU system run" entry within the
lookback window (entries outside that window are ignored).

Usage:
    python check_s2_sr.py                        # date=2026-02-01, days_back=30
    python check_s2_sr.py 2026-05-20             # date=2026-05-20, days_back=30
    python check_s2_sr.py 2026-05-20 14          # date=2026-05-20, days_back=14
"""

import importlib
import os
import sys
import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict


# ============================================================================
# CONSTANTS
# ============================================================================

STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
COPERNICUS_COLLECTION = "sentinel-2-l2a"
AOI_FILE = os.path.join("assets", "swissboundary_simplified_4326.json")
BASELINE_VERSION = "04.00"
EXPECTED_TILE_COUNTS = {8: 4, 22: 4, 65: 11, 108: 11}

EMPTY_ASSET_LIST = os.path.join("tools", "step0_empty_assets.csv")
ACQUISITION_PLAN_FILE = os.path.join("tools", "acquisitionplan.csv")
COLLECTION_NAME = "ch.swisstopo.swisseo_s2-sr_v200"

REMARK_READY = "Tiles ready awaiting GPU system run"
REMARK_NO_CANDIDATE = "No candidate scene"
REMARK_TILE_INCOMPLETE = "Tile upload incomplete"
REMARK_DOWNLOAD_INCOMPLETE = "Tile download incomplete"


# ============================================================================
# STAC SEARCH
# ============================================================================

def search_copernicus(date_str):
    """
    Search Copernicus STAC for S2 L2A scenes over Switzerland on date_str.
    Returns a list of STAC items, deduplicated to keep the newest satellite
    per orbit when both S2A and S2B are present.
    """
    with open(AOI_FILE, "r") as f:
        geojson_data = json.load(f)
    geometry = geojson_data["geometries"][0]

    query_body = {
        "collections": [COPERNICUS_COLLECTION],
        "intersects": geometry,
        "datetime": f"{date_str}T00:00:00Z/{date_str}T23:59:59Z",
        "limit": 100,
    }

    for attempt in range(3):
        response = requests.post(STAC_SEARCH_URL, json=query_body, timeout=60)
        response.raise_for_status()
        try:
            items = response.json().get("features", [])
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Copernicus API non-JSON response (attempt {attempt + 1}/3), retrying in 10s...")
                time.sleep(10)
            else:
                raise ValueError(f"Copernicus API returned non-JSON after 3 attempts (HTTP {response.status_code}): {response.text[:200]!r}") from e

    # Keep only items above the minimum baseline version
    items = [
        item for item in items
        if item["properties"].get("processing:version", "00.00") > BASELINE_VERSION
    ]

    # When both S2A and S2B cover the same orbit, keep only the newer satellite
    best_sat_per_orbit = {}
    for item in items:
        orbit = item["properties"]["sat:relative_orbit"]
        sat = item["id"].split("_")[0]  # "S2A" or "S2B"
        if orbit not in best_sat_per_orbit or sat > best_sat_per_orbit[orbit]:
            best_sat_per_orbit[orbit] = sat

    return [
        item for item in items
        if item["id"].split("_")[0] == best_sat_per_orbit.get(
            item["properties"]["sat:relative_orbit"]
        )
    ]


# ============================================================================
# ACQUISITION PLAN HELPERS
# ============================================================================

def load_acquisition_plan():
    """Load acquisitionplan.csv; return DataFrame or None on failure."""
    if not os.path.exists(ACQUISITION_PLAN_FILE):
        return None
    try:
        return pd.read_csv(ACQUISITION_PLAN_FILE)
    except Exception as e:
        print(f"  WARNING: could not load {ACQUISITION_PLAN_FILE}: {e}")
        return None


def get_expected_orbit_count(date_str, acq_plan):
    """
    Return number of orbits expected for date_str from acquisitionplan.csv,
    or None if the date is not in the plan (treated as historical).
    """
    if acq_plan is None or acq_plan.empty:
        return None
    count = (acq_plan["Acquisition Date"].astype(str) == date_str).sum()
    return count if count > 0 else None


# ============================================================================
# FSDI STAC HELPERS
# ============================================================================

def _load_fsdi_items_url(config_name):
    """
    Load the FSDI STAC items URL for the S2-SR collection from the given
    config module (e.g. 'dev_config.py' or 'prod_config.py').
    Returns the URL string, or None on failure.
    """
    config_basename = config_name.replace(".py", "")
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, repo_root)
    # configuration/__init__.py parses sys.argv on first import, so temporarily
    # set argv to just the config filename to avoid it misreading our date/days args.
    old_argv = sys.argv
    sys.argv = [sys.argv[0], config_name]
    try:
        cfg = importlib.import_module(f"configuration.{config_basename}")
        step0_url = cfg.PRODUCT_S2_LEVEL_2A["step0_collection"]
        if "#/collections/" in step0_url:
            base_domain = step0_url.split("#")[0].rstrip("/")
            collection_id = step0_url.split("#/collections/")[1].strip("/")
            api_base = base_domain + cfg.STAC_FSDI_API
        else:
            raise ValueError(f"Unexpected step0_collection format: {step0_url}")
        return f"{api_base.rstrip('/')}/collections/{collection_id}/items"
    except Exception as e:
        print(f"  WARNING: could not load FSDI config from {config_name}: {e}")
        return None
    finally:
        sys.argv = old_argv


def check_fsdi_published(date_str, fsdi_items_url):
    """Return True if at least one item already exists in FSDI STAC for date_str."""
    start = f"{date_str}T00:00:00Z"
    end = f"{date_str}T23:59:59Z"
    try:
        resp = requests.get(
            fsdi_items_url,
            params={"datetime": f"{start}/{end}", "limit": 1},
            timeout=30,
        )
        resp.raise_for_status()
        return len(resp.json().get("features", [])) > 0
    except Exception as e:
        print(f"  WARNING: FSDI STAC check failed for {date_str}: {e}")
        return False


# ============================================================================
# TILE COMPLETENESS
# ============================================================================

def check_tile_completeness(search_result):
    """
    Check whether each orbit has the expected number of tiles.
    Returns (valid_orbits, non_valid_orbits) as sets of orbit numbers.
    Orbits not listed in EXPECTED_TILE_COUNTS are considered valid by default.
    """
    orbit_to_tiles = defaultdict(set)
    for item in search_result:
        orbit_num = item["properties"]["sat:relative_orbit"]
        tile_id = item["properties"]["grid:code"].split("-")[1]  # "MGRS-32TLT" -> "32TLT"
        orbit_to_tiles[orbit_num].add(tile_id)

    valid_orbits = {
        orbit for orbit, tiles in orbit_to_tiles.items()
        if orbit not in EXPECTED_TILE_COUNTS or len(tiles) >= EXPECTED_TILE_COUNTS[orbit]
    }
    non_valid_orbits = {
        orbit for orbit, tiles in orbit_to_tiles.items()
        if orbit in EXPECTED_TILE_COUNTS and len(tiles) < EXPECTED_TILE_COUNTS[orbit]
    }
    return valid_orbits, non_valid_orbits


# ============================================================================
# CHECK A SINGLE DATE
# ============================================================================

def check_date(date_str, expected_orbit_count=None):
    """
    Determine data availability for one date.
    Returns (remark, non_valid_orbits):
      - REMARK_READY           if all expected orbits have complete tiles
      - REMARK_TILE_INCOMPLETE  if results exist but not all expected orbits are ready
      - REMARK_NO_CANDIDATE    if the STAC search returns no results

    expected_orbit_count: number of orbits from acquisitionplan.csv for this date.
      - If None (historical date not in plan): READY as soon as any valid orbit exists.
      - If set: wait until found valid orbits >= expected_orbit_count.
    """
    search_result = search_copernicus(date_str)

    if not search_result:
        return REMARK_NO_CANDIDATE, set()

    valid_orbits, non_valid_orbits = check_tile_completeness(search_result)
    valid_results = [
        item for item in search_result
        if item["properties"]["sat:relative_orbit"] in valid_orbits
    ]

    if not valid_results:
        return REMARK_TILE_INCOMPLETE, non_valid_orbits

    # If acquisition plan specifies how many orbits to expect, wait for all of them
    if expected_orbit_count is not None and len(valid_orbits) < expected_orbit_count:
        print(f"  Orbits with complete tiles: {len(valid_orbits)}/{expected_orbit_count} expected — waiting for remaining")
        return REMARK_TILE_INCOMPLETE, non_valid_orbits

    return REMARK_READY, non_valid_orbits


# ============================================================================
# CSV UPDATE
# ============================================================================

def update_csv(df, date_str, new_remark, non_valid_orbits):
    """
    Update or insert a row for (COLLECTION_NAME, date_str) in the dataframe.

    Rules:
      - Never overwrite 'cloudy' entries.
      - Never overwrite an existing READY entry.
      - If new_remark is NO_CANDIDATE and an entry already exists, leave it.
      - Otherwise update the existing remark, or append a new row.

    Returns (updated_df, was_ready_written).
    """
    remark_str = new_remark
    if new_remark == REMARK_TILE_INCOMPLETE and non_valid_orbits:
        remark_str = f"{REMARK_TILE_INCOMPLETE}: {sorted(non_valid_orbits)}"

    mask = (df["collection"] == COLLECTION_NAME) & (df["date"] == date_str)
    existing = df[mask]

    if len(existing) > 0:
        current_remark = str(existing.iloc[0]["remark"])

        if REMARK_READY in current_remark:
            print(f"    {date_str}: already marked ready, skipping")
            return df, False

        if "cloudy" in current_remark.lower():
            print(f"    {date_str}: cloudy entry, skipping")
            return df, False

        if new_remark == REMARK_NO_CANDIDATE:
            # Never overwrite any existing entry with "No candidate scene"
            # (tile_incomplete is better information; no_candidate is never an upgrade)
            return df, False

        df.loc[mask, "remark"] = remark_str
        print(f"    {date_str}: updated '{current_remark}' -> '{remark_str}'")
    else:
        new_row = pd.DataFrame([{
            "collection": COLLECTION_NAME,
            "date": date_str,
            "remark": remark_str,
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        print(f"    {date_str}: added '{remark_str}'")

    return df, new_remark == REMARK_READY


# ============================================================================
# GITHUB ACTIONS OUTPUT
# ============================================================================

def set_github_output(key, value):
    """Write key=value to $GITHUB_OUTPUT when running inside GitHub Actions."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")


# ============================================================================
# MAIN
# ============================================================================

def main():
    today = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03"
    days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    config_name = sys.argv[3] if len(sys.argv) > 3 else None

    print("=" * 60)
    print("check_s2_sr.py - Sentinel-2 Data Availability Check")
    print(f"Target date: {today}  |  Lookback window: {days_back} days")
    print("=" * 60)

    fsdi_items_url = None
    if config_name:
        fsdi_items_url = _load_fsdi_items_url(config_name)
        if fsdi_items_url:
            print(f"FSDI STAC check: {fsdi_items_url}")
        else:
            print("FSDI STAC check: disabled (config load failed)")

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%Y-%m-%d")

    df = pd.read_csv(EMPTY_ASSET_LIST)
    print(f"\nLoaded {len(df)} rows from {EMPTY_ASSET_LIST}")

    acq_plan = load_acquisition_plan()
    if acq_plan is not None:
        print(f"Loaded acquisition plan ({len(acq_plan)} rows) from {ACQUISITION_PLAN_FILE}")

    # Historical entries worth re-checking: "No candidate scene" (exact),
    # any "Tile upload incomplete" variant (e.g. "Tile upload incomplete: [22]"),
    # or "Tile download incomplete" (download failed during processing)
    historical_mask = (
        (df["collection"] == COLLECTION_NAME)
        & (
            (df["remark"] == REMARK_NO_CANDIDATE)
            | df["remark"].str.contains(REMARK_TILE_INCOMPLETE, na=False)
            | df["remark"].str.contains(REMARK_DOWNLOAD_INCOMPLETE, na=False)
        )
        & (df["date"] >= cutoff)
        & (df["date"] <= today)
    )
    historical_dates = df[historical_mask]["date"].tolist()

    # Target date first, then historical — dict.fromkeys preserves order and deduplicates
    dates_to_check = list(dict.fromkeys([today] + historical_dates))

    print(f"\nDates to check ({len(dates_to_check)} total):")
    print(f"  Target date: {today}")
    if historical_dates:
        print(f"  Historical 'No candidate scene' / 'Tile upload incomplete' / 'Tile download incomplete' within {days_back} days: {historical_dates}")

    # Check each date and accumulate updates in the dataframe
    for date_str in dates_to_check:
        print(f"\nChecking {date_str} ...")
        expected_orbit_count = get_expected_orbit_count(date_str, acq_plan)
        if expected_orbit_count is not None:
            print(f"  Acquisition plan: {expected_orbit_count} orbit(s) expected for {date_str}")
        else:
            print(f"  Acquisition plan: date not found — treated as historical (no orbit count constraint)")
        try:
            remark, non_valid_orbits = check_date(date_str, expected_orbit_count)
            suffix = f" (incomplete orbits: {sorted(non_valid_orbits)})" if non_valid_orbits else ""
            print(f"  CSDE STAC result: {remark}{suffix}")
            if remark == REMARK_READY and fsdi_items_url:
                if check_fsdi_published(date_str, fsdi_items_url):
                    print(f"  FSDI STAC check: items already published for {date_str} — skipping")
                    stale_mask = (
                        (df["collection"] == COLLECTION_NAME)
                        & (df["date"] == date_str)
                        & df["remark"].str.contains(REMARK_READY, na=False)
                    )
                    if stale_mask.any():
                        df = df[~stale_mask]
                        print(f"    {date_str}: removed stale READY entry from CSV")
                    continue
                else:
                    print(f"  FSDI STAC check: no items yet for {date_str} — will trigger processing")
            df, _ = update_csv(df, date_str, remark, non_valid_orbits)
        except Exception as e:
            print(f"  ERROR: {e}")

    df.to_csv(EMPTY_ASSET_LIST, index=False)
    print(f"\nSaved updated CSV ({len(df)} rows) to {EMPTY_ASSET_LIST}")

    # Trigger EC2 only if a READY entry exists within the lookback window.
    # Entries outside the window (e.g. old test entries from 2018) are ignored.
    actual_today = datetime.today().strftime("%Y-%m-%d")
    rerun_cutoff = (datetime.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    any_ready = (
        df["remark"].str.contains(REMARK_READY, na=False)
        & (df["date"] >= rerun_cutoff)
        & (df["date"] <= actual_today)
    ).any()
    result = "true" if any_ready else "false"
    set_github_output("needs-processing", result)

    print(f"\n{'=' * 60}")
    print(f"RESULT: needs-processing={result}")
    print("=" * 60)


if __name__ == "__main__":
    main()