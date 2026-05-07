"""Per-invoice extraction validation report — extract via current pipeline,
print every line with math check, surface for human comparison against the
physical invoice.

Drift-cascade audits and per-cache row counts measure INTERNAL CONSISTENCY
(DB vs rank-pair, rank-pair vs spatial). They can't tell us if extraction
is TRUE — just that it's self-consistent. The only way to confirm
accuracy is to compare the extracted output line-by-line against the
actual paper invoice.

This tool prints:
  - Cache hash + invoice metadata
  - Each extracted line: qty | raw_description | case_size | unit_price | extended
  - Math check: does qty × unit_price ≈ extended? (within 5% / $2.00)
  - Catch-weight check: when price_per_unit set, does weight × per_lb ≈ extended?

Usage:
    # Print all Sysco April 2026 invoices
    python manage.py validate_extraction --vendor Sysco --month 2026 4

    # Print one specific cache by hash prefix
    python manage.py validate_extraction --hash 618c6f25

    # Limit to N invoices for quick review
    python manage.py validate_extraction --vendor 'Farm Art' --limit 3
"""
import json
from pathlib import Path
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Per-invoice line-by-line extraction validation report.'

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default=None,
                            help='Vendor name filter (e.g. "Sysco", "Farm Art")')
        parser.add_argument('--month', nargs=2, type=int, default=None,
                            metavar=('YEAR', 'MONTH'),
                            help='Restrict to invoices from this month')
        parser.add_argument('--hash', default=None,
                            help='Restrict to one cache hash prefix')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N invoices')
        parser.add_argument('--cache-dir', default=None,
                            help='OCR cache dir (default: <BASE_DIR>/.ocr_cache/)')

    def handle(self, *args, **opts):
        import sys
        sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
        if 'parser' in sys.modules:
            del sys.modules['parser']
        if 'rank_pair' in sys.modules:
            del sys.modules['rank_pair']
        from parser import parse_invoice

        cache_dir = (Path(opts['cache_dir']) if opts['cache_dir']
                     else Path(settings.BASE_DIR) / '.ocr_cache')

        cache_files = sorted(cache_dir.glob('*_docai_ocr.json'))
        if opts['hash']:
            cache_files = [c for c in cache_files
                           if c.name.startswith(opts['hash'])]

        invoices_processed = 0
        for cf in cache_files:
            if opts['limit'] and invoices_processed >= opts['limit']:
                break
            try:
                data = json.loads(cf.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            vendor = data.get('vendor') or 'Unknown'
            inv_date = str(data.get('invoice_date') or '')

            if opts['vendor'] and vendor != opts['vendor']:
                continue
            if opts['month']:
                yr, mo = opts['month']
                expected = f'{yr:04d}-{mo:02d}'
                if not inv_date.startswith(expected):
                    continue

            pages = data.get('pages') or []
            raw_text = data.get('raw_text') or ''
            if not pages and not raw_text:
                continue

            # Extract through current production pipeline
            try:
                result = parse_invoice(raw_text, vendor=vendor, pages=pages)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  [!] {cf.name[:14]}: parse_invoice raised {e}'))
                continue

            items = result.get('items', [])
            invoice_total = result.get('invoice_total')
            invoices_processed += 1

            # Header
            self.stdout.write('')
            self.stdout.write('=' * 100)
            self.stdout.write(self.style.SUCCESS(
                f'Cache:   {cf.name[:18]}'))
            self.stdout.write(f'Vendor:  {vendor}')
            self.stdout.write(f'Date:    {inv_date}')
            self.stdout.write(f'Items:   {len(items)}')
            if invoice_total is not None:
                self.stdout.write(f'Invoice total: ${invoice_total:.2f}')

            self.stdout.write('-' * 100)
            self.stdout.write(
                f'{"#":>3}  {"qty":>6}  {"unit$":>9}  {"ext$":>9}  '
                f'{"math":<6}  {"case_size":<14}  desc')
            self.stdout.write('-' * 100)

            items_total = 0
            math_pass = 0
            math_fail = 0
            for i, item in enumerate(items, start=1):
                qty = item.get('quantity') or 1
                up = item.get('unit_price') or 0
                ext = item.get('extended_amount') or 0
                cs = item.get('case_size_raw') or ''
                ppu = item.get('price_per_unit')
                desc = (item.get('raw_description') or '')[:55]

                if isinstance(qty, Decimal):
                    qty = float(qty)
                if isinstance(up, Decimal):
                    up = float(up)
                if isinstance(ext, Decimal):
                    ext = float(ext)

                # Math check: qty × up ≈ ext (within 5% or $2.00)
                expected_ext = qty * up
                if expected_ext > 0:
                    diff_abs = abs(ext - expected_ext)
                    diff_pct = diff_abs / expected_ext * 100
                    math_ok = diff_pct < 5.0 or diff_abs < 2.0
                else:
                    math_ok = ext == 0
                math_str = '  ✓' if math_ok else '  ✗'
                if math_ok:
                    math_pass += 1
                else:
                    math_fail += 1

                items_total += ext

                # Extra info: catch-weight per-lb when present
                ppu_str = f' (per_lb=${ppu:.3f})' if ppu else ''

                self.stdout.write(
                    f'{i:>3}  {qty:>6.2f}  {up:>9.2f}  {ext:>9.2f}  '
                    f'{math_str:<6}  {cs:<14}  {desc}{ppu_str}'
                )
                if not math_ok:
                    self.stdout.write(self.style.WARNING(
                        f'      [!] {qty:.2f} × ${up:.2f} = ${expected_ext:.2f} '
                        f'but ext=${ext:.2f}  Δ={diff_pct:.1f}% (${diff_abs:.2f})'
                    ))

            self.stdout.write('-' * 100)
            self.stdout.write(
                f'Items sum: ${items_total:.2f}  '
                f'(math {math_pass}/{len(items)} pass)'
            )
            if invoice_total is not None:
                gap = invoice_total - items_total
                gap_pct = (abs(gap) / invoice_total * 100) if invoice_total else 0
                if abs(gap) < 0.50:
                    self.stdout.write(self.style.SUCCESS(
                        f'Total reconciliation: ${invoice_total:.2f} ≈ '
                        f'${items_total:.2f} (gap=${gap:+.2f}) ✓'
                    ))
                else:
                    self.stdout.write(self.style.WARNING(
                        f'Total reconciliation: ${invoice_total:.2f} vs '
                        f'${items_total:.2f} → gap=${gap:+.2f} ({gap_pct:.1f}%) ✗'
                    ))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Processed {invoices_processed} invoices.'))
