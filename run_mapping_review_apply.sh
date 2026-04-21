#!/bin/bash
# Mapping Review auto-apply — called by cron every 6 hours.
# Reads Y-marked rows from the Mapping Review Google Sheet tab, writes them
# to the Item Mapping tab (new rows for suggestions, col-F rewrites for
# SUPC and CORRECTION prefixes), and marks them DONE.
# Non-Y rows stay put for further manual review.
# Uses file locking to prevent concurrent runs and rotates logs.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/mapping_review_apply.lock"

# Acquire lock — exit silently if another instance is running
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "Another mapping-review run is already in progress — skipping."
    exit 0
fi

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi

source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/mapping_review_apply_$(date +%Y%m%d_%H%M%S).log"

echo "=== Mapping Review Apply: $(date) ===" | tee "$LOG_FILE"
python3 invoice_processor/discover_unmapped.py --apply-approved 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=$?

# Keep only the 30 most recent log files
ls -t "$LOG_DIR"/mapping_review_apply_*.log 2>/dev/null | tail -n +31 | xargs rm -f

exit $EXIT_CODE
