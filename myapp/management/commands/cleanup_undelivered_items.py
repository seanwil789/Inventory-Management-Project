"""Delete InvoiceLineItem rows that represent out-of-stock / never-delivered
orders. Sean (2026-05-02): Farm Art uses "zz " prefix for items that were
ordered but didn't ship; they appear on the invoice paperwork with qty=0
and unit_price=0. Pre-fix parser created ILI rows for them, distorting
cost coverage + sheet IUP averaging.

Targets:
  * raw_description starts with "zz " (literal preserved prefix)
  * unit_price == 0 AND extended_amount == 0  (didn't ship signal)

Vendor-scoped to Farm Art by default — other vendors may have legitimate
zero-price rows (Sysco freight credits, fuel surcharges).

Usage:
    python manage.py cleanup_undelivered_items                # dry-run
    python manage.py cleanup_undelivered_items --apply
    python manage.py cleanup_undelivered_items --vendor Sysco --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Delete out-of-stock / never-delivered ILI rows."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit deletes (default is dry-run).')
        parser.add_argument('--vendor', default='Farm Art',
                            help='Limit to one vendor (default: Farm Art).')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        vendor_filter = opts['vendor']

        criteria = (
            Q(raw_description__istartswith='zz ')
            | (Q(unit_price=0) & Q(extended_amount=0))
        )
        qs = InvoiceLineItem.objects.filter(criteria)
        if vendor_filter:
            qs = qs.filter(vendor__name=vendor_filter)

        rows = list(qs.select_related('product', 'vendor'))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== cleanup_undelivered_items '
            f'({"APPLY" if apply_writes else "DRY-RUN"}) — vendor={vendor_filter} ===\n'
        ))
        for ili in rows:
            prod = ili.product.canonical_name if ili.product else None
            self.stdout.write(
                f'  ID={ili.id:5d} {ili.invoice_date} qty={ili.quantity} '
                f'up={ili.unit_price} → {prod}'
            )
            self.stdout.write(f'    raw={(ili.raw_description or "")[:70]!r}')

        self.stdout.write('')
        self.stdout.write(f'Eligible: {len(rows)}')

        if apply_writes and rows:
            ids = [r.id for r in rows]
            deleted, _ = InvoiceLineItem.objects.filter(id__in=ids).delete()
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {deleted} undelivered rows.'
            ))
        elif rows:
            self.stdout.write(self.style.WARNING(
                'Dry-run — re-run with --apply to commit.'
            ))
