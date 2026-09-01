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
            return False, False
        except pd.errors.EmptyDataError:
            print("ERROR: Empty asset list file is empty.")
            return False, False

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

        # NOTE: we do NOT write the CSV here anymore. Rows for df_to_process are only
        # dropped from the CSV once we know a date was actually processed successfully
        # (see below). This ensures dates that fail (e.g. Copernicus STAC outage) stay
        # in the CSV and are retried on the next run, instead of being silently lost.

        reprocess_list = df_to_process['date'].tolist()
        print(f"Found {len(df_cloudy)} cloudy entries (kept in CSV).")
        print(f"Found {len(reprocess_list)} dates to actually reprocess for {collection_basename}")

        if not reprocess_list:
            if os.path.exists(backup_file):
                os.remove(backup_file)
            return False, False

        # Batch processing of dates
        success_count = 0
        failure_count = 0
        skipped_count = 0
        consecutive_failures = 0
        max_consecutive_failures = 2
        processed_dates = set()  # dates confirmed successful -> safe to drop from CSV
        aborted_early = False

        for i, check_date_str in enumerate(reprocess_list):
            print(f"\n{'='*60}")
            print(f"Processing date: {check_date_str} ({i + 1}/{len(reprocess_list)})")
            print(f"{'='*60}")

            date_failed = False

            try:
                python_path = sys.executable
                processor_script = os.path.join(script_dir, 'satromo_processor.py')

                if not os.path.exists(processor_script):
                    print(f"ERROR: Processor script not found: {processor_script}")
                    failure_count += 1
                    date_failed = True
                else:
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

                    # The processor exits 0 both when it produced a product and when it
                    # decided there was nothing to do. Watch its output for the "nothing
                    # to do" marker so a skipped date is never recorded as processed and
                    # dropped from the CSV.
                    skipped_as_empty = False

                    while True:
                        line = process.stdout.readline()
                        if not line and process.poll() is not None:
                            break
                        if line:
                            print(line, end='', flush=True)
                            if 'Date found in empty_asset_list' in line:
                                skipped_as_empty = True

                    return_code = process.poll()

                    if return_code == 0 and skipped_as_empty:
                        print(f"! {check_date_str} was skipped by the processor "
                              f"(still listed as having no source data). Nothing was "
                              f"produced, so the entry is kept in the CSV for the next run.")
                        skipped_count += 1
                    elif return_code == 0:
                        print(f"✓ Successfully processed {check_date_str}")
                        success_count += 1
                        processed_dates.add(check_date_str)
                    else:
                        print(f"✗ Process failed with exit code {return_code} for {check_date_str}")
                        failure_count += 1
                        date_failed = True

            except Exception as e:
                print(f"✗ Unexpected error processing {check_date_str}: {e}")
                failure_count += 1
                date_failed = True

            if date_failed:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= max_consecutive_failures:
                remaining = len(reprocess_list) - (i + 1)
                print(f"\n✗ CIRCUIT BREAKER: {consecutive_failures} consecutive failures "
                      f"(likely an upstream outage). Aborting batch, {remaining} remaining "
                      f"date(s) will be retried next run.")
                aborted_early = True
                break

        # Rebuild the CSV: keep everything except dates confirmed successfully processed.
        # Failed / not-yet-attempted dates from df_to_process stay in the file so they
        # are retried automatically on the next run.
        # sort_index() restores the original row order (df_outside_scope/df_cloudy/df_unresolved
        # are all index-subsets of the same original df) so an unrelated concurrent edit to the
        # CSV doesn't turn into a full-file reorder diff, which is what causes most git merge
        # conflicts on this file.
        df_unresolved = df_to_process[~df_to_process['date'].isin(processed_dates)]
        df_final = pd.concat([df_outside_scope, df_cloudy, df_unresolved]).sort_index()
        df_final.to_csv(config.EMPTY_ASSET_LIST, index=False)

        # Summary
        print(f"\n{'='*60}")
        print(f"PROCESSING SUMMARY")
        print(f"{'='*60}")
        print(f"Total dates queued: {len(reprocess_list)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {failure_count}")
        print(f"Skipped (nothing produced, kept in CSV): {skipped_count}")
        if aborted_early:
            print(f"Aborted early due to consecutive upstream failures")
        print(f"{'='*60}\n")

        if os.path.exists(backup_file):
            if failure_count == 0:
                os.remove(backup_file)
            else:
                print(f"Kept backup file: {backup_file} (some failures occurred)")

        return success_count > 0, aborted_early

    except Exception as e:
        print(f"✗ FATAL ERROR in process_empty_asset_list: {e}")
        backup_file = config.EMPTY_ASSET_LIST + '.bak'
        if os.path.exists(backup_file):
            shutil.copy2(backup_file, config.EMPTY_ASSET_LIST)
        return False, False


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

    result, aborted_early = process_empty_asset_list(collection, days_back, config_file)

    print()
    print("="*60)
    if aborted_early:
        print("✗ RERUN ABORTED EARLY (consecutive upstream failures)")
    elif result:
        print("✓ RERUN COMPLETED SUCCESSFULLY")
    else:
        print("✓ RERUN COMPLETED (NO FILES PROCESSED)")
    print("="*60)

    # Non-zero exit on circuit-breaker trip so CI surfaces the outage instead of
    # silently reporting success; otherwise exit 0 even with per-date failures,
    # since those dates simply remain queued in the CSV for the next run.
    sys.exit(1 if aborted_early else 0)


if __name__ == "__main__":
    main()