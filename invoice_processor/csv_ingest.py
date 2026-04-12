"""
Sysco CSV ingestion — updates the Item Mapping code library from a Sysco
portal CSV export without replacing the OCR invoice workflow.

CSV row types:
  H = invoice header   (date, total, etc.)
  F = field definitions
  P = product line     (SUPC | case_qty | split_qty | cust# | pack | brand | description | ...)

For each P row the ingestor:
  1. Skips SUPCs already in the code map.
  2. Tries an exact then fuzzy description match against existing mapping rows
     — if found, writes the SUPC into column G of that row.
  3. If no match, appends a stub row (vendor + description + SUPC, blank canonical)
     so it surfaces in the next run's unmatched report for manual naming.

Returns a summary dict so batch.py can log the results.
"""
import csv
import re
import os
from rapidfuzz import process, fuzz
from sheets import get_sheet_values, get_sheets_client
from config import SPREADSHEET_ID, MAPPING_TAB

FUZZY_THRESHOLD = 75


def _parse_csv(path: str) -> list[dict]:
    """Return list of product dicts from a Sysco CSV file."""
    items = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if not row or row[0] != 'P':
                continue
            while len(row) < 8:
                row.append('')
            supc  = row[1].strip()
            pack  = row[5].strip()
            brand = row[6].strip()
            desc  = row[7].strip()
            if supc and desc:
                items.append({'supc': supc, 'brand': brand, 'desc': desc, 'pack': pack})
    return items


def ingest_csv(path: str, dry_run: bool = False) -> dict:
    """
    Process one Sysco CSV file and update Item Mapping.
    Returns summary: {matched, added, skipped, total}
    """
    print(f"   Parsing CSV: {os.path.basename(path)}")
    items = _parse_csv(path)
    if not items:
        print("   No product rows found.")
        return {'matched': 0, 'added': 0, 'skipped': 0, 'total': 0}

    print(f"   Found {len(items)} product rows.")

    # ── Load current mapping ─────────────────────────────────────────────────
    mapping_rows = get_sheet_values(SPREADSHEET_ID, f'{MAPPING_TAB}!A:G')
    desc_to_row = {}   # raw_desc_upper -> 1-based sheet row
    code_set    = set()

    for i, row in enumerate(mapping_rows[1:], start=2):
        while len(row) < 7:
            row.append('')
        d = row[1].strip().upper()
        c = row[6].strip()
        if d:
            desc_to_row[d] = i
        if c:
            code_set.add(c)

    # ── Match each CSV item ──────────────────────────────────────────────────
    code_updates = []   # (row_index, supc)
    stub_rows    = []   # new rows to append
    skipped      = 0

    for item in items:
        supc = item['supc']
        desc = item['desc']
        norm = re.sub(r'\s+', ' ', desc.upper().strip())

        if supc in code_set:
            skipped += 1
            continue

        # Exact match
        if norm in desc_to_row:
            code_updates.append((desc_to_row[norm], supc))
            continue

        # Fuzzy match
        result = process.extractOne(norm, desc_to_row.keys(), scorer=fuzz.token_sort_ratio)
        if result and result[1] >= FUZZY_THRESHOLD:
            code_updates.append((desc_to_row[result[0]], supc))
            continue

        # No match — add stub
        stub_rows.append([
            'Sysco',    # vendor
            desc,       # item_description  (clean CSV text, not garbled OCR)
            '',         # category          (blank — user fills in)
            '',         # primary_descriptor
            '',         # secondary_descriptor
            '',         # canonical_name    (blank — user fills in)
            supc,       # sysco_item_code
        ])

    # ── Apply updates ────────────────────────────────────────────────────────
    if dry_run:
        print(f"   [DRY RUN] Would update {len(code_updates)} existing rows, "
              f"add {len(stub_rows)} stubs, skip {skipped} already-mapped.")
        return {'matched': len(code_updates), 'added': len(stub_rows),
                'skipped': skipped, 'total': len(items)}

    client = get_sheets_client()

    if code_updates:
        data = [{'range': f'{MAPPING_TAB}!G{row_i}', 'values': [[supc]]}
                for row_i, supc in code_updates]
        client.values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={'valueInputOption': 'USER_ENTERED', 'data': data},
        ).execute()
        print(f"   [✓] Matched {len(code_updates)} SUPC codes to existing rows.")

    if stub_rows:
        client.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f'{MAPPING_TAB}!A:G',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': stub_rows},
        ).execute()
        print(f"   [✓] Added {len(stub_rows)} new stub rows (canonical name needed).")
        print(f"       Open the '{MAPPING_TAB}' tab and fill in column F for:")
        for row in stub_rows:
            print(f"       SUPC {row[6]}  {row[1]}")

    if skipped:
        print(f"   [–] Skipped {skipped} SUPCs already in the mapping.")

    return {'matched': len(code_updates), 'added': len(stub_rows),
            'skipped': skipped, 'total': len(items)}
