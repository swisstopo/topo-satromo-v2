import os
import sys
import pandas as pd
import subprocess
from datetime import datetime, timedelta
import shutil

# Add parent directory to path for configuration import
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config
from main_functions import main_utils


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


# Remarks in the rows of the follow-up products (e.g. VHI) that mean "an input was
# not there yet, try again later". Rows with any other remark are left alone.
RETRY_REMARKS = ['LST missing']


def stac_item_exists(collection_url, date_str):
    """True if the STAC collection (URL as written in the config) has at least one item for this date."""
    api_path = getattr(config, 'STAC_FSDI_API')
    stac_catalog_url, collection_id = main_utils.extract_collection_id_from_url(collection_url, api_path)
    check_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    items = main_utils.get_stac_items_for_date(stac_catalog_url, collection_id, check_date)
    return len(items) > 0


def build_subprocess_env(script_dir):
    """Environment for the satromo_processor.py subprocess: current environment plus PYTHONPATH."""
    env = os.environ.copy()

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

    return env


def get_follow_up_products():
    """Names of the other products in the config that satromo_processor.py runs (e.g. PRODUCT_VHI).

    satromo_processor.py runs every product that has a 'step0_collection' (same rule as
    get_step0_dict() in step0_functions.py). S2-SR itself is left out: it is the product
    this script reprocesses in the first place.
    """
    products = []
    for name in dir(config):
        if name == 'PRODUCT_S2_LEVEL_2A':
            continue
        value = getattr(config, name)
        if isinstance(value, dict) and 'step0_collection' in value:
            products.append(name)
    return products


def run_satromo_processor(config_file, date_str, env, script_dir):
    """Run satromo_processor.py for one date, print its output live and return its exit code."""
    processor_script = os.path.join(script_dir, 'satromo_processor.py')
    if not os.path.exists(processor_script):
        print(f"ERROR: Processor script not found: {processor_script}")
        return 1

    command = [
        sys.executable,
        '-u',
        processor_script,
        config_file,
        date_str
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

    return process.poll()


def process_empty_asset_list(collection_basename, days_back, config_file):
    """
    Process and reprocess empty assets for a specific collection.
    """

    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = build_subprocess_env(script_dir)

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
                return_code = run_satromo_processor(config_file, check_date_str, env, script_dir)

                # Check whether the subprocess appended a brand new CSV row for this date
                # while it ran (e.g. "cloudy"). The final CSV rebuild below needs this to
                # replace the old placeholder row with the fresh one instead of losing it.
                rows_after = count_csv_rows_for_date(collection_basename, check_date_str)
                if rows_after > rows_before:
                    newly_marked_dates.add(check_date_str)

                # The processor exits 0 both when it produced S2-SR and when it decided
                # there was nothing to do. So we ask the S2-SR STAC collection directly
                # whether the date now has an item. We do not search the log for text:
                # other products in the config (e.g. VHI looks 7 days back) also print
                # messages about OTHER dates, which would make a log check unreliable.
                if return_code != 0:
                    print(f"✗ Process failed with exit code {return_code} for {check_date_str}")
                    failure_count += 1
                    date_failed = True
                elif (check_date_str in newly_marked_dates
                        or not stac_item_exists(config.PRODUCT_S2_LEVEL_2A['step0_collection'], check_date_str)):
                    print(f"! {check_date_str} was skipped by the processor "
                          f"(no S2-SR item in STAC). Nothing was produced, so the "
                          f"entry is kept in the CSV for the next run.")
                    skipped_count += 1
                else:
                    print(f"✓ Successfully processed {check_date_str}")
                    success_count += 1
                    processed_dates.add(check_date_str)

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

        # Follow-up run for the other products in the config (e.g. VHI).
        # S2-SR is produced inside step0 of satromo_processor.py. In that same call the
        # products that need S2-SR do not run yet, because S2-SR was not in STAC when
        # step0 checked it. A second call for the same date runs them now. Which products
        # run is decided by the config, exactly like in a normal satromo_processor.py run.
        # This runs after the loop, so all S2-SR dates of this batch are already in STAC
        # (products like VHI use several days of S2-SR).
        follow_up_products = get_follow_up_products()
        follow_up_failed = []
        if follow_up_products and processed_dates:
            print(f"\n{'='*60}")
            print(f"FOLLOW-UP RUN for {follow_up_products} on {len(processed_dates)} date(s)")
            print(f"{'='*60}")
            for date_str in sorted(processed_dates):
                print(f"\n--- Follow-up run for {date_str} ---")
                try:
                    return_code = run_satromo_processor(config_file, date_str, env, script_dir)
                except Exception as e:
                    print(f"✗ Unexpected error in follow-up run for {date_str}: {e}")
                    return_code = None
                if return_code == 0:
                    print(f"✓ Follow-up run done for {date_str}")
                else:
                    print(f"✗ Follow-up run failed for {date_str} (exit code {return_code})")
                    follow_up_failed.append(date_str)

        # Summary
        print(f"\n{'='*60}")
        print(f"PROCESSING SUMMARY")
        print(f"{'='*60}")
        print(f"Total dates queued: {len(reprocess_list)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {failure_count}")
        print(f"Skipped (nothing produced, kept in CSV): {skipped_count}")
        if follow_up_products:
            print(f"Follow-up products: {follow_up_products}")
            print(f"Follow-up runs: {len(processed_dates)}, failed: {follow_up_failed if follow_up_failed else 'none'}")
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


def retry_follow_up_products(days_back, config_file):
    """
    Retry dates where a follow-up product (e.g. VHI) was skipped because one of its
    inputs was not there yet (remark in RETRY_REMARKS, e.g. 'LST missing').

    For every such row: if the product already has a STAC item for that date, the
    row is simply removed. Otherwise satromo_processor.py is run again for the date,
    and the row is removed only if the product has a STAC item afterwards.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = build_subprocess_env(script_dir)

    end_date = datetime.today()
    start_date = end_date - timedelta(days=days_back)

    for product in get_follow_up_products():
        product_config = getattr(config, product)
        collection_basename = product_config['product_name']

        # The STAC check below needs the collection the product publishes to
        if 'step1_collection' not in product_config:
            print(f"{product}: no 'step1_collection' in config, retry of skipped dates not possible.")
            continue
        product_collection = product_config['step1_collection']

        df = pd.read_csv(config.EMPTY_ASSET_LIST)
        is_retry_remark = df['remark'].astype(str).apply(
            lambda remark: any(marker in remark for marker in RETRY_REMARKS)
        )
        mask_retry = (
            (df['collection'] == collection_basename) &
            (df['date'] >= start_date.strftime('%Y-%m-%d')) &
            (df['date'] <= end_date.strftime('%Y-%m-%d')) &
            is_retry_remark
        )
        df_retry = df[mask_retry]

        print(f"\n{'='*60}")
        print(f"RETRY {product}: {len(df_retry)} date(s) with remark {RETRY_REMARKS}")
        print(f"{'='*60}")

        resolved_rows = []  # (date, remark) of the rows that can be removed from the CSV
        for date_str, remark in zip(df_retry['date'], df_retry['remark']):
            print(f"\n--- Retry {product} for {date_str} ({remark}) ---")
            try:
                # A follow-up run earlier in this rerun may already have produced it
                if not stac_item_exists(product_collection, date_str):
                    return_code = run_satromo_processor(config_file, date_str, env, script_dir)
                    if return_code != 0:
                        print(f"✗ Retry failed with exit code {return_code} for {date_str}, kept in CSV")
                        continue

                if stac_item_exists(product_collection, date_str):
                    print(f"✓ {product} available for {date_str}, removing '{remark}' from CSV")
                    resolved_rows.append((date_str, remark))
                else:
                    print(f"! {product} still not produced for {date_str}, kept in CSV for the next run")
            except Exception as e:
                print(f"✗ Unexpected error in retry for {date_str}: {e}")

        # Re-read the CSV: the subprocesses write to it while they run (see process_empty_asset_list)
        if resolved_rows:
            df_now = pd.read_csv(config.EMPTY_ASSET_LIST)
            mask_resolved = df_now.apply(
                lambda row: (row['collection'] == collection_basename
                             and (row['date'], row['remark']) in resolved_rows),
                axis=1
            )
            df_now[~mask_resolved].to_csv(config.EMPTY_ASSET_LIST, index=False)

        print(f"\nRETRY {product} SUMMARY: resolved {len(resolved_rows)} of {len(df_retry)}")


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

    # Runs also after an early abort: the follow-up products (e.g. VHI) read from
    # swisstopo STAC, not from Copernicus, so an outage there does not affect them.
    retry_follow_up_products(days_back, config_file)

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