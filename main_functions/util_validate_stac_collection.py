"""
Validate the content of the ch.swisstopo.swisseo_s2-sr STAC collection.

Two independent checks (run one or both):

  Step A  --compare
          List dates present in the v100 collection but missing in v200,
          then look up each missing date in tools/step0_empty_assets.csv
          and print a summary.

  Step C  --list-missing
          Plain item-level diff: list the v100 items that have no v200
          item on the same date. No CSV cross-check.

  Step B  --check-assets
          For every v200 item in the time range, verify the asset count
          (expected: 17 .tif + 1 .png + 1 .json) and check with a HEAD
          request (curl) that every listed asset file actually exists.

Both steps also write a CSV problem report (--report, default
stac_validation_report.csv) with one row per problem:
  date (YYYY-MM-DD), step (A/B), issue, item_id, detail
Step A rows  = dates missing in v200 and NOT explained by step0_empty_assets.csv
Step B rows  = items with wrong asset count OR a missing asset file
Step C rows  = v100 items with no v200 item on the same date

Design goal: minimal data transfer.
  - STAC item listing is paginated with limit=100 and a datetime filter so
    only the requested months are downloaded.
  - Asset existence is checked with `curl -sI` (HTTP HEAD) -> no file body
    is transferred, only headers.

Usage (run from the repo root):
  python main_functions/util_validate_stac_collection.py --start 2024-01 --end 2024-12 --compare
  python main_functions/util_validate_stac_collection.py --start 2025-01 --end 2025-06 --check-assets
  python main_functions/util_validate_stac_collection.py --start 2024-01 --end 2025-12 --compare --check-assets
"""

import argparse
import calendar
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

STAC_BASE = "https://data.geo.admin.ch/api/stac/v0.9/collections"
COLLECTION_V100 = "ch.swisstopo.swisseo_s2-sr_v100"
COLLECTION_V200 = "ch.swisstopo.swisseo_s2-sr_v200"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(_REPO_ROOT, "tools", "step0_empty_assets.csv")

# Expected number of assets per item, grouped by file extension.
EXPECTED_TIF = 17
EXPECTED_PNG = 1
EXPECTED_JSON = 1
EXPECTED_TOTAL = EXPECTED_TIF + EXPECTED_PNG + EXPECTED_JSON

PAGE_LIMIT = 100
HEAD_WORKERS = 16
REQUEST_TIMEOUT = 60


# --------------------------------------------------------------------------
# STAC helpers
# --------------------------------------------------------------------------
def month_range_to_datetime(start_month, end_month):
    """Turn '2024-01' / '2024-12' into a STAC datetime interval string."""
    start = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    last_day = calendar.monthrange(end.year, end.month)[1]
    start_iso = f"{start.year:04d}-{start.month:02d}-01T00:00:00Z"
    end_iso = f"{end.year:04d}-{end.month:02d}-{last_day:02d}T23:59:59Z"
    return f"{start_iso}/{end_iso}"


def fetch_items(collection, datetime_interval):
    """
    Download all items of a collection within the datetime interval.

    Returns a list of dicts: {"id", "date", "assets"} where
      - date is the acquisition date as 'YYYY-MM-DD'
      - assets is a dict {asset_key: href}
    """
    url = f"{STAC_BASE}/{collection}/items"
    params = {"limit": PAGE_LIMIT, "datetime": datetime_interval}
    session = requests.Session()
    session.headers.update({"Accept-Encoding": "gzip"})

    items = []
    page = 1
    while url:
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        for feature in data.get("features", []):
            date_str = feature.get("properties", {}).get("datetime", "")
            date_only = date_str[:10] if date_str else ""
            assets = {
                key: asset.get("href", "")
                for key, asset in feature.get("assets", {}).items()
            }
            items.append(
                {"id": feature.get("id", ""), "date": date_only, "assets": assets}
            )

        # follow pagination
        url = None
        params = None  # the "next" href already carries the cursor
        for link in data.get("links", []):
            if link.get("rel") == "next":
                url = link.get("href")
                break
        page += 1

    return items


# --------------------------------------------------------------------------
# Step A: compare v100 vs v200
# --------------------------------------------------------------------------
def load_csv_remarks():
    """Return {date: remark} from step0_empty_assets.csv (v200 rows)."""
    remarks = {}
    if not os.path.exists(CSV_PATH):
        print(f"WARNING: {CSV_PATH} not found")
        return remarks
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if "v200" in (row.get("collection") or ""):
                remarks[row.get("date", "").strip()] = (row.get("remark") or "").strip()
    return remarks


def run_list_missing(datetime_interval):
    """Plain item-level diff: which v100 items have no v200 item on the same date."""
    print("=" * 70)
    print("STEP C - plain list: items in v100 but NOT in v200 (by date)")
    print("=" * 70)

    v100 = fetch_items(COLLECTION_V100, datetime_interval)
    v200 = fetch_items(COLLECTION_V200, datetime_interval)
    dates_v200 = {it["date"] for it in v200 if it["date"]}

    missing_items = sorted(
        (it for it in v100 if it["date"] and it["date"] not in dates_v200),
        key=lambda it: it["date"],
    )

    print(f"v100 items: {len(v100)}   v200 items: {len(v200)}")
    print(f"v100 items with no v200 counterpart: {len(missing_items)}\n")
    print(f"{'date':<12} v100 item id")
    print("-" * 70)
    for it in missing_items:
        print(f"{it['date']:<12} {it['id']}")
    print()

    return [
        {"date": it["date"], "step": "C", "issue": "in v100 but not in v200",
         "item_id": it["id"], "detail": ""}
        for it in missing_items
    ]


def run_compare(datetime_interval):
    print("=" * 70)
    print("STEP A - items in v100 but NOT in v200")
    print("=" * 70)

    v100 = fetch_items(COLLECTION_V100, datetime_interval)
    v200 = fetch_items(COLLECTION_V200, datetime_interval)

    dates_v100 = sorted({it["date"] for it in v100 if it["date"]})
    dates_v200 = {it["date"] for it in v200 if it["date"]}

    print(f"v100 items in range: {len(v100)}  ({len(dates_v100)} distinct dates)")
    print(f"v200 items in range: {len(v200)}  ({len(dates_v200)} distinct dates)")

    missing = [d for d in dates_v100 if d not in dates_v200]
    remarks = load_csv_remarks()

    print(f"\nMissing dates: {len(missing)}\n")
    explained, unexplained = 0, []
    print(f"{'date':<12} {'in CSV?':<9} remark")
    print("-" * 70)
    for d in missing:
        if d in remarks:
            explained += 1
            print(f"{d:<12} {'yes':<9} {remarks[d]}")
        else:
            unexplained.append(d)
            print(f"{d:<12} {'NO':<9} <-- not tracked in step0_empty_assets.csv")

    print("\n" + "-" * 70)
    print("SUMMARY (Step A)")
    print("-" * 70)
    print(f"  total dates missing in v200 : {len(missing)}")
    print(f"  explained by CSV            : {explained}")
    print(f"  NOT explained by CSV        : {len(unexplained)}")
    if unexplained:
        print("\n  Dates needing attention (missing AND not in CSV):")
        for d in unexplained:
            print(f"    - {d}")
    else:
        print("\n  All missing dates are accounted for in step0_empty_assets.csv.")
    print()

    report_rows = [
        {"date": d, "step": "A", "issue": "missing in v200, not in step0_empty_assets.csv",
         "item_id": "", "detail": "present in v100"}
        for d in unexplained
    ]
    return report_rows


# --------------------------------------------------------------------------
# Step B: asset integrity of v200
# --------------------------------------------------------------------------
def head_exists(url):
    """Return (url, http_status_int_or_0). Uses curl HEAD -> no body transfer."""
    try:
        out = subprocess.run(
            ["curl", "-sI", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "30", url],
            capture_output=True, text=True, timeout=40,
        )
        code = out.stdout.strip()
        return url, int(code) if code.isdigit() else 0
    except Exception:
        return url, 0


def run_check_assets(datetime_interval):
    print("=" * 70)
    print("STEP B - asset integrity of v200")
    print("=" * 70)

    items = fetch_items(COLLECTION_V200, datetime_interval)
    print(f"v200 items in range: {len(items)}")
    print(f"Expected per item: {EXPECTED_TIF} tif + {EXPECTED_PNG} png + "
          f"{EXPECTED_JSON} json = {EXPECTED_TOTAL} assets\n")

    count_problems = []   # items with wrong asset count
    all_asset_urls = []   # (item_id, asset_key, url) for existence check
    id_to_date = {it["id"]: it["date"] for it in items}

    for it in items:
        tif = png = js = other = 0
        for key in it["assets"]:
            low = key.lower()
            if low.endswith(".tif"):
                tif += 1
            elif low.endswith(".png"):
                png += 1
            elif low.endswith(".json"):
                js += 1
            else:
                other += 1
        total = len(it["assets"])
        if (tif, png, js) != (EXPECTED_TIF, EXPECTED_PNG, EXPECTED_JSON):
            count_problems.append({
                "id": it["id"], "date": it["date"], "total": total,
                "tif": tif, "png": png, "json": js, "other": other,
            })
        for key, url in it["assets"].items():
            all_asset_urls.append((it["id"], key, url))

    # ---- existence check (parallel HEAD) ----
    print(f"Checking existence of {len(all_asset_urls)} asset files with HTTP HEAD ...")
    missing_files = []
    url_to_meta = {url: (iid, key) for iid, key, url in all_asset_urls}
    with ThreadPoolExecutor(max_workers=HEAD_WORKERS) as pool:
        futures = [pool.submit(head_exists, url) for _, _, url in all_asset_urls]
        done = 0
        for fut in as_completed(futures):
            url, status = fut.result()
            done += 1
            if done % 200 == 0:
                print(f"  ... {done}/{len(all_asset_urls)}")
            if status != 200:
                iid, key = url_to_meta[url]
                missing_files.append((iid, key, url, status))

    # items that have at least one missing asset
    items_with_missing = sorted({m[0] for m in missing_files})

    # ---- report ----
    print("\n" + "-" * 70)
    print("SUMMARY (Step B)")
    print("-" * 70)
    print(f"  items checked                     : {len(items)}")
    print(f"  items with wrong asset COUNT      : {len(count_problems)}")
    print(f"  asset files listed               : {len(all_asset_urls)}")
    print(f"  asset files MISSING (HEAD != 200) : {len(missing_files)}")
    print(f"  items with >=1 missing asset file : {len(items_with_missing)}")

    if count_problems:
        print("\n  Items with wrong asset count:")
        for cp in count_problems:
            print(f"    - {cp['id']} ({cp['date']}): {cp['total']} assets "
                  f"[tif={cp['tif']} png={cp['png']} json={cp['json']} other={cp['other']}]")

    if missing_files:
        print("\n  Assets listed but NOT existing:")
        for iid, key, url, status in sorted(missing_files):
            print(f"    - [{status}] {iid}  {key}")
            print(f"            {url}")

    if items_with_missing:
        print("\n  Items missing one or more asset files:")
        for iid in items_with_missing:
            n = sum(1 for m in missing_files if m[0] == iid)
            print(f"    - {iid}  ({n} missing)")

    if not count_problems and not missing_files:
        print("\n  All items complete, all asset files exist.")
    print()

    report_rows = []
    for cp in count_problems:
        report_rows.append({
            "date": id_to_date.get(cp["id"], ""), "step": "B",
            "issue": "wrong asset count", "item_id": cp["id"],
            "detail": f"total={cp['total']} tif={cp['tif']} png={cp['png']} "
                      f"json={cp['json']} other={cp['other']}",
        })
    for iid, key, url, status in sorted(missing_files):
        report_rows.append({
            "date": id_to_date.get(iid, ""), "step": "B",
            "issue": "missing asset", "item_id": iid,
            "detail": f"[{status}] {key}",
        })
    return report_rows


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="start month, format YYYY-MM")
    parser.add_argument("--end", required=True, help="end month, format YYYY-MM")
    parser.add_argument("--compare", action="store_true",
                        help="Step A: list v100 dates missing in v200, cross-check CSV")
    parser.add_argument("--check-assets", action="store_true",
                        help="Step B: verify v200 asset counts and file existence")
    parser.add_argument("--list-missing", action="store_true",
                        help="Step C: plain list of items in v100 but not in v200 (no CSV cross-check)")
    parser.add_argument("--report", default="stac_validation_report.csv",
                        help="path of the CSV problem report to write "
                             "(default: stac_validation_report.csv)")
    args = parser.parse_args()

    if not (args.compare or args.check_assets or args.list_missing):
        parser.error("choose at least one of --compare / --check-assets / --list-missing")

    try:
        interval = month_range_to_datetime(args.start, args.end)
    except ValueError:
        parser.error("--start and --end must be YYYY-MM")

    print(f"Time range: {interval}\n")

    report_rows = []
    if args.list_missing:
        report_rows += run_list_missing(interval)
    if args.compare:
        report_rows += run_compare(interval)
    if args.check_assets:
        report_rows += run_check_assets(interval)

    report_rows.sort(key=lambda r: (r["date"], r["step"], r["item_id"]))
    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "step", "issue", "item_id", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    print("=" * 70)
    print(f"CSV problem report written: {args.report}  ({len(report_rows)} rows)")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
