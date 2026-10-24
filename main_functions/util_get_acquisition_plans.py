#!/usr/bin/python3.10

"""
Harvest Sentinel-2 acquisition plans from sentinels.copernicus.eu.

Scraping strategy (robust against ESA Liferay quirks):
  1. HTTP fetch with realistic User-Agent and cache-busting query parameter
  2. Section detection via <h2-h5> headings containing "sentinel-2[abc]"
  3. KML URL extraction via REGEX on the FULL href (not just last path segment)
     - Catches /documents/d/sentinel/<slug>            (canonical)
     - Catches /documents/247904/<id>/<slug>/version/x (Liferay variant)
  4. Fallback regex scan over raw HTML, assigns to satellite by slug prefix
  5. KML selection: prefer active (today in window), else most recent

Author: David Oesch (original), updated 2026-05-21
"""

import datetime
import os
import re
import sys
import time
import urllib.request as ul
from datetime import timedelta

import pandas as pd  # type: ignore
from lxml import html

from util_extract_acquisition_plans_s2 import extract_S2_entries


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

S2_URL = "https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-2/acquisition-plans"
URL_KML_PREFIX = "https://sentinels.copernicus.eu"
STORAGE_PATH = os.getcwd() + "/"

POLYGON_WKT = (
    "POLYGON((5.96 46.13,6.03 46.66,6.91 47.52,8.56 47.90,9.78 47.65,"
    "9.91 47.17,10.70 46.96,10.60 46.47,10.08 46.11,9.06 45.74,7.13 45.77,5.96 46.13))"
)

DATE_FORMAT = "%Y%m%dt%H%M%S"

# Matches the KML slug anywhere in a URL or text
KML_SLUG_RE = re.compile(
    r"(s2[abc]_mp_acq__kml_\d{8}t\d{6}_\d{8}t\d{6})",
    re.IGNORECASE,
)

# Matches a full href containing a KML slug (fallback regex scan)
KML_HREF_RE = re.compile(
    r'href="(/documents/[^"]*?s2[abc]_mp_acq__kml_\d{8}t\d{6}_\d{8}t\d{6}[^"]*)"',
    re.IGNORECASE,
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def fetch_html(url):
    """Fetch URL with realistic headers and cache busting; return decoded text."""
    cache_buster = f"?_t={int(time.time())}"
    req = ul.Request(url + cache_buster, headers=HTTP_HEADERS)
    with ul.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# KML link extraction
# ---------------------------------------------------------------------------

def extract_kml_links(html_text):
    """
    Extract Sentinel-2 KML links per satellite.

    Returns: {'S2A': {slug: full_url}, 'S2B': {...}, 'S2C': {...}}

    Two-stage approach:
      Stage 1: section-aware DOM parse (uses <h*> headings as anchors)
      Stage 2: raw-HTML regex fallback (assigns by slug prefix s2a/s2b/s2c)
    """
    result = {"S2A": {}, "S2B": {}, "S2C": {}}

    # --- Stage 1: section-aware DOM parsing ---
    try:
        tree = html.fromstring(html_text)
        section_map = {
            "sentinel-2a": "S2A",
            "sentinel-2b": "S2B",
            "sentinel-2c": "S2C",
        }
        current = None
        for el in tree.iter():
            if el.tag in ("h2", "h3", "h4", "h5"):
                text = (el.text_content() or "").strip().lower()
                current = None
                for key, sat in section_map.items():
                    if key in text:
                        current = sat
                        break
            elif current and el.tag == "a":
                href = el.get("href", "")
                match = KML_SLUG_RE.search(href)
                if match:
                    slug = match.group(1).lower()
                    full_url = href if href.startswith("http") else URL_KML_PREFIX + href
                    result[current][slug] = full_url
    except Exception as exc:
        print(f"  WARNING: DOM parsing failed: {exc}")

    # --- Stage 2: regex fallback over raw HTML ---
    # Assigns to satellite by slug prefix, independent of DOM structure.
    stage2_added = {"S2A": 0, "S2B": 0, "S2C": 0}
    for href_match in KML_HREF_RE.finditer(html_text):
        href = href_match.group(1)
        slug_match = KML_SLUG_RE.search(href)
        if not slug_match:
            continue
        slug = slug_match.group(1).lower()
        sat = "S" + slug[1:3].upper()
        if sat not in result:
            continue
        full_url = href if href.startswith("http") else URL_KML_PREFIX + href
        if slug not in result[sat]:
            result[sat][slug] = full_url
            stage2_added[sat] += 1

    for sat in ("S2A", "S2B", "S2C"):
        extra = f" (+{stage2_added[sat]} via regex fallback)" if stage2_added[sat] else ""
        print(f"  {sat}: {len(result[sat])} KML link(s) extracted{extra}")

    return result


# ---------------------------------------------------------------------------
# KML selection
# ---------------------------------------------------------------------------

def get_kml_candidates(kml_dict):
    """
    Order the KMLs from {slug: url} from best to worst.

    Priority:
      1. KMLs whose [start, end] window contains today (newest end_date first)
      2. All other KMLs (newest end_date first)

    A list is returned (not only the best one) because ESA sometimes links a
    freshly uploaded KML that is not yet public (HTTP 404). In that case the
    caller tries the next candidate.
    """
    today = datetime.datetime.now()
    active = []
    others = []

    for slug in kml_dict:
        parts = slug.split("_")
        if len(parts) < 2:
            continue
        try:
            start_str = parts[-2].lower()
            end_str = parts[-1].split(".")[0].lower()
            start_date = datetime.datetime.strptime(start_str, DATE_FORMAT)
            end_date = datetime.datetime.strptime(end_str, DATE_FORMAT)
        except (ValueError, IndexError):
            print(f"  WARNING: cannot parse dates from '{slug}', skipping")
            continue

        if start_date < today < end_date:
            active.append((end_date, slug))
        else:
            others.append((end_date, slug))

    active.sort(reverse=True)
    others.sort(reverse=True)

    candidates = []
    for end_date, slug in active:
        candidates.append(slug)
    for end_date, slug in others:
        candidates.append(slug)
    return candidates


# ---------------------------------------------------------------------------
# Download and AOI extraction
# ---------------------------------------------------------------------------

def download_kml(file_url, output_filename, output_path):
    """Download KML to <output_filename>.kml; return True on success."""
    kml_file_path = os.path.join(output_path, output_filename + ".kml")
    try:
        req = ul.Request(file_url, headers=HTTP_HEADERS)
        with ul.urlopen(req, timeout=60) as resp:
            content = resp.read()
    except Exception as exc:
        print(f"  ERROR downloading {file_url}: {exc}")
        return False

    with open(kml_file_path, "wb") as fh:
        fh.write(content)
    print(f"  Downloaded: {file_url}")
    return True


def extract_aoi(output_filename, output_path):
    """Extract AOI entries from <output_filename>.kml to <output_filename>_AOI.csv."""
    kml_file_path = os.path.join(output_path, output_filename + ".kml")
    platform_tag = output_filename[:3].upper()
    entries = extract_S2_entries(
        platform_tag, kml_file_path, output_filename + "_AOI.csv",
        output_path, POLYGON_WKT,
    )
    if not entries:
        print(f"  WARNING: no AOI entries extracted from {output_filename}")
        return False
    print(f"  AOI extraction successful: {output_filename}")
    return True


# ---------------------------------------------------------------------------
# CSV merge
# ---------------------------------------------------------------------------

def merge_aoi_files(directory, output_file):
    """Merge *_AOI.csv; filter dates >= today-2; add Publish Date (+3 days)."""
    merged = []
    today = datetime.datetime.now().date()

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith("_AOI.csv"):
            continue
        filepath = os.path.join(directory, filename)
        print(f"  Merging {filename} ...")
        try:
            df = pd.read_csv(filepath)
        except Exception as exc:
            print(f"  WARNING: cannot read {filepath}: {exc}")
            continue

        df["Acquisition Date"] = pd.to_datetime(df["ObservationTimeStart"]).dt.date
        df = df[df["Acquisition Date"] >= today - timedelta(days=2)]
        if df.empty:
            continue
        df = df[["Acquisition Date", "OrbitRelative", "Platform"]]
        df.rename(columns={"OrbitRelative": "Orbit"}, inplace=True)
        merged.append(df)

    if not merged:
        print("  No valid AOI data found.")
        return False

    out = pd.concat(merged).drop_duplicates().sort_values(by="Acquisition Date")
    out.to_csv(output_file, index=False)
    print(f"  Merged output saved: {output_file}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching Sentinel-2 acquisition plan page ...")
    html_text = fetch_html(S2_URL)
    print(f"  Page size: {len(html_text)} bytes")

    kml_links = extract_kml_links(html_text)

    output_names = {
        "S2A": "S2A_acquisition_plan",
        "S2B": "S2B_acquisition_plan",
        "S2C": "S2C_acquisition_plan",
    }

    results = {}
    for sat, output_name in output_names.items():
        print(f"\nProcessing {sat} ...")
        kml_dict = kml_links[sat]
        if not kml_dict:
            print(f"  No KML links found for {sat}")
            results[sat] = False
            continue

        # Diagnostic: list all available slugs, newest first
        for slug in sorted(kml_dict.keys(), reverse=True):
            print(f"    {slug}")

        # Try the best KML first; if it cannot be downloaded, try the next one
        results[sat] = False
        candidates = get_kml_candidates(kml_dict)
        for slug in candidates:
            print(f"  -> Trying KML: {slug}")
            if download_kml(kml_dict[slug], output_name, STORAGE_PATH):
                results[sat] = extract_aoi(output_name, STORAGE_PATH)
                break

    print("\nMerging AOI CSV files ...")
    merge_ok = merge_aoi_files(STORAGE_PATH, os.path.join("tools", "acquisitionplan.csv"))

    print("\n**********************")
    for sat, ok in results.items():
        print(f"  {sat}: {'Success' if ok else 'no planned acquisitions or error'}")
    print(f"  Merge: {'Success' if merge_ok else 'Failed'}")

    if all(results.values()) and merge_ok:
        print("\nAll Sentinel-2 operations completed successfully.")
        print("removing intermediate AOI files ...")
        for filename in os.listdir(STORAGE_PATH):
            if filename.endswith("_AOI.csv") or filename.endswith("_plan.kml"):
                os.remove(os.path.join(STORAGE_PATH,  filename))
    else:
        print("\nCompleted with warnings (see above).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("Acquisition plan update failed — continuing without updated plan.")
        sys.exit(0)