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

        # Group caches by (vendor, invoice_number) so multi-page invoices
        # parse as a single combined invoice — without this, each cache
        # parses in isolation and continuation pages lose section
        # attribution (their items become unsectioned because the section
        # header was on a different cache and section carry can't cross
        # cache boundaries). Origin: 2026-05-17 — INV 775632629 had 26
        # CANNED & DRY items mis-attributed because reprocess parsed each
        # of its 4 caches separately. validate_all_invoices was already
        # doing this grouping; reprocess wasn't.
        from collections import defaultdict
        from contextlib import redirect_stdout
        import io
        from section_validator import cache_page_order_key

        groups: dict[tuple, list] = defaultdict(list)
        for path, cache in caches:
            vendor = cache.get('vendor', 'Unknown')
            # Quick-extract invoice_number by parsing each cache alone
            # (cheap — needed only for grouping; actual parse happens once
            # per group below).
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    p_solo = parse_invoice(cache.get('raw_text', ''),
                                            vendor=vendor,
                                            pages=cache.get('pages'))
                inv_num = p_solo.get('invoice_number') or ''
            except Exception:
                inv_num = ''
            if not inv_num:
                # Fall back to per-cache processing for unknown invoice
                inv_num = f'__SOLO__{os.path.basename(path)}'
            groups[(vendor, inv_num)].append((path, cache))

        self.stdout.write(f'Grouped into {len(groups)} invoices.')

        idx = 0
        for (vendor, inv_num), entries in sorted(groups.items()):
            idx += 1
            # Sort caches by physical page order so section carry works
            entries = sorted(
                entries,
                key=lambda e: (cache_page_order_key(e[1].get('raw_text', '')),
                               os.path.basename(e[0])),
            )
            combined_text = '\n'.join(e[1].get('raw_text', '') for e in entries)
            combined_pages = []
            for path, cache in entries:
                combined_pages.extend(cache.get('pages') or [])
            invoice_date = entries[0][1].get('invoice_date', '')

            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    parsed = parse_invoice(combined_text, vendor=vendor,
                                            pages=combined_pages)
                items = parsed.get('items', [])
                if not items:
                    continue
                mapped = map_items(items, mappings=mappings, vendor=vendor)
                unmapped = sum(1 for m in mapped if m.get('confidence') == 'unmatched')
                total_items += len(mapped)
                total_unmapped += unmapped

                if not opts['dry_run']:
                    # Use first cache's sha as the source token (stable
                    # provenance across the combined-parse upsert).
                    source_token = os.path.basename(entries[0][0]).split('_')[0][:16]
                    written = write_invoice_to_db(
                        vendor_name=vendor,
                        invoice_date=invoice_date,
                        items=mapped,
                        source_file=source_token,
                        invoice_number=parsed.get('invoice_number') or '',
                    )
                    total_written += written

                if idx % 20 == 0 or idx == len(groups):
                    self.stdout.write(
                        f'  [{idx}/{len(groups)}] {vendor} {invoice_date}: '
                        f'{len(mapped)} items ({unmapped} unmapped)')

            except Exception as e:
                failures += 1
                self.stderr.write(f'  [!] {vendor} {inv_num}: {e}')
                if opts.get('verbosity', 1) >= 2:
                    traceback.print_exc()

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: {len(caches)} caches grouped into {len(groups)} invoices, '
            f'{total_items} parsed items, '
            f'{total_unmapped} unmapped ({total_unmapped/total_items*100:.1f}%)'
            if total_items else f'\nDone: {len(caches)} caches, 0 items'))
        if not opts['dry_run']:
            self.stdout.write(f'Rows upserted: {total_written}')
        if failures:
            self.stdout.write(self.style.WARNING(f'Failures: {failures}'))
