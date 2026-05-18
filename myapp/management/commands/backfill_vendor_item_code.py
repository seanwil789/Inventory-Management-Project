"""Backfill `InvoiceLineItem.vendor_item_code` from raw_description.

The field was added 2026-05-17 (migration 0073). Existing rows have
empty vendor_item_code; this command extracts the embedded SUPC (Sysco)
or item code from each row's raw_description and persists it. After
backfill, db_write's Phase 4f primary dedup key (vendor, invoice_number,
vendor_item_code) can match existing rows reliably on reprocess.

Strategy:
  - Sysco: 6-8 digit numeric token in raw_description = SUPC. Take the
    rightmost match (Sysco prints the SUPC after the description).
  - Farm Art: trailing numeric token (e.g. '... 1654186').
  - Other vendors: skip — leave vendor_item_code empty.

Usage:
  manage.py backfill_vendor_item_code --dry-run
  manage.py backfill_vendor_item_code --apply
  manage.py backfill_vendor_item_code --apply --vendor Sysco
"""
from __future__ import annotations
import re

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


SYSCO_SUPC_RE = re.compile(r'\b(\d{6,8})\b')
FARM_ART_CODE_RE = re.compile(r'\b(\d{6,8})\b')


def _extract_sysco_supc(raw_desc: str) -> str:
    """Rightmost 6-8 digit token in description. Sysco prints SUPC at end."""
    if not raw_desc:
        return ''
    matches = SYSCO_SUPC_RE.findall(raw_desc)
    return matches[-1] if matches else ''


def _extract_farm_art_code(raw_desc: str) -> str:
    if not raw_desc:
        return ''
    matches = FARM_ART_CODE_RE.findall(raw_desc)
    return matches[-1] if matches else ''


class Command(BaseCommand):
    help = 'Backfill vendor_item_code on existing InvoiceLineItem rows.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show extraction without saving')
        parser.add_argument('--apply', action='store_true',
                            help='Persist extracted vendor_item_code')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to one vendor (e.g. "Sysco")')

    def handle(self, *args, **opts):
        if not opts['dry_run'] and not opts['apply']:
            self.stdout.write('Pass --dry-run to preview or --apply to persist.')
            return

        qs = InvoiceLineItem.objects.filter(vendor_item_code='')
        if opts['vendor']:
            qs = qs.filter(vendor__name__iexact=opts['vendor'])

        per_vendor = {}
        sample = []
        for ili in qs.select_related('vendor'):
            vname = (ili.vendor.name if ili.vendor else '').lower()
            code = ''
            if vname == 'sysco':
                code = _extract_sysco_supc(ili.raw_description or '')
            elif vname == 'farm art':
                code = _extract_farm_art_code(ili.raw_description or '')
            else:
                continue
            if not code:
                continue
            per_vendor.setdefault(vname, {'matched': 0, 'rows': []})
            per_vendor[vname]['matched'] += 1
            if len(per_vendor[vname]['rows']) < 3:
                per_vendor[vname]['rows'].append(
                    (ili.id, (ili.raw_description or '')[:50], code))
            if opts['apply']:
                ili.vendor_item_code = code
                ili.save(update_fields=['vendor_item_code'])

        self.stdout.write(f"=== {'APPLY' if opts['apply'] else 'DRY-RUN'} report ===")
        total = 0
        for vname, stats in per_vendor.items():
            self.stdout.write(f"  {vname}: matched={stats['matched']}")
            for rid, desc, code in stats['rows']:
                self.stdout.write(f"    id={rid} code={code} desc={desc!r}")
            total += stats['matched']
        self.stdout.write(f"Total: {total} rows " +
                          ("updated" if opts['apply'] else "would be updated"))
