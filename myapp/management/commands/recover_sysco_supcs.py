"""Recover unmapped Sysco SUPC codes via cross-invoice OCR context.

The parser's catch-weight hardening recovered ~400 ILI rows whose
raw_description is the '[Sysco #NNN]' placeholder — real products with
real dollar value but SUPC codes not in code_map. This command scans ALL
cached Sysco OCR text for each unmapped SUPC, finds the strongest
nearby product-description context, fuzzy-matches it against existing
canonical Products, and proposes mappings.

Strategies (in priority order):
  1. `inline`: the SUPC appears on the SAME line as letters → that line
     is the product description (e.g. 'SANITIZER OASIS 146 MULTI QU
     6100536' → Floor Cleaner).
  2. `nearby`: the SUPC is standalone; scan ±3 lines for a real-looking
     description line. Lower confidence because column-dump OCR can put
     adjacent items' descriptions near the wrong code.

Junk filter rejects contexts matching 'GROUP TOTAL', 'MANIFEST#',
section headers (****), 'PURCHASE ORDER', 'TERMS', etc.
Shared-across-codes check rejects descriptions that appear for multiple
different SUPCs (a strong sign of column-dump misalignment).

Output:
  - HIGH (inline + fuzzy score >= 90): suggested for auto-apply
  - MEDIUM (score >= 75): surfaced for review
  - LOW: left for manual mapping or SUPC CSV import

By default, dry-run. With --apply, writes high-confidence mappings
directly to the local code_map cache (fast iteration) and to Google
Sheets' Item Mapping tab (persistent; picks up on next mapping refresh).

Usage:
  python manage.py recover_sysco_supcs                   # dry-run
  python manage.py recover_sysco_supcs --apply           # write cache + sheet
  python manage.py recover_sysco_supcs --apply --cache-only  # skip sheet write
  python manage.py recover_sysco_supcs --min-score 95    # tighter threshold
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem, Product


_PLACEHOLDER_RE = re.compile(r'^\[Sysco\s*#(\d+)\]$')
_JUNK_CTX = re.compile(
    r'GROUP\s*TOTAL|INVOICE\s+ADJUSTMENTS|\*{3,}|MANIFEST|PURCHASE ORDER|'
    r'EXTENDED|CASES\s+SPLIT|TERMS|OPEN:|CLOSE:|REMIT TO|PAYABLE ON|'
    r'CONFIDENTIAL|FRESH["\'\s]*MENU',
    re.IGNORECASE,
)


def _discover_contexts(cache_dir: Path, codes_needed: set) -> dict[str, list[tuple[str, str]]]:
    """Scan all Sysco OCR caches, return {code: [(source, description), ...]}."""
    contexts = defaultdict(list)
    for p in glob.glob(str(cache_dir / '*_docai_ocr.json')):
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get('vendor') != 'Sysco':
            continue
        lines = (d.get('raw_text') or '').splitlines()
        for i, line in enumerate(lines):
            for code in codes_needed:
                if code not in line:
                    continue
                if _JUNK_CTX.search(line):
                    continue
                letters = len(re.findall(r'[A-Za-z]{3,}', line))
                if letters >= 2:
                    contexts[code].append(('inline', line.strip()))
                else:
                    for j in range(max(0, i - 3), min(len(lines), i + 4)):
                        if j == i:
                            continue
                        nb = lines[j].strip()
                        if _JUNK_CTX.search(nb):
                            continue
                        nb_letters = re.findall(r'[A-Za-z]{4,}', nb)
                        if len(nb_letters) >= 2 and not re.search(r'\d{6,}', nb):
                            contexts[code].append(('nearby', nb[:100]))
                            break
    return contexts


def _pick_best_context(ctxs: list[tuple[str, str]], shared_descs: set) -> tuple[str, str] | None:
    """Pick the best (source, description) pair from context candidates."""
    good = [(s, d) for (s, d) in ctxs if d not in shared_descs]
    if not good:
        return None
    inlines = [d for s, d in good if s == 'inline']
    if inlines:
        return ('inline', Counter(inlines).most_common(1)[0][0])
    nearbys = [d for s, d in good if s == 'nearby']
    if nearbys:
        return ('nearby', Counter(nearbys).most_common(1)[0][0])
    return None


class Command(BaseCommand):
    help = 'Recover unmapped Sysco SUPCs via cross-invoice OCR context.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write high-confidence mappings to the local '
                                 'code_map cache AND to Google Sheets.')
        parser.add_argument('--cache-only', action='store_true',
                            help='With --apply: write to local cache only, '
                                 'skip Google Sheets write. Changes revert on '
                                 'next mapping refresh (TTL 1 hour).')
        parser.add_argument('--write-review', action='store_true',
                            help='Write MEDIUM-confidence (75-89 score) SUPC '
                                 'mapping suggestions to the Mapping Review tab '
                                 'for human approval. Writes the discovered '
                                 'context as raw_description + the fuzzy-matched '
                                 'canonical as Suggested Product, with Approve '
                                 'status blank so Sean reviews each.')
        parser.add_argument('--min-score', type=int, default=90,
                            help='Minimum fuzzy score for high-confidence (default 90).')

    def handle(self, *args, **opts):
        sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
        from rapidfuzz import process, fuzz, utils as fuzz_utils  # noqa: E402
        from mapper import _strip_sysco_prefix  # noqa: E402

        cache_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        mapping_cache = Path(settings.BASE_DIR) / 'invoice_processor' / 'mappings' / 'item_mappings.json'
        min_score = opts['min_score']

        # 1. Collect placeholder codes + dollar totals
        placeholder_info = {}
        for ili in InvoiceLineItem.objects.filter(
            vendor__name='Sysco', product__isnull=True,
            raw_description__regex=r'^\[Sysco\s*#\d+\]$',
        ):
            m = _PLACEHOLDER_RE.match(ili.raw_description)
            if not m:
                continue
            code = m.group(1)
            info = placeholder_info.setdefault(code, {'dollars': 0.0, 'row_count': 0})
            info['dollars'] += float(ili.unit_price or 0)
            info['row_count'] += 1

        if not placeholder_info:
            self.stdout.write('No unmapped Sysco placeholders found.')
            return

        total_dollars = sum(v['dollars'] for v in placeholder_info.values())
        self.stdout.write(
            f'Placeholders: {len(placeholder_info)} unique SUPCs, '
            f'${total_dollars:,.2f} total unmapped dollars'
        )

        # 2. Discover contexts for each code
        contexts = _discover_contexts(cache_dir, set(placeholder_info.keys()))

        # 3. Detect shared-across-codes descriptions (unreliable)
        desc_to_codes = defaultdict(set)
        for code, ctxs in contexts.items():
            for _, desc in ctxs:
                desc_to_codes[desc].add(code)
        shared = {d for d, cs in desc_to_codes.items() if len(cs) > 1}

        # 4. Fuzzy-match each code's best context against canonicals
        canonicals = list(Product.objects.values_list('canonical_name', flat=True))
        high_conf = []       # (code, dollars, ctx, canonical, score, src)
        medium_conf = []
        no_context = []

        for code, info in placeholder_info.items():
            ctxs = contexts.get(code, [])
            picked = _pick_best_context(ctxs, shared)
            if not picked:
                no_context.append(code)
                continue
            src, desc = picked
            stripped = _strip_sysco_prefix(desc.upper())
            result = process.extractOne(stripped, canonicals,
                                         scorer=fuzz.token_set_ratio,
                                         processor=fuzz_utils.default_process)
            score = result[1] if result else 0
            canon = result[0] if result else None

            if score >= min_score and src == 'inline':
                high_conf.append((code, info['dollars'], desc, canon, score, src))
            elif score >= 95 and src == 'nearby':
                # Very-high nearby matches are acceptable for auto-apply
                high_conf.append((code, info['dollars'], desc, canon, score, src))
            elif score >= 75:
                medium_conf.append((code, info['dollars'], desc, canon, score, src))
            else:
                medium_conf.append((code, info['dollars'], desc, canon, score, src))

        high_conf.sort(key=lambda x: -x[1])
        medium_conf.sort(key=lambda x: -x[1])

        self.stdout.write(
            f'\n  High-confidence (auto-apply eligible): {len(high_conf)} codes, '
            f'${sum(h[1] for h in high_conf):,.2f}'
        )
        self.stdout.write(
            f'  Medium-confidence (review):            {len(medium_conf)} codes, '
            f'${sum(h[1] for h in medium_conf):,.2f}'
        )
        self.stdout.write(f'  No usable context:                     {len(no_context)}\n')

        self.stdout.write(self.style.HTTP_INFO('\n=== HIGH-CONFIDENCE MAPPINGS ==='))
        for code, d, ctx, canon, score, src in high_conf:
            self.stdout.write(
                f'  ${d:>7.2f} #{code} [{src}] → {canon!r} ({score:.0f})  ctx: {ctx[:60]}'
            )

        if medium_conf:
            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== MEDIUM-CONFIDENCE (review) — top 10 ==='))
            for code, d, ctx, canon, score, src in medium_conf[:10]:
                self.stdout.write(
                    f'  ${d:>7.2f} #{code} [{src}] → {canon!r} ({score:.0f})  ctx: {ctx[:50]}'
                )

        # Medium-confidence → Mapping Review tab (independent of --apply)
        if opts['write_review'] and medium_conf:
            try:
                from sheets import get_sheets_client, get_sheet_values  # noqa: E402
                from config import SPREADSHEET_ID  # noqa: E402
                from invoice_processor.discover_unmapped import (
                    REVIEW_TAB, _ensure_review_tab_exists, _load_negative_matches,
                )
            except ImportError as e:
                self.stderr.write(f'Could not import for review write: {e}')
            else:
                client = get_sheets_client()
                _ensure_review_tab_exists(client)

                # Dedupe against existing Mapping Review rows (A=Vendor, B=Raw)
                existing = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:B")
                existing_keys = set()
                for row in existing[1:]:
                    while len(row) < 2:
                        row.append('')
                    existing_keys.add((row[0].strip(), row[1].strip()))

                rows = []
                for code, d, ctx, canon, score, src in medium_conf:
                    raw_desc = f'[Sysco #{code}] ctx: {ctx[:100]}'
                    if ('Sysco', raw_desc) in existing_keys:
                        continue
                    notes = (f'SUPC recovery · source={src} · code={code} · '
                             f'$={d:.2f}/row · Candidates: {canon} ({int(score)})')
                    rows.append([
                        'Sysco',                   # A: Vendor
                        raw_desc,                  # B: Raw Description
                        canon,                     # C: Suggested Product (prefilled for review)
                        round(score, 1),           # D: Score
                        1,                         # E: Count
                        '',                        # F: Approve? (Y/N) — blank, Sean reviews
                        f'${d:.2f}',               # G: Avg Price
                        '',                        # H: Times Seen
                        notes,                     # I: Notes
                    ])
                if rows:
                    client.values().append(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"'{REVIEW_TAB}'!A:I",
                        valueInputOption='USER_ENTERED',
                        insertDataOption='INSERT_ROWS',
                        body={'values': rows},
                    ).execute()
                    self.stdout.write(self.style.SUCCESS(
                        f'\n✔ Wrote {len(rows)} medium-confidence SUPC suggestions '
                        f'to "{REVIEW_TAB}" tab for review.'))
                else:
                    self.stdout.write(self.style.WARNING(
                        '\nNo new medium-confidence rows to write (all already in review tab).'))

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                f'\nDry run. Re-run with --apply to write high-confidence '
                f'mappings to the code_map cache' +
                ('' if opts['cache_only'] else ' and Google Sheets Item Mapping tab') + '.'))
            return

        # 5. Apply high-confidence mappings to local code_map cache
        try:
            with open(mapping_cache) as f:
                cache = json.load(f)
        except Exception as e:
            self.stderr.write(f'Could not load {mapping_cache}: {e}')
            return

        code_map = cache.setdefault('code_map', {})
        before_count = len(code_map)
        for code, _d, _ctx, canon, _score, _src in high_conf:
            code_map[code] = canon
        with open(mapping_cache, 'w') as f:
            json.dump(cache, f, indent=2)
        self.stdout.write(self.style.SUCCESS(
            f'\n✔ Updated {mapping_cache.name}: {before_count} → {len(code_map)} code entries'
        ))

        # 6. Write to Google Sheets Item Mapping tab (persistent)
        if opts['cache_only']:
            self.stdout.write(self.style.WARNING(
                'Skipped Google Sheets write (--cache-only). '
                'Changes will revert on next mapping refresh (TTL 1 hour).'
            ))
            return

        try:
            from sheets import get_sheets_client  # noqa: E402
            from config import SPREADSHEET_ID, MAPPING_TAB  # noqa: E402
        except ImportError as e:
            self.stderr.write(f'Could not import sheets client: {e}')
            return

        client = get_sheets_client()
        rows = []
        for code, _d, ctx, canon, _score, _src in high_conf:
            rows.append([
                'Sysco',          # A: Vendor
                ctx[:200],        # B: Raw Description (discovered context)
                '',               # C: Category (manual)
                '',               # D: Primary descriptor (manual)
                '',               # E: Secondary descriptor (manual)
                canon,             # F: Canonical — the recovered mapping
                code,              # G: SUPC Code
            ])
        client.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{MAPPING_TAB}!A:G",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows},
        ).execute()
        self.stdout.write(self.style.SUCCESS(
            f'✔ Appended {len(rows)} rows to "{MAPPING_TAB}" tab in Google Sheets.'
        ))
        self.stdout.write(
            '\nNext: run `python manage.py reprocess_invoices` to re-map '
            'existing [Sysco #NNN] rows now that these SUPCs resolve.'
        )
