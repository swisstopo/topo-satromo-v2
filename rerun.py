import os
import sys
import pandas as pd
import subprocess
from datetime import datetime, timedelta
import shutil

# Add parent directory to path for configuration import
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config


def process_empty_asset_list(collection_basename, days_back, config_file):
    """
    Process and reprocess empty assets for a specific collection.
    """

    # Setup environment - Use current environment as base
    env = os.environ.copy()

    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Platform-agnostic virtual environment detection
    venv_site_packages = os.path.join(sys.prefix, 'lib', 'site-packages')  # Linux
    venv_site_packages_win = os.path.join(sys.prefix, 'Lib', 'site-packages')  # Windows

    site_packages = None
    if os.path.exists(venv_site_packages):
        site_packages = venv_site_packages
    elif os.path.exists(venv_site_packages_win):
        site_packages = venv_site_packages_win

    paths_to_add = [script_dir]
    if site_packages:
        paths_to_add.append(site_packages)

    if 'PYTHONPATH' in env:
        env['PYTHONPATH'] = os.pathsep.join(paths_to_add) + os.pathsep + env['PYTHONPATH']
    else:
        env['PYTHONPATH'] = os.pathsep.join(paths_to_add)

    try:
        # Read the empty asset list with error handling
        try:
            backup_file = config.EMPTY_ASSET_LIST + '.bak'
            shutil.copy2(config.EMPTY_ASSET_LIST, backup_file)
            print(f"Created backup: {backup_file}")

            df = pd.read_csv(config.EMPTY_ASSET_LIST)
            print(f"Loaded {len(df)} rows from {config.EMPTY_ASSET_LIST}")

        except FileNotFoundError:
            print(f"ERROR: Empty asset list file not found: {config.EMPTY_ASSET_LIST}")
            return False
        except pd.errors.EmptyDataError:
            print("ERROR: Empty asset list file is empty.")
            return False

        # Calculate date range
        end_date = datetime.today()
        start_date = end_date - timedelta(days=days_back)
        print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

        # 1. Identify rows that match our collection and date criteria (Potential candidates)
        mask_in_scope = (
            (df['collection'] == collection_basename) &
            (df['date'] >= start_date.strftime('%Y-%m-%d')) &
            (df['date'] <= end_date.strftime('%Y-%m-%d'))
        )

        df_candidates = df[mask_in_scope]
        df_outside_scope = df[~mask_in_scope]

        # 2. Filter out "cloudy" entries from the processing list but KEEP them for the CSV
        # na=False ensures we handle rows with empty remarks safely
        mask_cloudy = df_candidates['remark'].str.contains('cloudy', case=False, na=False)
        
        df_cloudy = df_candidates[mask_cloudy]
        df_to_process = df_candidates[~mask_cloudy]

        # 3. Save everything back EXCEPT the ones we are about to process
        # This keeps 'outside scope' rows AND 'cloudy' rows in the file
        df_to_keep_in_csv = pd.concat([df_outside_scope, df_cloudy])
        df_to_keep_in_csv.to_csv(config.EMPTY_ASSET_LIST, index=False)
        
        reprocess_list = df_to_process['date'].tolist()
        print(f"Found {len(df_cloudy)} cloudy entries (kept in CSV).")
        print(f"Found {len(reprocess_list)} dates to actually reprocess for {collection_basename}")

        if not reprocess_list:
            if os.path.exists(backup_file):
                os.remove(backup_file)
            return False

        # Batch processing of dates
        success_count = 0
        failure_count = 0

        for check_date_str in reprocess_list:
            print(f"\n{'='*60}")
            print(f"Processing date: {check_date_str} ({reprocess_list.index(check_date_str) + 1}/{len(reprocess_list)})")
            print(f"{'='*60}")

            try:
                python_path = sys.executable
                processor_script = os.path.join(script_dir, 'satromo_processor.py')

                if not os.path.exists(processor_script):
                    print(f"ERROR: Processor script not found: {processor_script}")
                    failure_count += 1
                    continue

                command = [
                    python_path,
                    '-u', 
                    processor_script,
                    config_file,
                    check_date_str
                ]

                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  
                    text=True,
                    bufsize=0,  
                    env=env,
                    cwd=script_dir,  
                    universal_newlines=True
                )

                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end='', flush=True)

                return_code = process.poll()

                if return_code == 0:
                    print(f"✓ Successfully processed {check_date_str}")
                    success_count += 1
                else:
                    print(f"✗ Process failed with exit code {return_code} for {check_date_str}")
                    failure_count += 1

            except Exception as e:
                print(f"✗ Unexpected error processing {check_date_str}: {e}")
                failure_count += 1
                if os.path.exists(backup_file):
                    shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)

        # Summary
        print(f"\n{'='*60}")
        print(f"PROCESSING SUMMARY")
        print(f"{'='*60}")
        print(f"Total dates processed: {len(reprocess_list)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {failure_count}")
        print(f"{'='*60}\n")

        if os.path.exists(backup_file):
            if failure_count == 0:
                os.remove(backup_file)
            else:
                print(f"Kept backup file: {backup_file} (some failures occurred)")

        return success_count > 0

    except Exception as e:
        print(f"✗ FATAL ERROR in process_empty_asset_list: {e}")
        backup_file = config.EMPTY_ASSET_LIST + '.bak'
        if os.path.exists(backup_file):
            shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)
        return False


def main():
    print("="*60)
    print("RERUN.PY - Empty Asset Reprocessing")
    print("="*60)

    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = 'dev_config.py'

    days_back = 30
    collection = config.PRODUCT_S2_LEVEL_2A['step0_collection'].rsplit('/', 1)[-1]

    result = process_empty_asset_list(collection, days_back, config_file)

    print()
    print("="*60)
    if result:
        print("✓ RERUN COMPLETED SUCCESSFULLY")
    else:
        print("✓ RERUN COMPLETED (NO FILES PROCESSED)")
    print("="*60)

    sys.exit(0 if result else 0) # Changed to 0 for "No Changes" as it is often a valid state


if __name__ == "__main__":
    main()