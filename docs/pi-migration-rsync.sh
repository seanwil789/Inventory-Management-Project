#!/bin/bash
# Pi migration state-transfer helper.
#
# Usage:
#   bash docs/pi-migration-rsync.sh sean@wentworth-kitchen
#   bash docs/pi-migration-rsync.sh sean@100.x.y.z       # by tailnet IP
#
# Run this from the Chromebook (current host). It rsyncs the pieces of
# state that aren't in git but are needed for the Pi to be operational.
# Safe to run multiple times — rsync only copies what changed.
#
# Prerequisites on the Pi:
#   - SSH reachable
#   - Repo already cloned to ~/my-saas
#   - .venv created (don't have to be installed yet)

set -euo pipefail

DEST="${1:-}"
if [[ -z "$DEST" ]]; then
  echo "Usage: $0 user@host"
  echo "Example: $0 sean@wentworth-kitchen"
  exit 1
fi

SRC=/home/seanwil789/my-saas
REMOTE_BASE="~/my-saas"

echo "==> Transferring state from $SRC to $DEST:$REMOTE_BASE"
echo

# Use rsync's -a (archive) + -v (verbose) + -z (compress over network) +
# --info=progress2 (single-line progress bar instead of every-file spam)
RSYNC_OPTS=(-avz --info=progress2)

# 1. .env — secrets file. Single small file. SCP works fine but rsync
#    keeps it consistent with the rest.
echo "==> .env"
rsync "${RSYNC_OPTS[@]}" "$SRC/.env" "$DEST:$REMOTE_BASE/.env"

# 2. Credentials directory. Just service_account.json + .gitkeep.
echo
echo "==> invoice_processor/credentials/"
rsync "${RSYNC_OPTS[@]}" "$SRC/invoice_processor/credentials/" \
  "$DEST:$REMOTE_BASE/invoice_processor/credentials/"

# 3. SQLite DB. 2.5 MB. Pi must have its own .venv installed before this
#    is useful, but the file can land any time.
echo
echo "==> db.sqlite3"
rsync "${RSYNC_OPTS[@]}" "$SRC/db.sqlite3" "$DEST:$REMOTE_BASE/db.sqlite3"

# 4. OCR cache. 18 MB / 288 entries. Skipping this would force re-OCR
#    of every invoice on first run — slow + costs money.
echo
echo "==> .ocr_cache/"
rsync "${RSYNC_OPTS[@]}" "$SRC/.ocr_cache/" "$DEST:$REMOTE_BASE/.ocr_cache/"

# 5. Invoice totals cache. 20 KB. Required by COGs view + budget_sync.
echo
echo "==> .invoice_totals/"
rsync "${RSYNC_OPTS[@]}" "$SRC/.invoice_totals/" \
  "$DEST:$REMOTE_BASE/.invoice_totals/"

# 6. Historical stats. 24 KB. Production tracker JSON — used by some
#    auditing commands.
echo
echo "==> .historical_stats/"
rsync "${RSYNC_OPTS[@]}" "$SRC/.historical_stats/" \
  "$DEST:$REMOTE_BASE/.historical_stats/"

# 7. Mapping caches. 244 KB. Item mapping fuzzy-match learned rules +
#    negative matches. Skipping this means the mapping_review loop
#    starts from scratch.
echo
echo "==> invoice_processor/mappings/"
rsync "${RSYNC_OPTS[@]}" "$SRC/invoice_processor/mappings/" \
  "$DEST:$REMOTE_BASE/invoice_processor/mappings/"

echo
echo "==> Done. State transferred."
echo
echo "Next: SSH into the Pi and run:"
echo "  cd ~/my-saas && source .venv/bin/activate && python manage.py test myapp"
echo
echo "Expected: 399 tests pass in ~80 seconds."
