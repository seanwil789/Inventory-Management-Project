#!/bin/bash
# Monthly Synergy tab creation + carryover — called by cron at 00:05 on day 1.
# Idempotent: safe to re-run; if the tab exists, only refreshes stale rows.
# Uses file locking to prevent concurrent runs.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/monthly_synergy_tab.lock"

# Acquire lock — exit silently if another instance is running
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[$(date -Iseconds)] Another monthly-tab run is already in progress — skipping."
    exit 0
fi

LOGDIR="/home/seanwil789/my-saas/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/monthly_tab_$(date +%Y%m).log"

{
    echo ""
    echo "===== $(date -Iseconds) monthly tab run ====="
    .venv/bin/python manage.py create_monthly_synergy_tab
} >> "$LOGFILE" 2>&1
