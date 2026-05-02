#!/bin/bash
# Mapping-loop drift audit — re-evaluates curated ProductMappings against
# the current canonical pool. Surfaces drift when newer/more-specific
# canonicals appear after a PM was originally curated.
#
# Two-step run:
#   1. audit_pm_canonical_drift --safe-only --apply
#      Auto-clean the SAFE bucket (proposed canonical strict superset of
#      current — empirically 100% accurate). No human review needed; the
#      math guarantees the proposed canonical genuinely adds tokens that
#      appear in the PM description.
#
#   2. audit_pm_canonical_drift --exclude-bad
#      Queue the AMBIGUOUS bucket into ProductMappingProposal as
#      drift_audit-source proposals. Surfaces in /mapping-review/ for
#      Sean's review. Drops the BAD bucket (specificity-loss class).
#
# Cadence:
#   Sean said: quarterly safety net + event-driven post-curation. We
#   approximate with weekly auto-clean (silent SAFE flush) + monthly
#   AMBIGUOUS surface. Tighten or loosen based on how often new
#   canonicals get created.
#
# File-lock + log rotation matches the discover script.

set -e

cd /home/seanwil789/my-saas

LOCKFILE="/tmp/drift_audit.lock"

exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "Another drift audit is already in progress — skipping."
    exit 0
fi

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi

source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/drift_audit_$(date +%Y%m%d_%H%M%S).log"

MODE="${1:-weekly}"  # 'weekly' (safe-only) or 'monthly' (ambiguous queue)

echo "=== Drift Audit ($MODE): $(date) ===" | tee "$LOG_FILE"

if [ "$MODE" = "weekly" ]; then
    # Silent auto-clean of the 100%-accurate SAFE bucket
    python3 manage.py audit_pm_canonical_drift --safe-only --apply 2>&1 | tee -a "$LOG_FILE"
elif [ "$MODE" = "monthly" ]; then
    # Queue AMBIGUOUS proposals into /mapping-review/ for human eyeballs
    python3 manage.py audit_pm_canonical_drift --exclude-bad 2>&1 | tee -a "$LOG_FILE"
else
    echo "Unknown mode: $MODE (expected 'weekly' or 'monthly')" >&2
    exit 2
fi

EXIT_CODE=$?

# Keep only 30 most-recent logs per mode
ls -t "$LOG_DIR"/drift_audit_*.log 2>/dev/null | tail -n +31 | xargs rm -f

exit $EXIT_CODE
