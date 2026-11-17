#!/usr/bin/env python3
"""
check_vhi.py - Lightweight VHI check.

PART A - VHI entries in tools/step0_empty_assets.csv
The VHI processor writes 'LST missing' to tools/step0_empty_assets.csv when the
MeteoSwiss LST files for a date are not published yet, and step0 writes
'No S2-SR scene in time window' when the whole VHI time window has no S2-SR data.
This script goes through these entries within the lookback window and checks
each date, in this order:

  1. VHI already published in FSDI STAC      -> the entry is stale and is removed
  2. LST files still missing                 -> entry is kept
  3. No S2-SR item in the VHI time window    -> entry is kept
  4. An S2-SR date in the time window is
     'Tile upload incomplete' or
     'Tile download incomplete'               -> entry is kept (step0 would not be ready)
  5. Otherwise                               -> VHI can be processed (entry is kept,
                                                rerun.py retries it and removes it)

The VHI time window is [date - (temporal_coverage - 1), date], with
temporal_coverage and the S2-SR collection taken from PRODUCT_VHI in the config.

PART B - published VHI that misses S2-SR data published later
VHI is calculated with the S2-SR items that are in STAC at that moment. When an
S2-SR item of the time window is published later (delayed S2-SR), the VHI has to
be calculated again. A published VHI item is reprocessed when an S2-SR item in
its time window
  - is not in NDVI_INDEX_LIST of the VHI metadata json, and
  - was published (STAC 'created') after the VHI item was last written (STAC 'updated').
The second condition is needed because the VHI processor stops early once all
pixels are filled: NDVI_INDEX_LIST then does not list the older S2-SR items on
purpose. As in part A, no reprocess while an S2-SR date in the time window is
'Tile upload incomplete' or 'Tile download incomplete'.

At the end, sets the GitHub Actions outputs:
  vhi-needs-processing=true   if at least one date of case 5 (part A) or of part B
                              was found. Only then the EC2 has to be started for VHI.
  vhi-reprocess-dates=...     the dates of part B, comma separated (empty if none).
                              rerun.py reprocesses them with --force-vhi.

Usage:
    python check_vhi.py                                      # date=today, days_back=30, dev_config.py
    python check_vhi.py 2026-11-05                           # date=2026-11-05, days_back=30
    python check_vhi.py 2026-11-05 14                        # date=2026-11-05, days_back=14
    python check_vhi.py 2026-11-05 30 prod_config.py         # with prod config
"""

import importlib
import os
import sys
import requests
import pandas as pd
from datetime import datetime, timedelta


# ============================================================================
# CONSTANTS
# ============================================================================

EMPTY_ASSET_LIST = os.path.join("tools", "step0_empty_assets.csv")

REMARK_LST_MISSING = "LST missing"
REMARK_NO_S2 = "No S2-SR scene in time window"

# VHI remarks that are checked again: VHI could not be made yet for these dates
RETRY_REMARKS = [REMARK_LST_MISSING, REMARK_NO_S2]

# S2-SR remarks that keep step0 from being ready (see step0_functions.py):
# such a date is neither in STAC nor counted as "no data"
S2_INCOMPLETE_REMARKS = ["Tile upload incomplete", "Tile download incomplete"]


# ============================================================================
# CONFIG
# ============================================================================

def get_stac_items_url(collection_url, api_path):
    """
    Build the STAC items URL from a collection URL as written in the config, e.g.
    https://sys-data.int.bgdi.ch/#/collections/ch.swisstopo.swisseo_vhi_v200
    -> https://sys-data.int.bgdi.ch/api/stac/v0.9/collections/ch.swisstopo.swisseo_vhi_v200/items
    """
    if "#/collections/" not in collection_url:
        raise ValueError(f"Unexpected collection URL format: {collection_url}")
    base_domain = collection_url.split("#")[0].rstrip("/")
    collection_id = collection_url.split("#/collections/")[1].strip("/")
    api_base = base_domain + api_path
    return f"{api_base.rstrip('/')}/collections/{collection_id}/items"


def load_vhi_config(config_name):
    """
    Load the VHI settings from the given config module (e.g. 'dev_config.py').
    Returns a dict with:
      product_name, vhi_items_url, lst_base_url, temporal_coverage,
      s2_items_url and s2_collection_name (both None if VHI has no step0_collection)
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
    finally:
        sys.argv = old_argv

    vhi = cfg.PRODUCT_VHI

    vhi_config = {
        "product_name": vhi["product_name"],
        "lst_base_url": vhi["LST_current_data"],
        "temporal_coverage": int(vhi["temporal_coverage"]),
        # The STAC collection VHI is published to
        "vhi_items_url": get_stac_items_url(vhi["step1_collection"], cfg.STAC_FSDI_API),
        "s2_items_url": None,
        "s2_collection_name": None,
    }

    # The S2-SR collection VHI reads from. Without it VHI is not enabled in this config
    if "step0_collection" in vhi:
        vhi_config["s2_items_url"] = get_stac_items_url(vhi["step0_collection"], cfg.STAC_FSDI_API)
        vhi_config["s2_collection_name"] = vhi["step0_collection"].split("#/collections/")[1].strip("/")

    return vhi_config


# ============================================================================
# FSDI STAC CHECK
# ============================================================================

def stac_has_items(items_url, start_date_str, end_date_str):
    """Return True if the STAC collection has at least one item between the two dates (both included)."""
    start = f"{start_date_str}T00:00:00Z"
    end = f"{end_date_str}T23:59:59Z"
    try:
        resp = requests.get(
            items_url,
            # "nocache": the STAC API is behind CloudFront, which caches each search
            # URL for 10 minutes. A new value on every call gives a fresh answer.
            params={"datetime": f"{start}/{end}", "limit": 1,
                    "nocache": datetime.now().strftime('%Y%m%d%H%M%S%f')},
            timeout=30,
        )
        resp.raise_for_status()
        return len(resp.json().get("features", [])) > 0
    except Exception as e:
        print(f"  WARNING: FSDI STAC check failed for {start_date_str} to {end_date_str}: {e}")
        return False


def get_stac_items(items_url, start_date_str, end_date_str):
    """
    All items of the STAC collection between the two dates (both included), as a
    list of dicts (the STAC features). Returns None if the request fails.
    """
    start = f"{start_date_str}T00:00:00Z"
    end = f"{end_date_str}T23:59:59Z"
    items = []
    url = items_url
    params = {"datetime": f"{start}/{end}", "limit": 100}
    try:
        while url:
            # "nocache": see stac_has_items
            params["nocache"] = datetime.now().strftime('%Y%m%d%H%M%S%f')
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("features", []))

            # More than 100 items: the next page has its own URL with all parameters
            url = None
            params = {}
            for link in data.get("links", []):
                if link.get("rel") == "next":
                    url = link["href"]
    except Exception as e:
        print(f"  WARNING: FSDI STAC query failed for {start_date_str} to {end_date_str}: {e}")
        return None
    return items


def get_item_date(item):
    """
    Date of a STAC item from its id, e.g. '2026-10-01t235959' -> '2026-10-01'.
    Returns None for items whose id is not a date, e.g. the 'latest' item that
    has the collection name as id ('swisseo_vhi_v200').
    """
    date_str = item["id"][:10]
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return date_str


def parse_stac_time(time_str):
    """STAC time string, e.g. '2026-10-08T08:59:28.213908Z', as a datetime."""
    return datetime.fromisoformat(time_str.replace("Z", "+00:00"))


def get_ndvi_index_list(vhi_item):
    """
    The S2-SR item ids the VHI item was calculated with: NDVI_INDEX_LIST from the
    metadata json asset of the VHI item. Returns None if it cannot be read.
    """
    metadata_url = None
    for asset_name, asset in vhi_item.get("assets", {}).items():
        if asset_name.endswith("_metadata.json"):
            metadata_url = asset["href"]
    if metadata_url is None:
        print(f"  WARNING: no metadata json asset in VHI item {vhi_item['id']}")
        return None

    try:
        resp = requests.get(metadata_url, params={"nocache": datetime.now().strftime('%Y%m%d%H%M%S%f')},
                            timeout=30)
        resp.raise_for_status()
        metadata = resp.json()
    except Exception as e:
        print(f"  WARNING: could not read {metadata_url}: {e}")
        return None

    # The list is the same for the vegetation and the forest raster
    for raster_name in ["VEGETATION-10M", "FOREST-10M"]:
        index_list = metadata.get(raster_name, {}).get("PROPERTIES", {}).get("NDVI_INDEX_LIST")
        if index_list:
            return index_list.split(",")

    print(f"  WARNING: no NDVI_INDEX_LIST in {metadata_url}")
    return None


# ============================================================================
# LST CHECK
# ============================================================================

def get_lst_paths(date_str, lst_base_url):
    """
    The two MeteoSwiss LST files (SDL and SOL) VHI needs for date_str.
    Same paths as in step1_processors/step1_processor_vhi.py.
    """
    current_date = datetime.strptime(date_str, "%Y-%m-%d")
    year = date_str[:4]
    month = date_str[5:7]
    day = date_str[8:10]

    if current_date < datetime(2024, 1, 1):
        sdl_path = f"{lst_base_url}/MSG2004-2023/msg.SDL.H_ch02.lonlat_{year}{month}01000000.nc"
        sol_path = f"{lst_base_url}/MSG2004-2023/msg.SOL.H_ch02.lonlat_{year}{month}01000000.nc"
    elif current_date < datetime(2026, 4, 1):
        sdl_path = f"{lst_base_url}/MSG2024-2026/msg.SDL.H_ch02.lonlat_{year}{month}01000000.nc"
        sol_path = f"{lst_base_url}/MSG2024-2026/msg.SOL.H_ch02.lonlat_{year}{month}01000000.nc"
    else:
        sdl_path = f"{lst_base_url}/msg.SDL.H_ch02.lonlat_{year}{month}{day}000000.nc"
        sol_path = f"{lst_base_url}/msg.SOL.H_ch02.lonlat_{year}{month}{day}000000.nc"

    return [sdl_path, sol_path]


def check_lst_available(date_str, lst_base_url):
    """Return True if both LST files for date_str exist."""
    for lst_path in get_lst_paths(date_str, lst_base_url):
        try:
            response = requests.head(lst_path, timeout=30, allow_redirects=True)
            if response.status_code != 200:
                print(f"  LST file missing: {lst_path}")
                return False
        except Exception as e:
            print(f"  WARNING: LST check failed for {lst_path}: {e}")
            return False
    return True


# ============================================================================
# S2-SR TIME WINDOW CHECK
# ============================================================================

def get_incomplete_s2_dates(df, s2_collection_name, start_date_str, end_date_str):
    """
    Dates in [start_date_str, end_date_str] whose S2-SR row in the CSV is
    'Tile upload incomplete' or 'Tile download incomplete'.
    """
    in_window = (
        (df["collection"] == s2_collection_name)
        & (df["date"] >= start_date_str)
        & (df["date"] <= end_date_str)
    )
    is_incomplete = df["remark"].astype(str).apply(
        lambda remark: any(marker in remark for marker in S2_INCOMPLETE_REMARKS)
    )
    return sorted(set(df[in_window & is_incomplete]["date"].tolist()))


# ============================================================================
# PART B - VHI TO REPROCESS
# ============================================================================

def find_vhi_dates_to_reprocess(vhi_config, df, start_date_str, end_date_str):
    """
    Published VHI items between the two dates that miss an S2-SR item published
    later (see PART B in the docstring at the top).
    Returns a dict {date: [new S2-SR item ids]}, sorted by date.
    """
    temporal_coverage = vhi_config["temporal_coverage"]

    vhi_items = get_stac_items(vhi_config["vhi_items_url"], start_date_str, end_date_str)
    # S2-SR items for the time windows of all these VHI dates
    s2_start_date_str = (datetime.strptime(start_date_str, "%Y-%m-%d")
                         - timedelta(days=temporal_coverage - 1)).strftime("%Y-%m-%d")
    s2_items = get_stac_items(vhi_config["s2_items_url"], s2_start_date_str, end_date_str)

    if vhi_items is None or s2_items is None:
        print("  STAC query failed, no VHI reprocess check in this run")
        return {}

    # Leave out the 'latest' items (id is not a date)
    vhi_items = [item for item in vhi_items if get_item_date(item) is not None]
    s2_items = [item for item in s2_items if get_item_date(item) is not None]

    print(f"Published VHI items: {len(vhi_items)}  |  S2-SR items from {s2_start_date_str}: {len(s2_items)}")

    dates_to_reprocess = {}

    for vhi_item in sorted(vhi_items, key=lambda item: item["id"]):
        # VHI item id e.g. '2026-10-01t235959', S2-SR item id e.g. '2026-09-30t102821'
        date_str = get_item_date(vhi_item)
        window_start = (datetime.strptime(date_str, "%Y-%m-%d")
                        - timedelta(days=temporal_coverage - 1)).strftime("%Y-%m-%d")
        vhi_updated = parse_stac_time(vhi_item["properties"]["updated"])

        # S2-SR items in the time window that were published after the VHI was written
        later_s2_ids = []
        for s2_item in s2_items:
            s2_date_str = get_item_date(s2_item)
            if s2_date_str < window_start or s2_date_str > date_str:
                continue
            if parse_stac_time(s2_item["properties"]["created"]) > vhi_updated:
                later_s2_ids.append(s2_item["id"])

        if not later_s2_ids:
            continue

        # Only now read the metadata json (one request per VHI item)
        print(f"\nVHI {date_str}: S2-SR item(s) published after the VHI: {sorted(later_s2_ids)}")
        used_s2_ids = get_ndvi_index_list(vhi_item)
        if used_s2_ids is None:
            print(f"  NDVI_INDEX_LIST not readable — no reprocess")
            continue

        new_s2_ids = sorted(s2_id for s2_id in later_s2_ids if s2_id not in used_s2_ids)
        if not new_s2_ids:
            print(f"  All of them are in NDVI_INDEX_LIST — no reprocess")
            continue

        # Wait until all S2-SR dates of the time window are complete
        incomplete_dates = get_incomplete_s2_dates(df, vhi_config["s2_collection_name"], window_start, date_str)
        if incomplete_dates:
            print(f"  Not in NDVI_INDEX_LIST: {new_s2_ids}, but incomplete S2-SR date(s) in time window "
                  f"{incomplete_dates} — reprocess later")
            continue

        print(f"  Not in NDVI_INDEX_LIST: {new_s2_ids} — VHI {date_str} has to be reprocessed")
        dates_to_reprocess[date_str] = new_s2_ids

    return dates_to_reprocess


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
    today = sys.argv[1] if len(sys.argv) > 1 else datetime.today().strftime("%Y-%m-%d")
    days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    config_name = sys.argv[3] if len(sys.argv) > 3 else "dev_config.py"

    print("=" * 60)
    print("check_vhi.py - VHI Check")
    print(f"Target date: {today}  |  Lookback window: {days_back} days  |  Config: {config_name}")
    print("=" * 60)

    vhi_config = load_vhi_config(config_name)
    product_name = vhi_config["product_name"]
    temporal_coverage = vhi_config["temporal_coverage"]
    vhi_enabled = vhi_config["s2_items_url"] is not None

    print(f"VHI STAC check:    {vhi_config['vhi_items_url']}")
    print(f"LST source:        {vhi_config['lst_base_url']}")
    if vhi_enabled:
        print(f"S2-SR STAC check:  {vhi_config['s2_items_url']}")
        print(f"VHI time window:   {temporal_coverage} days")
    else:
        print("S2-SR STAC check:  none, PRODUCT_VHI has no 'step0_collection' (VHI not enabled in config)")
        print("                   -> only stale entries are removed, EC2 is never started for VHI")

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%Y-%m-%d")

    df = pd.read_csv(EMPTY_ASSET_LIST)
    print(f"\nLoaded {len(df)} rows from {EMPTY_ASSET_LIST}")

    # ------------------------------------------------------------------
    # PART A - VHI entries in the CSV
    # ------------------------------------------------------------------
    retry_mask = (
        (df["collection"] == product_name)
        & (df["remark"].isin(RETRY_REMARKS))
        & (df["date"] >= cutoff)
        & (df["date"] <= today)
    )
    # (date, remark) of each entry, without duplicates
    rows_to_check = list(dict.fromkeys(zip(df[retry_mask]["date"], df[retry_mask]["remark"])))

    print(f"\nEntries with {RETRY_REMARKS} within {days_back} days ({len(rows_to_check)} total): {rows_to_check}")

    removed_dates = []
    still_missing_dates = []
    vhi_not_enabled_dates = []
    no_s2_dates = []
    s2_incomplete_dates = []
    ready_dates = []

    for date_str, remark in rows_to_check:
        print(f"\nChecking {date_str} ({remark}) ...")

        # 1. VHI already published -> the entry is stale
        if stac_has_items(vhi_config["vhi_items_url"], date_str, date_str):
            print(f"  VHI STAC check: VHI already published for {date_str}")
            stale_mask = (
                (df["collection"] == product_name)
                & (df["date"] == date_str)
                & (df["remark"] == remark)
            )
            df = df[~stale_mask]
            print(f"    {date_str}: removed stale '{remark}' entry from CSV")
            removed_dates.append(date_str)
            continue

        # 2. LST still missing -> VHI cannot run yet
        if not check_lst_available(date_str, vhi_config["lst_base_url"]):
            print(f"  LST check: LST still missing for {date_str} — entry kept")
            still_missing_dates.append(date_str)
            continue
        print(f"  LST check: both LST files available for {date_str}")

        # Without an S2-SR collection in the config VHI does not run at all
        if not vhi_enabled:
            print(f"  VHI not enabled in config — entry kept, no processing triggered")
            vhi_not_enabled_dates.append(date_str)
            continue

        # VHI time window: [date - (temporal_coverage - 1), date]
        window_start = (datetime.strptime(date_str, "%Y-%m-%d")
                        - timedelta(days=temporal_coverage - 1)).strftime("%Y-%m-%d")

        # 3. No S2-SR at all in the time window -> VHI would only write 'No S2-SR scene in time window'
        if not stac_has_items(vhi_config["s2_items_url"], window_start, date_str):
            print(f"  S2-SR check: no S2-SR item from {window_start} to {date_str} — entry kept, no processing triggered")
            no_s2_dates.append(date_str)
            continue
        print(f"  S2-SR check: at least one S2-SR item from {window_start} to {date_str}")

        # 4. An S2-SR date in the time window is incomplete -> step0 would not be ready
        incomplete_dates = get_incomplete_s2_dates(df, vhi_config["s2_collection_name"], window_start, date_str)
        if incomplete_dates:
            print(f"  S2-SR check: incomplete S2-SR date(s) in time window {incomplete_dates} — entry kept, no processing triggered")
            s2_incomplete_dates.append(date_str)
            continue

        # 5. Everything VHI needs is there
        print(f"  {date_str}: VHI can be processed")
        ready_dates.append(date_str)

    df.to_csv(EMPTY_ASSET_LIST, index=False)
    print(f"\nSaved updated CSV ({len(df)} rows) to {EMPTY_ASSET_LIST}")

    # ------------------------------------------------------------------
    # PART B - published VHI that misses S2-SR data published later
    # ------------------------------------------------------------------
    dates_to_reprocess = {}
    if vhi_enabled:
        print(f"\n{'-' * 60}")
        print(f"Checking published VHI from {cutoff} to {today} for S2-SR items published later ...")
        dates_to_reprocess = find_vhi_dates_to_reprocess(vhi_config, df, cutoff, today)

    result = "true" if (ready_dates or dates_to_reprocess) else "false"
    reprocess_dates_str = ",".join(dates_to_reprocess.keys())
    set_github_output("vhi-needs-processing", result)
    set_github_output("vhi-reprocess-dates", reprocess_dates_str)

    print(f"\n{'=' * 60}")
    print(f"Removed (VHI already published):        {removed_dates if removed_dates else 'none'}")
    print(f"LST still missing:                      {still_missing_dates if still_missing_dates else 'none'}")
    if not vhi_enabled:
        print(f"LST available, VHI not enabled:         {vhi_not_enabled_dates if vhi_not_enabled_dates else 'none'}")
    print(f"No S2-SR in time window:                {no_s2_dates if no_s2_dates else 'none'}")
    print(f"Incomplete S2-SR date in time window:   {s2_incomplete_dates if s2_incomplete_dates else 'none'}")
    print(f"VHI to process:                         {ready_dates if ready_dates else 'none'}")
    if dates_to_reprocess:
        print("VHI to reprocess (new S2-SR in window):")
        for date_str, new_s2_ids in dates_to_reprocess.items():
            print(f"    {date_str}: {new_s2_ids}")
    else:
        print("VHI to reprocess (new S2-SR in window): none")
    print(f"RESULT: vhi-needs-processing={result}")
    print(f"RESULT: vhi-reprocess-dates={reprocess_dates_str}")
    print("=" * 60)


if __name__ == "__main__":
    main()
