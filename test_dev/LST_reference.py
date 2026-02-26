import os
import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

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
# Paths
s3_bucket_satromo = 's3-topo-satromo-prod/'
s3_path_lst = 'data/LST_TEST/' # needs file name addition

# Constants
# year = '2018'
# doy = '364'
n_days = 3  # Number of days to expand on each side of the target DOY
satellite = 'MSG' # 'MSG' or 'MFG'
channel = 'ch02' # 'ch02' for 0.2° resolution (MSG), 'ch05' for 0.5° resolution (MFG)

# Environments
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES' # to access public S3 buckets without credentials

##############################
# FUNCTIONS
# Function to load SDL and SOL data from S3 for a specific date and satellite
def load_sdl_sol(target_date, satellite='MSG'):
    """
    Load SDL and SOL satellite data from S3 for a specific date.
    
    Parameters
    ----------
    target_date : datetime
        Target date for data extraction.
    satellite : str, default='MSG'
        Satellite identifier. Accepts 'MSG'/'msg' (0.2° resolution) or 
        'MFG'/'mfg' (0.5° resolution).
    
    Returns
    -------
    tuple of xarray.Dataset
        (sdl_filtered, sol_filtered) - Filtered SDL and SOL datasets for 
        the specified date (00:00:00 to 23:59:59).
    
    Notes
    -----
    Requires s3_bucket_satromo and s3_path_lst to be defined in scope.
    Data is accessed from public S3 buckets (anon=True).
    """
    # Extract year and month from target date
    target_year = target_date.strftime('%Y')
    target_month = target_date.strftime('%m')

    # Define satellite-specific path variables
    if satellite in ('MSG', 'msg'):
        sat_res = '02'
    elif satellite in ('MFG', 'mfg'):
        sat_res = '05'
    else:
        raise ValueError("satellite must be 'MSG' or 'MFG'")

    # Construct S3 paths for SDL and SOL monthly files
    s3_path_sdl = f's3://{s3_bucket_satromo}{s3_path_lst}{satellite.upper()}_SDL/{satellite.lower()}.SDL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'
    s3_path_sol = f's3://{s3_bucket_satromo}{s3_path_lst}{satellite.upper()}_SOL/{satellite.lower()}.SOL.H_ch{sat_res}.lonlat_{target_year}{target_month}01000000.nc'

    # Load datasets from S3
    ds_sdl = xr.open_dataset(s3_path_sdl, engine='h5netcdf', storage_options={'anon': True})
    ds_sol = xr.open_dataset(s3_path_sol, engine='h5netcdf', storage_options={'anon': True})

    # Define time range for the full day  (00:00:00 to 23:59:59)
    start_time = target_date
    end_time = target_date + timedelta(days=1) - timedelta(seconds=1)

    # Filter data for the specific date
    sdl_filtered = ds_sdl.sel(time=slice(start_time, end_time))
    sol_filtered = ds_sol.sel(time=slice(start_time, end_time))
    
    return sdl_filtered, sol_filtered

# Function to calculate LST from SDL and SOL with flexible aggregation options
def calc_LST_for_date(ds_sol, ds_sdl, date, aggregation='max'):
    """
    Calculate LST for a specific date with flexible aggregation options.
    
    Args:
        ds_sol: xarray Dataset with SOL data (already loaded)
        ds_sdl: xarray Dataset with SDL data (already loaded)
        date: date string in format 'YYYY-MM-DD'
        aggregation: 'max', 'mean', or 'hour' (default: 'max')
    
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
    date_range = []
    for offset in range(-n_days, n_days + 1):
        date = center_date + timedelta(days=offset)
        
        # Check if date is within available data range
        if first_date and date < first_date:
            print(f"    Skipping {date.strftime('%Y-%m-%d')} (before first available date)")
            continue
        if last_date and date > last_date:
            print(f"    Skipping {date.strftime('%Y-%m-%d')} (after last available date)")
            continue
            
        date_range.append(date)
    
    return date_range

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
    years = range(2004, 2021)  # Use 1991-2020 for reference period, even if MSG data is available beyond 2020

# Loop through all DOYs
for doy in range (137, 367):
    print(f"\n{'#'*80}")
    print(f"{'#'*80}")
    print(f"PROCESSING DOY {doy}")
    print(f"{'#'*80}")
    print(f"{'#'*80}\n")

    # Convert year and DOY to date and date string
    doy_int = int(doy)
    doy_str = str(doy_int).zfill(3)
    # target_date = datetime.strptime(year + '-' + doy, '%Y-%j')
    # target_date_str = target_date.strftime('%Y-%m-%d')

    # Initialize lists to collect daily datasets
    lst_11am_list = []

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
                
                # Load data
                ds_sdl, ds_sol = load_sdl_sol(date, satellite)
                
                # Calculate LST for 11AM UTC
                ds_lst_11am = calc_LST_for_date(ds_sol, ds_sdl, date, 'hour')
                
                # Append to lists
                lst_11am_list.append(ds_lst_11am)

            except Exception as e:
                print(f"Error processing {date.strftime('%Y-%m-%d')}: {e}")
                dates_skipped_error += 1
                continue

    # Concatenate along time dimension
    print(f"\n{'='*60}")
    print("Concatenating all datasets...")
    ds_lst_11am_stacked = xr.concat(lst_11am_list, dim='time', data_vars='all')

    # Calculate statistics for each variable across the time dimension
    print(f"\n{'='*60}")
    print("Calculating statistics across all years and time window...")

    # Convert to Celsius for all calculations
    lst_11am_celsius = ds_lst_11am_stacked['LST_hour11'] - 273.15

    # Calculate statistics for LST_11am
    lst_11am_p05 = lst_11am_celsius.quantile(0.05, dim='time')
    lst_11am_p95 = lst_11am_celsius.quantile(0.95, dim='time')

    if 'quantile' in lst_11am_p05.coords:
        lst_11am_p05 = lst_11am_p05.drop_vars('quantile')
    if 'quantile' in lst_11am_p95.coords:
        lst_11am_p95 = lst_11am_p95.drop_vars('quantile')

    stats_11am = xr.Dataset({
        'LST_11am_min': lst_11am_celsius.min(dim='time'),
        'LST_11am_max': lst_11am_celsius.max(dim='time'),
        'LST_11am_mean': lst_11am_celsius.mean(dim='time'),
        'LST_11am_median': lst_11am_celsius.median(dim='time'),
        'LST_11am_p05': lst_11am_p05,
        'LST_11am_p95': lst_11am_p95
    })

    # Add metadata to all three datasets
    metadata = {
        'years': f'{years[0]}-{years[-1]}',
        'doy': doy_int,
        'window_days': 2 * n_days + 1,
        'n_samples': len(ds_lst_11am_stacked.time),
        'satellite': satellite,
        'first_available_date': first_available_date.strftime('%Y-%m-%d'),
        'last_available_date': last_available_date.strftime('%Y-%m-%d'),
        'description': 'LST statistics calculated from satellite data',
        'units': 'degrees Celsius'
    }

    stats_11am.attrs.update(metadata)
    stats_11am.attrs['aggregation_type'] = 'hour_11_UTC'

    ##############################
    # SAVE STATISTICS TO FILES

    # Create filenames
    base_filename = f'LST_statistics_DOY{doy_str}_{satellite}_{channel}_{years[0]}-{years[-1]}_{n_days*2+1}days'
    output_filename_11am = f'test_dev/LST_11AM_REFERENCE/{base_filename}_11am.nc'

    # Save datasets to NetCDF files
    stats_11am.to_netcdf(output_filename_11am)
    print(f"LST_11am statistics saved to: {output_filename_11am}")

