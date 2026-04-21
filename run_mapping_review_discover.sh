#!/bin/bash
# Mapping Review auto-discover — called by cron daily.
# Runs discover_unmapped.py --write which finds new unmapped invoice items
# (seen 2+ times), fuzzy-matches against existing products + Synergy sheet,
# and pushes suggestions to the Mapping Review tab for Sean's triage.
# Auto-approves suggestions scoring >=90%; others land blank for review.
# Uses file locking + log rotation matching run_invoice_batch.sh pattern.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/mapping_review_discover.lock"

# Acquire lock — exit silently if another instance is running
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "Another discover run is already in progress — skipping."
    exit 0
fi

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi

source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/mapping_review_discover_$(date +%Y%m%d_%H%M%S).log"

echo "=== Mapping Review Discover: $(date) ===" | tee "$LOG_FILE"
python3 invoice_processor/discover_unmapped.py --write 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=$?

# Keep only the 30 most recent log files
ls -t "$LOG_DIR"/mapping_review_discover_*.log 2>/dev/null | tail -n +31 | xargs rm -f

exit $EXIT_CODE
