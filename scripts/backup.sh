#!/usr/bin/env bash
#
# Off-site backup of my-saas state — everything NOT in git.
#
# Captures: db.sqlite3, .ocr_cache, .invoice_totals, .historical_stats,
# .kitchen_ops, .env, service_account.json, mappings cache, BoY PDF,
# budget CSV, aramark sample.
#
# Excludes: .git/ (on GitHub), .venv/, __pycache__, *.pyc, db.sqlite3.pre-*
# (local backups), the 1.4GB OneDrive zip (handle separately as one-shot),
# logs/ (regenerable; cron logs only).
#
# Output: timestamped, AES-256-CBC encrypted tarball in ~/.kitchen-backups/.
# Passphrase comes from $KITCHEN_BACKUP_PASS env var, or interactive prompt.
#
# Off-site upload: see UPLOAD section at bottom — fill in your destination
# (Backblaze B2, AWS S3, Google Drive via rclone, etc.). Stays commented
# until you configure it.
#
# Usage:
#   bash scripts/backup.sh                    # interactive passphrase
#   KITCHEN_BACKUP_PASS=xxx bash scripts/backup.sh   # for cron
#
# Restore:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in <file>.tar.gz.enc | tar -xzv -C /tmp/restore
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${KITCHEN_BACKUP_DIR:-$HOME/.kitchen-backups}"
RETAIN_DAYS=7
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/my-saas_${TS}.tar.gz.enc"

mkdir -p "$BACKUP_DIR"

# ── Paths to include (relative to REPO_DIR) ──────────────────────────────
INCLUDE=(
    "db.sqlite3"
    ".ocr_cache"
    ".invoice_totals"
    ".historical_stats"
    ".kitchen_ops"
    ".env"
    "invoice_processor/credentials"
    "invoice_processor/mappings"
    "The-Book-of-Yields-Accuracy-in-Food-Costing-and-Purchasing.pdf"
    "aramark-sample.pdf"
)

# Optional: include latest budget CSV if present (filename has spaces + parens)
shopt -s nullglob
BUDGET_FILES=("$REPO_DIR"/Men\'s\ Wentworth\ Food\ Budget\ *.csv)
shopt -u nullglob

# Verify everything we plan to include actually exists; warn but don't fail
ACTUAL=()
for path in "${INCLUDE[@]}"; do
    if [[ -e "$REPO_DIR/$path" ]]; then
        ACTUAL+=("$path")
    else
        echo "  [skip] $path (not found)" >&2
    fi
done
for f in "${BUDGET_FILES[@]}"; do
    rel="${f#$REPO_DIR/}"
    ACTUAL+=("$rel")
done

if [[ ${#ACTUAL[@]} -eq 0 ]]; then
    echo "[!] Nothing to back up — exiting" >&2
    exit 1
fi

# ── Passphrase ────────────────────────────────────────────────────────────
if [[ -z "${KITCHEN_BACKUP_PASS:-}" ]]; then
    echo "Enter backup passphrase (will not echo):" >&2
    read -r -s PASS
    echo "Confirm:" >&2
    read -r -s PASS2
    if [[ "$PASS" != "$PASS2" ]]; then
        echo "[!] Passphrases don't match — exiting" >&2
        exit 1
    fi
    KITCHEN_BACKUP_PASS="$PASS"
    unset PASS PASS2
fi

# ── Tar + gzip + encrypt in one pipeline (no plaintext on disk) ──────────
echo "Backing up ${#ACTUAL[@]} paths to $OUT..."
tar -C "$REPO_DIR" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='db.sqlite3.pre-*' \
    -czf - "${ACTUAL[@]}" \
| openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass "env:KITCHEN_BACKUP_PASS" \
    -out "$OUT"

unset KITCHEN_BACKUP_PASS

SIZE="$(du -h "$OUT" | cut -f1)"
echo "[✓] Wrote $OUT ($SIZE)"

# ── Rotation: keep last N days of local backups ──────────────────────────
find "$BACKUP_DIR" -name 'my-saas_*.tar.gz.enc' -type f -mtime +$RETAIN_DAYS -delete
KEPT="$(find "$BACKUP_DIR" -name 'my-saas_*.tar.gz.enc' -type f | wc -l)"
echo "[✓] Local rotation: $KEPT backups retained (last $RETAIN_DAYS days)"

# ── UPLOAD ────────────────────────────────────────────────────────────────
# Off-site upload is THE point of this script — local backups die with the
# laptop. Pick ONE destination, configure credentials separately (NOT in
# this file), then uncomment the matching block.
#
# Recommended: Backblaze B2 (~$0.005/GB/mo, encrypted-at-rest, no egress
# fees on small daily backups). Free tier: 10GB.
#
# Option A — Backblaze B2 via b2 CLI (`pip install b2` + `b2 authorize-account`):
#   b2 upload-file YOUR_BUCKET "$OUT" "my-saas/$(basename "$OUT")"
#
# Option B — AWS S3 via aws CLI:
#   aws s3 cp "$OUT" "s3://YOUR_BUCKET/my-saas/$(basename "$OUT")"
#
# Option C — rclone to any cloud (gdrive, dropbox, onedrive-personal, etc.):
#   rclone copy "$OUT" "REMOTE:my-saas-backups/" --progress
#
# Option D — scp to a personal VPS / NAS:
#   scp "$OUT" user@host:/path/to/backups/
#
# echo "[!] No off-site upload configured — backup is only LOCAL."
# echo "    Edit scripts/backup.sh UPLOAD section to enable."
