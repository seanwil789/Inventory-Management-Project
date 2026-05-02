#!/bin/bash
# Mapping-loop daily cron — populate /mapping-review/ queue with unmapped
# invoice items so Sean's curation surface stays current.
#
# Replaces (2026-05-02) the legacy discover_unmapped.py --write path that
# pushed suggestions to the Google Sheet's Mapping Review tab. The unified
# Django queue is now the single review surface; the sheet's Mapping
# Review tab is retired.
#
# What this runs:
#   populate_mapping_review_from_unmapped --apply
#     • Walks unmapped ILI rows (product=None)
#     • Runs mapper to get suggested canonical for each (vendor, raw_desc)
#     • Creates/updates ProductMappingProposal rows (source='discover_unmapped')
#     • Re-opens rejected proposals when raw still unmapped AND new
#       suggestion differs from the rejected target (per Sean's rule:
#       items without canonicals resurface until one is given)
#
# File-lock + log rotation matches run_invoice_batch.sh pattern.

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

echo "=== Mapping Review Discover (Django): $(date) ===" | tee "$LOG_FILE"
python3 manage.py populate_mapping_review_from_unmapped --apply 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=$?

# Keep only the 30 most recent log files
ls -t "$LOG_DIR"/mapping_review_discover_*.log 2>/dev/null | tail -n +31 | xargs rm -f

exit $EXIT_CODE
