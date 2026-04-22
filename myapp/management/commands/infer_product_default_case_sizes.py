"""Infer Product.default_case_size from the mode case_size across each
product's InvoiceLineItem history.

A product's default case_size becomes the single most common case_size
seen on its invoice lines, provided:
  1. The mode has at least --min-count occurrences (default 2) — one-off
     values aren't trustworthy defaults.
  2. The mode represents at least --min-share of that product's rows
     (default 0.5) — highly-inconsistent products should stay defaultless.

Without --apply, reports only.

Usage:
    python manage.py infer_product_default_case_sizes
    python manage.py infer_product_default_case_sizes --apply
    python manage.py infer_product_default_case_sizes --min-count 3 --min-share 0.6
    python manage.py infer_product_default_case_sizes --overwrite  # replace existing defaults
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from myapp.models import Product, InvoiceLineItem


class Command(BaseCommand):
    help = 'Set Product.default_case_size from mode of its invoice history.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write inferred defaults. Without this flag, report only.')
        parser.add_argument('--min-count', type=int, default=2,
                            help='Mode must appear at least this many times (default 2).')
        parser.add_argument('--min-share', type=float, default=0.5,
                            help='Mode must be >= this fraction of the product\'s rows (default 0.5).')
        parser.add_argument('--overwrite', action='store_true',
                            help='Replace defaults already set. Otherwise only fill empties.')

    def handle(self, *args, **opts):
        apply = opts['apply']
        min_count = opts['min_count']
        min_share = opts['min_share']
        overwrite = opts['overwrite']

        # Pull all non-empty case_size rows at once, grouped by product
        rows = (InvoiceLineItem.objects
                .exclude(case_size='')
                .values_list('product_id', 'case_size'))
        by_product: dict[int, Counter] = {}
        for pid, cs in rows:
            if pid is None:
                continue
            by_product.setdefault(pid, Counter())[cs] += 1

        products = {p.id: p for p in Product.objects.all()}

        set_count = 0
        skipped_low_count = 0
        skipped_low_share = 0
        skipped_already_set = 0
        no_history = 0
        samples = []

        for pid, product in products.items():
            counter = by_product.get(pid)
            if not counter:
                no_history += 1
                continue
            total = sum(counter.values())
            mode_cs, mode_n = counter.most_common(1)[0]
            share = mode_n / total

            if mode_n < min_count:
                skipped_low_count += 1
                continue
            if share < min_share:
                skipped_low_share += 1
                continue
            if product.default_case_size and not overwrite:
                skipped_already_set += 1
                continue
            if product.default_case_size == mode_cs:
                continue  # already correct

            if len(samples) < 25:
                samples.append({
                    'product': product.canonical_name,
                    'old': product.default_case_size,
                    'new': mode_cs,
                    'count': mode_n,
                    'total': total,
                    'share': share,
                })
            if apply:
                product.default_case_size = mode_cs
                product.save(update_fields=['default_case_size'])
            set_count += 1

        self.stdout.write(self.style.HTTP_INFO('=== Inference report ==='))
        self.stdout.write(f'Products with invoice history:       {len(by_product)}')
        self.stdout.write(f'  Would set default_case_size:       {set_count}')
        self.stdout.write(f'  Skipped (low count <{min_count}):         {skipped_low_count}')
        self.stdout.write(f'  Skipped (low share <{min_share:.0%}):        {skipped_low_share}')
        self.stdout.write(f'  Skipped (already set):             {skipped_already_set}')
        self.stdout.write(f'  Products without invoice history:  {no_history}')

        if samples:
            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== Sample assignments (first {len(samples)}) ==='))
            self.stdout.write(
                f'{"Product":<40}  {"Current":<12}  {"Inferred":<12}  {"Support"}')
            for s in samples:
                support = f'{s["count"]}/{s["total"]} ({s["share"]:.0%})'
                self.stdout.write(
                    f'{s["product"][:40]:<40}  {s["old"] or "—":<12}  '
                    f'{s["new"]:<12}  {support}')

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone. {set_count} Product.default_case_size values set.'))
        else:
            self.stdout.write(
                f'\n(Dry-run. Re-run with --apply to write.)')
