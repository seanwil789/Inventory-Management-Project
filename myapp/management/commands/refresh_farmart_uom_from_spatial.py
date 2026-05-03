"""Re-extract Farm Art purchase_uom + structured fields from cached OCR via
the spatial matcher (the only authoritative source for the actual invoice
U/M column).

Why: per `feedback_completeness.md` 2026-05-03 + the trust LAW. The actual
invoice U/M column distinguishes per-case ordering ("1 CASE of 4 gallons
Milk" → unit_price is case-level) from per-unit ordering ("1 GAL Shallot"
→ unit_price is per-gallon). Inferring U/M from raw_description multi-pack
text ("4/1GAL") is unreliable — the same pattern means different things
for Milk vs Shallot. Only the actual U/M column is trustworthy.

`spatial_matcher.match_farmart_spatial` reads the U/M column when DocAI's
bbox layout is reliable. This command runs ONLY that path against the
OCR cache + updates existing ILIs' purchase_uom + case_pack_* fields.
Does NOT touch product FK or match_confidence (preserves Sean's manual
review approvals).

Strategy:
  1. Walk every Farm Art OCR cache file in .ocr_cache/.
  2. Run match_farmart_spatial(pages) on each.
  3. For each spatial item, find existing ILI by (vendor, raw, date).
  4. Update purchase_uom + case_pack_* + case_total_weight_lb from spatial.

--reset clears all Farm Art purchase_uom BEFORE re-extracting (used to
undo the corrupted 2026-05-03 backfill_farmart_purchase_uom inference).
Without --reset, only sets purchase_uom on rows where it's currently
empty (preserve_if_none semantic).

Usage:
    python manage.py refresh_farmart_uom_from_spatial               # dry-run
    python manage.py refresh_farmart_uom_from_spatial --apply
    python manage.py refresh_farmart_uom_from_spatial --apply --reset
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Re-extract Farm Art purchase_uom from cached OCR via spatial matcher."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default is dry-run).')
        parser.add_argument('--reset', action='store_true',
                            help='Clear all Farm Art purchase_uom BEFORE '
                                 're-extracting. Use to undo prior corrupt '
                                 'backfill. Implies --apply for the clear.')

    def handle(self, *args, **opts):
        ip_dir = os.path.join(settings.BASE_DIR, 'invoice_processor')
        if ip_dir not in sys.path:
            sys.path.insert(0, ip_dir)
        from spatial_matcher import match_farmart_spatial

        apply_writes = opts['apply']
        reset = opts['reset']

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== refresh_farmart_uom_from_spatial '
            f'({"APPLY" if apply_writes else "DRY-RUN"}'
            f'{" + RESET" if reset else ""}) ===\n'
        ))

        # Phase 0: optional reset
        if reset:
            qs_reset = InvoiceLineItem.objects.filter(
                vendor__name='Farm Art').exclude(purchase_uom='')
            n_reset = qs_reset.count()
            self.stdout.write(f'Reset target: {n_reset} Farm Art ILIs with '
                              f'purchase_uom set.')
            if apply_writes:
                qs_reset.update(purchase_uom='')
                self.stdout.write(self.style.WARNING(
                    f'  Cleared {n_reset} purchase_uom values.'))
            else:
                self.stdout.write('  (dry-run — would clear)')
            self.stdout.write('')

        # Phase 1: walk OCR cache
        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not ocr_dir.exists():
            self.stderr.write(f'OCR cache not found at {ocr_dir}')
            return

        cache_files = sorted(ocr_dir.glob('*_docai_ocr.json'))
        farmart_caches = []
        for p in cache_files:
            try:
                with open(p) as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if cache.get('vendor') == 'Farm Art':
                farmart_caches.append((p, cache))

        self.stdout.write(f'Farm Art cache files: {len(farmart_caches)}')
        if not farmart_caches:
            self.stdout.write('No Farm Art OCR cache to process.')
            return

        # Phase 2: run spatial on each, match to existing ILIs
        spatial_extracted = 0
        spatial_failed = 0
        ili_updated = 0
        ili_unchanged = 0
        ili_not_found = 0
        per_uom = defaultdict(int)

        for path, cache in farmart_caches:
            pages = cache.get('pages') or []
            invoice_date = cache.get('invoice_date', '')
            if not pages or not invoice_date:
                spatial_failed += 1
                continue
            try:
                items = match_farmart_spatial(pages)
            except Exception as e:
                self.stderr.write(f'  [!] spatial failed on {path.name}: {e}')
                spatial_failed += 1
                continue
            if not items:
                spatial_failed += 1
                continue
            spatial_extracted += len(items)

            for item in items:
                raw = item.get('raw_description', '')
                uom = (item.get('purchase_uom') or item.get('unit_of_measure') or '').upper()
                if not (raw and uom):
                    continue
                # Match existing ILI by (vendor='Farm Art', raw, invoice_date)
                ili = (InvoiceLineItem.objects
                       .filter(vendor__name='Farm Art',
                               raw_description=raw,
                               invoice_date=invoice_date)
                       .first())
                if not ili:
                    ili_not_found += 1
                    continue
                # preserve_if_none: only set when currently empty (unless --reset already cleared)
                if ili.purchase_uom and ili.purchase_uom == uom:
                    ili_unchanged += 1
                    continue
                if ili.purchase_uom and not reset:
                    # Has different value; preserve unless --reset cleared first
                    ili_unchanged += 1
                    continue
                # Update purchase_uom + structured pack fields + per-unit
                # price/qty from spatial. Sean 2026-05-03: unit_price was
                # being stored as the line amount (extended) rather than
                # the actual U/P column value — fixed in spatial_matcher
                # and corrected here for historical rows.
                if apply_writes:
                    from decimal import Decimal
                    ili.purchase_uom = uom
                    if item.get('case_pack_count') is not None:
                        ili.case_pack_count = item['case_pack_count']
                    if item.get('case_pack_unit_size') is not None:
                        ili.case_pack_unit_size = item['case_pack_unit_size']
                    if item.get('case_pack_unit_uom'):
                        ili.case_pack_unit_uom = item['case_pack_unit_uom']
                    if item.get('case_total_weight_lb') is not None:
                        ili.case_total_weight_lb = item['case_total_weight_lb']
                    # Price + qty fields — only populated by spatial when
                    # the corresponding columns extracted cleanly. None
                    # values preserved (don't clobber existing).
                    if item.get('unit_price') is not None:
                        ili.unit_price = Decimal(str(item['unit_price']))
                    if item.get('extended_amount') is not None:
                        ili.extended_amount = Decimal(str(item['extended_amount']))
                    if item.get('quantity') is not None:
                        ili.quantity = Decimal(str(item['quantity']))
                    ili.save(update_fields=[
                        'purchase_uom', 'case_pack_count',
                        'case_pack_unit_size', 'case_pack_unit_uom',
                        'case_total_weight_lb',
                        'unit_price', 'extended_amount', 'quantity',
                    ])
                ili_updated += 1
                per_uom[uom] += 1

        self.stdout.write('')
        self.stdout.write(f'Spatial extractions:    {spatial_extracted}')
        self.stdout.write(f'Spatial failures:       {spatial_failed}')
        self.stdout.write(f'ILIs updated:           {ili_updated}')
        self.stdout.write(f'ILIs unchanged:         {ili_unchanged}')
        self.stdout.write(f'Spatial item not in DB: {ili_not_found}')
        self.stdout.write('')
        self.stdout.write('Updates by U/M value:')
        for u, n in sorted(per_uom.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {u:<10} {n}')

        if not apply_writes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
