# Checks that swissEO S2-SR uses the same reflectance offset across the whole archive.
#
# VHI applies a fixed offset of -0.1 to S2-SR. Deep lake water has a very low red reflectance,
# so its DN stays above 1000 only if the offset is present in the data.
# An archive period without the offset would show values of around 100 to 300 instead.
#
# Usage: python util/ref_ndvi_landsat/step0_check_s2_scaling.py

import json
import os
import re

import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.windows import from_bounds

import vi_reference_config as config

LAKE_GENEVA_WINDOW = (2520000, 1140000, 2525000, 1145000)
FIRST_YEAR = 2015
LAST_YEAR = 2026
MONTHS = [2, 5, 8, 11]
ITEMS_PER_MONTH = 2
MIN_DN_WITH_OFFSET = 800


def get_month_items(year, month):
    start = f"{year}-{month:02d}-01T00:00:00Z"
    end = f"{year}-{month:02d}-28T23:59:59Z"
    response = requests.get(config.S2_SR_ITEMS_URL, params={"datetime": f"{start}/{end}", "limit": 30}, timeout=60)
    response.raise_for_status()
    return response.json()["features"]


def get_asset_href(item, ending):
    for name, asset in item["assets"].items():
        if name.endswith(ending):
            return asset["href"]
    return None


def read_lake_values(red_href):
    with rasterio.open(red_href) as src:
        window = from_bounds(*LAKE_GENEVA_WINDOW, src.transform)
        data = src.read(1, window=window, boundless=True, fill_value=0)
    return data[data > 0]


def read_baselines(item):
    response = requests.get(get_asset_href(item, "_metadata.json"), timeout=60)
    response.raise_for_status()
    granules = json.loads(response.text)["SOURCE"]["GRANULES"].keys()
    baselines = {re.search(r"_N(\d{4})_", name).group(1) for name in granules}
    return ",".join(sorted(baselines))


def main():
    rows = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        for month in MONTHS:
            items_done = 0
            for item in get_month_items(year, month):
                if items_done == ITEMS_PER_MONTH:
                    break
                lake_values = read_lake_values(get_asset_href(item, "_b04_10m.tif"))
                if lake_values.size < 1000:
                    continue  # this orbit does not cover the lake window
                row = {
                    "item": item["id"],
                    "baselines": read_baselines(item),
                    "red_dn_p01": float(np.percentile(lake_values, 1)),
                    "red_dn_median": float(np.median(lake_values)),
                }
                row["offset_present"] = row["red_dn_p01"] >= MIN_DN_WITH_OFFSET
                rows.append(row)
                print(row)
                items_done += 1

    table = pd.DataFrame(rows)
    os.makedirs(config.RESULT_DIR, exist_ok=True)
    table.to_csv(os.path.join(config.RESULT_DIR, "s2_scaling_check.csv"), index=False)

    print("\nItems checked:", len(table))
    print("Items without offset:", int((~table["offset_present"]).sum()))
    print(table.groupby("baselines")["red_dn_p01"].agg(["count", "min", "median"]))


if __name__ == "__main__":
    main()
