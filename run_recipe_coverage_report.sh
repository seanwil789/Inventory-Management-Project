#!/bin/bash
# Recipe-coverage Stock + Flow report — the Measure step of the recipe-coverage
# goal (project_recipe_coverage_goal.md). Read-only. Fires weekly Sunday; Sean
# consumes the log at the biweekly checkpoints.
#
# Portable cd (resolves the script's own dir) so the SAME committed script runs
# on dev and Pi without per-host path localization — unlike the older run_*.sh
# which hardcode the dev path and must be hand-edited on the Pi.

set -e

cd "$(dirname "$(readlink -f "$0")")"

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: virtualenv not found at .venv/bin/activate" >&2
    exit 1
fi
source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/recipe_coverage_$(date +%Y%m%d_%H%M%S).log"

echo "=== Recipe Coverage Report: $(date) ===" | tee "$LOG_FILE"
python3 manage.py recipe_coverage_report 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

# Keep only the 30 most-recent logs
ls -t "$LOG_DIR"/recipe_coverage_*.log 2>/dev/null | tail -n +31 | xargs rm -f

exit $EXIT_CODE
