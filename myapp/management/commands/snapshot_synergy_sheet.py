"""Snapshot a Synergy tab to JSON for rollback safety. Restore mode reads
the JSON + writes back to the sheet cell-by-cell.

Sean's "save state, test write, revert if wrong" workflow per the Phase 3a
plan: before letting a code change touch live sheet writes, take a
snapshot. If the test write produces wrong values, restore from snapshot.

Usage:
    # Save current state of a tab to JSON
    python manage.py snapshot_synergy_sheet --tab "Synergy May 2026"

    # Restore from a saved snapshot
    python manage.py snapshot_synergy_sheet \\
        --restore .synergy_may2026_snapshot_20260502_153022.json

    # Dry-run restore (preview without writing)
    python manage.py snapshot_synergy_sheet \\
        --restore .synergy_may2026_snapshot_20260502_153022.json --dry-run

Snapshot file format (JSON):
    {
      "tab": "Synergy May 2026",
      "timestamp": "20260502_153022",
      "row_count": 551,
      "rows": [["A1", "B1", ...], ["A2", "B2", ...], ...]
    }

Snapshot is full A1:N — captures every cell. Restore writes the same
range, replacing whatever the sheet has now. There's a precedent for
this pattern: `.synergy_may2026_snapshot_20260501_142439.json` already
exists from the May 1 multi-category restructure session.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


SNAPSHOT_RANGE = "A1:N1000"   # captures the full Synergy data + summary panel


class Command(BaseCommand):
    help = "Snapshot or restore a Synergy Google Sheet tab."

    def add_arguments(self, parser):
        parser.add_argument('--tab', type=str, default=None,
                            help='Synergy tab name to snapshot (default: active tab)')
        parser.add_argument('--restore', type=str, default=None,
                            help='Path to a snapshot JSON file to restore from')
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview restore without writing to the sheet')
        parser.add_argument('--out-dir', type=str, default='.',
                            help='Directory to write snapshot JSON (default: cwd)')

    def handle(self, *args, tab=None, restore=None, dry_run=False,
               out_dir='.', **opts):
        # Bootstrap invoice_processor on path so we can use its sheets helpers
        from django.conf import settings
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)

        from sheets import get_sheets_client, get_sheet_values
        from config import SPREADSHEET_ID, ACTIVE_SHEET_TAB

        if restore:
            return self._restore(restore, dry_run, get_sheets_client,
                                  SPREADSHEET_ID)

        # Default = save snapshot
        target_tab = tab or ACTIVE_SHEET_TAB
        return self._snapshot(target_tab, out_dir,
                               get_sheet_values, SPREADSHEET_ID)

    def _snapshot(self, tab, out_dir, get_sheet_values, spreadsheet_id):
        """Read the entire tab to a JSON snapshot file."""
        self.stdout.write(f"Reading '{tab}' from Sheets API...")
        rows = get_sheet_values(spreadsheet_id, f"'{tab}'!{SNAPSHOT_RANGE}")
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        # Filename mirrors the existing pattern .synergy_<month><year>_snapshot_<ts>.json
        # Slugify tab name: "Synergy May 2026" → "synergy_may2026"
        slug = tab.lower().replace(' ', '').replace('synergy', 'synergy_')
        if slug.startswith('synergy__'):
            slug = slug[len('synergy_'):]   # collapse double underscore
        filename = f".{slug}_snapshot_{ts}.json"
        out_path = Path(out_dir) / filename

        snapshot = {
            'tab': tab,
            'timestamp': ts,
            'row_count': len(rows),
            'range': SNAPSHOT_RANGE,
            'rows': rows,
        }
        out_path.write_text(json.dumps(snapshot, indent=2))
        self.stdout.write(f"  [✓] Saved {len(rows)} rows to {out_path}")
        self.stdout.write(f"  Restore: python manage.py snapshot_synergy_sheet "
                          f"--restore {out_path}")
        return str(out_path)

    def _restore(self, snapshot_path, dry_run, get_sheets_client,
                  spreadsheet_id):
        """Read a snapshot JSON + write its rows back to the sheet."""
        path = Path(snapshot_path)
        if not path.exists():
            raise CommandError(f"Snapshot file not found: {snapshot_path}")

        snapshot = json.loads(path.read_text())
        tab = snapshot['tab']
        rows = snapshot['rows']
        rng = snapshot.get('range', SNAPSHOT_RANGE)

        self.stdout.write(
            f"Restoring '{tab}' from {path.name} "
            f"({len(rows)} rows, snapshot taken {snapshot.get('timestamp')})")

        if dry_run:
            self.stdout.write(f"  [DRY-RUN] Would write {len(rows)} rows to "
                              f"'{tab}'!{rng}")
            # Show first 5 rows for verification
            self.stdout.write("  First 5 rows:")
            for i, r in enumerate(rows[:5]):
                self.stdout.write(f"    {i+1}: {r[:6]}{'...' if len(r) > 6 else ''}")
            return None

        client = get_sheets_client()
        # CLEAR + write — without clear, partial-row snapshots could leave
        # stale data on rows the snapshot didn't cover. Snapshots are full-tab.
        self.stdout.write(f"  Clearing '{tab}'!{rng}...")
        client.values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!{rng}",
        ).execute()

        self.stdout.write(f"  Writing {len(rows)} rows...")
        # USER_ENTERED preserves the formula-vs-value semantics of the snapshot
        client.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!{rng}",
            valueInputOption='USER_ENTERED',
            body={'values': rows},
        ).execute()
        self.stdout.write(f"  [✓] Restored '{tab}' from {path.name}")
        return None
