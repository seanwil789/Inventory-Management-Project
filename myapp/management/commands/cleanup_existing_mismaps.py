"""Detach Product FKs from existing ILI rows that Phase 3e would reject.

Phase 3e (`_is_class_mismatch` in `invoice_processor/db_write.py`) prevents
class-mismatched FK attaches at WRITE time. But pre-Phase-3e historical
rows already exist in the DB — running reprocess only detaches them when
the parser produces an upsert-key-matching row, and it doesn't always.

This command sweeps current ILI rows, runs them through the same guard,
and detaches the FK from any row the guard would reject. Tags rows as
'unmatched_class_mismatch' so they surface in the unmapped queue + the
mapping-review UI for re-curation.

Usage:
    python manage.py cleanup_existing_mismaps                # dry-run
    python manage.py cleanup_existing_mismaps --apply        # commit
    python manage.py cleanup_existing_mismaps --apply --vendor Sysco
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem
from invoice_processor.db_write import _is_class_mismatch


class Command(BaseCommand):
    help = "Detach FKs from existing class-mismatched ILI rows."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit detaches (default is dry-run preview).')
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor (e.g. Sysco).')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        vendor_filter = opts['vendor']

        qs = (InvoiceLineItem.objects
              .exclude(product=None)
              .select_related('product', 'vendor'))
        if vendor_filter:
            qs = qs.filter(vendor__name=vendor_filter)

        hits = []
        for ili in qs:
            if _is_class_mismatch(ili.product, ili.raw_description or '',
                                  ili.case_size or ''):
                hits.append(ili)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== cleanup_existing_mismaps ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))

        for ili in hits:
            self.stdout.write(
                f'  ID={ili.id:5d} {ili.invoice_date} {ili.vendor.name if ili.vendor else "?":20s}'
            )
            self.stdout.write(f'    raw={ili.raw_description[:70]!r}')
            self.stdout.write(
                f'    → {ili.product.canonical_name!r} '
                f'[{ili.product.inventory_class}]  cs={ili.case_size!r}'
            )

        self.stdout.write('')
        self.stdout.write(f'Class-mismatch hits: {len(hits)}')

        if apply_writes and hits:
            updated = (InvoiceLineItem.objects
                       .filter(id__in=[i.id for i in hits])
                       .update(product=None,
                               match_confidence='unmatched_class_mismatch'))
            self.stdout.write(self.style.SUCCESS(
                f'Detached FK from {updated} rows; tagged as unmatched_class_mismatch.'
            ))
        elif not apply_writes and hits:
            self.stdout.write(self.style.WARNING(
                'Dry-run — re-run with --apply to commit.'
            ))
