"""Retroactively flag math-anomaly ILI rows.

Walks every existing InvoiceLineItem with qty + unit_price + extended_amount
populated, runs the same `validate_line_math` logic the live extraction
paths now use (catch-weight aware via price_per_pound when populated),
and persists `math_flagged=True` for any row that fails the qty × price ≈
extended check beyond tolerance.

Per `feedback_event_driven_pricing.md`: prices remain immutable historical
events. We're not changing what was on the invoice; we're recording our
trust assessment. math_flagged is a quality signal, not a price edit.

Run:
  python manage.py backfill_math_flagged                # dry-run report
  python manage.py backfill_math_flagged --apply        # actually flag
  python manage.py backfill_math_flagged --vendor "Sysco" --apply
"""
import os
import sys
from collections import Counter

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import transaction

from myapp.models import InvoiceLineItem


# Add invoice_processor to path for line_math import
_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)

from line_math import validate_line_math  # noqa: E402


class Command(BaseCommand):
    help = ('Retroactively set math_flagged=True on existing InvoiceLineItem '
            'rows that fail qty × price ≈ extended (catch-weight aware). '
            'Default is dry-run; pass --apply to persist.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', help='Filter to a single vendor name')
        parser.add_argument('--apply', action='store_true',
                            help='Persist math_flagged=True. Without this, '
                                 'dry-run report only.')

    def handle(self, *args, **opts):
        qs = (InvoiceLineItem.objects
              .exclude(quantity__isnull=True)
              .exclude(unit_price__isnull=True)
              .exclude(extended_amount__isnull=True)
              .select_related('vendor'))

        if opts.get('vendor'):
            qs = qs.filter(vendor__name=opts['vendor'])

        total_checked = qs.count()
        self.stdout.write(f'Checking {total_checked} ILI rows '
                          f'(qty + unit_price + extended_amount populated)...')

        flag_ids = []
        unflag_ids = []  # rows currently flagged but now-clean per validator
        flagged_by_vendor = Counter()
        unflagged_by_vendor = Counter()

        for ili in qs.only(
                'id', 'quantity', 'unit_price', 'extended_amount',
                'price_per_pound', 'raw_description', 'math_flagged',
                'vendor__name'):
            # Build a dict matching the parser-stage shape so validate_line_math
            # can read it. We use price_per_pound (DB-shape) directly.
            item = {
                'quantity': float(ili.quantity),
                'unit_price': float(ili.unit_price),
                'extended_amount': float(ili.extended_amount),
                'price_per_pound': (float(ili.price_per_pound)
                                    if ili.price_per_pound is not None else None),
                'raw_description': ili.raw_description,
            }
            # Don't self-correct — backfill is a measurement pass, not a fix
            validate_line_math(item, vendor=(ili.vendor.name if ili.vendor else ''))
            v_name = ili.vendor.name if ili.vendor else '(none)'
            should_flag = bool(item.get('math_flagged'))
            currently_flagged = bool(ili.math_flagged)

            if should_flag and not currently_flagged:
                flag_ids.append(ili.id)
                flagged_by_vendor[v_name] += 1
            elif not should_flag and currently_flagged:
                # Edge case: a row was previously flagged but no longer fails
                # (data was corrected via reprocess, or tolerance changed).
                # Clear the flag.
                unflag_ids.append(ili.id)
                unflagged_by_vendor[v_name] += 1

        self.stdout.write('')
        self.stdout.write('Plan:')
        self.stdout.write(f'  Set math_flagged=True on  {len(flag_ids)} rows')
        if flag_ids:
            self.stdout.write('  By vendor:')
            for v, n in sorted(flagged_by_vendor.items(),
                               key=lambda x: -x[1]):
                self.stdout.write(f'    {v:30} {n}')

        self.stdout.write(
            f'  Clear math_flagged on      {len(unflag_ids)} rows '
            f'(no longer anomalous)')
        if unflag_ids:
            for v, n in sorted(unflagged_by_vendor.items(),
                               key=lambda x: -x[1]):
                self.stdout.write(f'    {v:30} {n}')

        if not opts['apply']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to persist.'))
            return

        # Apply
        with transaction.atomic():
            if flag_ids:
                InvoiceLineItem.objects.filter(id__in=flag_ids).update(
                    math_flagged=True)
            if unflag_ids:
                InvoiceLineItem.objects.filter(id__in=unflag_ids).update(
                    math_flagged=False)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Applied: {len(flag_ids)} flagged, {len(unflag_ids)} cleared.'))
