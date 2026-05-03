"""Retroactive backfill: re-extract purchase_uom (and corrected
case_pack_count for count-units) for Farm Art ILIs from raw_description.

Closes the 81% Farm Art purchase_uom gap that pre-dated the parser fix
landing on 2026-05-03 (commit adds purchase_uom emit to
`_extract_farmart_pack`). Without this backfill, historical Farm Art
rows have purchase_uom='' and the synergy_sync writer falls back to
inventory_class instead of using the per-line U/M signal.

Also corrects the case_pack_count count/size swap for count-unit rows
('9CT' was stored as count=1, size=9 — corrected here to count=9, size=1).

Strategy:
  1. Walk Farm Art ILIs whose raw_description has extractable pack info.
  2. Run `_extract_farmart_pack(raw_description)` — same logic as the
     live parser writes for new invoices.
  3. Update purchase_uom + case_pack_count + case_pack_unit_size +
     case_pack_unit_uom + case_total_weight_lb where the new value
     differs from current.

Idempotent — re-runs no-op on rows already matching.

Usage:
    python manage.py backfill_farmart_purchase_uom               # dry-run
    python manage.py backfill_farmart_purchase_uom --apply
"""
from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Backfill Farm Art purchase_uom + corrected case_pack_count from raw_description."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default is dry-run).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after N updates (default: no limit).')

    def handle(self, *args, **opts):
        ip_dir = os.path.join(settings.BASE_DIR, 'invoice_processor')
        if ip_dir not in sys.path:
            sys.path.insert(0, ip_dir)
        from parser import _extract_farmart_pack

        apply_writes = opts['apply']
        limit = opts['limit'] or float('inf')

        qs = (InvoiceLineItem.objects
              .filter(vendor__name='Farm Art')
              .exclude(raw_description=''))

        total = qs.count()
        examined = 0
        updated_uom = 0
        updated_pack = 0
        unchanged = 0
        not_extractable = 0

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== backfill_farmart_purchase_uom '
            f'({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'Farm Art ILIs scanned: {total}')

        for ili in qs.iterator():
            if examined >= limit:
                break
            examined += 1
            extracted = _extract_farmart_pack(ili.raw_description)
            if not extracted:
                not_extractable += 1
                continue

            new_uom = extracted.get('unit_of_measure', '')
            new_count = extracted.get('case_pack_count')
            new_size = extracted.get('case_pack_unit_size')
            new_uom_unit = extracted.get('case_pack_unit_uom', '')
            new_weight = extracted.get('case_total_weight_lb')

            uom_changed = new_uom and ili.purchase_uom != new_uom
            count_changed = new_count is not None and ili.case_pack_count != new_count
            size_changed = new_size is not None and str(ili.case_pack_unit_size or '') != str(new_size)

            if not (uom_changed or count_changed or size_changed):
                unchanged += 1
                continue

            if uom_changed:
                updated_uom += 1
            if count_changed or size_changed:
                updated_pack += 1

            if examined <= 20 or (uom_changed and updated_uom <= 30):
                old_uom = ili.purchase_uom or '(blank)'
                old_count = ili.case_pack_count if ili.case_pack_count is not None else '(none)'
                old_size = ili.case_pack_unit_size if ili.case_pack_unit_size is not None else '(none)'
                self.stdout.write(
                    f'  ID={ili.id:5d} {ili.invoice_date} {ili.raw_description[:50]!r:<52}'
                )
                self.stdout.write(
                    f'        uom: {old_uom!r:<8} → {new_uom!r:<8}  '
                    f'pack: ({old_count}, {old_size}, {ili.case_pack_unit_uom!r}) → '
                    f'({new_count}, {new_size}, {new_uom_unit!r})'
                )

            if apply_writes:
                ili.purchase_uom = new_uom or ili.purchase_uom
                if new_count is not None:
                    ili.case_pack_count = new_count
                if new_size is not None:
                    ili.case_pack_unit_size = new_size
                if new_uom_unit:
                    ili.case_pack_unit_uom = new_uom_unit
                if new_weight is not None:
                    ili.case_total_weight_lb = new_weight
                ili.save(update_fields=[
                    'purchase_uom', 'case_pack_count', 'case_pack_unit_size',
                    'case_pack_unit_uom', 'case_total_weight_lb',
                ])

        self.stdout.write('')
        self.stdout.write(f'Examined:                {examined}')
        self.stdout.write(f'Updated purchase_uom:    {updated_uom}')
        self.stdout.write(f'Updated case_pack_*:     {updated_pack}')
        self.stdout.write(f'Unchanged:               {unchanged}')
        self.stdout.write(f'Not extractable:         {not_extractable}')
        if not apply_writes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
