"""Backfill `InvoiceLineItem.vendor_item_code` by re-parsing OCR caches
and matching back to existing ILIs by stable fields.

The field was added 2026-05-17 (migration 0073). Existing rows have
empty vendor_item_code; this command re-parses each invoice's OCR
cache to obtain the parser's explicit `sysco_item_code` per line, then
matches parser-output items to existing ILIs by (invoice_number,
unit_price, extended_amount) — fields that ARE stable across parser
versions. On match, sets vendor_item_code without otherwise changing
the ILI.

This is safer than regex-extracting from raw_description (descs are
unstable across parser versions; SUPC formats vary 4-13 digits).

Usage:
  manage.py backfill_vendor_item_code --dry-run
  manage.py backfill_vendor_item_code --apply
  manage.py backfill_vendor_item_code --apply --vendor Sysco
"""
from __future__ import annotations
import glob
import json
import os
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem

sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
from parser import parse_invoice  # noqa: E402


class Command(BaseCommand):
    help = ('Backfill vendor_item_code on existing InvoiceLineItem rows '
            'by re-parsing OCR caches and matching by (invoice, price, ext).')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show matches without saving')
        parser.add_argument('--apply', action='store_true',
                            help='Persist matched vendor_item_code')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to one vendor (e.g. "Sysco")')

    def handle(self, *args, **opts):
        if not opts['dry_run'] and not opts['apply']:
            self.stdout.write('Pass --dry-run to preview or --apply to persist.')
            return

        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not ocr_dir.exists():
            self.stderr.write(f'OCR cache not found at {ocr_dir}')
            return

        # Build a (invoice_number, unit_price, extended_amount) → SUPC map
        # by parsing every cache, then update ILIs matching the key.
        wanted_vendor = (opts['vendor'] or '').lower()
        key_to_code: dict[tuple, str] = {}
        caches_processed = 0
        for cache_path in glob.glob(str(ocr_dir / '*_docai_ocr.json')):
            try:
                with open(cache_path) as f:
                    doc = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            vendor = doc.get('vendor', '')
            if wanted_vendor and vendor.lower() != wanted_vendor:
                continue
            try:
                parsed = parse_invoice(
                    doc.get('raw_text', ''),
                    vendor=vendor,
                    pages=doc.get('pages'),
                )
            except Exception as e:
                self.stderr.write(f'parse failed for {cache_path[-30:]}: {e}')
                continue
            inv_num = parsed.get('invoice_number') or ''
            if not inv_num:
                continue
            caches_processed += 1
            for it in parsed.get('items') or []:
                code = (it.get('sysco_item_code')
                        or it.get('vendor_item_code')
                        or it.get('item_code') or '')
                if not code:
                    continue
                up = it.get('unit_price')
                ext = it.get('extended_amount')
                if up is None or ext is None:
                    continue
                key = (inv_num, round(float(up), 2), round(float(ext), 2))
                # First write wins (deterministic across re-parses)
                key_to_code.setdefault(key, str(code))

        self.stdout.write(f'Parsed {caches_processed} caches; '
                          f'{len(key_to_code)} (invoice, price, ext) keys mapped to codes.')

        # Match existing ILIs and update
        qs = InvoiceLineItem.objects.filter(vendor_item_code='')
        if opts['vendor']:
            qs = qs.filter(vendor__name__iexact=opts['vendor'])

        matched = 0
        per_invoice: dict[str, int] = {}
        for ili in qs.select_related('vendor'):
            up = ili.unit_price
            ext = ili.extended_amount
            if up is None or ext is None or not ili.invoice_number:
                continue
            key = (ili.invoice_number, round(float(up), 2), round(float(ext), 2))
            code = key_to_code.get(key)
            if not code:
                continue
            matched += 1
            per_invoice[ili.invoice_number] = per_invoice.get(ili.invoice_number, 0) + 1
            if opts['apply']:
                ili.vendor_item_code = code
                ili.save(update_fields=['vendor_item_code'])

        self.stdout.write(f"=== {'APPLY' if opts['apply'] else 'DRY-RUN'} report ===")
        self.stdout.write(f"Total ILIs matched: {matched}")
        for inv, n in sorted(per_invoice.items())[:10]:
            self.stdout.write(f"  {inv}: {n}")
        if len(per_invoice) > 10:
            self.stdout.write(f"  ... and {len(per_invoice) - 10} more invoices")
