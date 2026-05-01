#!/bin/bash
# Invoice batch processor — called by cron.
# Activates the virtualenv, runs the batch, and rotates logs to keep
# only the last 30 runs. Uses file locking to prevent concurrent runs.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/invoice_batch.lock"

# Acquire lock — exit silently if another instance is running
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "Another batch run is already in progress — skipping."
    exit 0
fi

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi

source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/invoice_batch_$(date +%Y%m%d_%H%M%S).log"

echo "=== Invoice Batch Run: $(date) ===" | tee "$LOG_FILE"
python3 invoice_processor/batch.py 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=$?

# After batch completes successfully, auto-insert any newly-approved canonicals
# into the active Synergy tab at their target section. Failure here is
# non-fatal — sheet-side is recoverable by manual restructure.
if [ $EXIT_CODE -eq 0 ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "=== Inserting new canonicals into active Synergy tab ===" | tee -a "$LOG_FILE"
    python3 invoice_processor/synergy_sync.py --insert-new 2>&1 | tee -a "$LOG_FILE" || \
        echo "[!] insert-new failed (non-fatal)" | tee -a "$LOG_FILE"
fi

# Keep only the 30 most recent log files
ls -t "$LOG_DIR"/invoice_batch_*.log 2>/dev/null | tail -n +31 | xargs rm -f

# Exit with the batch script's exit code
exit $EXIT_CODE
