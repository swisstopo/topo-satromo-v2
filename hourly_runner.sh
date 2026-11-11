#!/bin/bash

# Local alternative to .github/workflows/run_rerun.yml, to be run as a cron job.
# It does the same steps, but the processing runs on this machine instead of EC2:
#
#   1. Update the repository to the latest commit of the branch
#   2. Light check: acquisition plan, check_s2_sr.py, check_vhi.py,
#      upload collection assets, push tools/ to the branch
#   3. Only if needed (S2-SR tiles ready or VHI ready to process):
#      rerun.py, upload collection assets, push tools/ to the branch
#
# Usage:
#   ./hourly_runner.sh <config> <branch>
#   ./hourly_runner.sh prod_config.py main
#
# Cron example (crontab -e), at :17 from 00 to 13 local time
# (covers 23 to 11 UTC in winter and summer, as in run_rerun.yml):
#   17 0-13 * * * /mnt/c/Users/Localadmin/Documents/SATROMO/reprocess-prod/topo-satromo-v2/hourly_runner.sh prod_config.py main >> /mnt/c/Users/Localadmin/Documents/SATROMO/reprocess-prod/hourly_runner.log 2>&1

# ============================================================================
# CONFIGURATION - EDIT THESE VALUES
# ============================================================================

# Repository the job runs in (has its own .venv, secrets/ and local_assets/)
REPO_DIR="/mnt/c/Users/Localadmin/Documents/SATROMO/reprocess-prod/topo-satromo-v2"

# Python command
PYTHON_CMD="$REPO_DIR/.venv/bin/python"

# Lock file: only one run at a time. Outside the repository, so git does not see it.
LOCK_FILE="/mnt/c/Users/Localadmin/Documents/SATROMO/reprocess-prod/hourly_runner.lock"

# Number of days back for check_s2_sr.py and check_vhi.py (as in run_rerun.yml)
DAYS_BACK=30

# Maximum run time of rerun.py (as timeout-minutes: 840 in run_rerun.yml)
RERUN_TIMEOUT="14h"

# ============================================================================
# END CONFIGURATION
# ============================================================================

# Arguments: config file and branch
CONFIG_FILE="${1:-prod_config.py}"
BRANCH="${2:-main}"

# Log functions
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2
}

# Commit the files in tools/ and push them to the branch
# (same as the "Push tools files to repo" step in run_rerun.yml)
push_tools() {
    log "Pushing tools files to $BRANCH..."
    git add --force tools/
    git diff --staged --quiet || git commit -m 'Tools files done'
    if ! git pull --rebase origin "$BRANCH"; then
        error "git pull --rebase failed"
        exit 1
    fi
    if ! git push origin "$BRANCH"; then
        error "git push failed"
        exit 1
    fi
}

# Upload acquisitionplan.csv and dates_with_no_imagery.csv to the STAC collections
publish_collection_assets() {
    log "Publishing acquisitionplan.csv and step0_empty_assets.csv..."
    if ! $PYTHON_CMD main_functions/util_upload_collection_asset.py "$CONFIG_FILE"; then
        error "util_upload_collection_asset.py failed"
        exit 1
    fi
}

# Read a value that check_s2_sr.py / check_vhi.py wrote to the outputs file
read_output() {
    grep "^$1=" "$GITHUB_OUTPUT" | tail -1 | cut -d= -f2
}

log "========================================="
log "Starting workflow: config=$CONFIG_FILE branch=$BRANCH"
log "========================================="

# ----------------------------------------------------------------------------
# Lock: if another run is still going (e.g. a long rerun.py), stop here.
# flock releases the lock automatically when this script ends.
# ----------------------------------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "Another run is still going, this run does not start"
    exit 0
fi

# ----------------------------------------------------------------------------
# STEP 1 — Update the repository to the latest commit of the branch
# ----------------------------------------------------------------------------
if [ ! -d "$REPO_DIR" ]; then
    error "Repository directory does not exist: $REPO_DIR"
    exit 1
fi
cd "$REPO_DIR" || exit 1
log "Working directory: $REPO_DIR"

if [ ! -f "configuration/$CONFIG_FILE" ]; then
    error "Config file does not exist: configuration/$CONFIG_FILE"
    exit 1
fi

# Local code changes outside tools/ would be lost or block the update: stop
CHANGED_FILES=$(git status --porcelain --untracked-files=no -- . ':(exclude)tools')
if [ -n "$CHANGED_FILES" ]; then
    error "Local changes outside tools/, please commit or undo them first:"
    echo "$CHANGED_FILES" >&2
    exit 1
fi

if ! git fetch origin "$BRANCH"; then
    error "git fetch of branch $BRANCH failed"
    exit 1
fi
if ! git checkout "$BRANCH"; then
    error "git checkout of branch $BRANCH failed"
    exit 1
fi

REQUIREMENTS_BEFORE=$(git rev-parse HEAD:requirements.txt)

# Pulls the latest commit. Also pushes tools/ changes that are left over from
# a run that stopped before its push (e.g. rerun.py failed).
push_tools

# Install the dependencies again only when requirements.txt changed
REQUIREMENTS_AFTER=$(git rev-parse HEAD:requirements.txt)
if [ "$REQUIREMENTS_BEFORE" != "$REQUIREMENTS_AFTER" ]; then
    log "requirements.txt changed, installing dependencies..."
    if ! $PYTHON_CMD -m pip install -r requirements.txt; then
        error "pip install failed"
        exit 1
    fi
fi

# ----------------------------------------------------------------------------
# STEP 2 — Light check (STAGE 1 in run_rerun.yml)
# ----------------------------------------------------------------------------
# check_s2_sr.py and check_vhi.py write needs-processing / vhi-needs-processing
# to the file in $GITHUB_OUTPUT (as in GitHub Actions)
export GITHUB_OUTPUT=$(mktemp)
trap 'rm -f "$GITHUB_OUTPUT"' EXIT

# Same date as the GitHub runner, which runs in UTC
TODAY=$(date -u +%Y-%m-%d)

log "Getting acquisition plan..."
if ! $PYTHON_CMD main_functions/util_get_acquisition_plans.py; then
    error "util_get_acquisition_plans.py failed"
    exit 1
fi

log "Checking S2 data availability and updating CSV..."
if ! $PYTHON_CMD check_s2_sr.py "$TODAY" "$DAYS_BACK" "$CONFIG_FILE"; then
    error "check_s2_sr.py failed"
    exit 1
fi

log "Checking VHI 'LST missing' entries and updating CSV..."
if ! $PYTHON_CMD check_vhi.py "$TODAY" "$DAYS_BACK" "$CONFIG_FILE"; then
    error "check_vhi.py failed"
    exit 1
fi

publish_collection_assets
push_tools

NEEDS_PROCESSING=$(read_output "needs-processing")
VHI_NEEDS_PROCESSING=$(read_output "vhi-needs-processing")
log "needs-processing=$NEEDS_PROCESSING vhi-needs-processing=$VHI_NEEDS_PROCESSING"

# VHI dates where S2-SR data was published after the VHI (from check_vhi.py).
# rerun.py reads this variable and reprocesses these dates with --force-vhi
export VHI_REPROCESS_DATES=$(read_output "vhi-reprocess-dates")
log "vhi-reprocess-dates=$VHI_REPROCESS_DATES"

# ----------------------------------------------------------------------------
# STEP 3 — Full processing on this machine (STAGE 2 in run_rerun.yml),
#          only when S2-SR tiles are ready or VHI is ready to process
# ----------------------------------------------------------------------------
if [ "$NEEDS_PROCESSING" != "true" ] && [ "$VHI_NEEDS_PROCESSING" != "true" ]; then
    log "Nothing to process"
    log "========================================="
    log "Workflow completed successfully"
    log "========================================="
    exit 0
fi

log "Running rerun.py (maximum $RERUN_TIMEOUT)..."
if ! timeout "$RERUN_TIMEOUT" $PYTHON_CMD rerun.py "$CONFIG_FILE"; then
    error "rerun.py failed or ran longer than $RERUN_TIMEOUT"
    exit 1
fi

publish_collection_assets
push_tools

log "========================================="
log "Workflow completed successfully"
log "========================================="
