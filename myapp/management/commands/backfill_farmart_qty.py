"""Backfill missing Farm Art qty by reverse-deriving from ext / unit_price.

Farm Art applies a quiet ~1% vendor discount, so ext = qty × U/P × ~0.99.
When qty is missing but ext + U/P present, qty = round(ext / U/P).

Strict gate: only set qty when the rounded value lands near an integer
(tolerance ≤ 0.05) AND falls in a plausible range (1-50). Anything else
left untouched for manual review.

Usage:
    python manage.py backfill_farmart_qty           # dry-run
    python manage.py backfill_farmart_qty --apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from myapp.models import InvoiceLineItem
from decimal import Decimal


class Command(BaseCommand):
    help = "Backfill missing Farm Art qty from ext/unit_price math."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default dry-run).')
        parser.add_argument('--tolerance', type=float, default=0.05,
                            help='Max distance from integer (default 0.05).')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        tol = opts['tolerance']

        qs = (InvoiceLineItem.objects
              .filter(vendor__name='Farm Art')
              .filter(Q(quantity__isnull=True) | Q(quantity=0))
              .exclude(unit_price__isnull=True)
              .exclude(unit_price=0)
              .exclude(extended_amount__isnull=True)
              .exclude(extended_amount=0))

        n_total = qs.count()
        n_set = 0
        n_skipped = 0
        skip_reasons = {'out_of_range': 0, 'not_integer': 0}

        for ili in qs:
            up = float(ili.unit_price)
            ext = float(ili.extended_amount)
            ratio = ext / up

            if ratio < 0.5 or ratio > 50:
                skip_reasons['out_of_range'] += 1
                n_skipped += 1
                continue

            rounded = round(ratio)
            if rounded < 1:
                rounded = 1  # 0.99 → 1
            if abs(ratio - rounded) > tol:
                # Not close enough to an integer
                # Allow 0.95-1.05 → 1 specifically (the discount band)
                if 0.95 <= ratio < 1.05:
                    rounded = 1
                else:
                    skip_reasons['not_integer'] += 1
                    n_skipped += 1
                    continue

            if apply_writes:
                ili.quantity = Decimal(str(rounded))
                ili.save(update_fields=['quantity'])
            n_set += 1

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== backfill_farmart_qty ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'Candidates examined: {n_total}')
        self.stdout.write(self.style.SUCCESS(f'Would set qty:        {n_set}'))
        self.stdout.write(f'Skipped:              {n_skipped}')
        for reason, n in skip_reasons.items():
            self.stdout.write(f'  {reason:20} {n}')

        # New coverage estimate
        all_fa = InvoiceLineItem.objects.filter(vendor__name='Farm Art').count()
        existing_qty = (InvoiceLineItem.objects.filter(vendor__name='Farm Art')
                        .exclude(quantity__isnull=True).exclude(quantity=0).count())
        projected = existing_qty + (n_set if apply_writes else n_set)
        self.stdout.write(f'\nProjected qty coverage: {projected}/{all_fa} '
                          f'({projected/all_fa*100:.1f}%)')
        self.stdout.write(f'Current qty coverage:   {existing_qty}/{all_fa} '
                          f'({existing_qty/all_fa*100:.1f}%)')

        if not apply_writes:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
