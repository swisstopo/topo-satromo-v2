# vhi_percentiles

VHI climate reference values per warn region (31-68) for 1991-2020: min, p15, p20 and p33, once per day of year
(1-366) and once per month (1-12). They are the thresholds of the drought warning.

Run from the repository root (only reads public data, nothing is published):

    python util/vhi_percentiles/vhi_percentiles.py              vegetation (default)
    python util/vhi_percentiles/vhi_percentiles.py --forest     forest

## Method

1. All items of the VHI collection (`PRODUCT_VHI["product_name"]` of the configuration, read from data.geo.admin.ch)
   from 1991-01 to 2021-06 are listed, and the warnregion CSV of every item is downloaded
   (`REGION_NR, vhi_mean, availability_percentage`, made by `main_functions/main_extract_warnregions.py`).
   - 1991-2015: historic monthly Landsat VHI, the monthly value is placed on the 15th of the month
   - from 2016: daily Sentinel-2 VHI
2. A regional mean is used if it is between 0 and 100 (110 and 255 = no data) and at least 5 % of the region has valid
   pixels (`min_availability_percentage`).
3. Every region is interpolated to one value per day with PCHIP: a smooth curve through all real values that never
   overshoots (stays between the neighbouring values). This fills the time between the monthly values, the turn of the
   year and the gaps in the daily data (missing days, snow). The items of 2021 are only used to interpolate the end
   of 2020. Before 1991-01-15 the first value is used.
4. Daily values: the 30 values (one per year) of each day of year. In years without 29 February, day 366 uses the
   value of 31 December.
5. Monthly values: all interpolated days of a month in the 30 years (about 930 values), so regions with few real values
   are not biased.
6. Percentiles as in the earlier GEE script: the sorted value at index `floor(n * p)`, min = smallest value.

## Output (`output/`, git-ignored)

- `VHI_Tageswerte_Trockenheit_1991-2020.csv` and `VHI_Monatswerte_Trockenheit_1991-2020.csv`: same format as the
  earlier files (`;`, CRLF, 2 decimals, columns `min_regNN;p15_regNN;p20_regNN;p33_regNN`); with `--forest` the
  names get `_Wald`. The earlier files had `p20_reg50` twice; the fourth column of region 50 is now `p33_reg50`.
- `vhi_regional_means_<land cover>.csv`: the real (not interpolated) regional means, empty = no valid value
- `vhi_daily_interpolated_<land cover>.csv`: the interpolated daily values 1991-2020
- `plots/<land cover>/regNN_daily.png` and `regNN_monthly.png`: mean, bands p15-p85 / p20-p80 / p33-p66, min/max
  dashed, grey bars = share of real values
- `cache/<land cover>/`: downloaded warnregion CSVs (not downloaded again on a rerun; delete the folder to force it)

The log prints the number of items per year and the longest gap between two real values per region.
