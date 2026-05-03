"""Migrate Synergy monthly tab(s) to Phase 4 layout (Sean 2026-05-03).

Why: Sheet F+G convention split. F was "Case Size" raw string ("12/5.3OZ");
becomes case-pack count (12). G was empty "Unit" column; becomes per-item
unit description ("5.3 oz Container"). New H = "Ea"/"#" count-or-weigh flag
read by inventory counter at month-end. On Hand / IUP / P/# shift right
one column to I / J / K.

Mechanics:
  1. Insert a new column at position H via Sheets API insertDimension.
     This is atomic + shifts existing H/I/J data → I/J/K automatically.
  2. Update header row 4 labels for the new layout.
  3. Re-write F (case_pack_count from latest ILI) per product row.
  4. Write G (inventory_unit_descriptor, blank for weighed) per product row.
  5. Write H ("Ea"/"#" derived from inventory_class) per product row.

Dry-run by default. --apply to commit. --tab "Synergy May 2026" to limit
to one tab (recommended for first run). --all-monthly to process every
"Synergy <Month> YYYY" tab.

Usage:
    python manage.py migrate_synergy_layout_v2 --tab "Synergy May 2026"
    python manage.py migrate_synergy_layout_v2 --tab "Synergy May 2026" --apply
    python manage.py migrate_synergy_layout_v2 --all-monthly --apply
"""
from __future__ import annotations

import os
import re
import sys

from django.conf import settings
from django.core.management.base import BaseCommand


_MONTHLY_TAB_RE = re.compile(
    r'^Synergy (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}$'
)


class Command(BaseCommand):
    help = "Migrate Synergy monthly tab(s) to Phase 4 layout (insert col H, populate F/G/H from DB)."

    def add_arguments(self, parser):
        parser.add_argument('--tab', help='Single tab name to migrate (e.g. "Synergy May 2026")')
        parser.add_argument('--all-monthly', action='store_true',
                            help='Migrate every Synergy monthly tab.')
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default dry-run).')
        parser.add_argument('--skip-insert', action='store_true',
                            help='Skip the column-insert step (use if already inserted).')

    def handle(self, *args, **opts):
        ip_dir = os.path.join(settings.BASE_DIR, 'invoice_processor')
        if ip_dir not in sys.path:
            sys.path.insert(0, ip_dir)
        from sheets import get_sheets_client
        from synergy_sync import (SPREADSHEET_ID, build_sheet_index)

        from myapp.models import Product, InvoiceLineItem

        apply_writes = opts['apply']
        skip_insert = opts['skip_insert']

        # Determine target tabs
        client = get_sheets_client()
        meta = client.get(spreadsheetId=SPREADSHEET_ID,
                          fields='sheets(properties(sheetId,title))').execute()
        all_tabs = [(s['properties']['title'], s['properties']['sheetId'])
                    for s in meta.get('sheets', [])]

        if opts['tab']:
            targets = [(t, sid) for t, sid in all_tabs if t == opts['tab']]
            if not targets:
                self.stderr.write(f'Tab not found: {opts["tab"]}')
                return
        elif opts['all_monthly']:
            targets = [(t, sid) for t, sid in all_tabs
                       if _MONTHLY_TAB_RE.match(t)]
        else:
            self.stderr.write('Specify --tab "<name>" or --all-monthly')
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== migrate_synergy_layout_v2 '
            f'({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'Targets ({len(targets)}):')
        for t, _ in targets:
            self.stdout.write(f'  - {t}')
        self.stdout.write('')

        for tab, sheet_id in targets:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n--- {tab} ---'))

            # Step 1: insert column at position H (index 7, 0-based)
            if not skip_insert:
                if apply_writes:
                    self.stdout.write('  [1/4] Inserting new column H...')
                    try:
                        client.batchUpdate(
                            spreadsheetId=SPREADSHEET_ID,
                            body={
                                'requests': [{
                                    'insertDimension': {
                                        'range': {
                                            'sheetId': sheet_id,
                                            'dimension': 'COLUMNS',
                                            'startIndex': 7,
                                            'endIndex': 8,
                                        },
                                        'inheritFromBefore': False,
                                    }
                                }]
                            }
                        ).execute()
                        self.stdout.write(self.style.SUCCESS('        ✓ inserted'))
                    except Exception as e:
                        self.stderr.write(f'  [!] insert failed: {e}')
                        continue
                else:
                    self.stdout.write('  [1/4] (dry-run) would insert column H')

            # Step 2: update header row 4 labels
            if apply_writes:
                self.stdout.write('  [2/4] Updating header row 4...')
                try:
                    client.values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"'{tab}'!E4:K4",
                        valueInputOption='USER_ENTERED',
                        body={'values': [[
                            'Case Price',  # E
                            'Case Size',   # F
                            'Unit',        # G
                            'Ea/#',        # H (new)
                            'On Hand',     # I (was H)
                            'IUP',         # J (was I)
                            'P/#',         # K (was J)
                        ]]},
                    ).execute()
                    self.stdout.write(self.style.SUCCESS('        ✓ headers updated'))
                except Exception as e:
                    self.stderr.write(f'  [!] header update failed: {e}')
            else:
                self.stdout.write('  [2/4] (dry-run) would update headers')

            # Step 3 + 4: backfill F/G/H per product row
            self.stdout.write('  [3-4/4] Backfilling F/G/H per product...')
            products, _ = build_sheet_index(tab)
            self.stdout.write(f'        {len(products)} product rows')

            batch_data = []
            n_set = 0
            for entry in products:
                product_name = entry['product']
                row_num = entry['row']
                p = Product.objects.filter(canonical_name__iexact=product_name).first()
                if not p:
                    continue

                # F = latest ILI's case_pack_count
                latest = (InvoiceLineItem.objects
                          .filter(product=p, case_pack_count__isnull=False)
                          .order_by('-invoice_date', '-imported_at').first())
                f_val = (latest.case_pack_count
                         if latest and latest.case_pack_count is not None else '')
                g_val = ('' if p.inventory_class == 'weighed'
                         else (p.inventory_unit_descriptor or ''))
                klass = p.inventory_class or ''
                h_val = '#' if klass == 'weighed' else ('Ea' if klass.startswith('counted_') else '')

                batch_data.append({'range': f"'{tab}'!F{row_num}",
                                   'values': [[f_val]]})
                batch_data.append({'range': f"'{tab}'!G{row_num}",
                                   'values': [[g_val]]})
                batch_data.append({'range': f"'{tab}'!H{row_num}",
                                   'values': [[h_val]]})
                n_set += 1

            if apply_writes and batch_data:
                try:
                    client.values().batchUpdate(
                        spreadsheetId=SPREADSHEET_ID,
                        body={'valueInputOption': 'USER_ENTERED', 'data': batch_data},
                    ).execute()
                    self.stdout.write(self.style.SUCCESS(
                        f'        ✓ wrote F/G/H for {n_set} rows'))
                except Exception as e:
                    self.stderr.write(f'  [!] backfill failed: {e}')
            else:
                self.stdout.write(f'        (dry-run) would write F/G/H for {n_set} rows')

        if not apply_writes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
