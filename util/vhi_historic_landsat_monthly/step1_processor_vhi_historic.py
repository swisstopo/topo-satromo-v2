# On Windows, the GDAL Python bindings (used by the HORAYZON terrain module) crash if they are imported after
# rasterio. So they are imported first, even though this script does not use them (GDAL runs via subprocess).
from osgeo import gdal  # noqa: F401

import os
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"  # avoid directory listings on S3 (before rasterio is imported)
os.environ["AWS_NO_SIGN_REQUEST"] = "YES"  # the reference files and masks are in a public S3 bucket
import calendar
import json
import socket
import subprocess
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
import requests
import rioxarray  # noqa: F401  (adds the .rio functions to xarray, used to write the COGs)
import xarray as xr
from affine import Affine
from rasterio.transform import from_origin
from rasterio.warp import reproject, Resampling

# The script is started from the repository root as a file, so the repository and the util/ref_ndvi_landsat folder
# have to be added to the search path for the imports below
REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # util/vhi_historic_landsat_monthly/ -> repository
sys.path.insert(0, REPO_DIR)
sys.path.append(os.path.join(REPO_DIR, "util", "ref_ndvi_landsat"))

import configuration as config  # noqa: E402
from main_functions import main_utils, main_publish_stac_fsdi, main_extract_warnregions, main_thumbnails  # noqa: E402
from step0_processors.step0_utils import write_asset_as_empty  # noqa: E402
import step4_build_reference as step4  # noqa: E402  (the Landsat processing of the NDVI reference)
import vi_reference_config as ref_config  # noqa: E402

##############################
# INTRODUCTION
# This script calculates the historic vegetation health index (VHI) over Switzerland, one file per calendar month,
# from Landsat 5, 7 and 8, for 1991-01 to 2015-12. From 2016 onwards the daily Sentinel-2 VHI is used
# (step1_processor_vhi.py). It is only run manually.
#
# The Landsat scenes are processed exactly like the scenes of the new NDVI reference (util/ref_ndvi_landsat/step4_build_reference.py):
# QA and NDSI snow masks, HORAYZON terrain shadow, SCSc topographic correction and the correction curves
# that bring Landsat NDVI to swissEO S2-SR v200. This needs the files of util/ref_ndvi_landsat/output
# (static layers, correction curves, scene list), see util/ref_ndvi_landsat/README.md.
#
# Usage (from the repository root):
#   python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-07-01               one month
#   python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-01-01 2005-12-01    all months in between
# The dates are always the first of the month (item 2005-07-01t235959 = July 2005).

##############################
# CONTENT
# For every month:
# 1. Check that the LST data of the month exists (before any scene is processed)
# 2. Find the Landsat scenes of the month and process them (8 scenes in parallel, cached on the disk)
# 3. Monthly NDVI: mean per overpass, then mean of all overpasses
# 4. Calculate the VCI from the monthly NDVI and the monthly NDVI reference (M files)
# 5. Calculate the TCI from the monthly mean 11am LST and the monthly LST reference
# 6. Combine the VCI and TCI to the VHI
# 7. Mask for forest or all vegetation (30 m masks)
# 8. Write the COGs, warnregions, thumbnail and metadata file, and publish them to STAC
# If there is no usable data for a month, the month is written to the empty asset list (tools/step0_empty_assets.csv).

##############################
# SETTINGS
workWithPercentiles = True  # True: p05 and p95 of the reference data sets, False: min and max
alpha = 0.5  # weighting factor for the VHI (0.5 means equal weight for VCI and TCI)
vhi_version = "v200"
ref_ndvi_nodata = 255  # NoData value in the reference NDVI statistics
lst_hour = 11  # the LST of every day is taken at 11:00 UTC, as for the daily VHI
first_msg_year = 2004  # MSG LST starts in 2004, before that MFG (source not yet defined)
warnregions = "assets/warnregionen_vhi_2056.shp"  # same file as for the daily VHI


##############################
# MONTHS
def get_months(first_date_str, last_date_str):
    """All months from the first to the last date. Both dates must be the first of a month."""
    product = config.PRODUCT_VHI_HIST
    first_date = datetime.strptime(first_date_str, "%Y-%m-%d")
    last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
    allowed_first = datetime.strptime(product["first_month"], "%Y-%m-%d")
    allowed_last = datetime.strptime(product["last_month"], "%Y-%m-%d")
    for date in [first_date, last_date]:
        if date.day != 1:
            raise ValueError(f"{date:%Y-%m-%d} is not the first of a month")
        if date < allowed_first or date > allowed_last:
            raise ValueError(f"{date:%Y-%m-%d} is outside {product['first_month']} to {product['last_month']}")
    if last_date < first_date:
        raise ValueError("The end date is before the start date")

    months = []
    date = first_date
    while date <= last_date:
        months.append(date)
        date = (date + timedelta(days=32)).replace(day=1)
    return months


def write_empty_asset_once(product_name, date_str, remark):
    """Write the month to the empty asset list, but only once per month (runs can be repeated)."""
    if not main_utils.is_date_in_empty_asset_list(product_name, date_str):
        write_asset_as_empty(product_name, date_str, remark)


##############################
# GRID (the 30 m grid of the NDVI reference, aligned with the 10 m grid of swissEO S2-SR)
def get_grid():
    transform = from_origin(ref_config.GRID_LEFT, ref_config.GRID_TOP, ref_config.GRID_RESOLUTION, ref_config.GRID_RESOLUTION)
    shape = (ref_config.GRID_HEIGHT, ref_config.GRID_WIDTH)
    return transform, shape


##############################
# LANDSAT NDVI
def get_month_scenes(year, month):
    """The Landsat scenes of one month from the scene list of the NDVI reference (quality checks already done).
    The slot (year * 100 + month, e.g. 200507) names the cache folder of the month."""
    scenes = step4.make_scene_list()
    scenes = scenes[scenes["passes_quality"] & scenes["day"].str.startswith(f"{year}-{month:02d}")].copy()
    scenes["slot"] = year * 100 + month
    return scenes


def make_monthly_ndvi(slot, scenes):
    """Monthly NDVI on the 30 m grid: first the mean of the scenes of the same overpass (neighbouring rows overlap),
    then the mean of all overpasses. Returns the NDVI, the number of overpasses per pixel and the overpasses used."""
    transform, shape = get_grid()
    overpasses = step4.get_overpasses([slot], scenes)
    ndvi_sum = np.zeros(shape, dtype=np.float32)
    overpass_count = np.zeros(shape, dtype=np.uint16)
    overpass_sum = np.zeros(shape, dtype=np.float32)
    overpass_n = np.zeros(shape, dtype=np.uint8)

    for files in overpasses.values():
        overpass_sum.fill(0)
        overpass_n.fill(0)
        for file in files:
            with rasterio.open(file) as src:
                data = src.read(1)
                first_col = int(round((src.bounds.left - ref_config.GRID_LEFT) / ref_config.GRID_RESOLUTION))
                first_row = int(round((ref_config.GRID_TOP - src.bounds.top) / ref_config.GRID_RESOLUTION))
            rows = slice(first_row, first_row + data.shape[0])
            cols = slice(first_col, first_col + data.shape[1])
            valid = data != step4.CACHE_NODATA
            overpass_sum[rows, cols][valid] += data[valid] / step4.CACHE_SCALE
            overpass_n[rows, cols][valid] += 1
        has_value = overpass_n > 0
        ndvi_sum[has_value] += overpass_sum[has_value] / overpass_n[has_value]
        overpass_count[has_value] += 1

    ndvi = np.full(shape, np.nan, dtype=np.float32)
    has_value = overpass_count > 0
    ndvi[has_value] = ndvi_sum[has_value] / overpass_count[has_value]
    return ndvi, overpass_count, overpasses


##############################
# NDVI REFERENCE (M files)
def read_ndvi_reference(month, band_name):
    """One band of the monthly NDVI reference, found by its name (e.g. p05). Decoded to NDVI, NaN where no data."""
    path = f"{config.PRODUCT_VHI_HIST['NDVI_reference_data']}NDVI_Stats_M{month:02d}.tif"
    transform, shape = get_grid()
    with rasterio.open(path) as src:
        if (src.height, src.width) != shape or not src.transform.almost_equals(transform):
            raise ValueError(f"{path} is not on the 30 m grid of the reference")
        band_number = list(src.descriptions).index(band_name) + 1
        data = src.read(band_number)
    values = data.astype(np.float32) / 100 - 1  # value = DN / scale - 1, scale = 100
    values[data == ref_ndvi_nodata] = np.nan
    return values, path


##############################
# LST
def get_msg_paths(year, month):
    """The MeteoSwiss MSG files (one month of hourly values each) for 2004-2023, as in step1_processor_vhi.py."""
    base = config.PRODUCT_VHI_HIST["LST_current_data"]
    sdl_path = f"{base}/MSG2004-2023/msg.SDL.H_ch02.lonlat_{year}{month:02d}01000000.nc"
    sol_path = f"{base}/MSG2004-2023/msg.SOL.H_ch02.lonlat_{year}{month:02d}01000000.nc"
    return sdl_path, sol_path


def lst_files_exist(year, month):
    for path in get_msg_paths(year, month):
        response = requests.head(path, timeout=30, allow_redirects=True)
        if response.status_code != 200:
            print(f"LST file missing: {path}")
            return False
    return True


def load_monthly_lst(year, month):
    """Mean of the daily 11am LST over the month, in degrees Celsius, on the lat/lon grid of MeteoSwiss.
    Same formula as step1_processor_vhi.py. Returns (lat, lon, values, list of the days used)."""
    if year < first_msg_year:
        # TODO: before 2004 the LST comes from MFG. Add the reading of these files here once the source is defined.
        raise NotImplementedError("MFG LST source (1991-2003) not yet defined, see TODO in load_monthly_lst")

    sdl_path, sol_path = get_msg_paths(year, month)
    boltzmann = 5.670374419e-8
    emissivity = 0.98
    daily_lst = []
    days_used = []
    with xr.open_dataset(sdl_path, engine="h5netcdf") as ds_sdl, \
         xr.open_dataset(sol_path, engine="h5netcdf") as ds_sol:
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            target_time = datetime(year, month, day, lst_hour)
            sol = ds_sol["SOL"].sel(time=target_time, method="nearest")
            sdl = ds_sdl["SDL"].sel(time=target_time, method="nearest")
            # nearest also finds another hour if 11:00 is missing: only use the value of 11:00 itself
            if sol.time.values != np.datetime64(target_time) or sdl.time.values != np.datetime64(target_time):
                print(f"  no LST at {target_time:%Y-%m-%d %H:%M}, day left out")
                continue
            lst = ((sol.values - (1 - emissivity) * sdl.values) / boltzmann / emissivity) ** (1 / 4)
            daily_lst.append(lst)
            days_used.append(f"{target_time:%Y-%m-%dT%H%M%S}")
        lat = ds_sol.lat.values
        lon = ds_sol.lon.values
    if not daily_lst:
        return lat, lon, None, days_used
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # pixels without any value
        monthly_mean = np.nanmean(np.stack(daily_lst), axis=0) - 273.15  # Kelvin to Celsius
    return lat, lon, monthly_mean, days_used


def get_lst_reference_path(year, month):
    """The monthly LST reference (2004-2020) of the month.
    1991-2003 (MFG years): on the 0.05° grid (ch05), 2004-2015 (MSG years): on the 0.02° grid (ch02)."""
    product = config.PRODUCT_VHI_HIST
    if year < first_msg_year:
        return product["LST_reference_ch05"].format(month=month)
    return product["LST_reference_ch02"].format(month=month)


def resample_lst_to_grid(lat, lon, values, target_transform, target_shape):
    """Resample LST from the lat/lon grid to the 30 m grid in EPSG:2056 (nearest), as in step1_processor_vhi.py."""
    lat_ascending = lat[1] > lat[0]
    lon_ascending = lon[1] > lon[0]
    lat_res = abs(lat[1] - lat[0])
    lon_res = abs(lon[1] - lon[0])
    top_lat = lat[-1] if lat_ascending else lat[0]
    left_lon = lon[0] if lon_ascending else lon[-1]
    src_transform = Affine.translation(left_lon - lon_res / 2, top_lat + lat_res / 2) * Affine.scale(lon_res, -lat_res)

    # Rasterio expects the rows from north to south and the columns from west to east
    oriented = np.flipud(values) if lat_ascending else values
    if not lon_ascending:
        oriented = np.fliplr(oriented)

    resampled = np.empty(target_shape, dtype=np.float32)
    reproject(source=oriented.astype(np.float32), destination=resampled,
              src_transform=src_transform, src_crs="EPSG:4326",
              dst_transform=target_transform, dst_crs="EPSG:2056",
              resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    return resampled


##############################
# MASKS (30 m)
def get_mask_30m(mask_name):
    """A forest or vegetation mask on the 30 m grid. The 10 m mask (0/1) is averaged once to 30 m with gdalwarp:
    each 30 m pixel covers exactly 9 pixels of 10 m, so the average is the share covered (0, 1/9, ..., 1).
    The result is kept in local_assets. True where the share is at least mask_min_fraction_30m."""
    product = config.PRODUCT_VHI_HIST
    output_file = os.path.join("local_assets", mask_name.replace(".tif", "_30m.tif"))
    if not os.path.exists(output_file):
        local_10m = os.path.join("local_assets", mask_name)
        if os.path.exists(local_10m):
            source = local_10m
        else:
            source = product["vegetation_masks"].replace("s3://", "/vsis3/") + mask_name
        print(f"Creating the 30 m mask {output_file} from {source}")
        os.makedirs("local_assets", exist_ok=True)
        # -srcnodata None: 0 means "not in the mask" here, so it must count in the average
        subprocess.run(["gdalwarp", "-q", "-overwrite", "-srcnodata", "None",
                        "-te", str(ref_config.GRID_LEFT), str(ref_config.GRID_BOTTOM),
                        str(ref_config.GRID_RIGHT), str(ref_config.GRID_TOP),
                        "-tr", str(ref_config.GRID_RESOLUTION), str(ref_config.GRID_RESOLUTION),
                        "-r", "average", "-ot", "Float32", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
                        source, output_file], check=True)
    with rasterio.open(output_file) as src:
        share = src.read(1)
    return share >= product["mask_min_fraction_30m"]


##############################
# OUTPUT
def write_cog(values, filename):
    """Write the VHI as a Cloud Optimized GeoTIFF, as in step1_processor_vhi.py."""
    transform, shape = get_grid()
    height, width = shape
    xs = transform.c + (np.arange(width) + 0.5) * transform.a
    ys = transform.f + (np.arange(height) + 0.5) * transform.e
    data = xr.DataArray(values, dims=("y", "x"), coords={"y": ys, "x": xs})
    data = data.rio.write_crs(ref_config.GRID_CRS)
    data.rio.write_nodata(config.PRODUCT_VHI_HIST["no_data"], inplace=True)
    data.rio.to_raster(raster_path=f"{filename}.tif", driver="COG", COMPRESS="DEFLATE", BIGTIFF="YES",
                       NUM_THREADS="ALL_CPUS", ADD_ALPHA="NO",
                       OVERVIEW_RESAMPLING="NEAREST")  # overview pixels are real values, never averages


def raster_band_info(tif_filename):
    with rasterio.open(f"{tif_filename}.tif") as src:
        return [{
            "id": "vhi",
            "data_type": {"type": "PixelType", "precision": "int", "min": 0, "max": 255},
            "dimensions": [src.width, src.height],
            "crs": str(src.crs),
            "crs_transform": [src.transform.a, src.transform.b, src.transform.c,
                              src.transform.d, src.transform.e, src.transform.f],
        }]


def write_metadata_json(properties, filenames, warnregion_filenames, warnformats, mask_names, timestamp, processor_version):
    """The metadata file, with the same structure as for the daily VHI (keys FOREST-30M and VEGETATION-30M)."""
    product = config.PRODUCT_VHI_HIST
    item_label = timestamp.upper()
    metadata = {}
    for mask_type, geocat_id in [("forest", product["geocat_id_forest"]), ("vegetation", product["geocat_id_vegetation"])]:
        key = f"{mask_type.upper()}-30M"
        metadata[key] = {
            "BANDS": raster_band_info(filenames[mask_type]),
            "PROPERTIES": {
                "PRODUCT": product["product_name"],
                "ITEM": item_label,
                "ASSET": filenames[mask_type],
                "DATEITEMGENERATION": datetime.now().strftime("%Y-%m-%d"),
                "PROCESSORHASHLINK": processor_version["GithubLink"],
                "PROCESSORRELEASEVERSION": processor_version["ReleaseVersion"],
                "GEOCATID": geocat_id,
                "PROCESSING_DATE_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
                "PROCESSING_HOSTNAME": socket.gethostname(),
                "VHI_VERSION": vhi_version,
                **properties,
                "VEGETATION_MASK": mask_names[mask_type],
            },
        }
        for fmt in warnformats:
            metadata[f"{mask_type.upper()}-WARNREGIONS{fmt.replace('.', '-').upper()}"] = {
                "PRODUCT": product["product_name"],
                "ITEM": item_label,
                "ASSET": f"{warnregion_filenames[mask_type]}{fmt}",
                "SOURCE": f"{filenames[mask_type]}.tif",
                "VHI_VERSION": vhi_version,
                "format": fmt,
                "regionId": "RegionID",
                "vhiMean": "VHI Mean Region",
                "availabilityPercentage": "percentage of available pixels with information within region",
                "VEGETATION_MASK": mask_names[mask_type],
            }
    filename_metadata = f"{product['product_name'].replace('ch.swisstopo.', '')}_mosaic_{timestamp}_metadata.json"
    with open(filename_metadata, "w") as file:
        json.dump(metadata, file, indent=2, default=str)
    print(f"Created metadata JSON: {filename_metadata}")
    return filename_metadata


##############################
# PROCESSING FUNCTION
def process_product_vhi_historic(month_date_str, collection=None):
    """Process the historic VHI of one month. month_date_str is the first of the month, e.g. '2005-07-01'."""
    product = config.PRODUCT_VHI_HIST
    product_name = product["product_name"]
    if collection is None:
        collection = product["step1_collection"]
    month_date = datetime.strptime(month_date_str, "%Y-%m-%d")
    year, month = month_date.year, month_date.month
    timestamp = f"{month_date_str}t235959"
    print(f"********* processing {product_name}, historic month {month_date:%Y-%m} *********")

    ##############################
    # 1. LST CHECK (before any scene is processed)
    if year < first_msg_year:
        return f"VHI historic {month_date:%Y-%m}: MFG LST source (1991-2003) not yet defined, see TODO in load_monthly_lst"
    lst_reference_path = get_lst_reference_path(year, month)
    if not lst_files_exist(year, month):
        write_empty_asset_once(product_name, month_date_str, "LST missing")
        return f"VHI historic: LST data missing for {month_date:%Y-%m}, written to empty asset list."

    ##############################
    # 2. LANDSAT SCENES
    scenes = get_month_scenes(year, month)
    if len(scenes) == 0:
        print(f"No Landsat scene in {month_date:%Y-%m}")
        write_empty_asset_once(product_name, month_date_str, "No Landsat scene in month")
        return f"VHI historic: no Landsat scene in {month_date:%Y-%m}, written to empty asset list."
    slot = year * 100 + month
    print(f"{len(scenes)} Landsat scenes in {month_date:%Y-%m}")
    step4.prepare_slots([slot], scenes)

    ##############################
    # 3. MONTHLY NDVI
    ndvi, overpass_count, overpasses = make_monthly_ndvi(slot, scenes)
    step4.delete_slot(slot)  # the cache of the month is not needed any more
    used_files = [file for files in overpasses.values() for file in files]
    scene_ids = sorted(os.path.basename(file).replace(".tif", "") for file in used_files)
    days = sorted(key[0] for key in overpasses)
    print(f"Monthly NDVI from {len(overpasses)} overpasses ({len(scene_ids)} scenes)")

    forest_mask_name = product["forest_mask_name"]
    vegetation_mask_name = product["vegetation_mask_name"]
    vegetation_mask = get_mask_30m(vegetation_mask_name)
    if not np.isfinite(ndvi[vegetation_mask]).any():
        print(f"No usable Landsat data in {month_date:%Y-%m} (all scenes cloudy or masked)")
        write_empty_asset_once(product_name, month_date_str, "No usable Landsat data in month")
        return f"VHI historic: no usable Landsat data in {month_date:%Y-%m}, written to empty asset list."

    ##############################
    # 4. VCI
    # VCI = 100 * (NDVI - NDVI_min) / (NDVI_max - NDVI_min)
    if workWithPercentiles:
        ndvi_ref_min, ndvi_reference_path = read_ndvi_reference(month, "p05")
        ndvi_ref_max, _ = read_ndvi_reference(month, "p95")
        CI_method = "5th_and_95th_percentile"
    else:
        ndvi_ref_min, ndvi_reference_path = read_ndvi_reference(month, "min")
        ndvi_ref_max, _ = read_ndvi_reference(month, "max")
        CI_method = "min_and_max"
    vci_den = ndvi_ref_max - ndvi_ref_min
    vci_den[vci_den == 0] = np.nan  # avoid division by zero
    vci = (ndvi - ndvi_ref_min) / vci_den * 100
    del ndvi, ndvi_ref_min, ndvi_ref_max, vci_den
    print(f"Calculated VCI ({CI_method})")

    ##############################
    # 5. TCI
    # TCI = 100 * (LST_max - LST) / (LST_max - LST_min)
    transform, shape = get_grid()
    lat, lon, lst_values, lst_days = load_monthly_lst(year, month)
    if lst_values is None:
        write_empty_asset_once(product_name, month_date_str, "LST missing")
        return f"VHI historic: no LST value in {month_date:%Y-%m}, written to empty asset list."
    lst = resample_lst_to_grid(lat, lon, lst_values, transform, shape)
    print(f"Monthly mean LST from {len(lst_days)} days (11:00 UTC)")

    print(f"LST reference: {lst_reference_path}")
    reference_bands = ["p05", "p95"] if workWithPercentiles else ["min", "max"]
    with xr.open_dataset(lst_reference_path, engine="h5netcdf", storage_options={"anon": True}) as ds_lst_ref:
        lst_ref_values = {}
        for band in reference_bands:
            lst_ref_values[band] = resample_lst_to_grid(ds_lst_ref.lat.values, ds_lst_ref.lon.values,
                                                        ds_lst_ref[f"LST_11am_{band}"].values, transform, shape)
    lst_ref_min = lst_ref_values[reference_bands[0]]
    lst_ref_max = lst_ref_values[reference_bands[1]]
    tci_den = lst_ref_max - lst_ref_min
    tci_den[tci_den == 0] = np.nan
    tci = (lst_ref_max - lst) / tci_den * 100
    del lst, lst_ref_min, lst_ref_max, tci_den, lst_ref_values
    print("Calculated TCI")

    ##############################
    # 6. VHI
    # VHI = a*VCI + (1-a)*TCI, forced to [0 100], missing data where VCI or TCI is missing, as uint8
    vhi = vci * alpha + tci * (1 - alpha)
    del vci, tci
    vhi = np.clip(vhi, 0, 100)
    vhi = np.where(np.isnan(vhi), product["missing_data"], vhi).astype(np.uint8)
    print(f"Calculated VHI for {month_date:%Y-%m}")

    ##############################
    # 7. VEGETATION MASKS AND COGS
    forest_mask = get_mask_30m(forest_mask_name)
    vhi_forest = np.where(forest_mask, vhi, product["no_data"]).astype(np.uint8)
    vhi_vegetation = np.where(vegetation_mask, vhi, product["no_data"]).astype(np.uint8)
    del vhi

    short_name = product_name.replace("ch.swisstopo.", "")
    filenames = {"forest": f"{short_name}_mosaic_{timestamp}_forest-30m",
                 "vegetation": f"{short_name}_mosaic_{timestamp}_vegetation-30m"}
    write_cog(vhi_forest, filenames["forest"])
    write_cog(vhi_vegetation, filenames["vegetation"])

    ##############################
    # 8. WARNREGIONS, THUMBNAIL, METADATA
    warnformats = [".csv", ".geojson", ".parquet"]
    warnregion_filenames = {"forest": f"{short_name}_{timestamp}_forest-warnregions",
                            "vegetation": f"{short_name}_{timestamp}_vegetation-warnregions"}
    for mask_type in ["forest", "vegetation"]:
        main_extract_warnregions.export(f"{filenames[mask_type]}.tif", warnregions, warnregion_filenames[mask_type], f"{month_date_str}T235959Z",
                                        product["missing_data"], product["no_data"], product["scaling_factor"], "vhi")
        print(f"Created warnregions for {mask_type} areas")

    filename_thumbnail = main_thumbnails.create_thumbnail(f"{filenames['vegetation']}.tif", product_name)

    month_end = (month_date + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
    processor_version = main_utils.get_github_info()
    properties = {
        "MONTH": f"{month_date:%Y-%m}",
        "ALPHA": alpha,
        "TEMPORAL_COVERAGE": f"{product['temporal_coverage']} month",
        "NDVI_REFERENCE_DATA": ndvi_reference_path,
        "NDVI_INDEX_LIST": ",".join(scene_ids),
        "NDVI_SCENE_COUNT": len(scene_ids),
        "NDVI_OVERPASS_COUNT": len(overpasses),
        "DATE_FIRST_SCENE": days[0],
        "DATE_LAST_SCENE": days[-1],
        "NDVI_CORRECTION_CURVES": os.path.basename(ref_config.CORRECTION_CURVES_FILE) if ref_config.APPLY_CORRECTION else "none",
        "LST_REFERENCE_DATA": lst_reference_path,
        "LST_INDEX_LIST": ",".join(lst_days),
        "LST_SCENE_COUNT": len(lst_days),
        "VCI_AND_TCI_CALCULATED_WITH": CI_method,
        "NO_DATA": product["no_data"],
        "MISSING_DATA": product["missing_data"],
        "PIXEL_SIZE_METER": product["spatial_scale_export"],
        "SYSTEM_TIME_START": f"{month_date:%Y-%m-%d %H:%M:%S}",
        "SYSTEM_TIME_END": f"{month_end:%Y-%m-%d %H:%M:%S}",
    }
    mask_names = {"forest": forest_mask_name, "vegetation": vegetation_mask_name}
    filename_metadata = write_metadata_json(properties, filenames, warnregion_filenames, warnformats,
                                            mask_names, timestamp, processor_version)

    all_files = ([f"{filenames['forest']}.tif", f"{filenames['vegetation']}.tif"]
                 + [warnregion_filenames["forest"] + fmt for fmt in warnformats]
                 + [warnregion_filenames["vegetation"] + fmt for fmt in warnformats]
                 + [filename_metadata, filename_thumbnail])

    ##############################
    # 9. PUBLISH
    for mask_type, geocat_id in [("forest", product["geocat_id_forest"]), ("vegetation", product["geocat_id_vegetation"])]:
        main_publish_stac_fsdi.publish_to_stac(f"{filenames[mask_type]}.tif", timestamp, product_name, geocat_id,
                                               None, asset_title=f"{mask_type.upper()}-30M")
        for fmt in warnformats:
            main_publish_stac_fsdi.publish_to_stac(warnregion_filenames[mask_type] + fmt, timestamp, product_name, geocat_id,
                                                   None, asset_title=f"{mask_type.upper()}-WARNREGIONS{fmt.replace('.', '-').upper()}")
    main_publish_stac_fsdi.publish_to_stac(filename_metadata, timestamp, product_name, product["geocat_id_vegetation"],
                                           None, asset_title="Metadata")
    main_publish_stac_fsdi.publish_to_stac(filename_thumbnail, timestamp, product_name, product["geocat_id_vegetation"],
                                           None, asset_title="Thumbnail")

    for filepath in all_files:
        if Path(filepath).exists():
            Path(filepath).unlink()
    print(f"********* finished processing {product_name} for {timestamp} *********")
    return f"VHI historic {month_date:%Y-%m}: published."


##############################
# START
# This line is needed because the scenes are processed in 8 parallel worker processes: on Windows each worker
# reads this file again, and without it every worker would start the whole run again.
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-07-01 [2005-12-01]")
        sys.exit(1)
    first_date_str = sys.argv[2]
    last_date_str = sys.argv[3] if len(sys.argv) > 3 else first_date_str
    for month_date in get_months(first_date_str, last_date_str):
        result = process_product_vhi_historic(month_date.strftime("%Y-%m-%d"))
        print(result)
