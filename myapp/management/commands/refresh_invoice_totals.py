"""Rebuild .invoice_totals/YYYY-MM.json from the OCR cache.

Replays each cached invoice through the current parser to recover
invoice_total and groups multi-photo invoices by invoice_number so a
single physical invoice produces one cache entry (not one per photo).

Usage:
  python manage.py refresh_invoice_totals                      # current month
  python manage.py refresh_invoice_totals --month 2026 4       # specific month
  python manage.py refresh_invoice_totals --all-months         # every month
  python manage.py refresh_invoice_totals --dry-run            # preview only

Primary grouping key: (vendor, invoice_number). Falls back to
(vendor, date, file_hash) when the invoice_number can't be extracted.
When multiple photos of the same invoice_number produce different totals,
the max is taken — the "LAST PAGE" photo carries the definitive total and
earlier photos typically report partial GROUP TOTAL sums.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
from parser import parse_invoice, extract_sysco_metadata  # noqa: E402


class Command(BaseCommand):
    help = 'Rebuild .invoice_totals/YYYY-MM.json from OCR cache with invoice-number grouping.'

    def add_arguments(self, parser):
        parser.add_argument('--month', nargs=2, type=int, metavar=('YEAR', 'MONTH'),
                            help='Refresh a specific month (default: current month)')
        parser.add_argument('--all-months', action='store_true',
                            help='Rebuild cache for every month present in OCR')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would change without writing')

    def handle(self, *args, **opts):
        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        totals_dir = Path(settings.BASE_DIR) / '.invoice_totals'
        totals_dir.mkdir(exist_ok=True)

        if not ocr_dir.exists():
            self.stdout.write(self.style.ERROR(f'OCR cache not found at {ocr_dir}'))
            return

        if opts['all_months']:
            months = self._months_in_cache(ocr_dir)
        elif opts['month']:
            months = [tuple(opts['month'])]
        else:
            today = date.today()
            months = [(today.year, today.month)]

        for year, month in months:
            self._refresh(year, month, ocr_dir, totals_dir, opts['dry_run'])

    def _months_in_cache(self, ocr_dir: Path) -> list[tuple[int, int]]:
        months: set[tuple[int, int]] = set()
        for p in glob.glob(str(ocr_dir / '*_docai_ocr.json')):
            try:
                with open(p) as f:
                    d = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            m = re.match(r'^(\d{4})-(\d{2})-', d.get('invoice_date', ''))
            if m:
                months.add((int(m.group(1)), int(m.group(2))))
        return sorted(months)

    def _refresh(self, year: int, month: int, ocr_dir: Path,
                 totals_dir: Path, dry_run: bool) -> None:
        month_prefix = f'{year:04d}-{month:02d}'

        # Pass 1: load every cache for this month
        caches = []
        for p in glob.glob(str(ocr_dir / '*_docai_ocr.json')):
            try:
                with open(p) as f:
                    d = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not d.get('invoice_date', '').startswith(month_prefix):
                continue
            caches.append((p, d))

        if not caches:
            self.stdout.write(f'{month_prefix}: no caches found — skipping')
            return

        # Pass 2: re-parse each, extract (vendor, invoice_number, date, total)
        parsed = []
        parse_failures = 0
        for path, cache in caches:
            vendor = cache.get('vendor', 'Unknown')
            raw_text = cache.get('raw_text', '')
            inv_date = cache.get('invoice_date', '')
            inv_num = self._extract_invoice_number(raw_text, vendor)
            try:
                result = parse_invoice(raw_text, vendor=vendor)
                total = result.get('invoice_total')
            except Exception as e:
                self.stderr.write(f'  [!] parser error on {os.path.basename(path)}: {e}')
                parse_failures += 1
                total = None
            file_hash = os.path.basename(path).split('_')[0][:16]
            parsed.append({
                'vendor': vendor,
                'date': inv_date,
                'invoice_number': inv_num,
                'total': total,
                'source_file': file_hash,
            })

        # Pass 3: group by (vendor, invoice_number) — pick max total per group.
        # Fall back to (vendor, date, source_file) when invoice_number unknown.
        groups: dict[tuple, dict] = {}
        for e in parsed:
            if e['total'] is None or e['total'] <= 0:
                continue
            if e['invoice_number']:
                key = ('num', e['vendor'], e['invoice_number'])
            else:
                key = ('file', e['vendor'], e['date'], e['source_file'])
            existing = groups.get(key)
            if existing is None or e['total'] > existing['total']:
                groups[key] = e

        # Also retain any entries from the existing cache that carry real
        # filenames (from live pipeline processing) — those have provenance
        # we can't recover from OCR hashes. Merge by (vendor, invoice_number)
        # when possible, otherwise by (vendor, date, source_file).
        out_path = totals_dir / f'{month_prefix}.json'
        existing_entries = []
        if out_path.exists():
            try:
                with open(out_path) as f:
                    existing_entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing_entries = []

        for e in existing_entries:
            # If this existing entry has an invoice_number and we already have
            # that group, skip — we already have the authoritative total.
            # Otherwise, preserve the entry's source_file provenance.
            inv_num = e.get('invoice_number')
            if inv_num:
                key = ('num', e['vendor'], inv_num)
            else:
                key = ('file', e['vendor'], e.get('date', ''), e.get('source_file', ''))
            if key in groups:
                # Keep the better provenance if the existing entry has a real filename
                src = e.get('source_file', '')
                if src and not src.startswith(groups[key].get('source_file', '')):
                    if '.' in src:  # looks like a filename
                        groups[key]['source_file'] = src
                continue
            # Entry not represented in new groups — keep as-is (late-arriving
            # data or a type we didn't re-parse successfully)
            groups[key] = e

        # Post-merge: dedup by (vendor, date, total) for entries that share the
        # same amount — whether or not they carry an invoice_number. This folds
        # together photos of the same invoice whose numbers OCR differently (or
        # not at all) but whose parsed totals agree. Prefer the entry that has
        # an invoice_number.
        def _amt_key(e):
            return (e['vendor'], e.get('date', ''), round(float(e['total']), 2))

        seen_by_amt: dict[tuple, dict] = {}
        for e in groups.values():
            k = _amt_key(e)
            cur = seen_by_amt.get(k)
            if cur is None:
                seen_by_amt[k] = e
            elif e.get('invoice_number') and not cur.get('invoice_number'):
                seen_by_amt[k] = e
        filtered = list(seen_by_amt.values())

        entries = [
            {k: v for k, v in e.items() if k != 'invoice_number' or v}
            for e in filtered
        ]
        entries.sort(key=lambda x: (x.get('date', ''), x.get('vendor', '')))

        month_total = sum(e['total'] for e in entries)

        self.stdout.write(
            f'{month_prefix}: {len(caches)} caches → {len(entries)} invoice entries '
            f'(total ${month_total:.2f})'
            + (f'  [!] {parse_failures} parse failures' if parse_failures else '')
        )
        for e in entries:
            inv = f"#{e.get('invoice_number','?')}" if e.get('invoice_number') else '(no num)'
            self.stdout.write(
                f'    {e["date"]}  {e["vendor"]:22s}  {inv:12s}  ${e["total"]:.2f}'
            )

        if dry_run:
            self.stdout.write(self.style.WARNING('  (dry run — no write)'))
            return

        with open(out_path, 'w') as f:
            json.dump(entries, f, indent=2)
        self.stdout.write(self.style.SUCCESS(f'  Wrote {out_path}'))

    def _extract_invoice_number(self, raw_text: str, vendor: str) -> str | None:
        """Best-effort invoice-number extraction per vendor."""
        if not raw_text:
            return None
        if vendor == 'Sysco':
            meta = extract_sysco_metadata(raw_text)
            # Prefer invoice_number; fall back to manifest when INVOICE NUMBER
            # header isn't on the captured page (common on LAST-PAGE-only photos)
            return meta.get('invoice_number') or meta.get('manifest')
        if vendor == 'Farm Art':
            # Farm Art: "Invoice\n<number>" or "Invoice: <number>"
            m = re.search(r'Invoice\s*(?:No\.?|Number|#)?\s*[:\n]?\s*(\d{5,10})',
                          raw_text, re.IGNORECASE)
            if m:
                return m.group(1)
        if vendor == 'Exceptional Foods':
            m = re.search(r'Invoice\s*(?:No\.?|Number|#)?\s*[:\n]?\s*(\d{4,10})',
                          raw_text, re.IGNORECASE)
            if m:
                return m.group(1)
        if vendor == 'Philadelphia Bakery Merchants':
            m = re.search(r'Invoice\s*(?:No\.?|Number|#)?\s*[:\n]?\s*(\d{4,10})',
                          raw_text, re.IGNORECASE)
            if m:
                return m.group(1)
        return None
