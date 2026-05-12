"""validate_all_invoices — walk OCR caches, run parse_invoice, record
per-invoice validation status in InvoiceValidationStatus.

The durable validation-failure surface Sean asked for (B5 layer).
Without this, parse_invoice's section-reconciliation output is stdout-only
— there's no way to look back at "which invoices failed" or build a
review UI on top.

For each (vendor, invoice_number) group of caches:
  1. Combine cache pages and run parse_invoice
  2. Compute items_sum vs invoice_total gap
  3. Compute per-section reconciliation
  4. Classify as PASS / REVIEW / FAIL / PARTIAL
  5. Upsert InvoiceValidationStatus row

Run this:
  python manage.py validate_all_invoices                # all caches
  python manage.py validate_all_invoices --vendor Sysco  # single vendor
  python manage.py validate_all_invoices --month 2026 4  # one month
"""
import io
import json
import os
import re
import sys
from collections import defaultdict
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.conf import settings

# Add invoice_processor to path
_HERE = os.path.dirname(os.path.abspath(__file__))
_IP_PATH = os.path.abspath(os.path.join(_HERE, '..', '..', '..', 'invoice_processor'))
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)

from myapp.models import Vendor, InvoiceValidationStatus  # noqa: E402


# Classification thresholds. Match `project_parser_accuracy_goal.md`
# stated tolerances.
_INVOICE_GAP_PASS_PCT  = 5.0    # < 5% gap → PASS
_INVOICE_GAP_FAIL_PCT  = 10.0   # > 10% gap → FAIL
_SECTION_DIFF_TOLERANCE = 0.50  # per-section diff < $0.50 = reconciled


def _classify(items_sum: float, invoice_total: float | None,
              section_recon: list[dict],
              non_item_charges: float = 0.0) -> str:
    """Return 'pass' / 'review' / 'fail' / 'partial'.

    Three paths to PASS:
      (a) Invoice-total math with charges: items_sum + non_item_charges
          reconciles to invoice_total within $0.50. For vendors whose
          parsers extract fees/taxes (Delaware Linen).
      (b) Invoice-total math without charges: gap_pct < 5%.
      (c) Section reconciliation AND invoice-total gap is reasonable.
          Every section reconciles to its printed GROUP TOTAL within
          tolerance, AND items_sum vs invoice_total gap is below the
          FAIL threshold.

          The gap-guard is critical: when OCR captures only PART of a
          multi-page invoice (missing pages), the sections we DO see
          reconcile to their printed GROUP TOTALs, but items_sum is far
          short of invoice_total. Without the guard, invoices with 78%+
          missing pages were silently classified as PASS because the
          surface that was captured looked clean. See INV 1282480
          (2026-04-06): 1 of N pages OCR'd, $559 items_sum vs $2559
          invoice_total, sections reconcile, classifier said PASS.
          That hid $2000 of missing data from downstream consumers.
          (Bug surfaced by Sean 2026-05-09.)
    """
    if invoice_total is None or invoice_total == 0:
        return 'partial'
    gap_pct = abs(items_sum - invoice_total) / invoice_total * 100
    gap_with_charges = abs(items_sum + non_item_charges - invoice_total)
    sections_total = len(section_recon)
    sections_with_gap = sum(
        1 for r in section_recon
        if r.get('diff_abs') is not None
        and abs(r['diff_abs']) >= _SECTION_DIFF_TOLERANCE
    )
    # Path (a): invoice-total math reconciles, with OR without extracted
    # fees. The previous form required `non_item_charges > 0`, which only
    # PASSed when fees lived in the parallel non_item_charges field. After
    # B-SyscoFeeILI (2026-05-12) Sysco fees emit as synthetic ILI rows —
    # items_sum naturally includes them and non_item_charges stays 0. Drop
    # the non_item_charges > 0 gate so a perfect items_sum == invoice_total
    # match still PASSes regardless of where the fees lived. Safe vs. the
    # missing-pages false-PASS class (INV 1282480: items=$559, total=$2559,
    # gap_with_charges=$2000 — far above $0.50, still fails path (a)).
    if gap_with_charges < 0.50:
        return 'pass'
    # Path (c): section reconciliation passes ONLY when invoice-total gap is
    # also reasonable. Catches the missing-pages case described above.
    if (sections_total > 0 and sections_with_gap == 0
            and gap_pct < _INVOICE_GAP_FAIL_PCT):
        return 'pass'
    if gap_pct >= _INVOICE_GAP_FAIL_PCT:
        return 'fail'
    if sections_with_gap > 0:
        return 'review'
    # Path (b): no sections detected, fall back to invoice-total math.
    if gap_pct < _INVOICE_GAP_PASS_PCT:
        return 'pass'
    return 'review'


class Command(BaseCommand):
    help = ('Validate every cached invoice against printed totals + '
            'section reconciliation. Populates InvoiceValidationStatus.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', help='Filter to a single vendor name')
        parser.add_argument('--month', nargs=2, type=int,
                            metavar=('YEAR', 'MONTH'),
                            help='Filter to caches from a specific month')
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Without this flag, dry-run only.')
        parser.add_argument('--cache-dir',
                            default=str(settings.BASE_DIR / '.ocr_cache'),
                            help='OCR cache directory (default: .ocr_cache)')

    def handle(self, *args, **opts):
        from parser import parse_invoice, extract_sysco_metadata  # noqa: E402

        cache_dir = opts['cache_dir']
        vendor_filter = opts.get('vendor')
        month_filter = opts.get('month')
        apply_writes = opts.get('apply')

        # Load all caches that match filters
        caches = []
        for f in os.listdir(cache_dir):
            if not f.endswith('_docai_ocr.json'):
                continue
            with open(os.path.join(cache_dir, f)) as fh:
                d = json.load(fh)
            if vendor_filter and d.get('vendor') != vendor_filter:
                continue
            inv_date = d.get('invoice_date') or ''
            if month_filter:
                want = f'{month_filter[0]:04d}-{month_filter[1]:02d}'
                if not inv_date.startswith(want):
                    continue
            caches.append({
                'sha': f.split('_')[0],
                'cache': d,
            })

        # Group by (vendor, invoice_number). For Sysco, fall back to manifest
        # when invoice_number can't be extracted — BUT skip manifest-only
        # pages (Sean 2026-05-09 bug: INV 1282480 was a routing slip captured
        # as a phantom invoice with $2k phantom total).
        groups: dict = defaultdict(list)
        for entry in caches:
            d = entry['cache']
            vendor = d.get('vendor') or 'Unknown'
            inv_num = None
            if vendor == 'Sysco':
                meta = extract_sysco_metadata(d['raw_text'])
                inv_num = meta.get('invoice_number')
                # Manifest fallback ONLY when this isn't a manifest-cover page.
                # Manifest-cover pages start with "MANIFEST <num> NORMAL DELIVERY"
                # or similar — they're delivery routing slips, not invoices.
                # Their line items belong to a real invoice in the same
                # delivery (matched by date), but creating an IVS row for the
                # manifest itself produces a false invoice with phantom total.
                if not inv_num and meta.get('manifest'):
                    raw = d.get('raw_text', '')[:300].upper()
                    is_manifest_cover = bool(
                        re.search(r'MANIFEST\s+\d+\s+(NORMAL|SHIP\s*DAY|EXPEDITED)',
                                  raw)
                    )
                    if not is_manifest_cover:
                        # Real invoice with OCR-degraded invoice_number — fall
                        # back to manifest for grouping (best-effort recovery).
                        inv_num = meta.get('manifest')
                    # else: skip — manifest cover doesn't get its own IVS row
            else:
                # Other vendors: use a per-vendor regex (matches the logic
                # in reprocess_ocr_cache._extract_invoice_number).
                m = re.search(r'Invoice\s*(?:No\.?|Number|#)?\s*[:\n]?\s*(\d{4,10})',
                              d['raw_text'], re.IGNORECASE)
                inv_num = m.group(1) if m else None
            if not inv_num:
                continue
            groups[(vendor, inv_num)].append(entry)

        # Stats counters
        stats = defaultdict(int)
        results: list[dict] = []

        for (vendor_name, inv_num), entries in sorted(groups.items()):
            # Combine pages + run parse_invoice (silenced stdout to keep
            # report clean; parse_invoice prints anomaly notices we'll
            # surface in summary instead).
            # Sort entries by detected physical page order so rank_pair's
            # cross-cache section carry flows correctly. sha-sort and
            # filesystem-order do NOT preserve page sequence — INV
            # 775292014 / 775451714 / 775238251 all surfaced as cross-cache
            # section carry failures under sha-sort (Sean 2026-05-11).
            from section_validator import cache_page_order_key
            entries = sorted(
                entries,
                key=lambda e: (
                    cache_page_order_key(e['cache'].get('raw_text', '')),
                    e['sha'],
                ),
            )
            combined_text = '\n'.join(e['cache']['raw_text'] for e in entries)
            combined_pages = []
            for e in entries:
                # Tag each page with its source cache for diagnostic provenance.
                # rank_pair no longer resets carry_section at cache boundaries
                # — upstream page-order sort above ensures correct sequence
                # within an invoice, and grouping by invoice_number guarantees
                # no cross-invoice bleed.
                for page in (e['cache'].get('pages', []) or []):
                    page = {**page, '_cache_sha': e['sha']}
                    combined_pages.append(page)
            buf = io.StringIO()
            with redirect_stdout(buf):
                parsed = parse_invoice(combined_text, vendor=vendor_name,
                                        pages=combined_pages)
            items = parsed.get('items', [])
            items_sum = round(sum((it.get('extended_amount') or 0)
                                   for it in items), 2)
            invoice_total = parsed.get('invoice_total')
            section_recon = parsed.get('section_reconciliation') or []
            non_item_charges = parsed.get('non_item_charges') or 0.0

            invoice_gap = (round(items_sum - invoice_total, 2)
                           if invoice_total is not None else None)
            invoice_gap_pct = (round(abs(invoice_gap) / invoice_total * 100, 2)
                                if invoice_total else None)
            sections_total = len(section_recon)
            sections_with_gap = sum(
                1 for r in section_recon
                if r.get('diff_abs') is not None
                and abs(r['diff_abs']) >= _SECTION_DIFF_TOLERANCE
            )
            sections_reconciled = sections_total - sections_with_gap

            status = _classify(items_sum, invoice_total, section_recon,
                                non_item_charges=non_item_charges)
            stats[status] += 1

            # Earliest cache date as the invoice date (refines: prefer the
            # date with LAST PAGE marker since that's the actual invoice
            # date — DELV. DATE on the items page can differ).
            inv_date_str = None
            last_page_dates = [e['cache'].get('invoice_date') for e in entries
                               if 'LAST PAGE' in e['cache'].get('raw_text', '').upper()]
            if last_page_dates:
                inv_date_str = sorted(last_page_dates)[0]
            else:
                dates = [e['cache'].get('invoice_date') for e in entries
                         if e['cache'].get('invoice_date')]
                inv_date_str = min(dates) if dates else None

            inv_date = None
            if inv_date_str:
                try:
                    inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d').date()
                except ValueError:
                    inv_date = None

            cache_hashes = [e['sha'][:16] for e in entries]

            results.append({
                'vendor': vendor_name,
                'invoice_number': inv_num,
                'invoice_date': inv_date,
                'items_count': len(items),
                'items_sum': Decimal(str(items_sum)),
                'invoice_total': (Decimal(str(invoice_total))
                                   if invoice_total is not None else None),
                'invoice_gap': (Decimal(str(invoice_gap))
                                 if invoice_gap is not None else None),
                'invoice_gap_pct': (Decimal(str(invoice_gap_pct))
                                     if invoice_gap_pct is not None else None),
                'sections_total': sections_total,
                'sections_reconciled': sections_reconciled,
                'sections_with_gap': sections_with_gap,
                'section_reconciliation': section_recon,
                'cache_hashes': cache_hashes,
                'status': status,
            })

        # Print summary
        self.stdout.write('')
        self.stdout.write(f'Validated {len(groups)} invoices:')
        for s, n in sorted(stats.items()):
            self.stdout.write(f'  {s.upper():<8}: {n}')

        # Per-invoice detail (status != pass)
        non_pass = [r for r in results if r['status'] != 'pass']
        if non_pass:
            self.stdout.write('')
            self.stdout.write('Non-PASS invoices:')
            self.stdout.write(
                f'  {"vendor":<14} {"inv#":<11} {"date":<12} '
                f'{"status":<8} {"items":>10} {"total":>10} {"gap%":>7} '
                f'{"sec✓/total":>11}'
            )
            for r in sorted(non_pass, key=lambda x: (x['status'],
                                                     -(x['invoice_gap_pct'] or 0))):
                gap_pct = f'{r["invoice_gap_pct"]:.1f}%' if r['invoice_gap_pct'] else '—'
                items_sum_str = f'${r["items_sum"]:.2f}'
                inv_total_str = (f'${r["invoice_total"]:.2f}'
                                  if r['invoice_total'] else '—')
                date_str = str(r['invoice_date']) if r['invoice_date'] else '—'
                sec_str = f'{r["sections_reconciled"]}/{r["sections_total"]}'
                self.stdout.write(
                    f'  {r["vendor"]:<14} {r["invoice_number"]:<11} '
                    f'{date_str:<12} {r["status"]:<8} '
                    f'{items_sum_str:>10} {inv_total_str:>10} {gap_pct:>7} '
                    f'{sec_str:>11}'
                )

        # Apply: upsert InvoiceValidationStatus rows
        if not apply_writes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to write to DB.'))
            return

        upsert_count = 0
        for r in results:
            vendor_obj, _ = Vendor.objects.get_or_create(name=r['vendor'])
            ivs, created = InvoiceValidationStatus.objects.update_or_create(
                vendor=vendor_obj,
                invoice_number=r['invoice_number'],
                defaults={
                    'invoice_date':       r['invoice_date'],
                    'items_count':        r['items_count'],
                    'items_sum':          r['items_sum'],
                    'invoice_total':      r['invoice_total'],
                    'invoice_gap':        r['invoice_gap'],
                    'invoice_gap_pct':    r['invoice_gap_pct'],
                    'sections_total':     r['sections_total'],
                    'sections_reconciled': r['sections_reconciled'],
                    'sections_with_gap':  r['sections_with_gap'],
                    'section_reconciliation': r['section_reconciliation'],
                    'cache_hashes':       r['cache_hashes'],
                    'status':             r['status'],
                },
            )
            upsert_count += 1
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {upsert_count} InvoiceValidationStatus rows.'))
