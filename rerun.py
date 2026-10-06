import os
import sys
import pandas as pd
import subprocess
from datetime import datetime, timedelta
import shutil

# Add parent directory to path for configuration import
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config


def count_csv_rows_for_date(collection_basename, date_str):
    """How many rows the empty-asset CSV currently has for this collection+date.

    Used to detect whether the processor subprocess itself appended a fresh row
    (e.g. "cloudy") for a date while it was running -- something the parent
    process cannot see any other way, since the subprocess writes straight to
    the file on disk.
    """
    try:
        df = pd.read_csv(config.EMPTY_ASSET_LIST)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return 0
    return int(((df['collection'] == collection_basename) & (df['date'] == date_str)).sum())


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

        # Each date's remark as it stood before this run, so that afterwards we can
        # tell the ORIGINAL placeholder row (e.g. "Tiles ready awaiting GPU system
        # run") apart from any brand-new row a subprocess appends for the same date
        # while it runs (e.g. "cloudy") -- see the final CSV rebuild below.
        original_remark = dict(zip(df_to_process['date'], df_to_process['remark']))

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
        newly_marked_dates = set()  # dates where the subprocess itself appended a fresh empty-asset row this run
        aborted_early = False

        for i, check_date_str in enumerate(reprocess_list):
            print(f"\n{'='*60}")
            print(f"Processing date: {check_date_str} ({i + 1}/{len(reprocess_list)})")
            print(f"{'='*60}")

            date_failed = False

            # Row count for this date before running, so we can tell afterwards whether
            # the subprocess itself appended a fresh row for it (e.g. "cloudy") while it
            # ran -- the parent process has no other way to see that, since the
            # subprocess writes straight to the CSV file on disk.
            rows_before = count_csv_rows_for_date(collection_basename, check_date_str)

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
                    # to do" markers so a skipped date is never recorded as processed and
                    # dropped from the CSV. 'Date found in empty_asset_list' covers a date
                    # that was already known-empty before this run started; 'Cutting asset
                    # create for' is printed by write_asset_as_empty() itself, so it also
                    # catches a date that only turned out empty *during* this run (too
                    # cloudy, tile download/upload incomplete, etc).
                    skipped_as_empty = False

                    while True:
                        line = process.stdout.readline()
                        if not line and process.poll() is not None:
                            break
                        if line:
                            print(line, end='', flush=True)
                            if ('Date found in empty_asset_list' in line
                                    or 'Cutting asset create for' in line):
                                skipped_as_empty = True

                    return_code = process.poll()

                    # Beyond the stdout marker above, also check whether the subprocess
                    # appended a brand new CSV row for this date while it ran. This is the
                    # authoritative signal (the marker text could in principle change or be
                    # missed), and it is also what the final CSV rebuild below needs to
                    # replace the old placeholder row with the fresh one instead of losing it.
                    rows_after = count_csv_rows_for_date(collection_basename, check_date_str)
                    if rows_after > rows_before:
                        newly_marked_dates.add(check_date_str)

                    if return_code == 0 and (skipped_as_empty or check_date_str in newly_marked_dates):
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

        # Rebuild the CSV. Dates the loop above never touched (failed / not-yet-attempted,
        # or outside scope, or already-known-cloudy) must be left completely alone.
        # For dates this run DID resolve -- either it fully succeeded, or the subprocess
        # itself appended a fresh row for it (see newly_marked_dates above) -- the
        # ORIGINAL pre-run placeholder row (e.g. "Tiles ready awaiting GPU system run")
        # is now stale and must go.
        #
        # We re-read the CSV from disk here rather than reusing df/df_candidates/df_to_process
        # loaded at the top of this function. Subprocesses write straight to the file while
        # they run (write_asset_as_empty()), so those in-memory copies from before the loop
        # no longer reflect what's on disk; rebuilding the file from them would silently
        # discard whatever a subprocess just appended.
        # Dropping the stale row by its ORIGINAL remark (not just by collection+date) is
        # what keeps this safe: it removes only the old placeholder and leaves a freshly
        # appended row (different remark) in place.
        resolved_dates = processed_dates | newly_marked_dates
        df_now = pd.read_csv(config.EMPTY_ASSET_LIST)
        mask_stale = df_now.apply(
            lambda row: (row['collection'] == collection_basename
                         and row['date'] in resolved_dates
                         and row['remark'] == original_remark.get(row['date'])),
            axis=1
        )
        df_final = df_now[~mask_stale]
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