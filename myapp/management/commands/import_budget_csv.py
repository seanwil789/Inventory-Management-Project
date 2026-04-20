"""Import vendor+date+amount entries from the Wentworth budget CSV.

For historical months where OCR coverage is thin (especially Sysco
Jan-Mar 2026), the budget-sheet export is the authoritative record of
what was actually paid. This command reads the CSV and merges those
entries into .invoice_totals/YYYY-MM.json, tagging each as source=
"budget_csv" so it's distinguishable from pipeline-captured entries.

Usage:
  python manage.py import_budget_csv <csv_path>
  python manage.py import_budget_csv "Men's Wentworth Food Budget 2026(Mar).csv"
  python manage.py import_budget_csv <csv_path> --dry-run
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

# Budget CSV vendor labels → canonical vendor names
CSV_VENDOR_MAP = {
    "Sysco":                         "Sysco",
    "FarmArt":                       "Farm Art",
    "Farm Art":                      "Farm Art",
    "Exceptional":                   "Exceptional Foods",
    "Exceptional Foods":             "Exceptional Foods",
    "PBM":                           "Philadelphia Bakery Merchants",
    "Philadelphia Bakery Merchants": "Philadelphia Bakery Merchants",
    "Delaware County Linen":         "Delaware County Linen",
    "Colonial Village Meat Markets": "Colonial Village Meat Markets",
    "Colonial Meat":                 "Colonial Village Meat Markets",
    "Aramark":                       "Aramark",
}


class Command(BaseCommand):
    help = 'Import budget-sheet CSV entries into .invoice_totals/ cache.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path', type=str)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        csv_path = Path(opts['csv_path'])
        if not csv_path.is_absolute():
            csv_path = Path(settings.BASE_DIR) / csv_path
        if not csv_path.exists():
            self.stderr.write(f'CSV not found: {csv_path}')
            return

        entries = self._parse_csv(csv_path)
        if not entries:
            self.stdout.write('No entries parsed from CSV.')
            return

        # Group by YYYY-MM
        by_month: dict[str, list[dict]] = {}
        for e in entries:
            key = e['date'][:7]
            by_month.setdefault(key, []).append(e)

        totals_dir = Path(settings.BASE_DIR) / '.invoice_totals'
        totals_dir.mkdir(exist_ok=True)

        for month, new_entries in sorted(by_month.items()):
            path = totals_dir / f'{month}.json'
            existing = []
            if path.exists():
                try:
                    with open(path) as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    existing = []

            # Key by (vendor, date, total) — replace budget_csv entries on
            # re-import, skip entries already captured by the pipeline
            # (those have an invoice_number OR a real filename in source_file).
            pipeline_keys = {
                (e['vendor'], e.get('date', ''), round(float(e.get('total', 0)), 2))
                for e in existing
                if e.get('invoice_number')
                or (e.get('source_file', '') and '.' in e.get('source_file', ''))
            }

            filtered_existing = [
                e for e in existing
                if e.get('source') != 'budget_csv'  # drop prior budget_csv
            ]

            new_additions = []
            for e in new_entries:
                k = (e['vendor'], e['date'], round(float(e['total']), 2))
                if k in pipeline_keys:
                    continue  # pipeline has it already, don't duplicate
                new_additions.append(e)

            merged = filtered_existing + new_additions
            merged.sort(key=lambda x: (x.get('date', ''), x.get('vendor', '')))

            month_total = sum(e['total'] for e in merged)
            csv_count = sum(1 for e in merged if e.get('source') == 'budget_csv')

            self.stdout.write(
                f'{month}: {len(merged)} entries (${month_total:.2f}) — '
                f'{csv_count} from CSV, {len(merged) - csv_count} from pipeline'
            )

            if not opts['dry_run']:
                with open(path, 'w') as f:
                    json.dump(merged, f, indent=2)

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('\n(dry run — no writes)'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nImported {len(entries)} CSV rows'))

    def _parse_csv(self, csv_path: Path) -> list[dict]:
        entries = []
        with open(csv_path) as f:
            reader = csv.reader(f)
            for row in reader:
                # Row structure: _,date,vendor,amount,_,_,_,_,_,_,...
                if len(row) < 4:
                    continue
                date_str = row[1].strip()
                vendor_raw = row[2].strip()
                amount_str = row[3].strip()

                if not date_str or not vendor_raw or not amount_str:
                    continue
                if vendor_raw in ('Store', 'Total Expenses', ''):
                    continue

                # Parse date — CSV uses "Tuesday, March 3, 2026" OR "3/27/2026"
                date_iso = self._parse_date(date_str)
                if not date_iso:
                    continue

                # Parse amount — format " $958.75 " or " $1,478.10 "
                amount_clean = amount_str.replace('$', '').replace(',', '').strip()
                try:
                    amount = float(amount_clean)
                except ValueError:
                    continue
                if amount <= 0:
                    continue

                canonical_vendor = CSV_VENDOR_MAP.get(vendor_raw)
                if not canonical_vendor:
                    continue  # ignore unknown vendor rows

                entries.append({
                    'vendor': canonical_vendor,
                    'date': date_iso,
                    'total': round(amount, 2),
                    'source': 'budget_csv',
                    'source_file': csv_path.name,
                })
        return entries

    def _parse_date(self, s: str) -> str | None:
        for fmt in ('%A, %B %d, %Y', '%B %d, %Y', '%m/%d/%Y', '%m/%d/%y',
                    '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return None
