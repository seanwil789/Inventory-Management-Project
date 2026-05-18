"""Delete ILIs whose (unit_price, extended_amount) doesn't match any
parser-emitted item for the same invoice.

Stricter than dedup_bad_supc_rows (which targets bad SUPC + good
sibling). This targets rows that the current parser doesn't emit AT
ALL — typically OLD junk from earlier failed reprocesses (e.g., a
$506.85 'COFFEE BEAN' row on INV 775825138 created when an earlier
buggy reprocess attributed a GROUP TOTAL value to an item).

Safety:
- Preserves user_edited rows even if (price, ext) doesn't match parser
- Preserves fee rows (descs starting with 'Sysco Fuel/CC/Sales')
- Preserves rows where (invoice, price, ext) IS in parser output
- Skips invoices where parse failed (no truth available)
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
    help = 'Delete ILIs whose (price, ext) isn\'t in parser output for the invoice.'

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

        # Build invoice → set of parser-emitted (price, ext) tuples
        truth: dict[str, set[tuple[float, float]]] = {}
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
            pairs = set()
            for it in parsed.get('items') or []:
                up = it.get('unit_price')
                ext = it.get('extended_amount')
                if up is not None and ext is not None:
                    pairs.add((round(float(up), 2), round(float(ext), 2)))
            if pairs:
                truth.setdefault(inv, set()).update(pairs)

        qs = InvoiceLineItem.objects.exclude(invoice_number='').select_related('vendor')
        if opts['vendor']:
            qs = qs.filter(vendor__name__iexact=opts['vendor'])

        to_delete: list[InvoiceLineItem] = []
        for ili in qs:
            if ili.user_edited:
                continue
            desc = (ili.raw_description or '').lower()
            # Preserve fee rows (parser may not emit them via items, they
            # come from extract_sysco_fees path)
            if (desc.startswith('sysco ') and
                    ('surcharge' in desc or 'processing' in desc or 'tax' in desc)):
                continue
            invoice_truth = truth.get(ili.invoice_number)
            if invoice_truth is None:
                continue  # no parse — skip
            key = (round(float(ili.unit_price or 0), 2),
                   round(float(ili.extended_amount or 0), 2))
            if key in invoice_truth:
                continue
            to_delete.append(ili)

        per_invoice: dict[str, int] = {}
        total_ext_deleted = 0.0
        for ili in to_delete:
            per_invoice[ili.invoice_number] = per_invoice.get(ili.invoice_number, 0) + 1
            total_ext_deleted += float(ili.extended_amount or 0)

        self.stdout.write(f"Invoices with parser truth: {len(truth)}")
        self.stdout.write(f"Rows to delete: {len(to_delete)} (\${total_ext_deleted:.2f})")
        for inv, n in sorted(per_invoice.items(), key=lambda x: -x[1])[:15]:
            self.stdout.write(f"  {inv}: {n}")

        self.stdout.write("\nSample (top 10 by ext):")
        sample = sorted(to_delete, key=lambda r: float(r.extended_amount or 0), reverse=True)[:10]
        for ili in sample:
            self.stdout.write(
                f"  id={ili.id} inv={ili.invoice_number} ext={ili.extended_amount} "
                f"code={ili.vendor_item_code!r} desc={(ili.raw_description or '')[:50]!r}")

        if opts['apply'] and to_delete:
            ids = [i.id for i in to_delete]
            InvoiceLineItem.objects.filter(id__in=ids).delete()
            self.stdout.write(f"\nDELETED {len(ids)} rows (\${total_ext_deleted:.2f} of items).")
        elif opts['dry_run']:
            self.stdout.write("\n(dry-run, no deletes)")
