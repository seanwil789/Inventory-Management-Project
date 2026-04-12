#!/bin/bash
# Invoice batch processor — called by cron.
# Activates the virtualenv, runs the batch, and rotates logs to keep
# only the last 30 runs.

cd /home/seanwil789/my-saas
source .venv/bin/activate

LOG_DIR="logs"
LOG_FILE="$LOG_DIR/invoice_batch_$(date +%Y%m%d_%H%M%S).log"

echo "=== Invoice Batch Run: $(date) ===" | tee "$LOG_FILE"
python3 invoice_processor/batch.py 2>&1 | tee -a "$LOG_FILE"

# Keep only the 30 most recent log files
ls -t "$LOG_DIR"/invoice_batch_*.log 2>/dev/null | tail -n +31 | xargs rm -f
