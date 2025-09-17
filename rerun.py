import os
import sys
import pandas as pd
import subprocess
from datetime import datetime
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config
from datetime import datetime, timedelta
import shutil

#General settings

def process_empty_asset_list(collection_basename, days_back, config_file):
    """
    Process and reprocess empty assets for a specific collection.

    Args:
        collection_basename (str): Base name of the collection
        days_back (int): Number of days to look back
        config_file (str): Path to the configuration file

    Returns:
        bool: True if assets were reprocessed, False otherwise
    """
    env = os.environ.copy()

    # Add virtual environment site-packages to PYTHONPATH
    venv_site_packages = os.path.join(sys.prefix, 'Lib', 'site-packages')
    if 'PYTHONPATH' in env:
        env['PYTHONPATH'] = f"{venv_site_packages};{env['PYTHONPATH']}"
    else:
        env['PYTHONPATH'] = venv_site_packages


    try:
        # Read the empty asset list with error handling
        try:
            # make a copy of the file defined config.EMPTY_ASSET_LIST
            shutil.copy2(config.EMPTY_ASSET_LIST,config.EMPTY_ASSET_LIST + '.bak')
            # read
            df = pd.read_csv(config.EMPTY_ASSET_LIST)
        except FileNotFoundError:
            print(f"Empty asset list file not found: {config.EMPTY_ASSET_LIST}")
            return False
        except pd.errors.EmptyDataError:
            print("Empty asset list file is empty.")
            return False

        # Calculate date range more efficiently
        end_date = datetime.today()
        start_date = end_date - timedelta(days=days_back)

        # Vectorized filtering for better performance
        mask = (
            (df['collection'] == collection_basename) &
            (df['date'] >= start_date.strftime('%Y-%m-%d')) &
            (df['date'] <= end_date.strftime('%Y-%m-%d'))
        )

        # Select and remove rows in a single operation
        df_selection = df[mask]
        df = df[~mask]

        # Get reprocess list
        reprocess_list = df_selection['date'].tolist()

        # Save updated DataFrame back to CSV
        df.to_csv(config.EMPTY_ASSET_LIST, index=False)

        # Batch processing of dates
        if reprocess_list:
            print(f"Reprocessing {len(reprocess_list)} dates for {collection_basename}")

            # Use a list comprehension for subprocess calls
            for check_date_str in reprocess_list:
                try:
                    command = [
                        sys.executable,  # Use the current Python interpreter
                        'satromo_processor.py',
                        config_file,
                        check_date_str
                    ]
                    # Start the subprocess
                    with subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env
                    ) as process:
                        # Read and print stdout line by line
                        for stdout_line in iter(process.stdout.readline, ''):
                            print(stdout_line, end='', flush=True)
                        # Wait for the process to complete
                        process.wait()

                except subprocess.CalledProcessError as e:
                    print(f"Error processing date {check_date_str}: {e}")
                    print(f"Error output: {e.stderr}")
                    shutil.copy2(config.EMPTY_ASSET_LIST + '.bak',config.EMPTY_ASSET_LIST)

            #remove backup
            os.remove(config.EMPTY_ASSET_LIST + '.bak')

            return True

        print(f"No dates to reprocess for {collection_basename}")
        #remove backup
        os.remove(config.EMPTY_ASSET_LIST + '.bak')
        return False

    except Exception as e:
        print(f"Unexpected error in process_empty_asset_list: {e}")
        shutil.copy2(config.EMPTY_ASSET_LIST + '.bak',config.EMPTY_ASSET_LIST)
        return False

def main():
    # Determine configuration path if DEV or provided configuration
    # Specific arguments



    if len(config.sys.argv) > 1:
        config_file = config.sys.argv[1]  # First argument after the script name
        print("Using Configuration:", config_file)
    else:
        config_file = 'dev_config.py'


    # Rerun CloudScore+
    days_back = 30
    result = process_empty_asset_list(config.PRODUCT_S2_LEVEL_CSPLUS['step0_collection'].rsplit('/', 1)[-1], days_back, config_file)


if __name__ == "__main__":
    main()
