"""Audit structured-field coverage per ILI vendor — Phase 1 of structured
invoice-line schema migration tracking.

After Phase 1 of the structured schema migration ships, the new fields
(quantity, purchase_uom, case_pack_count, case_pack_unit_size,
case_pack_unit_uom, case_total_weight_lb, count_per_lb_low,
count_per_lb_high) populate from db_write threading. The bottleneck for
each field is which vendor's parser/spatial_matcher extracts it.

This audit reports per-vendor % populated for each field — drives Phase 2
prioritization (where to add a normalizer next) and verifies backfill
worked after `reprocess_ocr_cache --apply`.

Usage:
    python manage.py audit_structured_field_coverage
    python manage.py audit_structured_field_coverage --vendor Sysco
    python manage.py audit_structured_field_coverage --json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from myapp.models import InvoiceLineItem


STRUCTURED_FIELDS = [
    'quantity', 'purchase_uom',
    'case_pack_count', 'case_pack_unit_size', 'case_pack_unit_uom',
    'case_total_weight_lb',
    'count_per_lb_low', 'count_per_lb_high',
]


class Command(BaseCommand):
    help = "Report % populated for each structured field, per vendor."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor name')
        parser.add_argument('--json', action='store_true',
                            help='Emit JSON instead of human-readable table')

    def handle(self, *args, vendor=None, json: bool = False, **opts):
        qs = InvoiceLineItem.objects.all()
        if vendor:
            qs = qs.filter(vendor__name=vendor)

        # Per-vendor totals
        per_vendor = defaultdict(lambda: {'total': 0,
                                           **{f: 0 for f in STRUCTURED_FIELDS}})
        for ili in qs.select_related('vendor').only(
                'vendor__name', *STRUCTURED_FIELDS):
            v = ili.vendor.name if ili.vendor else '(none)'
            per_vendor[v]['total'] += 1
            for f in STRUCTURED_FIELDS:
                val = getattr(ili, f)
                # CharField is populated when non-empty; numeric when not None.
                if isinstance(val, str):
                    if val:
                        per_vendor[v][f] += 1
                elif val is not None:
                    per_vendor[v][f] += 1

        # Aggregate row
        agg = {'total': sum(d['total'] for d in per_vendor.values()),
               **{f: sum(d[f] for d in per_vendor.values())
                  for f in STRUCTURED_FIELDS}}

        if json:
            import json as _json
            self.stdout.write(_json.dumps({'per_vendor': dict(per_vendor),
                                            'all': agg}, indent=2))
            return

        # Human table
        vendors = sorted(per_vendor.keys(), key=lambda v: -per_vendor[v]['total'])
        col_w = 22
        header = f"{'Vendor':<{col_w}} {'Total':>6}  " + ' '.join(
            f"{f[:18]:>18}" for f in STRUCTURED_FIELDS)
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        for v in vendors:
            d = per_vendor[v]
            tot = d['total']
            row = f"{v[:col_w]:<{col_w}} {tot:>6}  "
            for f in STRUCTURED_FIELDS:
                pct = (100 * d[f] / tot) if tot else 0
                row += f"{d[f]:>5} ({pct:>4.0f}%)   "
            self.stdout.write(row)

        self.stdout.write('-' * len(header))
        tot = agg['total']
        row = f"{'ALL':<{col_w}} {tot:>6}  "
        for f in STRUCTURED_FIELDS:
            pct = (100 * agg[f] / tot) if tot else 0
            row += f"{agg[f]:>5} ({pct:>4.0f}%)   "
        self.stdout.write(row)

        # Phase 2 prioritization hints
        self.stdout.write('')
        self.stdout.write('Phase 2 prioritization hints:')
        for f in STRUCTURED_FIELDS:
            pct = (100 * agg[f] / tot) if tot else 0
            if pct < 50:
                worst_vendors = sorted(
                    [(v, d[f] / d['total'] if d['total'] else 0, d['total'])
                     for v, d in per_vendor.items() if d['total'] >= 10],
                    key=lambda x: x[1])[:3]
                self.stdout.write(
                    f"  {f}: {pct:.0f}% overall — worst vendors: "
                    + ', '.join(f"{v} ({100*p:.0f}% of {n})"
                                for v, p, n in worst_vendors))
