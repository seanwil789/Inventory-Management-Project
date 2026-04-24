"""One-off: back up current Mapping Review tab, then clear + repopulate
with the 45 placeholder SUPC rows (23 unique codes, minus the 3 just
auto-applied) sorted by dollar impact, top 8 at the top.

Run:  python invoice_processor/reset_mapping_review.py [--dry-run]
"""
import os
import sys
import json
import argparse
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import django
django.setup()

from myapp.models import InvoiceLineItem, Vendor
from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID
from discover_unmapped import REVIEW_TAB, _ensure_review_tab_exists
from mapper import load_mappings


def _placeholder_summaries():
    """Group placeholder rows by SUPC code, compute impact metrics.
    Returns list of dicts sorted by total_dollars desc."""
    sysco = Vendor.objects.get(name='Sysco')
    code_map = load_mappings().get('code_map', {})

    # Rows where raw_description is a bracketed Sysco placeholder
    groups = defaultdict(lambda: {'count': 0, 'total_dollars': 0.0,
                                    'invoice_dates': set()})
    for ili in InvoiceLineItem.objects.filter(
            vendor=sysco, raw_description__startswith='[Sysco #'):
        # Extract code from "[Sysco #NNN]"
        code = ili.raw_description.replace('[Sysco #', '').rstrip(']').strip()
        # Skip SUPCs that just got auto-applied to code_map — they resolve
        # on next reprocess, no need to show in review
        if code in code_map:
            continue
        g = groups[code]
        g['count'] += 1
        g['total_dollars'] += float(ili.extended_amount or 0)
        if ili.invoice_date:
            g['invoice_dates'].add(str(ili.invoice_date))

    out = []
    for code, g in groups.items():
        out.append({
            'code': code,
            'count': g['count'],
            'total_dollars': round(g['total_dollars'], 2),
            'avg_dollars': round(g['total_dollars'] / g['count'], 2),
            'dates': sorted(g['invoice_dates']),
        })
    out.sort(key=lambda x: -x['total_dollars'])
    return out


def _build_review_rows(summaries):
    """Convert SUPC summaries to Mapping Review tab row format.
    Columns: A=Vendor, B=Raw Description, C=Suggested Product (blank for
    user lookup), D=Score, E=Count, F=Approve (Y/N), G=Avg Price,
    H=Times Seen, I=Notes."""
    rows = []
    for i, s in enumerate(summaries):
        priority_tag = '⭐ HIGH IMPACT · ' if i < 8 else ''
        dates_preview = ', '.join(s['dates'][:3])
        if len(s['dates']) > 3:
            dates_preview += f' (+{len(s["dates"]) - 3} more)'
        notes = (
            f'{priority_tag}SUPC #{s["code"]} · code={s["code"]} · '
            f'{s["count"]} rows · ${s["total_dollars"]:.2f} total · '
            f'avg ${s["avg_dollars"]:.2f} · dates: {dates_preview} · '
            f'LOOK UP ON SYSCO PORTAL, paste desc in col C'
        )
        rows.append([
            'Sysco',                           # A: Vendor
            f'[Sysco #{s["code"]}]',           # B: Raw Description
            '',                                # C: Suggested Product — BLANK
            '',                                # D: Score
            s['count'],                        # E: Count
            '',                                # F: Approve? (Y/N)
            f'${s["avg_dollars"]:.2f}',        # G: Avg Price
            s['count'],                        # H: Times Seen
            notes,                             # I: Notes
        ])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='Preview only — no backup write, no clear, no write')
    ap.add_argument('--skip-backup', action='store_true',
                    help='Skip the JSON backup (faster; use when testing)')
    args = ap.parse_args()

    summaries = _placeholder_summaries()
    rows = _build_review_rows(summaries)

    print(f'Placeholder SUPCs to push: {len(summaries)} unique codes '
          f'({sum(s["count"] for s in summaries)} rows total)')
    print(f'Top 8 by dollar impact:')
    for i, s in enumerate(summaries[:8]):
        print(f'  #{s["code"]:<12} {s["count"]} rows  ${s["total_dollars"]:>8.2f}  '
              f'avg ${s["avg_dollars"]:>6.2f}  dates: {s["dates"][:2]}')
    if len(summaries) > 8:
        print(f'  ... plus {len(summaries) - 8} more SUPCs below')

    if args.dry_run:
        print('\n[DRY RUN] No changes made.')
        return 0

    client = get_sheets_client()
    _ensure_review_tab_exists(client)

    # Step 1: back up current tab contents
    if not args.skip_backup:
        existing = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:I")
        backup_path = f'/tmp/mapping_review_backup_{datetime.now():%Y%m%d-%H%M}.json'
        with open(backup_path, 'w') as f:
            json.dump(existing, f, indent=2)
        print(f'\n[✓] Backed up {len(existing)} existing rows to {backup_path}')

    # Step 2: clear data rows (preserve header at A1)
    client.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{REVIEW_TAB}'!A2:I",
    ).execute()
    print(f'[✓] Cleared Mapping Review data rows (header preserved).')

    # Step 3: write the new rows. RAW mode so [Sysco #NNN] placeholders
    # don't get mangled as sheet formulas (per prior session note).
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{REVIEW_TAB}'!A2",
        valueInputOption='RAW',
        body={'values': rows},
    ).execute()
    print(f'[✓] Wrote {len(rows)} SUPC rows to Mapping Review tab.')

    print(f'\nDone. Open the Mapping Review tab — top 8 high-impact are at '
          f'the top. Look up each SUPC on the Sysco portal and paste the '
          f'canonical product name into column C, then mark F=Y.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
