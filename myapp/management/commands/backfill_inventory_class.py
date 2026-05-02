"""Backfill Product.inventory_class from category + default_case_size signals.

Phase 3e prerequisite: the mapper inventory_class type-check (rejects yogurt
→ shrimp class mismatches) needs Product.inventory_class populated. This
command applies CONSERVATIVE heuristics that only set the field when the
signal is high-confidence; ambiguous products are left blank for human
review.

Heuristic priority order (first match wins):

  1. **Skip** if inventory_class already set (idempotent re-runs).
  2. **Skip** category=Pseudo (admin / synthetic recipes).
  3. **Skip** primary_descriptor contains 'Cheese' (subjective: block vs
     shredded vs sliced vs crumbled — needs Sean's domain call).
  4. **Skip** category=Spices (some weighed bulk, some counted small jars).
  5. **counted_with_volume** if default_case_size matches volume regex
     `(GAL|GALLON|QT|QUART|PT|PINT|FL\\s*OZ|FLOZ)` not adjacent to letters
     (avoids 'GALA' in Apple Gala or 'PT' suffix in part numbers).
  6. **weighed** if category=Proteins (anchored exception: canonical_name
     contains 'Egg' → counted_with_weight by dozen).
  7. **counted_with_weight** for clear non-volume packaged categories:
     Bakery, Smallwares, Coffee/Concessions, Chemicals, Drystock.
  8. **Produce**: split by case-size signal — `\\bCT\\b` →
     counted_with_weight; `LB` (with no CT) → weighed; otherwise skip.
  9. **Dairy** (non-cheese, non-volume): leave blank — needs review.

Writes nothing under --dry-run. Reports per-category outcomes.

Usage:
    python manage.py backfill_inventory_class               # dry-run preview
    python manage.py backfill_inventory_class --apply       # commit
    python manage.py backfill_inventory_class --category Proteins --apply
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from myapp.models import Product

VOLUME_RE = re.compile(
    r'(?:^|[^A-Za-z])(GAL|GALLON|QT|QUART|PT|PINT|FL\s*OZ|FLOZ)(?=$|[^A-Za-z])',
    re.IGNORECASE,
)
CT_RE = re.compile(r'(?:^|[^A-Za-z])CT(?=$|[^A-Za-z])', re.IGNORECASE)
LB_RE = re.compile(r'(?:^|[^A-Za-z])LB(?=$|[^A-Za-z])', re.IGNORECASE)

CLEAR_COUNTED_CATEGORIES = {
    'Bakery',
    'Smallwares',
    'Coffee/Concessions',
    'Chemicals',
    'Drystock',
}


def classify(p: Product) -> tuple[str | None, str]:
    """Return (inventory_class, reason). class=None means leave blank."""
    if p.inventory_class:
        return None, 'already_set'

    cat = (p.category or '').strip()
    pdesc = (p.primary_descriptor or '').strip()
    cs = (p.default_case_size or '').strip()
    name = (p.canonical_name or '').strip()

    if cat == 'Pseudo':
        return None, 'skip_pseudo'

    if 'Cheese' in pdesc:
        return None, 'skip_cheese_subjective'

    if cat == 'Spices':
        return None, 'skip_spices_mixed'

    # Volume-packed signal beats category default (Cream/Milk/Mayo gallons).
    if cs and VOLUME_RE.search(cs):
        return 'counted_with_volume', 'volume_regex'

    if cat == 'Proteins':
        if 'Egg' in name:
            return 'counted_with_weight', 'eggs_dozen_carve_out'
        return 'weighed', 'proteins_default'

    if cat in CLEAR_COUNTED_CATEGORIES:
        return 'counted_with_weight', f'{cat}_default'

    if cat == 'Produce':
        if cs and CT_RE.search(cs):
            return 'counted_with_weight', 'produce_count_ct'
        if cs and LB_RE.search(cs):
            return 'weighed', 'produce_weight_lb'
        return None, 'produce_no_signal'

    if cat == 'Dairy':
        return None, 'dairy_non_cheese_review'

    return None, f'unhandled_category_{cat or "blank"}'


class Command(BaseCommand):
    help = "Backfill Product.inventory_class via conservative category + case-size heuristics."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default is dry-run preview).')
        parser.add_argument('--category', default='',
                            help='Limit to one category (e.g. Proteins).')
        parser.add_argument('--verbose', action='store_true',
                            help='Print every product, not just per-category counts.')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        cat_filter = opts['category']
        verbose = opts['verbose']

        qs = Product.objects.all().order_by('category', 'canonical_name')
        if cat_filter:
            qs = qs.filter(category=cat_filter)

        per_cat_outcome: dict[str, Counter[str]] = defaultdict(Counter)
        per_cat_writes: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        skip_reasons: Counter[str] = Counter()
        total_set = 0
        total_skip = 0

        for p in qs:
            cls, reason = classify(p)
            cat = p.category or '(blank)'
            if cls is None:
                per_cat_outcome[cat][reason] += 1
                skip_reasons[reason] += 1
                total_skip += 1
                if verbose:
                    self.stdout.write(f'  SKIP   [{reason:30s}] {p.canonical_name}')
                continue

            per_cat_outcome[cat][cls] += 1
            per_cat_writes[cat].append((p.canonical_name, cls, reason))
            total_set += 1
            if verbose:
                self.stdout.write(f'  SET    [{cls:22s}] {p.canonical_name}  ({reason})')

            if apply_writes:
                p.inventory_class = cls
                p.save(update_fields=['inventory_class'])

        # Report
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== inventory_class backfill ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        for cat in sorted(per_cat_outcome):
            self.stdout.write(self.style.MIGRATE_LABEL(f'{cat}:'))
            for outcome, n in per_cat_outcome[cat].most_common():
                self.stdout.write(f'  {n:4d}  {outcome}')
            sample = per_cat_writes[cat][:3]
            for name, cls, reason in sample:
                self.stdout.write(f'        e.g. {name}  →  {cls}  ({reason})')

        self.stdout.write('')
        self.stdout.write(f'Total products considered: {qs.count()}')
        self.stdout.write(f'Would set: {total_set}    Skip: {total_skip}')
        if not apply_writes and total_set:
            self.stdout.write(self.style.WARNING(
                'Dry-run — re-run with --apply to commit.'
            ))
