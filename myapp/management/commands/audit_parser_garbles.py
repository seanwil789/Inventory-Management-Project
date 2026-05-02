"""Surface ILI rows tagged as parser-garble (match_confidence='unmatched_garbled')
for parser-bug diagnosis.

Sean (2026-05-02): when an invoice line shows up in /mapping-review/
combining multiple products (parser line-merge bug, OCR break failure,
column-bleed), Sean rejects with reason='typo_or_garble'. The reject
view tags the underlying ILIs with match_confidence='unmatched_garbled'.
This audit lists those rows so parser fixes can target the actual
failure modes.

Output groups by vendor + frequency-sorted so the most-impactful
parser bugs surface first.

Usage:
    python manage.py audit_parser_garbles
    python manage.py audit_parser_garbles --vendor Sysco
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count, Min, Max

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "List ILI rows tagged unmatched_garbled (parser bug candidates)."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor (e.g. Sysco).')
        parser.add_argument('--limit', type=int, default=30,
                            help='Cap the per-group sample size (default 30).')

    def handle(self, *args, **opts):
        qs = InvoiceLineItem.objects.filter(match_confidence='unmatched_garbled')
        if opts['vendor']:
            qs = qs.filter(vendor__name=opts['vendor'])

        total = qs.count()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== audit_parser_garbles ({total} rows) ===\n'
        ))

        if total == 0:
            self.stdout.write('No garbled rows. Sean either has not flagged any, '
                              'or the parser has been pristine since last flag.')
            return

        # Group by (vendor, raw_description) → pick most-frequent first
        groups = (qs.values('vendor__name', 'raw_description')
                    .annotate(n=Count('id'),
                              first_seen=Min('invoice_date'),
                              last_seen=Max('invoice_date'))
                    .order_by('-n', '-last_seen'))

        for g in groups[:opts['limit']]:
            self.stdout.write(
                f'  ×{g["n"]:3d}  {g["vendor__name"][:18]:18s} '
                f'{g["first_seen"]} → {g["last_seen"]}'
            )
            self.stdout.write(f'    raw={g["raw_description"][:90]!r}')

        self.stdout.write('')
        self.stdout.write(f'Distinct (vendor, raw) groups: {groups.count()}')
        # Per-vendor totals
        per_vendor = Counter()
        for g in qs.values_list('vendor__name', flat=True):
            per_vendor[g] += 1
        self.stdout.write('Per vendor:')
        for v, n in per_vendor.most_common():
            self.stdout.write(f'  {v[:25]:25s} {n}')
