"""Multi-photo-aware OCR cache replay.

Fix #3 of the parser min_items / multi-photo gap (memo:
project_urgent_parser_min_items_fix.md). Groups cached OCR pages by
(vendor, invoice_number) and replays each group as ONE document instead
of per-photo. Eliminates the per-page <1 item drop that even Fix #1's
min_items=1 floor can't recover when an individual page yields zero
items but the merged document yields many.

Differs from `reprocess_invoices` which processes each cache file
independently (one cache → one parse → one set of ILIs). For
single-photo invoices the two commands are equivalent. For multi-photo
invoices (Sysco's Mon/Tue paper-page workflow), this command is the
correct path: concat raw_text + extend pages tokens BEFORE the parser
runs, so a 5-page invoice produces ONE merged parse with all rows
captured even when individual pages would have been rejected.

Idempotent — db_write upserts on (vendor, product, date) or
(vendor, raw_description, date), and Track C orphan cleanup deletes
stale [Sysco #NNN] placeholders when real descriptions arrive.

Usage:
  python manage.py reprocess_ocr_cache                    # current month, all vendors
  python manage.py reprocess_ocr_cache --month 2026 4     # April 2026
  python manage.py reprocess_ocr_cache --month 2026 4 --vendor Sysco
  python manage.py reprocess_ocr_cache --month 2026 4 --dry-run
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
from parser import parse_invoice, extract_sysco_metadata  # noqa: E402
from mapper import load_mappings, map_items  # noqa: E402
from db_write import write_invoice_to_db  # noqa: E402


def _extract_invoice_number(raw_text: str, vendor: str) -> str | None:
    """Best-effort invoice-number extraction per vendor. Mirrors the
    helper in refresh_invoice_totals so multi-photo grouping uses the
    same key the totals cache groups on."""
    if not raw_text:
        return None
    if vendor == 'Sysco':
        meta = extract_sysco_metadata(raw_text)
        return meta.get('invoice_number') or meta.get('manifest')
    if vendor in ('Farm Art', 'Exceptional Foods',
                  'Philadelphia Bakery Merchants'):
        m = re.search(r'Invoice\s*(?:No\.?|Number|#)?\s*[:\n]?\s*(\d{4,10})',
                      raw_text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


class Command(BaseCommand):
    help = ('Replay OCR cache through current parser, grouping multi-photo '
            'invoices by (vendor, invoice_number) so each physical invoice '
            'parses as one merged document.')

    def add_arguments(self, parser):
        parser.add_argument('--month', nargs=2, type=int,
                            metavar=('YEAR', 'MONTH'),
                            help='Reprocess a specific month only')
        parser.add_argument('--year', type=int,
                            help='Reprocess every cache from a specific year')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to one vendor (e.g. "Sysco")')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse + map but do not write to DB')

    def handle(self, *args, **opts):
        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not ocr_dir.exists():
            self.stderr.write(f'OCR cache not found at {ocr_dir}')
            return

        # Pass 1: load every matching cache
        caches: list[tuple[str, dict]] = []
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

        # Pass 2: group by (vendor, invoice_number). Caches without an
        # extractable invoice_number stay as their own group keyed by
        # (vendor, date, file_hash) so they're processed standalone.
        groups: dict[tuple, list[tuple[str, dict]]] = defaultdict(list)
        for path, cache in caches:
            vendor = cache.get('vendor', 'Unknown')
            inv_num = _extract_invoice_number(cache.get('raw_text', ''), vendor)
            if inv_num:
                key = ('num', vendor, inv_num)
            else:
                fhash = os.path.basename(path).split('_')[0][:16]
                key = ('file', vendor, cache.get('invoice_date', ''), fhash)
            groups[key].append((path, cache))

        merged_groups = sum(1 for g in groups.values() if len(g) > 1)
        self.stdout.write(
            f'Loaded {len(caches)} cache(s) → {len(groups)} invoice group(s) '
            f'({merged_groups} multi-photo merged)'
        )

        # Load mappings once
        self.stdout.write('Loading mappings...')
        mappings = load_mappings()
        self.stdout.write(
            f"  {len(mappings.get('desc_map', {}))} description mappings, "
            f"{len(mappings.get('code_map', {}))} code mappings loaded"
        )

        total_items = 0
        total_written = 0
        total_unmapped = 0
        failures = 0

        # Pass 3: for each group, merge + parse + map + write
        sorted_keys = sorted(groups.keys())
        for i, key in enumerate(sorted_keys, 1):
            group = groups[key]
            vendor = group[0][1].get('vendor', 'Unknown')
            invoice_date = group[0][1].get('invoice_date', '')
            inv_num_label = key[2] if key[0] == 'num' else '(no num)'

            if len(group) == 1:
                merged_raw = group[0][1].get('raw_text', '')
                merged_pages = group[0][1].get('pages', []) or []
                source_token = os.path.basename(group[0][0]).split('_')[0][:16]
            else:
                # Multi-photo merge: concat raw_text with separators so the
                # parser sees clean line breaks between photos. Pages list
                # extends — each photo contributes its own page dict with
                # its own tokens, so spatial matchers iterate across all.
                merged_raw = '\n\n'.join(
                    c.get('raw_text', '') for _, c in group
                )
                merged_pages = []
                for _, c in group:
                    p_list = c.get('pages', []) or []
                    merged_pages.extend(p_list)
                # Use first cache's hash; tag with group size for provenance
                first_hash = os.path.basename(group[0][0]).split('_')[0][:16]
                source_token = f'{first_hash}+{len(group)-1}'

            try:
                parsed = parse_invoice(merged_raw, vendor=vendor,
                                       pages=merged_pages)
                items = parsed.get('items', [])
                if not items:
                    if len(group) > 1:
                        self.stdout.write(
                            f'  [-] {vendor} #{inv_num_label} '
                            f'({len(group)} photos): 0 items even merged'
                        )
                    continue

                mapped = map_items(items, mappings=mappings, vendor=vendor)
                unmapped_count = sum(
                    1 for m in mapped if m.get('confidence') == 'unmatched'
                )
                total_items += len(mapped)
                total_unmapped += unmapped_count

                if not opts['dry_run']:
                    written = write_invoice_to_db(
                        vendor_name=vendor,
                        invoice_date=invoice_date,
                        items=mapped,
                        source_file=source_token,
                    )
                    total_written += written

                if len(group) > 1 or i % 20 == 0 or i == len(sorted_keys):
                    tag = (f'[{len(group)}-photo merge] '
                           if len(group) > 1 else '')
                    self.stdout.write(
                        f'  [{i}/{len(sorted_keys)}] {tag}{vendor} '
                        f'#{inv_num_label} {invoice_date}: '
                        f'{len(mapped)} items ({unmapped_count} unmapped)'
                    )

            except Exception as e:
                failures += 1
                self.stderr.write(f'  [!] {vendor} #{inv_num_label}: {e}')
                if opts.get('verbosity', 1) >= 2:
                    traceback.print_exc()

        pct = (total_unmapped / total_items * 100) if total_items else 0
        self.stdout.write(self.style.SUCCESS(
            f'\nDone: {len(sorted_keys)} groups, {total_items} parsed items, '
            f'{total_unmapped} unmapped ({pct:.1f}%)'
        ))
        if not opts['dry_run']:
            self.stdout.write(f'Rows upserted: {total_written}')
        if failures:
            self.stdout.write(self.style.WARNING(f'Failures: {failures}'))
