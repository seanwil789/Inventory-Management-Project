"""Backfill missing qty across all vendors by deriving from ext / unit_price.

Generalization of backfill_farmart_qty. Two acceptance bands per vendor:
  * Standard: |ratio - round(ratio)| ≤ tol (default 0.05)
  * Discount band (Farm Art only): fractional part ≥ 0.93 → snap UP

Skip ratios > 100 (likely measurement, not count) and < 0.5.

Usage:
    python manage.py backfill_invoice_qty                  # dry-run all vendors
    python manage.py backfill_invoice_qty --apply
    python manage.py backfill_invoice_qty --vendor Sysco --apply
"""
from math import floor, ceil
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db.models import Q
from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Backfill missing qty across all vendors via ext/unit_price math."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default dry-run).')
        parser.add_argument('--vendor', help='Limit to one vendor.')
        parser.add_argument('--tolerance', type=float, default=0.05,
                            help='Max distance from integer (default 0.05).')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        tol = opts['tolerance']
        vendor = opts['vendor']

        qs = (InvoiceLineItem.objects
              .filter(Q(quantity__isnull=True) | Q(quantity=0))
              .exclude(unit_price__isnull=True)
              .exclude(unit_price=0)
              .exclude(extended_amount__isnull=True)
              .exclude(extended_amount=0))
        if vendor:
            qs = qs.filter(vendor__name=vendor)

        n_total = qs.count()
        per_vendor = {}

        for ili in qs:
            vname = ili.vendor.name if ili.vendor else 'unknown'
            stats = per_vendor.setdefault(vname, {
                'examined': 0, 'set': 0, 'skipped': 0,
            })
            stats['examined'] += 1

            up = float(ili.unit_price)
            ext = float(ili.extended_amount)
            ratio = ext / up

            if ratio < 0.5 or ratio > 100:
                stats['skipped'] += 1
                continue

            frac = ratio - floor(ratio)
            rounded = None

            # Farm Art has a known ~1% discount → frac ≥ 0.93 means ceil
            if vname == 'Farm Art' and frac >= 0.93:
                rounded = ceil(ratio)
            elif frac <= tol:
                rounded = floor(ratio) if floor(ratio) >= 1 else 1
            elif abs(ratio - round(ratio)) <= tol:
                rounded = round(ratio)

            if rounded is None or rounded < 1:
                stats['skipped'] += 1
                continue

            if apply_writes:
                ili.quantity = Decimal(str(rounded))
                ili.save(update_fields=['quantity'])
            stats['set'] += 1

        # Report
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== backfill_invoice_qty ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'  {"Vendor":30} {"Examined":>9} {"Set":>5} {"Skipped":>8}')
        self.stdout.write('  ' + '-' * 60)
        for v, s in sorted(per_vendor.items(), key=lambda kv: -kv[1]['examined']):
            self.stdout.write(f'  {v[:30]:30} {s["examined"]:>9} {s["set"]:>5} {s["skipped"]:>8}')

        # Coverage projection
        self.stdout.write('')
        for vname in per_vendor:
            all_v = InvoiceLineItem.objects.filter(vendor__name=vname).count()
            popped = (InvoiceLineItem.objects.filter(vendor__name=vname)
                      .exclude(quantity__isnull=True).exclude(quantity=0).count())
            projected = popped + (per_vendor[vname]['set']
                                   if not apply_writes else 0)
            self.stdout.write(f'  {vname:30} qty: {popped}/{all_v} ({popped/all_v*100:.1f}%) '
                              + (f'→ projected {projected}/{all_v} ({projected/all_v*100:.1f}%)'
                                 if not apply_writes else ''))

        if not apply_writes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
