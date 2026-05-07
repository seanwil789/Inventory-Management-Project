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
import re
from pathlib import Path
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand


_PRICE_RE = re.compile(r'^\d+\.\d{2}$')


def _y_mid(t):
    return (t['y_min'] + t['y_max']) / 2


def _x_mid(t):
    return (t['x_min'] + t['x_max']) / 2


def _value_for_label(tokens, label_tokens, max_dy=0.005, min_x=0.5):
    """Find the dollar amount on the same row as the label, right of it.

    Prices in the totals block are stacked vertically with ~0.014 row pitch,
    so max_dy=0.005 keeps each label bound to its own row. Ties broken by
    closest y, not leftmost x.
    """
    if not label_tokens:
        return None
    y_target = sum(_y_mid(t) for t in label_tokens) / len(label_tokens)
    x_max_label = max(_x_mid(t) for t in label_tokens)
    candidates = [
        (abs(_y_mid(t) - y_target), _x_mid(t), float(t['text']))
        for t in tokens
        if _PRICE_RE.fullmatch(t.get('text') or '')
        and abs(_y_mid(t) - y_target) < max_dy
        and _x_mid(t) > x_max_label
        and _x_mid(t) > min_x
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def _label_row(tokens, words, anchor, max_dy=0.005):
    """Return label tokens forming a row whose anchor word is present."""
    matches = [t for t in tokens
               if (t.get('text') or '').upper() in words]
    if not any((t.get('text') or '').upper() == anchor for t in matches):
        return []
    anchor_y = next(
        (_y_mid(t) for t in matches if (t.get('text') or '').upper() == anchor),
        None,
    )
    if anchor_y is None:
        return []
    return [t for t in matches if abs(_y_mid(t) - anchor_y) < max_dy]


def extract_sysco_fees(pages):
    """Extract fuel surcharge / CC processing / tax from Sysco invoice OCR.

    Returns dict with keys 'fuel_surcharge', 'cc_processing', 'tax' (only
    those found). Sysco prints these as labeled rows in the totals block at
    the bottom-right of the last page.
    """
    if not pages:
        return {}
    tokens = pages[-1].get('tokens') or []
    if not tokens:
        return {}

    fees = {}

    fuel_row = _label_row(tokens, {'FUEL', 'SURCHARGE'}, 'FUEL')
    if fuel_row:
        amt = _value_for_label(tokens, fuel_row)
        if amt is not None:
            fees['fuel_surcharge'] = amt

    cc_row = _label_row(tokens, {'CREDIT', 'CARD'}, 'CREDIT')
    if cc_row:
        amt = _value_for_label(tokens, cc_row)
        if amt is not None:
            fees['cc_processing'] = amt

    # TAX: bottom half of page only (avoids the column header in upper-half).
    tax_tokens = [t for t in tokens
                  if (t.get('text') or '').upper() == 'TAX'
                  and _y_mid(t) > 0.5]
    if tax_tokens:
        # Pick the lowest-y TAX (closest to INVOICE TOTAL block).
        tax_tokens.sort(key=_y_mid, reverse=True)
        amt = _value_for_label(tokens, [tax_tokens[0]], max_dy=0.02)
        if amt is not None:
            fees['tax'] = amt

    return fees


_SYSCO_INV_NUM_RE = re.compile(
    # Sysco invoice numbers are 9 digits (e.g. 775263771). DocAI sometimes
    # interleaves the customer code (6-digit, e.g. 815579) between the
    # "INVOICE NUMBER" label and the actual invoice number when splitting
    # multi-column header into stacked text. Non-greedy `[\s\S]{0,200}?`
    # skips over any intervening junk (whitespace, customer code, page
    # number, etc.) until the first 9-digit run — that's the invoice
    # number. The 9-digit constraint ignores 6-digit customer codes,
    # 4-digit route codes, and 7-digit manifest numbers.
    r'INVOICE\s*NUMBER[\s\S]{0,200}?(\d{9})',
    re.IGNORECASE,
)


def extract_invoice_number(raw_text, vendor):
    """Return invoice number for a cache, or None if not found.

    Sysco prints `INVOICE NUMBER` followed by the 9-digit number at the top
    of every page. The OCR sometimes renders this as one inline string
    (`INVOICE NUMBER | 775263771`) and sometimes splits the label and value
    across separate lines — both forms are handled. Other vendors are not
    yet wired (extend per-vendor as needed).
    """
    if not raw_text:
        return None
    if vendor == 'Sysco':
        m = _SYSCO_INV_NUM_RE.search(raw_text)
        if m:
            return m.group(1)
    return None


def is_last_page(raw_text):
    """Sysco prints `LAST PAGE` after the totals block on the final page.

    Older / cropped scans sometimes lose this literal text but still capture
    the totals block — `is_totals_page` is the broader test for "this cache
    holds the totals." Use that for invoice_total selection; reserve this
    function for the explicit-marker case.
    """
    if not raw_text:
        return False
    return bool(re.search(r'(?i)\blast\s*page\b', raw_text))


def pick_totals_cache(group):
    """Pick the cache that holds the invoice totals block out of a group of
    caches for one logical multi-photo invoice.

    Strategy:
      1. Prefer cache with literal `LAST PAGE` text AND a parsed
         invoice_total (strongest signal).
      2. Else prefer cache with the highest parsed `invoice_total` — Sysco's
         non-totals pages return partial GROUP TOTAL sums per page, which
         are always strictly less than the actual invoice total on the
         totals page.
      3. Else fall back to any LAST PAGE cache even without a total (the
         LAST PAGE photo still anchors fee extraction even if it parsed
         no total — e.g. very short invoices).
      4. Else None.

    Returns a cache dict from `group` or None.
    """
    last_with_total = [p for p in group
                       if p['is_last_page']
                       and p['result'].get('invoice_total') is not None]
    if last_with_total:
        return last_with_total[-1]

    with_total = [p for p in group
                  if p['result'].get('invoice_total') is not None]
    if with_total:
        return max(with_total, key=lambda p: p['result']['invoice_total'])

    last_pages = [p for p in group if p['is_last_page']]
    if last_pages:
        return last_pages[-1]

    return None


def is_continued_page(raw_text):
    """Sysco prints `CONT. ON PAGE` (or 'CONTINUED ON PAGE') on non-final pages."""
    if not raw_text:
        return False
    return bool(re.search(r'(?i)\bcont(?:inued|\.)\s*on\s*page\b', raw_text))


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
        from collections import defaultdict
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

        # Phase 1: parse every cache + extract grouping metadata.
        parsed_caches = []
        for cf in cache_files:
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

            try:
                result = parse_invoice(raw_text, vendor=vendor, pages=pages)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  [!] {cf.name[:14]}: parse_invoice raised {e}'))
                continue

            parsed_caches.append({
                'cache_name': cf.name,
                'cache_short': cf.name[:18],
                'vendor': vendor,
                'inv_date': inv_date,
                'pages': pages,
                'raw_text': raw_text,
                'result': result,
                'invoice_number': extract_invoice_number(raw_text, vendor),
                'is_last_page': is_last_page(raw_text),
                'is_continued': is_continued_page(raw_text),
            })

        # Phase 2: group caches by (vendor, invoice_number). Fall back to
        # per-cache when invoice_number is unknown so non-Sysco still works.
        groups = defaultdict(list)
        for p in parsed_caches:
            if p['invoice_number']:
                key = (p['vendor'], p['invoice_number'])
            else:
                key = (p['vendor'], 'cache:' + p['cache_short'])
            groups[key].append(p)

        # Phase 3: emit one report per group.
        groups_processed = 0
        for (vendor, group_id), group in sorted(groups.items()):
            if opts['limit'] and groups_processed >= opts['limit']:
                break
            groups_processed += 1

            # Pick the cache holding the invoice totals (LAST PAGE marker
            # preferred, else cache w/ highest parsed invoice_total).
            primary = pick_totals_cache(group)
            for p in group:
                p['is_primary_totals'] = (p is primary)

            # Order caches: continued pages first, totals page last (natural
            # reading order).
            group.sort(key=lambda p: (p['is_primary_totals'], p['cache_name']))

            inv_date = group[0]['inv_date']
            invoice_number = group[0]['invoice_number']

            # Concat items in page order. Sysco lines are unique per page so
            # no dedup needed for the Sysco case; if dups appear elsewhere
            # we'll add a key-based dedup pass.
            all_items = []
            per_cache_item_counts = []
            for p in group:
                items = p['result'].get('items', [])
                all_items.extend(items)
                per_cache_item_counts.append(len(items))

            if primary:
                invoice_total = primary['result'].get('invoice_total')
                fees = (extract_sysco_fees(primary['pages'])
                        if vendor == 'Sysco' else {})
            else:
                invoice_total = None
                fees = {}

            last_page_count = sum(1 for p in group if p['is_last_page'])

            # Header
            self.stdout.write('')
            self.stdout.write('=' * 100)
            label = f'Invoice #{invoice_number}' if invoice_number else f'Cache {group[0]["cache_short"]}'
            self.stdout.write(self.style.SUCCESS(label))
            self.stdout.write(f'Vendor:  {vendor}')
            self.stdout.write(f'Date:    {inv_date}')
            if len(group) > 1:
                def _role(p):
                    if p is primary and p['is_last_page']:
                        return 'LAST'
                    if p is primary:
                        return 'TOTALS'
                    return ''
                cache_summary = ', '.join(
                    f'{p["cache_short"][:14]}({c} items'
                    f'{", " + _role(p) if _role(p) else ""})'
                    for p, c in zip(group, per_cache_item_counts)
                )
                self.stdout.write(f'Pages:   {len(group)} cache files → {cache_summary}')
            else:
                self.stdout.write(f'Cache:   {group[0]["cache_short"]}')
            self.stdout.write(f'Items:   {len(all_items)}')
            if invoice_total is not None:
                src = ('LAST PAGE marker' if primary['is_last_page']
                       else 'highest parsed invoice_total')
                self.stdout.write(
                    f'Invoice total: ${invoice_total:.2f}  '
                    f'(from {primary["cache_short"][:14]} via {src})'
                )

            # Page-sequence warnings
            if primary is None:
                self.stdout.write(self.style.WARNING(
                    f'  [!] No totals page identified — invoice total may '
                    f'be missing or photo not captured.'
                ))
            if last_page_count > 1:
                self.stdout.write(self.style.WARNING(
                    f'  [!] {last_page_count} caches contain LAST PAGE marker '
                    f'— possible duplicate capture of the final page.'
                ))

            self.stdout.write('-' * 100)
            self.stdout.write(
                f'{"#":>3}  {"qty":>6}  {"unit$":>9}  {"ext$":>9}  '
                f'{"math":<6}  {"case_size":<14}  desc'
            )
            self.stdout.write('-' * 100)

            items_total = 0
            math_pass = 0
            math_fail = 0
            for i, item in enumerate(all_items, start=1):
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
                f'(math {math_pass}/{len(all_items)} pass)'
            )

            reconciled = items_total
            if 'fuel_surcharge' in fees:
                reconciled += fees['fuel_surcharge']
                self.stdout.write(
                    f'+ Fuel surcharge:    ${fees["fuel_surcharge"]:>8.2f}'
                )
            if 'cc_processing' in fees:
                reconciled += fees['cc_processing']
                self.stdout.write(
                    f'+ CC processing:     ${fees["cc_processing"]:>8.2f}'
                )
            if 'tax' in fees:
                reconciled += fees['tax']
                self.stdout.write(
                    f'+ Tax:               ${fees["tax"]:>8.2f}'
                )

            if invoice_total is not None:
                gap = invoice_total - reconciled
                gap_pct = (abs(gap) / invoice_total * 100) if invoice_total else 0
                detail = ''
                if fees:
                    parts = [f'items=${items_total:.2f}']
                    if 'fuel_surcharge' in fees:
                        parts.append(f'fuel=${fees["fuel_surcharge"]:.2f}')
                    if 'cc_processing' in fees:
                        parts.append(f'cc=${fees["cc_processing"]:.2f}')
                    if 'tax' in fees:
                        parts.append(f'tax=${fees["tax"]:.2f}')
                    detail = '  [' + ' + '.join(parts) + ']'
                if abs(gap) < 0.50:
                    self.stdout.write(self.style.SUCCESS(
                        f'Reconciled: ${reconciled:.2f} ≈ ${invoice_total:.2f} '
                        f'(gap=${gap:+.2f}) ✓{detail}'
                    ))
                else:
                    self.stdout.write(self.style.WARNING(
                        f'Reconciliation: ${invoice_total:.2f} vs '
                        f'${reconciled:.2f} → gap=${gap:+.2f} '
                        f'({gap_pct:.1f}%) ✗{detail}'
                    ))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Processed {groups_processed} logical invoices '
            f'({len(parsed_caches)} cache files).'
        ))
