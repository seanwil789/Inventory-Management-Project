#!/bin/bash
# Rebuild .invoice_totals/YYYY-MM.json from the OCR cache for the current
# month. Runs daily (6am) via cron so the COGs dashboard stays current even
# when the live pipeline's per-invoice cache write misses a total (e.g.
# parser couldn't extract invoice_total on first pass).
#
# For older months the cache is stable after its last late-arriving invoice;
# the job only refreshes the current month to avoid churn.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/refresh_invoice_totals.lock"
LOGDIR="logs"
LOGFILE="$LOGDIR/refresh_invoice_totals_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOGDIR"

# File lock to prevent concurrent runs
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Refresh already running"; exit 0; }

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi

source .venv/bin/activate

echo "=== Refresh Invoice Totals $(date) ===" >> "$LOGFILE"
python3 manage.py refresh_invoice_totals >> "$LOGFILE" 2>&1
echo "=== Done $(date) ===" >> "$LOGFILE"

# Keep last 10 logs
ls -t "$LOGDIR"/refresh_invoice_totals_*.log 2>/dev/null | tail -n +11 | xargs -r rm
