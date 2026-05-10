"""Surface ILI rows with unit_price wildly off the per-Product median — likely
parser-fragmentation bugs.

Per `project_costing_accuracy_as_flexibility.md` (2026-05-09 audit findings):
phantom $1-5 unit_price entries, where the parser caught a per-pound or quantity
field instead of the per-case price, corrupt volatility analysis. Examples:
  - Bacon Martins 30530:        real $70-72/cs, phantom $4.39 / $4.69
  - Sausage Patties 10.0LB:     real $40.50/cs, phantom $3.99 / $4.05
  - Butter Prints 36/1#:        real $97/cs,   phantom $1.40
  - Pork Loin Boneless:         real $45-50/cs, phantom $2.02
  - Cream Heavy 12/1QT:         real $41-51/cs, phantom $3.20

These look identical to legitimate prices to the existing math validator
(which only checks qty × price ≈ ext within a single line). The signature is
external: same Product + same case_size, but unit_price << median of history.

Algorithm per (product_id, case_size) group with n>=4 rows:
  median = statistics.median(unit_prices)
  Flag rows where:
    unit_price < (0.20 × median)   ← likely fragment (per-lb or qty captured)
    unit_price > (5.00 × median)   ← possibly concatenation (two prices merged)

Read-only by default; --apply marks `math_flagged=True` so the row appears in
audit_math_anomalies + /mapping-review/'s anomaly surface for human triage.
This does NOT change unit_price — only flags. The fix path (correcting the
specific row) is human-mediated.

Usage:
    python manage.py audit_price_outliers                  # dry-run
    python manage.py audit_price_outliers --apply
    python manage.py audit_price_outliers --vendor "Sysco"
    python manage.py audit_price_outliers --low-ratio 0.15 --high-ratio 6.0
    python manage.py audit_price_outliers --min-group-size 5
    python manage.py audit_price_outliers --json /tmp/outliers.json
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = ('Audit ILI rows for unit_price outliers vs per-Product median. '
            'Read-only by default; --apply marks math_flagged=True.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Mark detected outliers as math_flagged=True. '
                                 'Default is dry-run.')
        parser.add_argument('--vendor', default=None,
                            help='Restrict to one vendor name (icontains)')
        parser.add_argument('--low-ratio', type=float, default=0.20,
                            help='Flag if unit_price < (low_ratio × median). '
                                 'Default 0.20 (price is <20%% of median).')
        parser.add_argument('--high-ratio', type=float, default=5.00,
                            help='Flag if unit_price > (high_ratio × median). '
                                 'Default 5.00 (price is >5x median).')
        parser.add_argument('--min-group-size', type=int, default=4,
                            help='Min ILI rows in a (product, case_size) group '
                                 'before computing median. Default 4.')
        parser.add_argument('--json', default=None,
                            help='Write full outlier list as JSON to this path')

    def handle(self, *args, **opts):
        apply = opts['apply']
        vendor = opts['vendor']
        low_ratio = opts['low_ratio']
        high_ratio = opts['high_ratio']
        min_group = opts['min_group_size']

        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Price-outlier audit [{mode}] ==='))
        self.stdout.write(
            f'Thresholds: flag if unit_price < {low_ratio:.2f}× median '
            f'OR > {high_ratio:.2f}× median  (min group size: {min_group})')

        qs = (InvoiceLineItem.objects
              .filter(product__isnull=False, unit_price__isnull=False)
              .select_related('product', 'vendor'))
        if vendor:
            qs = qs.filter(vendor__name__icontains=vendor)

        # Group by (product_id, case_size). case_size None or empty handled
        # explicitly so groups don't merge across "blank" and "12/1QT".
        groups: dict[tuple, list] = defaultdict(list)
        for ili in qs.iterator():
            try:
                up = float(ili.unit_price)
            except (TypeError, ValueError):
                continue
            if up <= 0:
                continue
            cs_key = ili.case_size or '<no_case_size>'
            groups[(ili.product_id, cs_key)].append((ili, up))

        total_rows = sum(len(v) for v in groups.values())
        n_groups = sum(1 for v in groups.values() if len(v) >= min_group)
        self.stdout.write(
            f'Scanned: {total_rows} priced ILI rows in {len(groups)} '
            f'(product, case_size) groups')
        self.stdout.write(
            f'Eligible groups (>= {min_group} rows): {n_groups}')

        outliers = []
        suppressed_variant_clusters = 0
        for (pid, cs), rows in groups.items():
            if len(rows) < min_group:
                continue
            prices = [up for _, up in rows]
            med = statistics.median(prices)
            if med <= 0:
                continue
            low_threshold = med * low_ratio
            high_threshold = med * high_ratio
            for ili, up in rows:
                if up < low_threshold or up > high_threshold:
                    # Phase B (Sean 2026-05-10): lonely-outlier vs cluster
                    # peer-check. When a "low outlier" has multiple peers
                    # within 1.5x of itself, it's likely a legitimate variant
                    # cluster (Pringle Original $10 vs Pringle BBQ $11 vs
                    # Cheddar $12 — all real, all different SKUs under one
                    # canonical) NOT a parser-fragmentation phantom.
                    # Phantom-of-one (Cream Heavy $1.40 alone vs $49 median
                    # with no nearby peers) keeps firing — that's the signal
                    # we want to preserve.
                    nearby_peers = sum(
                        1 for _, p in rows
                        if 0.7 * up <= p <= 1.5 * up and p != up
                    )
                    if nearby_peers >= 2:
                        suppressed_variant_clusters += 1
                        continue
                    direction = 'LOW' if up < low_threshold else 'HIGH'
                    ratio = up / med
                    outliers.append({
                        'ili_id': ili.id,
                        'product_id': pid,
                        'canonical': ili.product.canonical_name,
                        'case_size': cs,
                        'vendor': ili.vendor.name if ili.vendor else None,
                        'invoice_date': (ili.invoice_date.isoformat()
                                         if ili.invoice_date else None),
                        'raw_description': ili.raw_description,
                        'unit_price': up,
                        'median': round(med, 2),
                        'ratio': round(ratio, 3),
                        'direction': direction,
                        'currently_flagged': ili.math_flagged,
                    })

        # Sort by ratio (most extreme first)
        outliers.sort(key=lambda o: (o['direction'] == 'HIGH', o['ratio']))

        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Outliers detected: {len(outliers)} '
            f'(suppressed {suppressed_variant_clusters} variant-cluster rows '
            f'via peer-check) ==='))

        already_flagged = sum(1 for o in outliers if o['currently_flagged'])
        new_flags = len(outliers) - already_flagged
        self.stdout.write(
            f'  Already math_flagged: {already_flagged}')
        self.stdout.write(
            f'  Newly detected:       {new_flags}')

        if not outliers:
            self.stdout.write(self.style.SUCCESS(
                '\nClean — no price outliers in scope.'))
            return

        # Show the top 30
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            '=== Top 30 outliers (by ratio extremeness) ==='))
        self.stdout.write(
            f'{"ILI":>6}  {"DIR":<4}  {"CANONICAL":<28}  '
            f'{"CASE_SZ":<10}  {"PRICE":>8}  {"MEDIAN":>8}  '
            f'{"RATIO":>6}  {"VENDOR":<22}  RAW')
        for o in outliers[:30]:
            flagged_marker = 'F' if o['currently_flagged'] else ' '
            v = (o['vendor'] or '?')[:20]
            self.stdout.write(
                f"  {o['ili_id']:>5}{flagged_marker} {o['direction']:<4}  "
                f"{o['canonical'][:26]:<28}  "
                f"{o['case_size'][:8]:<10}  "
                f"${o['unit_price']:>7.2f}  ${o['median']:>7.2f}  "
                f"{o['ratio']:>6.2f}x  {v:<22}  "
                f"{o['raw_description'][:35]}")

        if len(outliers) > 30:
            self.stdout.write(
                f'  ... + {len(outliers)-30} more (use --json for full list)')

        if opts['json']:
            Path(opts['json']).write_text(json.dumps(outliers, indent=2))
            self.stdout.write(self.style.SUCCESS(
                f'\nFull report written to {opts["json"]} '
                f'({len(outliers)} entries)'))

        if apply:
            with transaction.atomic():
                ids_to_flag = [o['ili_id'] for o in outliers
                               if not o['currently_flagged']]
                updated = (InvoiceLineItem.objects
                           .filter(id__in=ids_to_flag)
                           .update(math_flagged=True))
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                f'Applied: {updated} ILI rows newly marked math_flagged=True. '
                f'Surface in audit_math_anomalies + /mapping-review/.'))
        else:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run — no DB writes. Re-run with --apply to mark '
                'math_flagged=True on detected outliers.'))
