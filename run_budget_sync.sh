#!/bin/bash
# Budget sync — writes invoice totals to Wentworth budget xlsx.
# Runs weekly (Friday 8am) via cron. Deduplicates automatically.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/budget_sync.lock"
LOGDIR="logs"
LOGFILE="$LOGDIR/budget_sync_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOGDIR"

# File lock to prevent concurrent runs
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Budget sync already running"; exit 0; }

source .venv/bin/activate

echo "=== Budget Sync $(date) ===" >> "$LOGFILE"
python3 invoice_processor/budget_sync.py >> "$LOGFILE" 2>&1
echo "=== Done $(date) ===" >> "$LOGFILE"

# Keep last 10 budget sync logs
ls -t "$LOGDIR"/budget_sync_*.log 2>/dev/null | tail -n +11 | xargs -r rm
