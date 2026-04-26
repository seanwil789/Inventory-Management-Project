"""Surface and (optionally) fill missing Product.default_case_size from
each Product's ILI history.

Why this matters: case_size drives IUP (price-per-unit-within-case) and
$/lb math used by inventory valuation. Wrong or missing case_size →
wrong dollar math during the month-end count.

Algorithm per Product with ≥1 ILI but no default_case_size:
  1. Pull all distinct InvoiceLineItem.case_size values for that Product
  2. Bucket:
     - auto:      single distinct value across ILIs, OR a clear winner
                  (≥70% dominance, ≥2 samples)
     - ambiguous: multiple distinct values, no clear winner
     - no_data:   Product has ILIs but none carry case_size
  3. With --apply: fill the 'auto' bucket. 'ambiguous' and 'no_data'
     get listed for human follow-up.

Usage:
    python manage.py audit_case_size_coverage           # dry-run report
    python manage.py audit_case_size_coverage --apply   # fill auto bucket
    python manage.py audit_case_size_coverage --verbose # also list per-Product detail
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from myapp.models import Product, InvoiceLineItem


def _derive(product):
    """Return (case_size, kind) where kind in {'auto', 'ambiguous', 'no_data'}."""
    distinct = Counter()
    for cs in (InvoiceLineItem.objects.filter(product=product)
               .exclude(case_size='').exclude(case_size__isnull=True)
               .values_list('case_size', flat=True)):
        cs = (cs or '').strip()
        if cs:
            distinct[cs] += 1
    if not distinct:
        return None, 'no_data'
    if len(distinct) == 1:
        return next(iter(distinct.keys())), 'auto'
    # Multiple values — pick winner if dominant
    top, top_n = distinct.most_common(1)[0]
    total = sum(distinct.values())
    if top_n / total >= 0.7 and top_n >= 2:
        return top, 'auto'
    return None, 'ambiguous'


class Command(BaseCommand):
    help = 'Audit and fill missing Product.default_case_size from ILI history.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Fill the auto-fillable bucket. Default is dry-run.')
        parser.add_argument('--verbose', action='store_true',
                            help='List ambiguous + no_data buckets in detail.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        verbose = opts['verbose']
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'=== {mode} mode ===\n')

        # Find Products with ≥1 ILI but missing default_case_size
        candidates = (Product.objects
                      .annotate(n_ili=Count('invoicelineitem'))
                      .filter(n_ili__gt=0)
                      .filter(default_case_size__in=['', None])
                      .order_by('canonical_name'))
        total = candidates.count()
        self.stdout.write(f'Products with ILIs but missing default_case_size: {total}\n')

        auto_bucket    = []   # (product, derived_case_size, sample_count)
        ambig_bucket   = []   # (product, distinct_dict)
        no_data_bucket = []   # (product, ili_count)

        for p in candidates:
            cs, kind = _derive(p)
            if kind == 'auto':
                # Re-pull distinct for sample count
                d = Counter(c.strip() for c in InvoiceLineItem.objects
                            .filter(product=p).exclude(case_size='')
                            .exclude(case_size__isnull=True)
                            .values_list('case_size', flat=True) if c)
                auto_bucket.append((p, cs, sum(d.values()), dict(d)))
            elif kind == 'ambiguous':
                d = Counter(c.strip() for c in InvoiceLineItem.objects
                            .filter(product=p).exclude(case_size='')
                            .exclude(case_size__isnull=True)
                            .values_list('case_size', flat=True) if c)
                ambig_bucket.append((p, dict(d)))
            else:
                no_data_bucket.append((p, p.n_ili))

        self.stdout.write(f'  auto-fillable:  {len(auto_bucket)}')
        self.stdout.write(f'  ambiguous:      {len(ambig_bucket)}')
        self.stdout.write(f'  no_data:        {len(no_data_bucket)}')
        self.stdout.write('')

        # Show auto bucket sample
        self.stdout.write('=== Auto-fillable (top 20 shown) ===')
        for p, cs, n, _d in auto_bucket[:20]:
            self.stdout.write(f"  {p.canonical_name!r:<35} → case_size={cs!r:<12} ({n} ILI sample)")
        if len(auto_bucket) > 20:
            self.stdout.write(f'  ... +{len(auto_bucket) - 20} more')

        self.stdout.write('')
        self.stdout.write('=== Ambiguous (need human decision; top 15 shown) ===')
        for p, d in ambig_bucket[:15]:
            top3 = sorted(d.items(), key=lambda x: -x[1])[:3]
            top_str = ', '.join(f'{cs!r}×{n}' for cs, n in top3)
            self.stdout.write(f"  {p.canonical_name!r:<35} → {top_str}")
        if len(ambig_bucket) > 15:
            self.stdout.write(f'  ... +{len(ambig_bucket) - 15} more')

        if verbose:
            self.stdout.write('')
            self.stdout.write('=== No-data Products (ILIs carry no case_size) ===')
            for p, n in no_data_bucket:
                self.stdout.write(f"  {p.canonical_name!r:<35} ({n} ILIs)")

        # Apply
        if apply_changes:
            self.stdout.write('')
            self.stdout.write('=== APPLYING auto-fill ===')
            n_filled = 0
            with transaction.atomic():
                for p, cs, _n, _d in auto_bucket:
                    p.default_case_size = cs
                    p.save(update_fields=['default_case_size'])
                    n_filled += 1
            self.stdout.write(self.style.SUCCESS(f'  Filled {n_filled} Products with auto-derived case_size.'))
            self.stdout.write(f'  Remaining: {len(ambig_bucket)} ambiguous + {len(no_data_bucket)} no-data → human follow-up.')
        else:
            self.stdout.write('')
            self.stdout.write(f'(Dry-run — would auto-fill {len(auto_bucket)} Products. Re-run with --apply.)')
