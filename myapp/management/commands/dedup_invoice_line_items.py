"""Dedup InvoiceLineItem rows where (vendor, invoice_date, raw_description,
unit_price, extended_amount) match across multiple rows.

Surfaced 2026-05-02: 304 dup groups on Pi (312 inflated rows). Root cause:
db_write upsert lookup uses (vendor, product, invoice_date) when product
is set at write time. When fuzzy-quarantine creates the row with
product=None, then a later /mapping-review/ approval attaches the FK,
the next reprocess lookup with (vendor, product, date) doesn't find the
original row → CREATE produces a duplicate.

This command sweeps existing dups. Per-group strategy:
  1. Same (vendor, date, raw, unit_price, ext) → TRUE duplicate.
     Keep the row with most structured-field coverage; tie-break by
     lowest id (oldest survives, preserves provenance). Delete others.
  2. Same (vendor, date, raw) but DIFFERENT (unit_price, ext) →
     legitimate price-differ (parser produced multiple parses, only
     one is right). Surface for review, do NOT auto-delete.

--dry-run is default; --apply commits.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count

from myapp.models import InvoiceLineItem


_STRUCTURED_FIELDS = (
    'quantity', 'purchase_uom', 'case_pack_count', 'case_pack_unit_size',
    'case_pack_unit_uom', 'case_total_weight_lb',
    'count_per_lb_low', 'count_per_lb_high', 'price_per_pound',
)


def _structured_score(ili: InvoiceLineItem) -> int:
    """Count populated structured fields. Higher = better — survives merge."""
    return sum(
        1 for f in _STRUCTURED_FIELDS
        if getattr(ili, f, None) not in (None, '')
    )


class Command(BaseCommand):
    help = 'Dedup InvoiceLineItem rows where vendor+date+raw+price+ext match.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit deletes (default is dry-run).')
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor.')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        vendor_filter = opts['vendor']

        qs = InvoiceLineItem.objects.exclude(product=None)
        if vendor_filter:
            qs = qs.filter(vendor__name=vendor_filter)

        # True-dup groups: identical (vendor, date, raw, unit_price, ext)
        true_dup_keys = (qs.values(
            'vendor__name', 'invoice_date', 'raw_description',
            'unit_price', 'extended_amount',
        ).annotate(n=Count('id')).filter(n__gt=1))

        # Mixed-price groups: same (vendor, date, raw) but different prices
        same_raw = (qs.values(
            'vendor__name', 'invoice_date', 'raw_description',
        ).annotate(n=Count('id')).filter(n__gt=1))
        true_set = {(d['vendor__name'], d['invoice_date'], d['raw_description'])
                    for d in true_dup_keys}
        all_set = {(d['vendor__name'], d['invoice_date'], d['raw_description'])
                   for d in same_raw}
        mixed = all_set - true_set

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== dedup_invoice_line_items ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'True-dup groups: {len(true_set)}')
        self.stdout.write(f'Mixed-price groups (NOT auto-deleted): {len(mixed)}')

        deleted = 0
        for d in true_dup_keys:
            siblings = list(qs.filter(
                vendor__name=d['vendor__name'],
                invoice_date=d['invoice_date'],
                raw_description=d['raw_description'],
                unit_price=d['unit_price'],
                extended_amount=d['extended_amount'],
            ).order_by('id'))
            if len(siblings) < 2:
                continue
            # Keep the one with most structured coverage; tie-break on lowest id
            siblings.sort(key=lambda i: (-_structured_score(i), i.id))
            survivor = siblings[0]
            losers = siblings[1:]
            self.stdout.write(
                f'  {d["invoice_date"]} {d["vendor__name"][:18]:18s} '
                f'${d["unit_price"]:>8} keep ID={survivor.id} '
                f'delete IDs={[l.id for l in losers]}'
            )
            if apply_writes:
                for l in losers:
                    l.delete()
                    deleted += 1

        # Surface mixed-price groups for manual review.
        if mixed:
            self.stdout.write(self.style.MIGRATE_LABEL(
                f'\n--- Mixed-price groups (review manually) ---'
            ))
            for vendor_name, inv_date, raw in sorted(mixed)[:30]:
                rows = qs.filter(
                    vendor__name=vendor_name,
                    invoice_date=inv_date,
                    raw_description=raw,
                ).order_by('id')
                prices = [(r.id, r.unit_price, r.extended_amount, r.match_confidence)
                          for r in rows]
                self.stdout.write(f'  {inv_date} {vendor_name[:18]:18s} raw={raw[:50]!r}')
                for pid, up, ext, conf in prices:
                    self.stdout.write(f'    ID={pid:5d} unit_price={up} ext={ext} conf={conf}')
            if len(mixed) > 30:
                self.stdout.write(f'  ... +{len(mixed)-30} more (run --vendor X to scope)')

        self.stdout.write('')
        if apply_writes:
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {deleted} duplicate rows.'
            ))
        else:
            potential = sum(d['n'] - 1 for d in true_dup_keys)
            self.stdout.write(self.style.WARNING(
                f'Dry-run — would delete {potential} rows. Re-run with --apply.'
            ))
