"""Delete ILIs whose vendor_item_code is NOT a real parser-derived SUPC
for their invoice. These are bad-backfill artifacts — the naive backfill
(regex over raw_description) sometimes grabbed UPC fragments instead of
the actual Sysco SUPC, leaving rows with codes that don't match what
the parser produces.

Strategy:
  For each invoice (by invoice_number):
    1. Re-parse the OCR cache via parser.parse_invoice
    2. Collect the set of TRUE SUPCs the parser emits for that invoice
    3. For each DB ILI on that invoice with a non-empty vendor_item_code:
       - If its code is in the true-SUPC set → keep
       - If not → mark for deletion (unless user_edited)

Safety: preserves user_edited rows even if their code is bad.
Conservative: only deletes when re-parse succeeded AND the row's code
is definitively not in the parser's output (skips invoices where
re-parse failed).
"""
from __future__ import annotations
import glob
import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem

sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
from parser import parse_invoice  # noqa: E402


class Command(BaseCommand):
    help = ('Delete ILIs with vendor_item_codes that do not match any '
            'parser-derived SUPC for their invoice.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--vendor', type=str, default=None)

    def handle(self, *args, **opts):
        if not opts['dry_run'] and not opts['apply']:
            self.stdout.write('Pass --dry-run or --apply')
            return

        ocr_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        wanted_vendor = (opts['vendor'] or '').lower()

        # Build invoice_number → set of true SUPCs from parser
        true_supcs: dict[str, set[str]] = {}
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
                parsed = parse_invoice(doc.get('raw_text', ''),
                                       vendor=vendor,
                                       pages=doc.get('pages'))
            except Exception:
                continue
            inv = parsed.get('invoice_number') or ''
            if not inv:
                continue
            codes = set()
            for it in parsed.get('items') or []:
                code = it.get('sysco_item_code') or ''
                if code:
                    codes.add(str(code))
            if codes:
                true_supcs.setdefault(inv, set()).update(codes)

        # Find ILIs with codes not in true_supcs[invoice]
        qs = InvoiceLineItem.objects.exclude(vendor_item_code='').exclude(
            invoice_number='').select_related('vendor')
        if opts['vendor']:
            qs = qs.filter(vendor__name__iexact=opts['vendor'])

        to_delete: list[InvoiceLineItem] = []
        no_truth: list[InvoiceLineItem] = []
        for ili in qs:
            truth = true_supcs.get(ili.invoice_number)
            if truth is None:
                no_truth.append(ili)
                continue
            if ili.vendor_item_code in truth:
                continue
            if ili.user_edited:
                continue
            to_delete.append(ili)

        per_invoice: dict[str, int] = {}
        for ili in to_delete:
            per_invoice[ili.invoice_number] = per_invoice.get(ili.invoice_number, 0) + 1

        self.stdout.write(f"Invoices with parser truth set: {len(true_supcs)}")
        self.stdout.write(f"ILIs with no truth (parse failed or no invoice match): {len(no_truth)}")
        self.stdout.write(f"ILIs with bad vendor_item_code (not in parser output): {len(to_delete)}")
        for inv, n in sorted(per_invoice.items(), key=lambda x: -x[1])[:15]:
            self.stdout.write(f"  {inv}: {n}")

        # Sample to inspect
        self.stdout.write("\nSample (first 10):")
        for ili in to_delete[:10]:
            self.stdout.write(
                f"  id={ili.id} inv={ili.invoice_number} code={ili.vendor_item_code!r} "
                f"desc={(ili.raw_description or '')[:55]!r}")

        if opts['apply'] and to_delete:
            ids = [i.id for i in to_delete]
            InvoiceLineItem.objects.filter(id__in=ids).delete()
            self.stdout.write(f"\nDELETED {len(ids)} rows with bad vendor_item_code.")
        elif opts['dry_run']:
            self.stdout.write("\n(dry-run, no deletes)")
