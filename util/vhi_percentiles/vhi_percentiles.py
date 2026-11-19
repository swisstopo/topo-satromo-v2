import os
import sys
from datetime import date

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # write PNG files only, no window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

##############################
# INTRODUCTION
# This script calculates the VHI climate reference values (min, p15, p20, p33) per warn region (31-68) for the
# period 1991-2020, as daily values (day of year 1-366) and monthly values (month 1-12).
#
# Sources (all published in the VHI collection, read only):
#   1991-2015: historic monthly Landsat VHI, the monthly value is placed on the 15th of the month
#   2016-    : daily Sentinel-2 VHI (gaps, e.g. no item or no valid value in a region because of snow)
# The regional means come from the published warnregion CSV files of every item (REGION_NR, vhi_mean,
# availability_percentage), made by main_functions/main_extract_warnregions.py.
#
# Every region gets one value per day for 1991-2020 by PCHIP interpolation (smooth, no overshooting).
# Daily percentiles: the 30 values (one per year) of each day of year.
# Monthly percentiles: all interpolated days of a month in the 30 years (about 930 values).
#
# Usage (from the repository root):
#   python util/vhi_percentiles/vhi_percentiles.py              vegetation (default)
#   python util/vhi_percentiles/vhi_percentiles.py --forest     forest
# Results in util/vhi_percentiles/output/ (git-ignored), see README.md.

##############################
# SETTINGS
first_year = 1991
last_year = 2020
first_daily_year = 2016  # before that: one monthly Landsat value, from then on daily Sentinel-2 values
end_of_interpolation = "2021-06-30"  # items after 2020 are only used to interpolate the end of 2020
min_availability_percentage = 5  # a regional mean is used only if at least this share of the region has valid pixels
regions = list(range(31, 69))  # warn regions 31-68
warnregions_file = "assets/warnregionen_vhi_2056.shp"  # for the region names in the plots
# The VHI data is complete only on the production STAC (data.geo.admin.ch). The script only reads public data.
source_stac_api = "https://data.geo.admin.ch/api/stac/v0.9/"

# Percentiles in the CSV files and the additional ones for the plots (0.0 = min, 1.0 = max)
csv_statistics = {"min": 0.0, "p15": 0.15, "p20": 0.20, "p33": 0.33}
plot_statistics = {"min": 0.0, "p15": 0.15, "p20": 0.20, "p33": 0.33, "p66": 0.66, "p80": 0.80, "p85": 0.85, "max": 1.0}

# Plot colours: one dark green hue, the bars in light grey
green = "#1b5e20"
bar_grey = "#d9d9d9"

##############################
# COMMAND LINE AND CONFIGURATION
# "--forest" is removed from sys.argv before the configuration is imported (it reads sys.argv itself)
land_cover = "vegetation"
if "--forest" in sys.argv:
    land_cover = "forest"
    sys.argv.remove("--forest")

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # util/vhi_percentiles/ -> repository
sys.path.insert(0, REPO_DIR)
import configuration as config  # noqa: E402

collection_id = config.PRODUCT_VHI["product_name"]  # ch.swisstopo.swisseo_vhi_v200

OUTPUT_DIR = os.path.join(REPO_DIR, "util", "vhi_percentiles", "output")
CACHE_DIR = os.path.join(OUTPUT_DIR, "cache", land_cover)
PLOT_DIR = os.path.join(OUTPUT_DIR, "plots", land_cover)

if land_cover == "forest":
    file_suffix = "_Wald"
else:
    file_suffix = ""


##############################
# FUNCTIONS

def list_items(start_date, end_date):
    """Return a list of (item_id, csv_url) of all items between the two dates, with the warnregion CSV of the land cover."""
    url = f"{source_stac_api}collections/{collection_id}/items"
    params = {"datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z", "limit": 100}
    items = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        for feature in data["features"]:
            csv_url = None
            for asset_key, asset in feature["assets"].items():
                if asset_key.endswith(f"_{land_cover}-warnregions.csv"):
                    csv_url = asset["href"]
            if csv_url is None:
                print(f"WARNING: item {feature['id']} has no {land_cover}-warnregions.csv, skipped")
            else:
                items.append((feature["id"], csv_url))
        # the next link already contains all parameters
        url = None
        params = None
        for link in data.get("links", []):
            if link["rel"] == "next":
                url = link["href"]
    return items


def get_value_date(item_id):
    """Date at which the value of an item is placed: the 15th for the historic monthly items, else the item date."""
    item_date = date.fromisoformat(item_id[:10])
    if item_date.year < first_daily_year:
        return date(item_date.year, item_date.month, 15)
    return item_date


def download_csv(csv_url):
    """Download a warnregion CSV into the cache (only if it is not there yet) and return the local path."""
    local_path = os.path.join(CACHE_DIR, csv_url.split("/")[-1])
    if not os.path.exists(local_path):
        response = requests.get(csv_url, timeout=60)
        response.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(response.content)
    return local_path


def read_regional_means(local_path):
    """Return a dict {region: VHI mean} with only the valid values (0-100 and enough available pixels)."""
    table = pd.read_csv(local_path)
    values = {}
    for _, row in table.iterrows():
        region = int(row["REGION_NR"])
        vhi_mean = row["vhi_mean"]
        availability = row["availability_percentage"]
        if 0 <= vhi_mean <= 100 and availability >= min_availability_percentage:
            values[region] = float(vhi_mean)
    return values


def gee_percentile(values, p):
    """Percentile as in the old GEE script: the sorted value at index floor(n * p). p = 1.0 gives the maximum."""
    sorted_values = np.sort(values)
    index = int(np.floor(len(sorted_values) * p))
    index = min(index, len(sorted_values) - 1)
    return sorted_values[index]


def add_doy_366(table):
    """Add the column 'doy' (day of year); in years without 29 February, a copy of 31 December gets doy 366."""
    table = table.copy()
    table["doy"] = table.index.dayofyear
    is_dec_31 = (table.index.month == 12) & (table.index.day == 31) & (~table.index.is_leap_year)
    extra_rows = table[is_dec_31].copy()
    extra_rows["doy"] = 366
    return pd.concat([table, extra_rows])


def calculate_statistics(grouped_values, statistics):
    """For each group (doy or month) and region, calculate the statistics. Returns {region: DataFrame}."""
    results = {}
    for region in regions:
        rows = []
        for group_key, group in grouped_values:
            row = {"key": group_key}
            values = group[region].to_numpy()
            for name, p in statistics.items():
                row[name] = gee_percentile(values, p)
            row["mean"] = values.mean()
            rows.append(row)
        results[region] = pd.DataFrame(rows).set_index("key")
    return results


def write_percentile_csv(results, key_name, filename):
    """Write the CSV in the format of the old files: ';', CRLF, 2 decimals, columns min/p15/p20/p33 per region."""
    header = [key_name]
    for region in regions:
        for name in csv_statistics:
            header.append(f"{name}_reg{region}")
    keys = results[regions[0]].index
    with open(filename, "w", newline="") as f:
        f.write(";".join(header) + "\r\n")
        for key in keys:
            line = [str(key)]
            for region in regions:
                for name in csv_statistics:
                    line.append(f"{results[region].loc[key, name]:.2f}")
            f.write(";".join(line) + "\r\n")
    print(f"Written: {filename}")


def plot_region(stats, real_share, x_labels, title, filename):
    """Seasonal curve: grey bars (share of real values), green bands p15-p85, p20-p80, p33-p66, mean, min/max dashed."""
    x = stats.index.to_numpy()
    fig, ax = plt.subplots(figsize=(12, 5))

    # Bars on the right axis, drawn behind the VHI axis
    ax_bars = ax.twinx()
    bar_width = 0.8 if len(x) <= 12 else 1.0
    ax_bars.bar(x, real_share, width=bar_width, color=bar_grey, label="Share of real values (not interpolated)")
    ax_bars.set_ylim(0, 100)
    ax_bars.set_ylabel("Real values [%]")
    ax.set_zorder(ax_bars.get_zorder() + 1)
    ax.patch.set_visible(False)

    # Bands with transparency: overlapping bands make the inner band darker
    ax.fill_between(x, stats["p15"], stats["p85"], color=green, alpha=0.15, linewidth=0, label="p15 - p85")
    ax.fill_between(x, stats["p20"], stats["p80"], color=green, alpha=0.2, linewidth=0, label="p20 - p80")
    ax.fill_between(x, stats["p33"], stats["p66"], color=green, alpha=0.3, linewidth=0, label="p33 - p66")
    ax.plot(x, stats["mean"], color=green, linewidth=2, label="Mean")
    ax.plot(x, stats["min"], color=green, linewidth=1, linestyle="--", label="Min / Max")
    ax.plot(x, stats["max"], color=green, linewidth=1, linestyle="--")

    ax.set_ylim(0, 100)
    ax.set_ylabel("VHI")
    ax.set_xlim(x[0] - 0.5, x[-1] + 0.5)
    if x_labels is not None:
        ax.set_xticks(x)
        ax.set_xticklabels(x_labels)
    else:
        ax.set_xlabel("Day of year")
    ax.set_title(title)

    # One legend for both axes
    handles, labels = ax.get_legend_handles_labels()
    bar_handles, bar_labels = ax_bars.get_legend_handles_labels()
    ax.legend(handles + bar_handles, labels + bar_labels, loc="lower center", ncol=3, fontsize=8)

    fig.tight_layout()
    fig.savefig(filename, dpi=120)
    plt.close(fig)


##############################
# MAIN
if __name__ == "__main__":
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(PLOT_DIR, exist_ok=True)
    print(f"Land cover: {land_cover}, source: {source_stac_api}collections/{collection_id}")

    # 1. List all items
    items = list_items(f"{first_year}-01-01", end_of_interpolation)
    items_per_year = {}
    for item_id, _ in items:
        year = int(item_id[:4])
        items_per_year[year] = items_per_year.get(year, 0) + 1
    print("Items per year:")
    for year in sorted(items_per_year):
        print(f"  {year}: {items_per_year[year]}")

    # 2. + 3. Download the warnregion CSVs and build the raw table (dates x regions, empty = no valid value)
    raw_rows = {}
    for number, (item_id, csv_url) in enumerate(items, start=1):
        if number % 100 == 0:
            print(f"Reading warnregion CSV {number} of {len(items)}")
        local_path = download_csv(csv_url)
        raw_rows[pd.Timestamp(get_value_date(item_id))] = read_regional_means(local_path)
    raw = pd.DataFrame.from_dict(raw_rows, orient="index", columns=regions).sort_index()
    raw.index.name = "date"
    raw_filename = os.path.join(OUTPUT_DIR, f"vhi_regional_means_{land_cover}.csv")
    raw.to_csv(raw_filename)
    print(f"Written: {raw_filename}")

    # 4. Interpolate every region to one value per day (PCHIP between real values, nearest value at the ends)
    all_days = pd.date_range(f"{first_year}-01-01", end_of_interpolation, freq="D")
    raw_daily = raw.reindex(all_days)
    interpolated = raw_daily.interpolate(method="pchip", limit_area="inside")
    interpolated = interpolated.ffill().bfill()  # before the first and after the last real value: nearest value
    interpolated = interpolated.clip(0, 100)

    # keep only the climate period
    period_start = pd.Timestamp(f"{first_year}-01-01")
    period_end = pd.Timestamp(f"{last_year}-12-31")
    interpolated = interpolated.loc[period_start:period_end]
    is_real = raw_daily.loc[period_start:period_end].notna()
    interpolated_filename = os.path.join(OUTPUT_DIR, f"vhi_daily_interpolated_{land_cover}.csv")
    interpolated.round(2).to_csv(interpolated_filename, index_label="date")
    print(f"Written: {interpolated_filename}")

    # Longest gap between two real values per region (in days)
    print("Longest gap between two real values per region:")
    for region in regions:
        real_dates = raw[region].dropna().index
        real_dates = real_dates[(real_dates >= period_start) & (real_dates <= period_end)]
        if len(real_dates) < 2:
            print(f"  Region {region}: fewer than 2 real values")
            continue
        gaps = real_dates[1:] - real_dates[:-1]
        longest = gaps.argmax()
        print(f"  Region {region}: {gaps[longest].days} days ({real_dates[longest].date()} to {real_dates[longest + 1].date()}),"
              f" {len(real_dates)} real values")

    # 5. Daily percentiles: group by day of year (30 values each)
    daily_table = add_doy_366(interpolated)
    daily_groups = daily_table.groupby("doy")
    daily_results = calculate_statistics(daily_groups, plot_statistics)

    # 6. Monthly percentiles: all interpolated days of a month
    monthly_table = interpolated.copy()
    monthly_table["month"] = monthly_table.index.month
    monthly_groups = monthly_table.groupby("month")
    monthly_results = calculate_statistics(monthly_groups, plot_statistics)

    # 7. Write the two CSV files
    write_percentile_csv(daily_results, "doy",
                         os.path.join(OUTPUT_DIR, f"VHI_Tageswerte_Trockenheit{file_suffix}_{first_year}-{last_year}.csv"))
    write_percentile_csv(monthly_results, "month",
                         os.path.join(OUTPUT_DIR, f"VHI_Monatswerte_Trockenheit{file_suffix}_{first_year}-{last_year}.csv"))

    # 8. Plots: one daily and one monthly PNG per region
    real_daily = add_doy_366(is_real.astype(float))
    real_share_daily = real_daily.groupby("doy").mean() * 100
    real_monthly = is_real.astype(float)
    real_monthly["month"] = real_monthly.index.month
    real_share_monthly = real_monthly.groupby("month").mean() * 100

    region_names = {}
    shapes = gpd.read_file(os.path.join(REPO_DIR, warnregions_file), encoding="utf-8")  # the names are UTF-8 (ö, ü, ä)
    for _, row in shapes.iterrows():
        region_names[int(row["REGION_NR"])] = row["Name"]

    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for region in regions:
        name = region_names.get(region, "")
        plot_region(daily_results[region], real_share_daily[region].to_numpy(), None,
                    f"Region {region} {name} – VHI {land_cover}, daily, {first_year}-{last_year}",
                    os.path.join(PLOT_DIR, f"reg{region}_daily.png"))
        plot_region(monthly_results[region], real_share_monthly[region].to_numpy(), month_labels,
                    f"Region {region} {name} – VHI {land_cover}, monthly, {first_year}-{last_year}",
                    os.path.join(PLOT_DIR, f"reg{region}_monthly.png"))
    print(f"Plots written to {PLOT_DIR}")
    print("Done.")
