"""Replay OCR cache through the current parser + mapper and upsert to DB.

Purpose: historical invoice rows in the DB reflect OLD parser output —
pre-Gap-1/2/3 fixes, pre-extended_amount storage. This command re-parses
every cached invoice through the current code path and upserts the results,
bringing product-level $ attribution from ~68% to ~92%+.

Safe to re-run — the upsert in db_write.write_invoice_to_db keys off
(vendor, product, date) or (vendor, raw_description, date) so repeat
runs replace existing rows rather than duplicating.

Usage:
  python manage.py reprocess_invoices                    # all OCR caches
  python manage.py reprocess_invoices --month 2026 4     # single month
  python manage.py reprocess_invoices --year 2026        # one year
  python manage.py reprocess_invoices --dry-run          # preview only
"""
from __future__ import annotations

import glob
import json
import os
import sys
import traceback
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
from parser import parse_invoice  # noqa: E402
from mapper import load_mappings, map_items  # noqa: E402
from db_write import write_invoice_to_db  # noqa: E402


class Command(BaseCommand):
    help = 'Re-parse + re-map + upsert every cached invoice through the current code.'

    def add_arguments(self, parser):
        parser.add_argument('--month', nargs=2, type=int, metavar=('YEAR', 'MONTH'),
                            help='Reprocess a specific month only')
        parser.add_argument('--year', type=int,
                            help='Reprocess every cache from a specific year')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse + map but do not write to DB')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to one vendor (e.g. "Sysco")')

    def handle(self, *args, **opts):
        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not ocr_dir.exists():
            self.stderr.write(f'OCR cache not found at {ocr_dir}')
            return

        caches = []
        for p in glob.glob(str(ocr_dir / '*_docai_ocr.json')):
            try:
                with open(p) as f:
                    d = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self.stderr.write(f'skip {os.path.basename(p)}: {e}')
                continue

            inv_date = d.get('invoice_date', '')
            vendor = d.get('vendor', 'Unknown')

            if opts['vendor'] and vendor != opts['vendor']:
                continue
            if opts['year'] and not inv_date.startswith(f"{opts['year']}"):
                continue
            if opts['month']:
                y, m = opts['month']
                if not inv_date.startswith(f'{y:04d}-{m:02d}'):
                    continue
            caches.append((p, d))

        if not caches:
            self.stdout.write('No matching caches found.')
            return

        self.stdout.write(f'Reprocessing {len(caches)} OCR cache(s)...')

        # Load mappings ONCE — expensive if it hits Sheets; subsequent calls
        # inside map_items will reuse the pre-loaded dict.
        self.stdout.write('Loading mappings...')
        mappings = load_mappings()
        self.stdout.write(
            f"  {len(mappings.get('desc_map', {}))} description mappings, "
            f"{len(mappings.get('code_map', {}))} code mappings loaded")

        total_items = 0
        total_written = 0
        total_unmapped = 0
        failures = 0

        for i, (path, cache) in enumerate(caches, 1):
            vendor = cache.get('vendor', 'Unknown')
            raw_text = cache.get('raw_text', '')
            invoice_date = cache.get('invoice_date', '')
            fname = os.path.basename(path)

            try:
                parsed = parse_invoice(raw_text, vendor=vendor,
                                       pages=cache.get('pages'))
                items = parsed.get('items', [])
                if not items:
                    continue
                mapped = map_items(items, mappings=mappings, vendor=vendor)
                unmapped = sum(1 for m in mapped if m.get('confidence') == 'unmatched')
                total_items += len(mapped)
                total_unmapped += unmapped

                if not opts['dry_run']:
                    # Preserve original filename in source_file when available.
                    # OCR cache filenames are content hashes; use hash prefix as a
                    # stable provenance token — the existing live-pipeline rows
                    # with real filenames are protected by the upsert key.
                    source_token = fname.split('_')[0][:16]
                    written = write_invoice_to_db(
                        vendor_name=vendor,
                        invoice_date=invoice_date,
                        items=mapped,
                        source_file=source_token,
                    )
                    total_written += written

                if i % 20 == 0 or i == len(caches):
                    self.stdout.write(
                        f'  [{i}/{len(caches)}] {vendor} {invoice_date}: '
                        f'{len(mapped)} items ({unmapped} unmapped)')

            except Exception as e:
                failures += 1
                self.stderr.write(f'  [!] {fname}: {e}')
                if opts.get('verbosity', 1) >= 2:
                    traceback.print_exc()

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: {len(caches)} caches, {total_items} parsed items, '
            f'{total_unmapped} unmapped ({total_unmapped/total_items*100:.1f}% if total)'
            if total_items else f'\nDone: {len(caches)} caches, 0 items'))
        if not opts['dry_run']:
            self.stdout.write(f'Rows upserted: {total_written}')
        if failures:
            self.stdout.write(self.style.WARNING(f'Failures: {failures}'))
