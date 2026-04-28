import os
import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import tempfile
import shutil
import s3fs

##############################
# INTRODUCTION
# This script calculates land surface temperature (LST) from surface downwelling longwave 
# radiation (SDL) and surface outgoing longwave radiation (SOL) and aggregates it to 
# climate reference period (1991-2020) statistics for the VHI calculation. 

##############################
# CONTENT
# 1. 

##############################
# CONFIGURATION / PARAMETERS

data_source = 'local' # 's3' or 'local'

# Paths
s3_bucket_satromo = 's3-topo-satromo-prod/'
s3_path_lst = 'data/LST_TEST/' # needs file name addition
local_data_dir = 'test_dev/LST_TEST_DATA/' 

# Processing mode
mode = 'monthly' # 'doy_window' or 'monthly'

# Constants
# year = '2018'
# doy = '364'
n_days = 3  # Number of days to expand on each side of the target DOY (doy_window' mode)
target_months = [8] # e.g. [8] for August, [7, 8] for July+August ('monthly' mode)
satellite = 'MSG' # 'MSG' or 'MFG'
channel = 'ch02' # 'ch02' for 0.2° resolution (MSG), 'ch05' for 0.5° resolution (MFG)

# Environments
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES' # to access public S3 buckets without credentials

##############################
# TEMP DIRECTORY & S3 CACHE SETUP
# S3 download cache: encapsulates temp directory, S3 connection, and file registry
class S3Cache:
    """
    Manages local caching of S3 files in a temporary directory.

    Downloads files from S3 on first access and reuses local copies
    for subsequent requests. Supports eviction of files no longer needed.
    """

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix='lst_cache_')
        self.s3 = s3fs.S3FileSystem(
            anon=True,
            key=None,
            secret=None,
            token=None
        )
        self._cache = {}  # maps s3_path -> local_path
        print(f"Created temporary cache directory: {self.temp_dir}")

    def get(self, s3_path):
        """
        Return the local path to a cached file, downloading it from S3 if needed.

        Parameters
        ----------
        s3_path : str
            Full S3 URI (e.g. 's3://bucket/path/to/file.nc').

        Returns
        -------
        str
            Path to the locally cached file.
        """
        if s3_path in self._cache:
            return self._cache[s3_path]

        s3_key = s3_path.replace('s3://', '')
        local_filename = s3_key.replace('/', '_')
        local_path = os.path.join(self.temp_dir, local_filename)

        print(f"  Downloading {s3_path} -> {local_path}")
        try:
            ds = xr.open_dataset(s3_path, engine='h5netcdf', storage_options={'anon': True})
            ds.to_netcdf(local_path)
            ds.close()
        except Exception:
            if os.path.exists(local_path):
                os.remove(local_path)
            raise

        self._cache[s3_path] = local_path
        return local_path

    def evict_unneeded_months(self, needed_year_months):
        """
        Delete cached files whose year-month is not in the needed set.

        Parameters
        ----------
        needed_year_months : set of (int, int)
            Set of (year, month) tuples that should be retained.
        """
        to_evict = [
            s3_path for s3_path in list(self._cache)
            if not any(f'{y}{m:02d}' in s3_path for y, m in needed_year_months)
        ]
        for s3_path in to_evict:
            local_path = self._cache.pop(s3_path)
            if os.path.exists(local_path):
                os.remove(local_path)
                print(f"  Evicted cached file: {os.path.basename(local_path)}")

    def cleanup(self):
        """Remove the temporary directory and all cached files."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self._cache.clear()
        print(f"Temp directory removed: {self.temp_dir}")

cache = S3Cache()

##############################
# FUNCTIONS
# Function to check if a year is a leap year (for date range handling)
def is_leap_year(year):
    """Check if a year is a leap year."""
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)

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

# Function to get all dates for specific months in a year, with availability boundaries
def get_dates_for_months(year, target_months, first_date=None, last_date=None):
    """
    Return all dates within the given calendar months for a specific year.
    
    For month sequences that wrap across a year boundary (e.g. [12, 1] or [1, 12]),
    months earlier than the first month in the list are assumed to belong to year-1.
    Months outside the available data range are silently skipped.
    Respects first_date / last_date availability boundaries per individual date.
    
    Parameters
    ----------
    year : int
        Primary year (the year label; wrap months are pulled from year-1).
    target_months : list of int
        Calendar months to include, e.g. [8], [7, 8], [12, 1].
    first_date : datetime, optional
        Earliest available date; dates before this are skipped.
    last_date : datetime, optional
        Latest available date; dates after this are skipped.
    
    Returns
    -------
    list of datetime
        All available dates across the requested months.
    """
    dates = []

    for month in target_months:
        if month > target_months[-1]:
            year_for_month = year - 1
        else:
            year_for_month = year

        # Get last day of month using datetime only (day 0 of next month)
        last_day = (datetime(year_for_month, month % 12 + 1, 1) - timedelta(days=1)).day

        # Check if the entire month is outside the available range — skip with a warning
        month_start = datetime(year_for_month, month, 1)
        month_end = datetime(year_for_month, month, last_day)

        if (last_date and month_start > last_date) or (first_date and month_end < first_date):
            print(f"    Skipping {year_for_month}-{month:02d} (outside available data range)")
            continue

        # Iterate over days, skipping those outside the availability window
        for day in range(1, last_day + 1):
            d = datetime(year_for_month, month, day)
            if first_date and d < first_date:
                continue
            if last_date and d > last_date:
                continue
            dates.append(d)

    return dates

# Function to load SDL and SOL data from a local cache for a specific date and satellite
def load_sdl_sol(target_date, cache, satellite='MSG', sat_res='02'):
    """
    Load SDL and SOL satellite data for a specific date.
    Monthly NetCDF files are downloaded from S3 to a local temp directory on first
    access and reused for subsequent dates in the same month — avoiding redundant
    S3 traffic.
    
    Parameters
    ----------
    target_date : datetime
        Target date for data extraction.
    cache : S3Cache
        Cache instance managing local downloads and eviction.
    satellite : str, default='MSG'
        Satellite identifier. Accepts 'MSG'/'msg' (0.2° resolution) or 
        'MFG'/'mfg' (0.5° resolution).
    sat_res : str, default='02'
        Spatial resolution code for file naming. '02' for MSG, '05' for MFG.
    
    Returns
    -------
    tuple of xarray.Dataset
        (sdl_filtered, sol_filtered) - Filtered SDL and SOL datasets for 
        the specified date (00:00:00 to 23:59:59).
    """
    # Extract year and month from target date
    target_year = target_date.strftime('%Y')
    target_month = target_date.strftime('%m')

    # Construct S3 paths for SDL and SOL monthly files
    s3_path_sdl = f's3://{s3_bucket_satromo}{s3_path_lst}{satellite.upper()}_SDL/{satellite.lower()}.SDL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'
    s3_path_sol = f's3://{s3_bucket_satromo}{s3_path_lst}{satellite.upper()}_SOL/{satellite.lower()}.SOL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'

    # Download monthly files to temp dir (skipped if already cached)
    local_sdl = cache.get(s3_path_sdl)
    local_sol = cache.get(s3_path_sol)

    # Load datasets from local files
    ds_sdl = xr.open_dataset(local_sdl, engine='h5netcdf')
    ds_sol = xr.open_dataset(local_sol, engine='h5netcdf')

    # Define time range for the full day  (00:00:00 to 23:59:59)
    start_time = target_date
    end_time = target_date + timedelta(days=1) - timedelta(seconds=1)

    # Filter data for the specific date
    sdl_filtered = ds_sdl.sel(time=slice(start_time, end_time))
    sol_filtered = ds_sol.sel(time=slice(start_time, end_time))
    
    return sdl_filtered, sol_filtered

# Alternative function to load SDL and SOL data from local files
def load_sdl_sol_locally(target_date, local_dir, satellite='MSG', sat_res='02'):
    """
    Load SDL and SOL satellite data for a specific date from local files.

    Parameters
    ----------
    target_date : datetime
        Target date for data extraction.
    local_data_dir : str
        Path to local directory containing satellite data, structured as:
        local_data_dir/MSG_SDL/msg.SDL.H_ch02.lonlat_YYYYMM01000000.nc
        local_data_dir/MSG_SOL/msg.SOL.H_ch02.lonlat_YYYYMM01000000.nc
    satellite : str, default='MSG'
        Satellite identifier. Accepts 'MSG'/'msg' or 'MFG'/'mfg'.

    Returns
    -------
    tuple of xarray.Dataset
        (sdl_filtered, sol_filtered) - Filtered SDL and SOL datasets for
        the specified date (00:00:00 to 23:59:59).
    """
    target_year = target_date.strftime('%Y')
    target_month = target_date.strftime('%m')

    sdl_filename = f'{satellite.lower()}.SDL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'
    sol_filename = f'{satellite.lower()}.SOL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'

    sdl_path = os.path.join(local_dir, f'{satellite.upper()}_SDL', sdl_filename)
    sol_path = os.path.join(local_dir, f'{satellite.upper()}_SOL', sol_filename)

    ds_sdl = xr.open_dataset(sdl_path, engine='h5netcdf')
    ds_sol = xr.open_dataset(sol_path, engine='h5netcdf')

    start_time = target_date
    end_time = target_date + timedelta(days=1) - timedelta(seconds=1)

    # Filter data for the specific date
    sdl_filtered = ds_sdl.sel(time=slice(start_time, end_time))
    sol_filtered = ds_sol.sel(time=slice(start_time, end_time))
    
    return sdl_filtered, sol_filtered

# Function to calculate LST from SDL and SOL with flexible aggregation options
def calc_LST_for_date(ds_sol, ds_sdl, date, aggregation='hour'):
    """
    Calculate LST for a specific date with flexible aggregation options.
    
    Args:
        ds_sol: xarray Dataset with SOL data (already loaded)
        ds_sdl: xarray Dataset with SDL data (already loaded)
        date: date string in format 'YYYY-MM-DD'
        aggregation: 'max', 'mean', or 'hour' (default: 'hour')
    
    Returns:
        xarray Dataset with calculated LST
    """
    # Merge datasets
    ds = xr.merge([ds_sol, ds_sdl], compat='override')

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
        # Filter for 11AM UTC
        hour = 11 
        target_hour = date.replace(hour=hour, minute=0, second=0)
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
            'time': [date],
            'lat': ds.lat,
            'lon': ds.lon
        }
    )
    
    return ds_output

##############################
# PROCESSING
# Data availability boundaries
if satellite in ('MFG', 'mfg'):
    first_available_date = datetime(1991, 1, 1)
    last_available_date = datetime(2003, 12, 31)  
    years = range(1991, 2004)
elif satellite in ('MSG', 'msg'):
    first_available_date = datetime(2004, 1, 1)
    last_available_date = datetime(2023, 12, 31)
    years = range(2007, 2010)  # Use 1991-2020 for reference period, even if MSG data is available beyond 2020

try:
    if mode == 'doy_window':
        doy_range = range(1, 367) # 1 to 366 to cover all DOYs (including leap day)
    else:
        doy_range = [None] # Not used in monthly mode
    
    # Loop through all DOYs
    for doy in doy_range:
        if mode == 'doy_window':
            print(f"\n{'#'*80}")
            print(f"PROCESSING DOY {doy}")
            print(f"{'#'*80}\n")

            # Convert year and DOY to date and date string
            doy_int = int(doy)
            doy_str = str(doy_int).zfill(3)
            period_label = f'DOY{doy_str}'

        elif mode == 'monthly':
            print(f"\n{'#'*80}")
            print(f"PROCESSING MONTHS {target_months}")
            print(f"{'#'*80}\n")
            period_label = f'{"M" * len(target_months)}{target_months[-1]:02d}'
                

        # Determine which (year, month) pairs are needed across all years,
        # then evict any cached files that fall outside this set
        if mode == 'doy_window':
            needed_year_months = set()
            for y in years:
                center = datetime.strptime(f'{y}-{doy_int}', '%Y-%j')
                for offset in range(-n_days, n_days + 1):
                    d = center + timedelta(days=offset)
                    needed_year_months.add((d.year, d.month))
        else:
            needed_year_months = {(y, m) for y in years for m in target_months}

        cache.evict_unneeded_months(needed_year_months)

        # Initialize lists to collect daily datasets
        lst_list = []

        # Track statistics
        total_dates_requested = 0
        total_dates_processed = 0
        dates_skipped_boundary = 0
        dates_skipped_error = 0

        # Process each year
        for year in years:
            print(f"\n{'='*60}")
            print(f"Processing year {year}")
            print(f"{'='*60}")
            
            # Get date range for this year (with boundary protection)
            if mode == 'doy_window':
                date_range = get_date_range_for_doy(
                    year, doy_int, n_days, 
                    first_date=first_available_date,
                    last_date=last_available_date
                )
            else:
                date_range = get_dates_for_months(
                    year, target_months, 
                    first_date=first_available_date,
                    last_date=last_available_date
                )
            
            total_dates_requested += (2 * n_days + 1)
            dates_skipped_boundary += (2 * n_days + 1) - len(date_range)

            # Process each date in the range
            for date in date_range:
                try:
                    print(f"Processing {date.strftime('%Y-%m-%d')}...")
                    
                    # Load data
                    if data_source == 's3':
                        ds_sdl, ds_sol = load_sdl_sol(date, cache, satellite, channel)
                    else:
                        ds_sdl, ds_sol = load_sdl_sol_locally(date, local_data_dir, satellite, channel)

                    # Calculate LST for 11AM UTC
                    ds_lst = calc_LST_for_date(ds_sol, ds_sdl, date, 'hour')
                    
                    # Append to lists
                    lst_list.append(ds_lst)

                except Exception as e:
                    print(f"Error processing {date.strftime('%Y-%m-%d')}: {e}")
                    dates_skipped_error += 1
                    continue

        # Concatenate along time dimension
        print(f"\n{'='*60}")
        print("Concatenating all datasets...")
        ds_lst_stacked = xr.concat(lst_list, dim='time', data_vars='all')

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

        if mode == 'doy_window':
            period_metadata = {
                'doy': doy_int,
                'window_days': 2 * n_days + 1,
            }
        else:
            period_metadata = {
                'months': ', '.join(str(m) for m in target_months),
            }

        # Add metadata to all three datasets
        metadata = {
            'years': f'{years[0]}-{years[-1]}',
            'n_samples': len(ds_lst_stacked.time),
            'satellite': satellite,
            'first_available_date': first_available_date.strftime('%Y-%m-%d'),
            'last_available_date': last_available_date.strftime('%Y-%m-%d'),
            'description': 'LST statistics calculated from satellite data',
            'units': 'degrees Celsius',
            **period_metadata # 
        }

        stats.attrs.update(metadata)
        stats.attrs['aggregation_type'] = 'hour_11_UTC'

        ##############################
        # SAVE STATISTICS TO FILES
         # Create filenames
        if mode == 'doy_window':
            base_filename = f'LST_statistics_{period_label}_{satellite}_{channel}_{years[0]}-{years[-1]}_{n_days*2+1}days'
        else:
            base_filename = f'LST_statistics_{period_label}_{satellite}_{channel}_{years[0]}-{years[-1]}'
        output_filename_11am = f'test_dev/LST_REFERENCE/{base_filename}_11am.nc'

        # Save datasets to NetCDF files
        stats.to_netcdf(output_filename_11am)
        print(f"LST_11am statistics saved to: {output_filename_11am}")

finally:
    # Always clean up the temp directory, even if an error occurred mid-run
    cache.cleanup()