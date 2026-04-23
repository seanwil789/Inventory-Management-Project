"""Backfill InvoiceLineItem.price_per_pound from cached OCR text.

Track B ships the field going forward via db_write, but the 1,757 existing
rows need a one-shot backfill. The parser (parser.py:891, :1313) computes
$/lb deterministically from raw OCR text, so replaying the cache recovers
the field without touching any other column.

Matching strategy (strict — preserves accuracy):
  1. For each `*_docai_ocr.json` cache, re-parse through parser.parse_invoice.
  2. For each parsed item that has price_per_unit, find the ILI row by the
     4-field key (vendor, invoice_date, raw_description, unit_price).
  3. Update ONLY price_per_pound. Never rewrites any other field.
  4. Skip rows whose price_per_pound is already set (idempotent re-runs).

No match / ambiguous match → counted, skipped, reported. Never guess.

Usage:
  python manage.py backfill_price_per_pound               # dry-run
  python manage.py backfill_price_per_pound --apply       # writes
  python manage.py backfill_price_per_pound --vendor Sysco
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


def _match_and_update(items: list[dict], vendor: str, parsed_date,
                      dry_run: bool = True) -> tuple[int, int, int, int]:
    """For each item with a price_per_unit value, locate the matching ILI
    row by (vendor, date, raw_description, unit_price) and update
    price_per_pound. Returns (updated, no_match, ambiguous, already_set).

    Never updates a row whose price_per_pound is already populated, so
    repeated runs are idempotent even if the parser's output drifts.
    """
    updated = no_match = ambiguous = already_set = 0

    for item in items:
        ppu = item.get('price_per_unit')
        if ppu in (None, ''):
            continue
        try:
            ppu_decimal = Decimal(str(ppu))
        except (InvalidOperation, ValueError):
            continue

        up_raw = item.get('unit_price')
        if up_raw in (None, ''):
            continue
        try:
            unit_price_decimal = Decimal(str(up_raw))
        except (InvalidOperation, ValueError):
            continue

        qs = InvoiceLineItem.objects.filter(
            vendor__name=vendor,
            invoice_date=parsed_date,
            raw_description=item.get('raw_description', ''),
            unit_price=unit_price_decimal,
        )
        n = qs.count()
        if n == 0:
            no_match += 1
            continue
        if n > 1:
            ambiguous += 1
            continue

        ili = qs.first()
        if ili.price_per_pound is not None:
            already_set += 1
            continue
        if not dry_run:
            ili.price_per_pound = ppu_decimal
            ili.save(update_fields=['price_per_pound'])
        updated += 1

    return updated, no_match, ambiguous, already_set


class Command(BaseCommand):
    help = 'Backfill InvoiceLineItem.price_per_pound from cached OCR text.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write updates. Default is dry-run.')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor (e.g. "Sysco").')
        parser.add_argument('--cache-dir', type=str, default=None,
                            help='Override OCR cache directory (for tests).')

    def handle(self, *args, **opts):
        sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
        from parser import parse_invoice  # noqa: E402

        cache_dir = (Path(opts['cache_dir']) if opts['cache_dir']
                     else Path(settings.BASE_DIR) / '.ocr_cache')
        if not cache_dir.exists():
            self.stderr.write(f'OCR cache not found at {cache_dir}')
            return

        caches: list[tuple[str, dict]] = []
        for p in glob.glob(str(cache_dir / '*_docai_ocr.json')):
            try:
                with open(p) as f:
                    d = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            vendor = d.get('vendor', 'Unknown')
            if opts['vendor'] and vendor != opts['vendor']:
                continue
            caches.append((p, d))

        if not caches:
            self.stdout.write('No matching caches found.')
            return

        self.stdout.write(f'Scanning {len(caches)} OCR cache(s)...')

        total_updated = total_no_match = total_ambig = total_already = 0
        parse_failures = 0

        for path, cache in caches:
            vendor = cache.get('vendor', 'Unknown')
            raw_text = cache.get('raw_text', '')
            inv_date_str = cache.get('invoice_date', '')
            if not raw_text or not inv_date_str:
                continue
            try:
                parsed_date = datetime.strptime(inv_date_str, '%Y-%m-%d').date()
            except ValueError:
                continue
            try:
                parsed = parse_invoice(raw_text, vendor=vendor)
            except Exception as e:
                parse_failures += 1
                self.stderr.write(f'  [!] parse failed on '
                                  f'{os.path.basename(path)}: {e}')
                continue

            u, n, a, s = _match_and_update(
                parsed.get('items', []),
                vendor,
                parsed_date,
                dry_run=not opts['apply'],
            )
            total_updated += u
            total_no_match += n
            total_ambig += a
            total_already += s

        verb = 'Would update' if not opts['apply'] else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} price_per_pound on {total_updated} row(s). '
            f'No match: {total_no_match}. '
            f'Ambiguous: {total_ambig}. '
            f'Already set: {total_already}.'))
        if parse_failures:
            self.stdout.write(self.style.WARNING(
                f'Parse failures: {parse_failures}'))
        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                'Dry run — re-run with --apply to write.'))
