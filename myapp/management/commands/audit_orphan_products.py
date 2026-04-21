"""Report Product rows that have zero InvoiceLineItem — candidates for
retirement or manual review. Non-destructive: prints a report, doesn't
delete.

Post-purge (2026-04-20) the DB has 266 orphan products, up from 63 —
most are products whose historical invoice lines got dropped but whose
ProductMapping rows still exist. Some are legitimate "never invoiced
yet" items (Aramark coffee supplies per project_pipeline_review.md).

Usage:
    python manage.py audit_orphan_products
    python manage.py audit_orphan_products --category Proteins
    python manage.py audit_orphan_products --json out.json
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count

from myapp.models import Product, InvoiceLineItem, ProductMapping


class Command(BaseCommand):
    help = 'List products with zero invoice lines — retirement candidates.'

    def add_arguments(self, parser):
        parser.add_argument('--category', type=str, default=None,
                            help='Restrict to one product category')
        parser.add_argument('--json', type=str, default=None,
                            help='Write full report as JSON to this path')

    def handle(self, *args, **opts):
        qs = Product.objects.annotate(n=Count('invoicelineitem')).filter(n=0)
        if opts['category']:
            qs = qs.filter(category__iexact=opts['category'])

        orphans = list(qs.order_by('category', 'canonical_name'))
        total = orphans and Product.objects.count() or 0

        by_cat = Counter(p.category or '(uncategorized)' for p in orphans)

        self.stdout.write(self.style.HTTP_INFO(
            f'=== Orphan product audit ==='))
        self.stdout.write(
            f'Total products: {Product.objects.count()}\n'
            f'Orphans (0 invoice lines): {len(orphans)}\n')

        if not orphans:
            self.stdout.write(self.style.SUCCESS('No orphans. Clean catalog.'))
            return

        self.stdout.write(self.style.HTTP_INFO('\n=== By category ==='))
        for cat, n in by_cat.most_common():
            self.stdout.write(f'  {n:>4}  {cat}')

        # Check each orphan against ProductMapping — a product with
        # mappings but no line items is either a recent canonical rename
        # or something waiting for its first invoice.
        self.stdout.write(self.style.HTTP_INFO('\n=== Orphans detail ==='))
        self.stdout.write(f'{"Category":<25} {"Product":<35} {"Mappings":>8}  Notes')
        self.stdout.write(f'{"-"*25} {"-"*35} {"-"*8}  {"-"*5}')

        report = []
        for p in orphans[:100]:
            mapping_count = ProductMapping.objects.filter(product=p).count()
            note = ''
            if mapping_count == 0:
                note = 'no mappings either — retire?'
            elif mapping_count > 0:
                note = f'has mappings — waiting on invoice or recently renamed'
            self.stdout.write(
                f'{(p.category or "-"):<25} {p.canonical_name[:35]:<35} {mapping_count:>8}  {note}')
            report.append({
                'product_id': p.id,
                'canonical_name': p.canonical_name,
                'category': p.category,
                'primary_descriptor': p.primary_descriptor,
                'mapping_count': mapping_count,
            })

        if len(orphans) > 100:
            self.stdout.write(f'  ... + {len(orphans) - 100} more (use --json for full list)')

        if opts['json']:
            Path(opts['json']).write_text(json.dumps(report, indent=2))
            self.stdout.write(self.style.SUCCESS(
                f'\n  Full report written to {opts["json"]} ({len(report)} entries)'))

        self.stdout.write(self.style.WARNING(
            '\nFix path:\n'
            '  - "no mappings either" products are safe to retire. Use Django admin\n'
            '    or a data migration to delete them after a spot-check.\n'
            '  - Products with mappings but no invoices may be recent reprocess\n'
            '    artifacts (purge dropped old rows) or legitimate "first invoice\n'
            '    pending" items. Cross-reference project_pipeline_review.md.'))
