"""Surface Products whose most-recent invoice is stale, so the price
on file may be outdated for inventory dollar valuation.

Bucket by age:
  fresh:    last invoice ≤ 30 days ago
  aging:    31-60 days ago
  stale:    61-90 days ago
  cold:     91+ days ago
  no_invoice: Product has zero invoice history

Wrong/old prices silently corrupt inventory dollar value — a low-frequency
spice invoiced 6 months ago at $X may now cost $1.5X. Without surfacing,
this stays an unknown unknown.

Usage:
    python manage.py audit_last_invoice_age            # bucket summary
    python manage.py audit_last_invoice_age --threshold 60  # custom days
    python manage.py audit_last_invoice_age --verbose  # list each Product
    python manage.py audit_last_invoice_age --vendor Sysco  # filter
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Max, Count

from myapp.models import Product, InvoiceLineItem


class Command(BaseCommand):
    help = 'Audit how stale each Product\'s most-recent invoice is.'

    def add_arguments(self, parser):
        parser.add_argument('--threshold', type=int, default=60,
                            help='Days threshold to flag as "stale" (default 60).')
        parser.add_argument('--verbose', action='store_true',
                            help='List each Product with its last invoice date.')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to ILIs from this vendor only.')
        parser.add_argument('--reference-date', type=str, default=None,
                            help='YYYY-MM-DD reference date (default: today).')

    def handle(self, *args, **opts):
        verbose = opts['verbose']
        vendor_filter = opts['vendor']
        ref_str = opts['reference_date']
        ref = (date.fromisoformat(ref_str) if ref_str else date.today())

        # Annotate each Product with last invoice date (filtered by vendor if asked)
        ili_qs = InvoiceLineItem.objects.filter(invoice_date__isnull=False)
        if vendor_filter:
            ili_qs = ili_qs.filter(vendor__name=vendor_filter)

        # Build a (product_id → last_date, n_ili) dict
        last_by_product = {}
        for r in (ili_qs.values('product_id')
                       .annotate(last=Max('invoice_date'), n=Count('id'))):
            if r['product_id'] is not None:
                last_by_product[r['product_id']] = (r['last'], r['n'])

        # Categorize all Products
        buckets = {'fresh': [], 'aging': [], 'stale': [], 'cold': [], 'no_invoice': []}
        for p in Product.objects.all().only('id', 'canonical_name', 'category',
                                              'default_case_size'):
            entry = last_by_product.get(p.id)
            if entry is None:
                buckets['no_invoice'].append((p, None, 0))
                continue
            last_date, n = entry
            days = (ref - last_date).days
            if days <= 30:
                buckets['fresh'].append((p, last_date, n, days))
            elif days <= 60:
                buckets['aging'].append((p, last_date, n, days))
            elif days <= 90:
                buckets['stale'].append((p, last_date, n, days))
            else:
                buckets['cold'].append((p, last_date, n, days))

        # Summary
        threshold = opts['threshold']
        flagged = sum(1 for k in ('stale', 'cold')
                      for _ in buckets[k]) + sum(
                          1 for entry in buckets['aging'] if entry[3] > threshold)
        total_with = sum(len(buckets[k]) for k in ('fresh','aging','stale','cold'))
        self.stdout.write(f'Reference date: {ref}')
        if vendor_filter:
            self.stdout.write(f'Vendor filter:  {vendor_filter}')
        self.stdout.write('')
        self.stdout.write(f'Products with invoice history: {total_with}')
        self.stdout.write(f'  Fresh   (≤30 days):  {len(buckets["fresh"]):>4}')
        self.stdout.write(f'  Aging   (31-60d):    {len(buckets["aging"]):>4}')
        self.stdout.write(f'  Stale   (61-90d):    {len(buckets["stale"]):>4}')
        self.stdout.write(f'  Cold    (91+ days):  {len(buckets["cold"]):>4}')
        self.stdout.write(f'Products with NO invoice history:  {len(buckets["no_invoice"])}')
        self.stdout.write('')
        if total_with:
            pct_fresh = len(buckets['fresh']) / total_with * 100
            self.stdout.write(f'Price freshness: {pct_fresh:.0f}% of active Products invoiced in last 30 days.')

        # Detail
        if verbose:
            self.stdout.write('')
            self.stdout.write('=== Cold (91+ days — most stale) ===')
            for p, ld, n, d in sorted(buckets['cold'], key=lambda x: -x[3])[:50]:
                self.stdout.write(f"  {d:>4}d  {ld}  {p.canonical_name!r:<35} ({n} ILIs)")
            self.stdout.write('')
            self.stdout.write('=== Stale (61-90 days) ===')
            for p, ld, n, d in sorted(buckets['stale'], key=lambda x: -x[3])[:30]:
                self.stdout.write(f"  {d:>4}d  {ld}  {p.canonical_name!r:<35} ({n} ILIs)")
        else:
            # Always surface the top-15 oldest as a teaser
            self.stdout.write('')
            self.stdout.write('=== Top 15 oldest (re-run with --verbose for full lists) ===')
            cold_sorted = sorted(buckets['cold'], key=lambda x: -x[3])
            for p, ld, n, d in cold_sorted[:15]:
                self.stdout.write(f"  {d:>4}d  {ld}  {p.canonical_name!r:<35} ({n} ILIs)")
