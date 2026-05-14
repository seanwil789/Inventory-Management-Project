"""Backfill `invoice_number` on existing InvoiceLineItem rows that were
ingested before parser.py's `extract_invoice_number` learned the 2x2
grid OCR layout pattern (2026-05-14 fix).

Distinct from `backfill_invoice_number_on_ili.py` which uses IVS rows
as the source of truth — that command can't help when invoice_number
was never extracted (no IVS row exists). This command re-runs
`extract_invoice_number` against the on-disk OCR cache for each affected
source_file.

For each ILI row with empty `invoice_number`:
  1. Locate OCR cache by source_file prefix in `.ocr_cache/`.
  2. Load `raw_text` from the `_docai_ocr.json` file.
  3. Re-run `extract_invoice_number(raw_text, vendor_name)`.
  4. If a number comes back, update the row (and all sibling rows
     sharing the same source_file) with the new invoice_number.

Read-only by default. --apply commits inside a transaction.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import InvoiceLineItem


def _extract_invoice_number(raw_text: str, vendor: str) -> str | None:
    """Shim: import the parser helper from invoice_processor/."""
    path = str(settings.BASE_DIR / 'invoice_processor')
    if path not in sys.path:
        sys.path.insert(0, path)
    from parser import extract_invoice_number  # noqa: E402
    return extract_invoice_number(raw_text, vendor)


def _locate_ocr_cache(source_file: str) -> str | None:
    """Find the `_docai_ocr.json` file for a given source_file prefix.
    Returns absolute path or None when no match.
    """
    cache_dir = settings.BASE_DIR / '.ocr_cache'
    if not cache_dir.exists():
        return None
    # source_file is typically a 16-char sha256 prefix (e.g.
    # '97a1bf8ec66dc9c4') but can include a '+N' page suffix
    # (e.g. '288614761e8750c6+1'). Strip the +N before globbing.
    base = source_file.split('+')[0]
    matches = glob.glob(str(cache_dir / f'{base}*_docai_ocr.json'))
    return matches[0] if matches else None


class Command(BaseCommand):
    help = ('Backfill invoice_number on ILI rows where extraction '
            'failed pre-2026-05-14 (2x2 grid OCR layout fix).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Default is dry-run.')
        parser.add_argument('--vendor', default=None,
                            help='Restrict to one vendor name (icontains).')
        parser.add_argument('--since', default=None,
                            help='Restrict to invoices on/after this date (YYYY-MM-DD).')

    def handle(self, *args, **opts):
        apply = opts['apply']
        vendor_filter = opts['vendor']
        since = opts['since']
        mode = 'APPLY' if apply else 'DRY-RUN'

        qs = InvoiceLineItem.objects.filter(invoice_number='')
        if vendor_filter:
            qs = qs.filter(vendor__name__icontains=vendor_filter)
        if since:
            qs = qs.filter(invoice_date__gte=since)

        # Group by (vendor, source_file) — one OCR cache lookup per group
        groups: dict[tuple[str, str], list[InvoiceLineItem]] = defaultdict(list)
        for ili in qs.select_related('vendor'):
            vname = ili.vendor.name if ili.vendor else ''
            groups[(vname, ili.source_file)].append(ili)

        self.stdout.write(
            self.style.HTTP_INFO(
                f'[{mode}] {qs.count()} ILI row(s) with empty invoice_number '
                f'across {len(groups)} (vendor, source_file) group(s)\n'
            )
        )

        resolved = 0
        unresolved = 0
        no_cache = 0
        ili_to_update: list[tuple[InvoiceLineItem, str]] = []

        for (vname, src), rows in sorted(groups.items()):
            ocr_path = _locate_ocr_cache(src)
            if not ocr_path:
                no_cache += 1
                self.stdout.write(
                    f'  [no-cache] {vname:<30} src={src!r:<35} '
                    f'({len(rows)} row{"s" if len(rows) != 1 else ""})'
                )
                continue
            try:
                with open(ocr_path) as f:
                    data = json.load(f)
                raw_text = data.get('raw_text') or ''
            except Exception as e:
                no_cache += 1
                self.stdout.write(f'  [load-err] {vname} src={src}: {e}')
                continue

            inv_num = _extract_invoice_number(raw_text, vname)
            if inv_num:
                resolved += 1
                tag = 'WOULD UPDATE' if not apply else 'UPDATING'
                self.stdout.write(
                    f'  [{tag}] {vname:<30} src={src!r:<35} -> '
                    f'invoice_number={inv_num!r}  ({len(rows)} row{"s" if len(rows) != 1 else ""})'
                )
                for r in rows:
                    ili_to_update.append((r, inv_num))
            else:
                unresolved += 1
                self.stdout.write(
                    f'  [no-extract] {vname:<30} src={src!r:<35} '
                    f'({len(rows)} row{"s" if len(rows) != 1 else ""})'
                )

        self.stdout.write('')
        self.stdout.write(
            f'Summary: resolved={resolved} groups, unresolved={unresolved}, '
            f'no_cache={no_cache}; total ILI rows to update={len(ili_to_update)}'
        )

        if apply and ili_to_update:
            with transaction.atomic():
                for r, inv_num in ili_to_update:
                    r.invoice_number = inv_num
                    r.save(update_fields=['invoice_number'])
            self.stdout.write(self.style.SUCCESS(
                f'\nApplied: {len(ili_to_update)} ILI row(s) updated.'
            ))
        elif not apply and ili_to_update:
            self.stdout.write(self.style.WARNING(
                f'\nDry-run only. Re-run with --apply to commit '
                f'{len(ili_to_update)} update(s).'
            ))
