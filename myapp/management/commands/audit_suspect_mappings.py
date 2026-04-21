"""Audit InvoiceLineItem rows for mappings where the canonical Product
name and the raw invoice description share no meaningful word tokens.

These are almost always wrong mappings — a real product would have at
least one 3+letter word in common between its canonical name and the
invoice description. When it doesn't, the ProductMapping row that
produced the match is probably wrong.

Known examples in production data (2026-04):
  - "Mop Heads" ← "Bib Aprons - White"  (Linen product wrong-mapped)
  - "White Bread" ← "Club White"          (PBM product wrong-mapped)

Usage:
    python manage.py audit_suspect_mappings
    python manage.py audit_suspect_mappings --min-count 3
    python manage.py audit_suspect_mappings --vendor "Farm Art"
    python manage.py audit_suspect_mappings --json out.json
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from myapp.models import InvoiceLineItem


# Sysco brand-prefix tokens — noise, not product identity
_NOISE = {
    'whlfcls', 'grecosn', 'coopr', 'emba', 'ssa', 'sys', 'cls', 'imp', 'cur',
    'ckd', 'bnls', 'sysfpnat', 'syfpnat', 'sysclb', 'sycor', 'syfs',
    'patrpck', 'kontos', 'bbrlimp', 'bbrlcls', 'fleishm', 'arezcls', 'arezimp',
    'calmini', 'delmnt', 'spart', 'intl', 'packer', 'leport', 'portcls',
    'portprd', 'altacuc', 'versfnr', 'steramn', 'millbak', 'highbak',
    'suprptz', 'inaugthom', 'thrcrab', 'maeploy', 'minmaid', 'jdmtcls',
    'casacls', 'simplot', 'pillsby', 'hormel', 'keyston', 'ecolab',
    'heinz', 'regina', 'roland', 'gatorade', 'labella', 'tropcna',
    'lacroix', 'sysprm', 'sysgrd', 'sysrel',
}


def _stems(s: str) -> set[str]:
    """3+letter word tokens with naive plural stripping + brand-prefix
    removal. 'pineapples' and 'pineapple' collapse to the same stem so
    canonical 'Pineapple' and raw 'PINEAPPLES' overlap."""
    stems = set()
    for t in re.findall(r'[A-Za-z]{3,}', s or ''):
        low = t.lower()
        if low in _NOISE:
            continue
        # Strip trailing 's' for simple plurals (≥4 chars, not ending in 'ss').
        # Catches 'eggs'→'egg', 'onions'→'onion', 'pineapples'→'pineapple'.
        # Too aggressive to catch irregulars (mice, geese) but covers the
        # common case. 'ss' exclusion protects 'grass', 'glass'.
        if len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
            low = low[:-1]
        stems.add(low)
    return stems


class Command(BaseCommand):
    help = 'Report InvoiceLineItem rows where canonical and raw_desc share no tokens.'

    def add_arguments(self, parser):
        parser.add_argument('--min-count', type=int, default=1,
                            help='Only report (product, raw_desc) pairs seen N+ times (default 1)')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor')
        parser.add_argument('--json', type=str, default=None,
                            help='Write full report as JSON to this path')
        parser.add_argument('--write-to-review', action='store_true',
                            help='Push correction rows to the Mapping Review tab with '
                                 'top-3 candidate canonicals in notes. Auto-apply picks '
                                 'them up on the next cron pass once you set status=Y '
                                 'and fill in the chosen canonical in col E.')
        parser.add_argument('--dry-run', action='store_true',
                            help='With --write-to-review: preview the rows without writing.')

    def handle(self, *args, **opts):
        qs = (InvoiceLineItem.objects
              .filter(product__isnull=False)
              .exclude(raw_description='')
              .select_related('product', 'vendor'))

        if opts['vendor']:
            qs = qs.filter(vendor__name__icontains=opts['vendor'])

        # Group by (product_id, normalized raw_desc) to dedupe repeats
        groups: dict[tuple, dict] = defaultdict(
            lambda: {'count': 0, 'dates': [], 'vendors': set(),
                     'canonical': '', 'raw_desc': '', 'product_id': None})

        suspect_count = 0
        total_scanned = 0

        for ili in qs.iterator():
            total_scanned += 1
            canon_stems = _stems(ili.product.canonical_name)
            desc_stems = _stems(ili.raw_description)

            # Guard: need meaningful content on both sides to call it suspect
            if len(canon_stems) < 1 or len(desc_stems) < 2:
                continue
            if canon_stems & desc_stems:
                continue  # at least one token overlaps — not suspect

            suspect_count += 1
            key = (ili.product_id, ili.raw_description.strip().lower())
            g = groups[key]
            g['count'] += 1
            g['canonical'] = ili.product.canonical_name
            g['raw_desc'] = ili.raw_description
            g['product_id'] = ili.product_id
            if ili.vendor:
                g['vendors'].add(ili.vendor.name)
            g['dates'].append(ili.invoice_date.isoformat() if ili.invoice_date else '')

        # Filter by min_count + sort by count descending
        filtered = sorted(
            [(k, v) for k, v in groups.items() if v['count'] >= opts['min_count']],
            key=lambda kv: -kv[1]['count'],
        )

        # Report
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Suspect mapping audit ==='))
        self.stdout.write(
            f'Total scanned: {total_scanned} InvoiceLineItem rows with product set\n'
            f'Suspect rows (zero token overlap): {suspect_count}\n'
            f'Unique (product, raw_desc) pairs: {len(groups)}\n'
            f'Pairs shown (min_count={opts["min_count"]}): {len(filtered)}\n')

        if not filtered:
            self.stdout.write(self.style.SUCCESS('No suspect mappings at this threshold.'))
            return

        self.stdout.write(self.style.HTTP_INFO(
            '\n=== Suspects (by frequency, highest first) ==='))
        for (pid, _), g in filtered[:50]:
            vendors = ', '.join(sorted(g['vendors'])) or '—'
            dates = sorted(set(g['dates']))
            date_range = f"{dates[0][:7]} → {dates[-1][:7]}" if len(dates) > 1 else (dates[0] if dates else '—')
            self.stdout.write(
                f'  [×{g["count"]:3d}]  {g["canonical"]:<35}  ←  "{g["raw_desc"][:50]}"')
            self.stdout.write(
                f'           vendors: {vendors:<30}  dates: {date_range}')

        if len(filtered) > 50:
            self.stdout.write(f'  ... + {len(filtered) - 50} more (use --json for full list)')

        if opts['json']:
            out = [
                {
                    'count': v['count'],
                    'canonical': v['canonical'],
                    'raw_description': v['raw_desc'],
                    'product_id': v['product_id'],
                    'vendors': sorted(v['vendors']),
                    'first_date': min(v['dates']) if v['dates'] else None,
                    'last_date': max(v['dates']) if v['dates'] else None,
                }
                for (_, _), v in filtered
            ]
            Path(opts['json']).write_text(json.dumps(out, indent=2))
            self.stdout.write(self.style.SUCCESS(
                f'\n  Full report written to {opts["json"]} ({len(out)} entries)'))

        self.stdout.write(self.style.WARNING(
            '\nFix path: find each raw_description in the Google Sheets "Item Mapping"\n'
            'tab and correct the canonical (column F). Then `python invoice_processor/\n'
            'cleanup_mappings.py --apply` + `python manage.py reprocess_invoices` to\n'
            'pick up corrections.'))

        if opts['write_to_review']:
            self._write_corrections_to_review(filtered, dry_run=opts['dry_run'])

    def _write_corrections_to_review(self, filtered, dry_run: bool = False):
        """Push each flagged suspect pair to the Mapping Review tab as a
        correction row. Notes carries 'Row N · WAS: X · Candidates: ...' —
        apply_approved reads the row number and rewrites col F of the
        Item Mapping sheet when the user sets status=Y and fills col E."""
        import sys
        from django.conf import settings
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)
        from rapidfuzz import process, fuzz, utils as fuzz_utils
        from sheets import get_sheets_client, get_sheet_values
        from config import SPREADSHEET_ID, MAPPING_TAB
        from discover_unmapped import (REVIEW_TAB, _ensure_review_tab_exists,
                                        _load_negative_matches, _is_wildcard_negated)
        from mapper import _strip_sysco_prefix
        from myapp.models import Product

        client = get_sheets_client()
        canonical_list = list(Product.objects.values_list('canonical_name', flat=True))
        self.stdout.write(f'\n=== Writing corrections to "{REVIEW_TAB}" ===')
        self.stdout.write(f'  {len(canonical_list)} canonicals in DB (candidate pool)')

        # Build (vendor_upper, desc_upper) -> Item Mapping row number lookup
        mapping_rows = get_sheet_values(SPREADSHEET_ID, f'{MAPPING_TAB}!A:G')
        row_lookup = {}
        for i, row in enumerate(mapping_rows[1:], start=2):
            while len(row) < 7:
                row.append('')
            v = row[0].strip().upper()
            d = row[1].strip().upper()
            if d:
                row_lookup[(v, d)] = i
        self.stdout.write(f'  {len(row_lookup)} rows indexed in Item Mapping')

        if not dry_run:
            _ensure_review_tab_exists(client)

        # Dedupe against existing Mapping Review rows. Real schema:
        # col A=Vendor, col B=Raw Description — dedup key is (A, B).
        existing = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:B")
        existing_keys = set()
        for row in existing[1:]:
            while len(row) < 2:
                row.append('')
            existing_keys.add((row[0].strip(), row[1].strip()))

        # Skip pairs that have a WILDCARD negative entry only. Specific
        # (vendor, raw, suggested) triples don't block corrections — they
        # mean "this specific canonical was wrong," which is exactly the
        # situation corrections are here to fix.
        negatives = _load_negative_matches()

        new_rows = []
        skipped_no_row = 0
        skipped_dupes = 0
        skipped_negatives = 0

        for (pid, _), g in filtered:
            vendor = sorted(g['vendors'])[0] if g['vendors'] else ''
            raw_desc = g['raw_desc']
            wrong_canonical = g['canonical']

            if (vendor, raw_desc) in existing_keys:
                skipped_dupes += 1
                continue
            if _is_wildcard_negated(vendor, raw_desc, negatives):
                skipped_negatives += 1
                continue

            key = (vendor.upper(), raw_desc.upper())
            row_num = row_lookup.get(key)
            if row_num is None:
                skipped_no_row += 1
                continue

            # Top-3 candidates excluding the wrong canonical. Strip Sysco
            # brand prefix first so the search sees the product name, not
            # "WHLFCLS" / "SYS IMP" / etc. noise. Matches the mapper's
            # stripped_fuzzy tier behavior.
            search_key = _strip_sysco_prefix(raw_desc)
            pool = [c for c in canonical_list if c != wrong_canonical]
            top3 = process.extract(
                search_key, pool,
                scorer=fuzz.token_set_ratio,
                processor=fuzz_utils.default_process,
                limit=3,
            )
            cand_str = ' · '.join(f'{name} ({int(score)})' for name, score, _ in top3) \
                if top3 else '(no candidates)'
            notes = f'Row {row_num} · WAS: {wrong_canonical} · Candidates: {cand_str}'

            new_rows.append([
                vendor,                # A: Vendor
                raw_desc,              # B: Raw Description
                '',                    # C: Suggested Product — blank for Sean to fill
                '',                    # D: Score
                g['count'],            # E: Count
                '',                    # F: Approve? (Y/N) — blank, manual review
                '',                    # G: Avg Price
                '',                    # H: Times Seen
                notes,                 # I: Notes — "Row N · WAS: X · Candidates: ..."
                                        # apply_approved reads "Row N" from here
                                        # and rewrites col F of Item Mapping row N.
            ])

        if not new_rows:
            self.stdout.write(self.style.WARNING('  No new correction rows to write.'))
            if skipped_dupes:
                self.stdout.write(f'  Skipped {skipped_dupes} already in review tab')
            if skipped_negatives:
                self.stdout.write(f'  Skipped {skipped_negatives} previously rejected')
            if skipped_no_row:
                self.stdout.write(f'  Skipped {skipped_no_row} without a matching Item Mapping row')
            return

        if dry_run:
            self.stdout.write(self.style.HTTP_INFO(
                f'\n[DRY RUN] Would write {len(new_rows)} correction row(s):'))
            for r in new_rows:
                self.stdout.write(f'    [{r[1]}] {r[3][:50]:<50}')
                self.stdout.write(f'      {r[8]}')
            self.stdout.write(f'\n  Skipped — dupes: {skipped_dupes} · negatives: {skipped_negatives} · no-row: {skipped_no_row}')
            return

        client.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{REVIEW_TAB}'!A:I",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': new_rows},
        ).execute()

        self.stdout.write(self.style.SUCCESS(
            f'\n✔ Wrote {len(new_rows)} correction row(s) to "{REVIEW_TAB}"'))
        if skipped_dupes:
            self.stdout.write(f'  Skipped {skipped_dupes} already in review tab')
        if skipped_negatives:
            self.stdout.write(f'  Skipped {skipped_negatives} previously rejected (in negative_matches.json)')
        if skipped_no_row:
            self.stdout.write(f'  Skipped {skipped_no_row} without a matching Item Mapping row')
        self.stdout.write(self.style.WARNING(
            '\nNext steps:\n'
            '  1. Open the sheet → "Mapping Review" tab.\n'
            '  2. For each correction row, paste the chosen canonical into\n'
            '     col C (Suggested Product). Candidates live in col I (Notes).\n'
            '     If none fit, type a new canonical.\n'
            '  3. Set col F (Approve? Y/N) to "Y".\n'
            '  4. The 6-hour cron (run_mapping_review_apply.sh) will auto-apply\n'
            '     on the next pass, rewriting col F of the Item Mapping row\n'
            '     (referenced in col I Notes as "Row N").\n'
            '  5. Set col F to "N" to reject — adds (vendor, raw_desc) to\n'
            '     negative_matches.json so future audits skip it.'))
