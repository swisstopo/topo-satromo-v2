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

    Args:
        collection_basename (str): Base name of the collection
        days_back (int): Number of days to look back
        config_file (str): Path to the configuration file

    Returns:
        bool: True if assets were reprocessed, False otherwise
    """

    # Setup environment - Use current environment as base
    env = os.environ.copy()

    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Platform-agnostic virtual environment detection
    # Linux uses 'lib/site-packages', Windows uses 'Lib/site-packages'
    venv_site_packages = os.path.join(sys.prefix, 'lib', 'site-packages')  # Linux
    venv_site_packages_win = os.path.join(sys.prefix, 'Lib', 'site-packages')  # Windows

    # Determine which path exists on current platform
    site_packages = None
    if os.path.exists(venv_site_packages):
        site_packages = venv_site_packages
    elif os.path.exists(venv_site_packages_win):
        site_packages = venv_site_packages_win

    # Build PYTHONPATH with both script directory and virtual environment
    # os.pathsep is ':' on Linux/Mac, ';' on Windows
    paths_to_add = [script_dir]
    if site_packages:
        paths_to_add.append(site_packages)

    # Ensure PYTHONPATH includes necessary paths
    if 'PYTHONPATH' in env:
        env['PYTHONPATH'] = os.pathsep.join(paths_to_add) + os.pathsep + env['PYTHONPATH']
    else:
        env['PYTHONPATH'] = os.pathsep.join(paths_to_add)

    try:
        # Read the empty asset list with error handling
        try:
            # Make a backup copy
            backup_file = config.EMPTY_ASSET_LIST + '.bak'
            shutil.copy2(config.EMPTY_ASSET_LIST, backup_file)
            print(f"Created backup: {backup_file}")

            # Read the CSV
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

        # Vectorized filtering for better performance
        mask = (
            (df['collection'] == collection_basename) &
            (df['date'] >= start_date.strftime('%Y-%m-%d')) &
            (df['date'] <= end_date.strftime('%Y-%m-%d'))
        )

        # Select and remove rows in a single operation
        df_selection = df[mask]
        df_remaining = df[~mask]

        # Get reprocess list
        reprocess_list = df_selection['date'].tolist()

        print(f"Found {len(reprocess_list)} dates to reprocess for {collection_basename}")

        if not reprocess_list:
            print(f"No dates to reprocess for {collection_basename}")
            # Remove backup
            if os.path.exists(backup_file):
                os.remove(backup_file)
            return False

        # Save updated DataFrame (with selected rows removed) back to CSV
        df_remaining.to_csv(config.EMPTY_ASSET_LIST, index=False)
        print(f"Updated {config.EMPTY_ASSET_LIST} - removed {len(reprocess_list)} entries")

        # Batch processing of dates
        success_count = 0
        failure_count = 0

        for check_date_str in reprocess_list:
            print(f"\n{'='*60}")
            print(f"Processing date: {check_date_str} ({reprocess_list.index(check_date_str) + 1}/{len(reprocess_list)})")
            print(f"{'='*60}")

            try:
                # Build command with absolute paths
                python_path = sys.executable
                processor_script = os.path.join(script_dir, 'satromo_processor.py')

                # Ensure processor script exists
                if not os.path.exists(processor_script):
                    print(f"ERROR: Processor script not found: {processor_script}")
                    failure_count += 1
                    continue

                command = [
                    python_path,
                    '-u',  # CRITICAL: Unbuffered output for real-time display
                    processor_script,
                    config_file,
                    check_date_str
                ]

                print(f"Command: {' '.join(command)}")
                print(f"Working directory: {script_dir}")
                print(f"Python executable: {python_path}")
                print("")

                # Run subprocess with real-time output
                # FIXED: Use unbuffered output and properly handle streams
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # Merge stderr to stdout for real-time output
                    text=True,
                    bufsize=0,  # Unbuffered
                    env=env,
                    cwd=script_dir,  # Set working directory explicitly
                    universal_newlines=True
                )

                # Read output in real-time line by line
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        # Process finished and no more output
                        break
                    if line:
                        print(line, end='', flush=True)

                # Get return code
                return_code = process.poll()

                if return_code == 0:
                    print(f"✓ Successfully processed {check_date_str}")
                    success_count += 1
                else:
                    print(f"✗ Process failed with exit code {return_code} for {check_date_str}")
                    failure_count += 1

            except subprocess.SubprocessError as e:
                print(f"✗ Subprocess error processing date {check_date_str}: {e}")
                failure_count += 1
                # Restore backup on error
                if os.path.exists(backup_file):
                    shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)
                    print(f"Restored backup to {config.EMPTY_ASSET_LIST}")

            except Exception as e:
                print(f"✗ Unexpected error processing {check_date_str}: {e}")
                import traceback
                traceback.print_exc()
                failure_count += 1
                # Restore backup on error
                if os.path.exists(backup_file):
                    shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)
                    print(f"Restored backup to {config.EMPTY_ASSET_LIST}")

        # Summary
        print(f"\n{'='*60}")
        print(f"PROCESSING SUMMARY")
        print(f"{'='*60}")
        print(f"Total dates processed: {len(reprocess_list)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {failure_count}")
        print(f"{'='*60}\n")

        # Remove backup if all successful
        if os.path.exists(backup_file):
            if failure_count == 0:
                os.remove(backup_file)
                print(f"Removed backup file (all processing successful)")
            else:
                print(f"Kept backup file: {backup_file} (some failures occurred)")

        return success_count > 0

    except Exception as e:
        print(f"✗ FATAL ERROR in process_empty_asset_list: {e}")
        import traceback
        traceback.print_exc()

        # Restore backup on fatal error
        backup_file = config.EMPTY_ASSET_LIST + '.bak'
        if os.path.exists(backup_file):
            shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)
            print(f"Restored backup to {config.EMPTY_ASSET_LIST}")
        return False


def main():
    """Main entry point for rerun script"""

    print("="*60)
    print("RERUN.PY - Empty Asset Reprocessing")
    print("="*60)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python: {sys.executable}")
    print(f"Working directory: {os.getcwd()}")

    # Determine configuration file
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
        print(f"Using configuration from command line: {config_file}")
    else:
        config_file = 'dev_config.py'
        print(f"Using default configuration: {config_file}")

    print("="*60)
    print()

    # Configuration
    days_back = 30

    # Uncomment the collection you want to reprocess
    # collection = config.PRODUCT_S2_LEVEL_CSPLUS['step0_collection'].rsplit('/', 1)[-1]
    collection = config.PRODUCT_S2_LEVEL_2A['step0_collection'].rsplit('/', 1)[-1]

    print(f"Collection: {collection}")
    print(f"Days back: {days_back}")
    print()

    # Run the reprocessing
    result = process_empty_asset_list(collection, days_back, config_file)

    print()
    print("="*60)
    if result:
        print("✓ RERUN COMPLETED SUCCESSFULLY")
    else:
        print("✗ RERUN COMPLETED WITH NO CHANGES")
    print(f"Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    # Exit with appropriate code
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
