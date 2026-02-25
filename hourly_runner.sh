#!/bin/bash

# Simplified Workflow Script with Locking
# Runs Python scripts without git operations or dependency management

# ============================================================================
# CONFIGURATION - EDIT THESE VALUES
# ============================================================================

# Your repository path
REPO_DIR="/mnt/c/Users/Localadmin/Documents/SATROMO/topo-satromo-v2/topo-satromo-v2"



# Python command
PYTHON_CMD="/mnt/c/Users/Localadmin/Documents/SATROMO/topo-satromo-v2/topo-satromo-v2/.venv/bin/python"

# Lock file location
LOCK_FILE="//mnt/c/Users/Localadmin/Documents/SATROMO/topo-satromo-v2/topo-satromo-v2/workflow.lock"

# Maximum lock age in seconds (3 hours = 10800 seconds)
MAX_LOCK_AGE=10800

# ============================================================================
# END CONFIGURATION
# ============================================================================

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Log functions
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

info() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"
}

# Function to check if lock file is stale
check_stale_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local lock_age=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE" 2>/dev/null)))
        if [ $lock_age -gt $MAX_LOCK_AGE ]; then
            warn "Lock file is $lock_age seconds old (stale), removing it"
            rm -f "$LOCK_FILE"
            return 1
        fi
        return 0
    fi
    return 1
}

# Function to acquire lock
acquire_lock() {
    local max_wait=10
    local waited=0
    
    # Check for stale locks first
    check_stale_lock
    
    # Try to acquire lock with timeout
    while [ $waited -lt $max_wait ]; do
        if mkdir "$LOCK_FILE" 2>/dev/null; then
            # Lock acquired successfully
            echo $$ > "$LOCK_FILE/pid"
            echo "$(date +%s)" > "$LOCK_FILE/timestamp"
            echo "$RUN_MODE" > "$LOCK_FILE/mode"
            trap 'release_lock' EXIT INT TERM
            return 0
        fi
        
        # Lock exists, check if process is still running
        if [ -f "$LOCK_FILE/pid" ]; then
            local lock_pid=$(cat "$LOCK_FILE/pid" 2>/dev/null)
            local lock_mode=$(cat "$LOCK_FILE/mode" 2>/dev/null)
            
            if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
                error "Another workflow is already running (PID: $lock_pid, Mode: $lock_mode)"
                info "Current job: $RUN_MODE mode will not start"
                exit 0  # Exit gracefully, not an error
            else
                warn "Lock exists but process $lock_pid is not running, removing stale lock"
                rm -rf "$LOCK_FILE"
            fi
        fi
        
        sleep 1
        waited=$((waited + 1))
    done
    
    error "Could not acquire lock after $max_wait seconds"
    exit 1
}

# Function to release lock
release_lock() {
    if [ -d "$LOCK_FILE" ]; then
        rm -rf "$LOCK_FILE"
        log "Lock released"
    fi
}

# Determine run mode based on current time or argument
CURRENT_MINUTE=$(date +%M)
CURRENT_HOUR=$(date +%H)

if [ "$CURRENT_HOUR" == "01" ] && [ "$CURRENT_MINUTE" == "00" ]; then
    RUN_MODE="processor"
elif [ "$CURRENT_MINUTE" == "33" ]; then
    RUN_MODE="rerun"
else
    # Allow manual override
    RUN_MODE="${1:-processor}"
fi

log "========================================="
log "Starting workflow: $RUN_MODE mode"
log "========================================="

# Acquire lock before proceeding
acquire_lock

# Check if repository directory exists
if [ ! -d "$REPO_DIR" ]; then
    error "Repository directory does not exist: $REPO_DIR"
    exit 1
fi

# Go to repository
cd "$REPO_DIR" || exit 1
log "Working directory: $REPO_DIR"

# Run CS PUBLISH (common to both workflows)
log "Running CS PUBLISH script..."
if ! $PYTHON_CMD main_functions/csplus_publish.py prod_config.py; then
    error "CS PUBLISH script failed"
    exit 1
fi

# Run the appropriate processor based on mode
if [ "$RUN_MODE" == "processor" ]; then
    log "Running PROCESSOR script..."
    OUTPUT=$($PYTHON_CMD satromo_processor.py prod_config.py 2>&1)  # Capture output
    if echo "$OUTPUT" | grep -q "cloudy"; then
        log "PROCESSOR script encountered 'cloudy' remark. Output: $OUTPUT"
    elif [ $? -ne 0 ]; then
        error "PROCESSOR script failed with output: $OUTPUT"
        exit 1
    else
        log "PROCESSOR script completed successfully!"
    fi
elif [ "$RUN_MODE" == "rerun" ]; then
    log "Running RERUN PROCESSOR script..."
    OUTPUT=$($PYTHON_CMD rerun.py prod_config.py 2>&1)  # Capture output
    if echo "$OUTPUT" | grep -q "cloudy"; then
        log "RERUN PROCESSOR script encountered 'cloudy' remark. Output: $OUTPUT"
    elif [ $? -ne 0 ]; then
        error "RERUN PROCESSOR script failed with output: $OUTPUT"
        exit 1
    else
        log "RERUN PROCESSOR script completed successfully!"
    fi
else
    error "Invalid run mode: $RUN_MODE"
    exit 1
fi

log "========================================="
log "Workflow completed successfully: $RUN_MODE mode"
log "========================================="

# Lock will be released automatically by trap
