"""Backfill quantity on existing catch-weight ILI rows that pre-date the
B-Salmon fix (2026-05-10 commits 0d2f864 + 0691364).

The bug: rank_pair + spatial_matcher stored quantity=1 (case count) for
Sysco catch-weight rows (MEATS/POULTRY/SEAFOOD with a 3-decimal per-lb
token), even though the actual shipped weight is in T/WT. validate_line_math
fired qty(1) × ppp ≠ ext (line total) → false-positive math_flagged.

The fix is going-forward; db_write doesn't update existing rows on re-parse.
This cmd cleans up the historical pollution.

Criteria for a backfill candidate:
  * price_per_pound IS NOT NULL (it's a catch-weight)
  * quantity = 1 (the pre-fix bug pattern; post-fix rows have qty=weight)
  * extended_amount > 0
  * unit_price ≈ extended_amount (catch-weight stores line total in unit_price)
  * 0.1 < (ext / ppp) < 1000 (sanity guard — implausible weights stay flagged)

For each candidate:
  - quantity = round(extended_amount / price_per_pound, 3)
  - case_total_weight_lb = derived_weight
  - math_flagged = False (the fix removes the cause)
  - math_diff_abs = None
  - math_diff_pct = None

Usage:
  python manage.py backfill_catch_weight_qty                # dry-run
  python manage.py backfill_catch_weight_qty --apply        # commit
  python manage.py backfill_catch_weight_qty --vendor Sysco --apply
"""
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db.models import Q, F

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = ('Backfill quantity on existing catch-weight ILI rows that '
            'were stored with qty=1 pre-B-Salmon-fix.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', help='Filter to a single vendor name')
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Without this flag, dry-run only.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N candidates (for testing)')

    def handle(self, *args, **opts):
        vendor_name = opts.get('vendor')
        apply_writes = opts.get('apply')
        limit = opts.get('limit')

        # Candidates: catch-weight rows (ppp populated) with qty=1 +
        # unit_price ≈ extended_amount (catch-weight convention).
        qs = InvoiceLineItem.objects.filter(
            price_per_pound__isnull=False,
            quantity=1,
            extended_amount__gt=0,
            price_per_pound__gt=0,
        )
        if vendor_name:
            qs = qs.filter(vendor__name=vendor_name)

        self.stdout.write(
            f'Scanning {qs.count()} catch-weight ILI rows with qty=1...')

        ok = 0
        skipped = 0
        skipped_weight = 0
        flagged_count = 0
        updates = []  # list of (ili, new_qty) tuples

        for ili in qs.iterator():
            ext = float(ili.extended_amount)
            ppp = float(ili.price_per_pound)
            up = float(ili.unit_price or 0)
            # Catch-weight stores line total in unit_price. Skip rows
            # where unit_price disagrees with ext (those aren't classic
            # catch-weight, may be a different schema convention).
            if abs(up - ext) >= 0.01:
                skipped += 1
                continue
            derived_weight = round(ext / ppp, 3)
            if not (0.1 < derived_weight < 1000):
                skipped_weight += 1
                continue
            ok += 1
            if ili.math_flagged:
                flagged_count += 1
            updates.append((ili, derived_weight))
            if limit and ok >= limit:
                break

        self.stdout.write('')
        self.stdout.write(f'Candidates: {ok}')
        self.stdout.write(f'  of which math_flagged=True: {flagged_count}')
        self.stdout.write(f'Skipped (unit_price ≠ ext, non-classic CW): {skipped}')
        self.stdout.write(f'Skipped (implausible derived weight): {skipped_weight}')

        if updates:
            # Show top 10 for human-eye sanity check
            self.stdout.write('')
            self.stdout.write('Sample changes (first 10):')
            self.stdout.write(
                f'  {"vendor":<14} {"date":<12} {"product":<22} '
                f'{"old_qty":>7} → {"new_qty":>8} '
                f'{"ext":>9} {"ppp":>8} flag')
            for ili, new_qty in updates[:10]:
                product_name = (ili.product.canonical_name
                                if ili.product_id else '(unmapped)')[:22]
                self.stdout.write(
                    f'  {ili.vendor.name[:14]:<14} '
                    f'{str(ili.invoice_date):<12} '
                    f'{product_name:<22} '
                    f'{1:>7} → {new_qty:>8.3f} '
                    f'${float(ili.extended_amount):>7.2f} '
                    f'${float(ili.price_per_pound):>6.3f} '
                    f'{ili.math_flagged}')

        if not apply_writes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to commit.'))
            return

        # Apply: bulk update
        self.stdout.write('')
        self.stdout.write(f'Applying {len(updates)} updates...')
        applied = 0
        for ili, new_qty in updates:
            ili.quantity = Decimal(str(new_qty))
            ili.case_total_weight_lb = Decimal(str(new_qty))
            if ili.math_flagged:
                ili.math_flagged = False
                ili.save(update_fields=[
                    'quantity', 'case_total_weight_lb', 'math_flagged'])
            else:
                ili.save(update_fields=['quantity', 'case_total_weight_lb'])
            applied += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done. Updated {applied} rows; cleared math_flagged on '
            f'{flagged_count} of them.'))
