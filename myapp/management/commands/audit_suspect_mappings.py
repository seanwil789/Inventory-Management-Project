"""Audit InvoiceLineItem rows for mappings where the canonical Product
name and the raw invoice description share no meaningful word tokens.

These are almost always wrong mappings — a real product would have at
least one 3+letter word in common between its canonical name and the
invoice description. When it doesn't, the ProductMapping row that
produced the match is probably wrong.

Known examples in production data (2026-04):
  - "Mop Heads" ← "Bib Aprons - White"  (Linen product wrong-mapped)
  - "White Bread" ← "Club White"          (PBM product wrong-mapped)

Usage:
    python manage.py audit_suspect_mappings
    python manage.py audit_suspect_mappings --min-count 3
    python manage.py audit_suspect_mappings --vendor "Farm Art"
    python manage.py audit_suspect_mappings --json out.json
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from myapp.models import InvoiceLineItem


# Sysco brand-prefix tokens — noise, not product identity
_NOISE = {
    'whlfcls', 'grecosn', 'coopr', 'emba', 'ssa', 'sys', 'cls', 'imp', 'cur',
    'ckd', 'bnls', 'sysfpnat', 'syfpnat', 'sysclb', 'sycor', 'syfs',
    'patrpck', 'kontos', 'bbrlimp', 'bbrlcls', 'fleishm', 'arezcls', 'arezimp',
    'calmini', 'delmnt', 'spart', 'intl', 'packer', 'leport', 'portcls',
    'portprd', 'altacuc', 'versfnr', 'steramn', 'millbak', 'highbak',
    'suprptz', 'inaugthom', 'thrcrab', 'maeploy', 'minmaid', 'jdmtcls',
    'casacls', 'simplot', 'pillsby', 'hormel', 'keyston', 'ecolab',
    'heinz', 'regina', 'roland', 'gatorade', 'labella', 'tropcna',
    'lacroix', 'sysprm', 'sysgrd', 'sysrel',
}


def _stems(s: str) -> set[str]:
    """3+letter word tokens with naive plural stripping + brand-prefix
    removal. 'pineapples' and 'pineapple' collapse to the same stem so
    canonical 'Pineapple' and raw 'PINEAPPLES' overlap."""
    stems = set()
    for t in re.findall(r'[A-Za-z]{3,}', s or ''):
        low = t.lower()
        if low in _NOISE:
            continue
        # Strip trailing 's' for simple plurals (≥4 chars, not ending in 'ss').
        # Catches 'eggs'→'egg', 'onions'→'onion', 'pineapples'→'pineapple'.
        # Too aggressive to catch irregulars (mice, geese) but covers the
        # common case. 'ss' exclusion protects 'grass', 'glass'.
        if len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
            low = low[:-1]
        stems.add(low)
    return stems


class Command(BaseCommand):
    help = 'Report InvoiceLineItem rows where canonical and raw_desc share no tokens.'

    def add_arguments(self, parser):
        parser.add_argument('--min-count', type=int, default=1,
                            help='Only report (product, raw_desc) pairs seen N+ times (default 1)')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor')
        parser.add_argument('--json', type=str, default=None,
                            help='Write full report as JSON to this path')

    def handle(self, *args, **opts):
        qs = (InvoiceLineItem.objects
              .filter(product__isnull=False)
              .exclude(raw_description='')
              .select_related('product', 'vendor'))

        if opts['vendor']:
            qs = qs.filter(vendor__name__icontains=opts['vendor'])

        # Group by (product_id, normalized raw_desc) to dedupe repeats
        groups: dict[tuple, dict] = defaultdict(
            lambda: {'count': 0, 'dates': [], 'vendors': set(),
                     'canonical': '', 'raw_desc': '', 'product_id': None})

        suspect_count = 0
        total_scanned = 0

        for ili in qs.iterator():
            total_scanned += 1
            canon_stems = _stems(ili.product.canonical_name)
            desc_stems = _stems(ili.raw_description)

            # Guard: need meaningful content on both sides to call it suspect
            if len(canon_stems) < 1 or len(desc_stems) < 2:
                continue
            if canon_stems & desc_stems:
                continue  # at least one token overlaps — not suspect

            suspect_count += 1
            key = (ili.product_id, ili.raw_description.strip().lower())
            g = groups[key]
            g['count'] += 1
            g['canonical'] = ili.product.canonical_name
            g['raw_desc'] = ili.raw_description
            g['product_id'] = ili.product_id
            if ili.vendor:
                g['vendors'].add(ili.vendor.name)
            g['dates'].append(ili.invoice_date.isoformat() if ili.invoice_date else '')

        # Filter by min_count + sort by count descending
        filtered = sorted(
            [(k, v) for k, v in groups.items() if v['count'] >= opts['min_count']],
            key=lambda kv: -kv[1]['count'],
        )

        # Report
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Suspect mapping audit ==='))
        self.stdout.write(
            f'Total scanned: {total_scanned} InvoiceLineItem rows with product set\n'
            f'Suspect rows (zero token overlap): {suspect_count}\n'
            f'Unique (product, raw_desc) pairs: {len(groups)}\n'
            f'Pairs shown (min_count={opts["min_count"]}): {len(filtered)}\n')

        if not filtered:
            self.stdout.write(self.style.SUCCESS('No suspect mappings at this threshold.'))
            return

        self.stdout.write(self.style.HTTP_INFO(
            '\n=== Suspects (by frequency, highest first) ==='))
        for (pid, _), g in filtered[:50]:
            vendors = ', '.join(sorted(g['vendors'])) or '—'
            dates = sorted(set(g['dates']))
            date_range = f"{dates[0][:7]} → {dates[-1][:7]}" if len(dates) > 1 else (dates[0] if dates else '—')
            self.stdout.write(
                f'  [×{g["count"]:3d}]  {g["canonical"]:<35}  ←  "{g["raw_desc"][:50]}"')
            self.stdout.write(
                f'           vendors: {vendors:<30}  dates: {date_range}')

        if len(filtered) > 50:
            self.stdout.write(f'  ... + {len(filtered) - 50} more (use --json for full list)')

        if opts['json']:
            out = [
                {
                    'count': v['count'],
                    'canonical': v['canonical'],
                    'raw_description': v['raw_desc'],
                    'product_id': v['product_id'],
                    'vendors': sorted(v['vendors']),
                    'first_date': min(v['dates']) if v['dates'] else None,
                    'last_date': max(v['dates']) if v['dates'] else None,
                }
                for (_, _), v in filtered
            ]
            Path(opts['json']).write_text(json.dumps(out, indent=2))
            self.stdout.write(self.style.SUCCESS(
                f'\n  Full report written to {opts["json"]} ({len(out)} entries)'))

        self.stdout.write(self.style.WARNING(
            '\nFix path: find each raw_description in the Google Sheets "Item Mapping"\n'
            'tab and correct the canonical (column F). Then `python invoice_processor/\n'
            'cleanup_mappings.py --apply` + `python manage.py reprocess_invoices` to\n'
            'pick up corrections.'))
