import os
import sys
import requests
import xarray as xr
from datetime import datetime, timedelta

# The script is started from the repository root as a file, so the repository
# has to be added to the search path for the config import below
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import configuration as config  # noqa: E402

##############################
# INTRODUCTION
# This script calculates land surface temperature (LST) from surface downwelling longwave
# radiation (SDL) and surface outgoing longwave radiation (SOL) and aggregates it to
# DOY reference statistics (2004-2020, 11am UTC, +/- 3 days window) for the VHI calculation.
#
# The SDL and SOL input is the MeteoSwiss all-sky data published on data.geo.admin.ch,
# i.e. exactly the same source and the same LST calculation as the daily VHI
# (step1_processors/step1_processor_vhi.py).
#
# Run from the repository root:
#   python main_functions/LST_reference.py

##############################
# CONTENT
# 1. Configuration / parameters
# 2. Functions (download, date range, LST calculation)
# 3. Processing: loop over the DOYs, calculate the statistics, save to NetCDF

##############################
# CONFIGURATION / PARAMETERS

# DOYs to process: [196] for a test, range(1, 367) for the full reference
doys_to_process = [196]

# Paths
lst_source_url = f"{config.PRODUCT_VHI['LST_current_data']}/MSG2004-2023"  # same source as the daily VHI (before 2024)
local_source_dir = os.path.join(REPO_DIR, 'local_assets', 'LST_SOURCE')  # downloaded SDL/SOL monthly files
local_output_dir = os.path.join(REPO_DIR, 'local_assets', 'LST_REFERENCE')  # resulting DOY reference files

# Constants
n_days = 3  # Number of days to expand on each side of the target DOY
satellite = 'MSG'
channel = 'ch02'  # 0.02° resolution (MSG)
years = range(2004, 2021)  # 2004-2020
first_available_date = datetime(2004, 1, 1)
last_available_date = datetime(2020, 12, 31)

os.makedirs(local_source_dir, exist_ok=True)
os.makedirs(local_output_dir, exist_ok=True)

##############################
# FUNCTIONS
# Function to download a file once (skipped if it is already in the local folder)
def download_file(url, local_path):
    """Download a file from a URL to a local path, unless it already exists."""
    if os.path.exists(local_path):
        return local_path

    print(f"  Downloading {url}")
    temp_path = local_path + '.part'
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    # Rename only when the download is complete, so no broken file stays in the folder
    os.replace(temp_path, local_path)
    return local_path

# Function to download the SDL and SOL monthly files of a specific month
def download_month(year, month):
    """
    Download the SDL and SOL monthly files of a month from data.geo.admin.ch.
    The file names are the same as in step1_processor_vhi.py.

    Returns
    -------
    tuple of str
        (local_sdl_path, local_sol_path)
    """
    sdl_filename = f'msg.SDL.H_{channel}.lonlat_{year}{month:02d}01000000.nc'
    sol_filename = f'msg.SOL.H_{channel}.lonlat_{year}{month:02d}01000000.nc'

    local_sdl = download_file(f'{lst_source_url}/{sdl_filename}', os.path.join(local_source_dir, sdl_filename))
    local_sol = download_file(f'{lst_source_url}/{sol_filename}', os.path.join(local_source_dir, sol_filename))
    return local_sdl, local_sol

# Function to get date range for a given DOY, handling year boundaries and data availability
def get_date_range_for_doy(year, doy, n_days, first_date=None, last_date=None):
    """
    Get date range for a given DOY, handling year boundaries and data availability.

    Parameters
    ----------
    year : int
        Target year
    doy : int
        Target day of year
    n_days : int
        Number of days to expand on each side
    first_date : datetime, optional
        First available date in dataset (dates before this will be excluded)
    last_date : datetime, optional
        Last available date in dataset (dates after this will be excluded)

    Returns
    -------
    list of datetime
        List of dates in the range (only includes available dates)
    """
    # Convert DOY to date
    center_date = datetime.strptime(f'{year}-{doy}', '%Y-%j')

    # Create date range
    dates = []
    for offset in range(-n_days, n_days + 1):
        date = center_date + timedelta(days=offset)

        # Check if date is within available data range
        if first_date and date < first_date:
            print(f"    Skipping {date.strftime('%Y-%m-%d')} (before first available date)")
            continue
        if last_date and date > last_date:
            print(f"    Skipping {date.strftime('%Y-%m-%d')} (after last available date)")
            continue

        dates.append(date)

    return dates

# Function to calculate LST from radiance
# Keep identical to calc_LST_for_date in step1_processors/step1_processor_vhi.py
def calc_LST_for_date(ds_sol, ds_sdl, date, aggregation='hour', hour=None):
    """
    Calculate LST for a specific date with flexible aggregation options.

    Args:
        ds_sol: xarray Dataset with SOL data (already loaded)
        ds_sdl: xarray Dataset with SDL data (already loaded)
        date: date string in format 'YYYY-MM-DD'
        aggregation: 'max', 'mean', or 'hour' (default: 'hour')
        hour: Specific hour (0-23) when aggregation='hour' (e.g., 11 for 11am)

    Returns:
        xarray Dataset with calculated LST
    """
    # Convert date string to datetime
    target_date = datetime.strptime(date, '%Y-%m-%d')

    # Define time range for the full day
    start_time = target_date
    end_time = target_date + timedelta(days=1) - timedelta(seconds=1)

    # Filter data for the specific date
    sol_filtered = ds_sol.sel(time=slice(start_time, end_time))
    sdl_filtered = ds_sdl.sel(time=slice(start_time, end_time))

    # Check if we have data for the target date
    if len(sol_filtered.time) == 0 or len(sdl_filtered.time) == 0:
        print(f"No data found for {target_date.strftime('%Y-%m-%d')}")
        return None

    # Merge datasets
    ds = xr.merge([sol_filtered, sdl_filtered], compat='override')

    # Calculate LST
    Boltzmann = 5.670374419e-8
    Emissivity = 0.98
    ds['LST'] = ((ds['SOL']-(1-Emissivity)*ds['SDL'])/Boltzmann/(Emissivity))**(1/4)

    # Apply aggregation
    if aggregation == 'mean':
        lst_aggregated = ds['LST'].mean(dim='time')
        var_name = 'LST_mean'
    elif aggregation == 'max':
        lst_aggregated = ds['LST'].max(dim='time')
        var_name = 'LST_max'
    elif aggregation == 'hour':
        if hour is None:
            raise ValueError("hour must be specified when aggregation='hour'")
        if not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")

        # Filter for specific hour
        target_hour = target_date.replace(hour=hour, minute=0, second=0)
        ds_hour = ds.sel(time=target_hour, method='nearest')
        lst_aggregated = ds_hour['LST']
        var_name = f'LST_hour{hour:02d}'
    else:
        raise ValueError("aggregation must be 'max', 'mean', or 'hour'")


    # Create output dataset
    ds_output = xr.Dataset(
        data_vars={
            var_name: (('lat', 'lon'), lst_aggregated.values)
        },
        coords={
            'time': [target_date],
            'lat': ds.lat,
            'lon': ds.lon
        }
    )

    return ds_output

##############################
# PROCESSING
# Loop through the DOYs
for doy in doys_to_process:
    print(f"\n{'#'*80}")
    print(f"PROCESSING DOY {doy}")
    print(f"{'#'*80}\n")

    doy_int = int(doy)
    doy_str = str(doy_int).zfill(3)

    # Initialize list to collect daily datasets
    lst_list = []

    # Track statistics
    total_dates_requested = 0
    dates_skipped_boundary = 0
    dates_skipped_error = 0

    # Process each year
    for year in years:
        print(f"\n{'='*60}")
        print(f"Processing year {year}")
        print(f"{'='*60}")

        # Get date range for this year (with boundary protection)
        date_range = get_date_range_for_doy(
            year, doy_int, n_days,
            first_date=first_available_date,
            last_date=last_available_date
        )

        total_dates_requested += (2 * n_days + 1)
        dates_skipped_boundary += (2 * n_days + 1) - len(date_range)

        # Process each date in the range
        for date in date_range:
            try:
                print(f"Processing {date.strftime('%Y-%m-%d')}...")

                # Download the monthly files (only once) and open them
                local_sdl, local_sol = download_month(date.year, date.month)
                with xr.open_dataset(local_sdl, engine='h5netcdf') as ds_sdl, \
                     xr.open_dataset(local_sol, engine='h5netcdf') as ds_sol:

                    # Calculate LST for 11AM UTC (same call as in step1_processor_vhi.py)
                    ds_lst = calc_LST_for_date(ds_sol, ds_sdl, date.strftime('%Y-%m-%d'), aggregation='hour', hour=11)

                if ds_lst is None:
                    dates_skipped_error += 1
                    continue

                lst_list.append(ds_lst)

            except Exception as e:
                print(f"Error processing {date.strftime('%Y-%m-%d')}: {e}")
                dates_skipped_error += 1
                continue

    # Concatenate along time dimension
    print(f"\n{'='*60}")
    print("Concatenating all datasets...")
    ds_lst_stacked = xr.concat(lst_list, dim='time', data_vars='all')

    print(f"Dates requested: {total_dates_requested}, used: {len(ds_lst_stacked.time)}, "
          f"skipped (outside data range): {dates_skipped_boundary}, skipped (no data / error): {dates_skipped_error}")

    # Calculate statistics for each variable across the time dimension
    print(f"\n{'='*60}")
    print("Calculating statistics across all years and time window...")

    # Convert to Celsius for all calculations
    lst_celsius = ds_lst_stacked['LST_hour11'] - 273.15

    # Calculate statistics for LST
    lst_p05 = lst_celsius.quantile(0.05, dim='time')
    lst_p95 = lst_celsius.quantile(0.95, dim='time')

    if 'quantile' in lst_p05.coords:
        lst_p05 = lst_p05.drop_vars('quantile')
    if 'quantile' in lst_p95.coords:
        lst_p95 = lst_p95.drop_vars('quantile')

    stats = xr.Dataset({
        'LST_11am_min': lst_celsius.min(dim='time'),
        'LST_11am_max': lst_celsius.max(dim='time'),
        'LST_11am_mean': lst_celsius.mean(dim='time'),
        'LST_11am_median': lst_celsius.median(dim='time'),
        'LST_11am_p05': lst_p05,
        'LST_11am_p95': lst_p95
    })

    # Add metadata
    metadata = {
        'years': f'{years[0]}-{years[-1]}',
        'n_samples': len(ds_lst_stacked.time),
        'satellite': satellite,
        'first_available_date': first_available_date.strftime('%Y-%m-%d'),
        'last_available_date': last_available_date.strftime('%Y-%m-%d'),
        'description': 'LST statistics calculated from satellite data',
        'source': f'{lst_source_url} (MeteoSwiss all-sky SDL/SOL)',
        'units': 'degrees Celsius',
        'doy': doy_int,
        'window_days': 2 * n_days + 1,
    }

    stats.attrs.update(metadata)
    stats.attrs['aggregation_type'] = 'hour_11_UTC'

    ##############################
    # SAVE STATISTICS TO FILES
    # Same file name as the reference used by step1_processor_vhi.py
    base_filename = f'LST_statistics_DOY{doy_str}_{satellite}_{channel}_{years[0]}-{years[-1]}_{n_days*2+1}days'
    output_filename_11am = os.path.join(local_output_dir, f'{base_filename}_11am.nc')

    stats.to_netcdf(output_filename_11am)
    print(f"LST_11am statistics saved to: {output_filename_11am}")
