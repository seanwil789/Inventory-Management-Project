"""Retroactive backfill: decompose existing ILI.case_size strings into
structured fields for rows that never went through the new db_write
fallback path.

Catches rows that were written BEFORE the 2026-05-02 db_write structured
fallback landed (Colonial handwritten invoices in particular — they have
no OCR cache to replay through reprocess_ocr_cache).

Strategy:
  1. Find ILI rows with case_size populated but case_pack_count IS NULL.
  2. Run _structured_pack_from_case_size on the case_size string.
  3. If decomposable, populate the structured fields.
  4. Skip rows whose case_size doesn't match any pack regex.

Idempotent — re-runs do nothing on rows already populated.

Usage:
    python manage.py backfill_structured_from_case_size               # dry-run
    python manage.py backfill_structured_from_case_size --apply
    python manage.py backfill_structured_from_case_size --vendor "Colonial..."
"""
from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Retro-decompose ILI.case_size strings into structured fields."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default is dry-run).')
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor.')

    def handle(self, *args, **opts):
        # Lazy-import the helper from invoice_processor.
        ip_dir = os.path.join(settings.BASE_DIR, 'invoice_processor')
        if ip_dir not in sys.path:
            sys.path.insert(0, ip_dir)
        from parser import _structured_pack_from_case_size

        apply_writes = opts['apply']
        vendor_filter = opts['vendor']

        # Eligible: case_size populated, case_pack_count NULL
        qs = (InvoiceLineItem.objects
              .exclude(case_size='')
              .filter(case_pack_count__isnull=True))
        if vendor_filter:
            qs = qs.filter(vendor__name__icontains=vendor_filter)

        total = qs.count()
        decomposed = skipped = 0
        per_vendor = {}

        for ili in qs.iterator():
            fallback = _structured_pack_from_case_size(ili.case_size)
            v = ili.vendor.name if ili.vendor else '(none)'
            per_vendor.setdefault(v, [0, 0])
            if fallback and fallback.get('case_pack_count') is not None:
                decomposed += 1
                per_vendor[v][0] += 1
                if apply_writes:
                    ili.case_pack_count = fallback['case_pack_count']
                    ili.case_pack_unit_size = fallback.get('case_pack_unit_size')
                    ili.case_pack_unit_uom = fallback.get('case_pack_unit_uom') or ''
                    if ili.case_total_weight_lb is None:
                        ili.case_total_weight_lb = fallback.get('case_total_weight_lb')
                    ili.save(update_fields=[
                        'case_pack_count', 'case_pack_unit_size',
                        'case_pack_unit_uom', 'case_total_weight_lb',
                    ])
            else:
                skipped += 1
                per_vendor[v][1] += 1

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== backfill_structured_from_case_size '
            f'({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        for v, (d, s) in sorted(per_vendor.items()):
            self.stdout.write(f'  {v[:30]:30s} decomposed: {d:>4}  skipped: {s:>4}')
        self.stdout.write('')
        self.stdout.write(f'Total eligible:  {total}')
        self.stdout.write(f'Decomposed:      {decomposed}')
        self.stdout.write(f'Skipped (cs not decomposable): {skipped}')
        if not apply_writes and decomposed:
            self.stdout.write(self.style.WARNING(
                'Dry-run — re-run with --apply to commit.'))
        elif apply_writes:
            self.stdout.write(self.style.SUCCESS(
                f'Updated {decomposed} rows.'))
