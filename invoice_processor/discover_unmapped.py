"""
Discover unmapped invoice items and suggest canonical name mappings.

Queries the DB for items with product=NULL, groups by description,
fuzzy-matches against existing Product names and Synergy sheet rows,
and outputs actionable suggestions that can be written to the Item Mapping sheet.

Usage:
  python discover_unmapped.py                # preview suggestions
  python discover_unmapped.py --write        # write top suggestions to Item Mapping
  python discover_unmapped.py --vendor Sysco # only show one vendor
  python discover_unmapped.py --min-count 3  # only items seen 3+ times
"""
import os
import sys
import re
import json
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))

# Bootstrap Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import django
django.setup()

from rapidfuzz import fuzz, process, utils as fuzz_utils
from myapp.models import InvoiceLineItem, Product, Vendor
from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID, ACTIVE_SHEET_TAB

# Descriptions that are never real products — filter these out
JUNK_PATTERNS = re.compile(
    r'^\s*$'
    r'|FUEL\s*SURCHARGE'
    r'|CREDIT\s*CARD'
    r'|REMOTE.?STOCK'
    r'|GROUP\s*TOTAL'
    r'|ORDER\s*SUMMARY'
    r'|MISC\s*CHARGES'
    r'|\*{3,}'
    r'|CHARGE\s+FOR'
    r'|SALES\s*TAX'
    r'|DELIVERY\s*FEE'
    r'|ASK\s+YOUR\s+MA'
    r'|PA\s+SALES\s+TAX'
    r'|T/WT='
    r'|DAIRY\s*\*{2}'
    r'|^\[Sysco\s*#\d+\]$',
    re.IGNORECASE,
)


def is_junk(desc: str) -> bool:
    """Return True if description is a non-product line (surcharge, header, etc.)."""
    return bool(JUNK_PATTERNS.search(desc))


def clean_description(desc: str) -> str:
    """Clean up a raw description for display and matching."""
    # Strip leading quantities/units from Sysco-style descriptions
    cleaned = re.sub(r'^\d+\.?\d*\s*(?:CS|BG|LB|OZ|CT|GAL|EA|PK)\s+', '', desc, flags=re.IGNORECASE)
    # Strip leading pack sizes like "125 LB", "612 CT"
    cleaned = re.sub(r'^\d+\.?\d*\s*(?:LB|OZ|CT|GAL|EA|PK|#)\s+', '', cleaned, flags=re.IGNORECASE)
    # Strip Sysco brand prefixes
    cleaned = re.sub(r'^(?:WHLFCLS|GRECOSN|COOPR|SYS\s*CLS|SYFPNAT|BBRL(?:CLS|IMP)|'
                     r'AREZIMP|ARZRSVS|IMP(?:/MCC)?|TROPCNA|FLEISHM|CHOBANI|'
                     r'FRANKRH|EMBASSA|PLANTER|LABELLA|MINMAID|DRISCOL|VOLLRTH|'
                     r'KEYSTON|CTTORKADV|OZSCTHBRT|HORMEL|PURLIFE)\s+',
                     '', cleaned, flags=re.IGNORECASE)
    # Strip trailing Sysco item codes
    cleaned = re.sub(r'\s+\d{6,7}\s*$', '', cleaned)
    # Strip trailing pack info that got appended
    cleaned = re.sub(r'\s+\d+/\d+\s*$', '', cleaned)
    return cleaned.strip()


def load_synergy_products(sheet_tab: str = None) -> list[dict]:
    """Load product names and vendors from the Synergy sheet."""
    tab = sheet_tab or ACTIVE_SHEET_TAB
    raw = get_sheet_values(SPREADSHEET_ID, f"'{tab}'!A:C")
    products = []
    current_section = ""
    for i, row in enumerate(raw):
        while len(row) < 3:
            row.append("")
        sub_cat = row[0].strip()
        product = row[1].strip()
        vendor = row[2].strip()
        if sub_cat and sub_cat.lower() not in ("sub category", ""):
            current_section = sub_cat
        if product and product.lower() not in ("product", "sub category", ""):
            products.append({
                "product": product,
                "vendor": vendor,
                "section": current_section,
            })
    return products


def suggest_canonical(desc: str, vendor: str,
                      product_names: list[str],
                      synergy_names: list[str],
                      synergy_products: list[dict] = None) -> dict | None:
    """
    Suggest a canonical name for an unmapped description by fuzzy matching
    against existing Product names and Synergy sheet names.
    """
    cleaned = clean_description(desc)
    if len(cleaned) < 3:
        return None

    # Try matching against existing canonical Product names first
    if product_names:
        matches = process.extract(
            cleaned, product_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
            limit=3,
        )
        if matches and matches[0][1] >= 80:
            # Look up category from Product model
            cat = ""
            try:
                prod = Product.objects.filter(canonical_name=matches[0][0]).first()
                if prod:
                    cat = prod.category or ""
            except Exception:
                pass
            return {
                "suggested": matches[0][0],
                "score": matches[0][1],
                "source": "product_db",
                "cleaned": cleaned,
                "category": cat,
            }

    # Try matching against Synergy sheet product names
    if synergy_names:
        matches = process.extract(
            cleaned, synergy_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
            limit=3,
        )
        if matches and matches[0][1] >= 75:
            # Look up section from synergy products
            cat = ""
            for sp in synergy_products:
                if sp["product"] == matches[0][0]:
                    cat = sp.get("section", "")
                    break
            return {
                "suggested": matches[0][0],
                "score": matches[0][1],
                "source": "synergy_sheet",
                "cleaned": cleaned,
                "category": cat,
            }

    return None


REVIEW_TAB = "Mapping Review"


# Known mismatches where fuzzy matching produces wrong results.
# Key = (cleaned_description_substring, wrong_canonical)
_KNOWN_MISMATCHES = {
    ("tortilla", "Corn"),
    ("tortilla", "Lime"),
    ("corn starch", "Corn"),
    ("cornstarch", "Corn"),
    ("pickle", "Dill"),
    ("pineapple juice", "Pineapple"),
    ("corn meal", "Corn"),
    ("polenta", "Corn"),
    ("lacroix", "Lime"),
    ("la croix", "Lime"),
    ("cheese mozz", "Milk"),
    ("mozz pizza", "Milk"),
    # Learned from review rejections
    ("gluten", "AP Flour"),
    ("semi", "Chocolate Sauce"),
    ("chip", "Chocolate Sauce"),
    ("hershey", "Chocolate Sauce"),
    ("brst", "Chicken Base"),
    ("crumb", "Rye Bread"),
    ("potato", "Baking Soda"),
    ("russet", "Baking Soda"),
    ("spice", "Ground Pork"),
}

AUTO_APPROVE_THRESHOLD = 90  # Score >= this gets auto-approved

NEGATIVE_MATCH_PATH = os.path.join(os.path.dirname(__file__), "mappings", "negative_matches.json")


def _load_negative_matches() -> set[tuple[str, str]]:
    """Load rejected (vendor, description) pairs so they aren't re-suggested."""
    if os.path.exists(NEGATIVE_MATCH_PATH):
        with open(NEGATIVE_MATCH_PATH) as f:
            data = json.load(f)
        return set(tuple(pair) for pair in data)
    return set()


def _save_negative_matches(negatives: set[tuple[str, str]]):
    """Save rejected pairs to disk."""
    os.makedirs(os.path.dirname(NEGATIVE_MATCH_PATH), exist_ok=True)
    with open(NEGATIVE_MATCH_PATH, "w") as f:
        json.dump(sorted(negatives), f, indent=2)


def _is_known_mismatch(desc: str, suggested: str) -> bool:
    """Check if a suggestion is a known false positive."""
    desc_lower = desc.lower()
    suggested_lower = suggested.lower()
    for substring, wrong in _KNOWN_MISMATCHES:
        if substring in desc_lower and wrong.lower() == suggested_lower:
            return True
    return False


def _get_review_sheet_id(client) -> int | None:
    """Get the sheetId for the Mapping Review tab."""
    meta = client.get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == REVIEW_TAB:
            return sheet["properties"]["sheetId"]
    return None


def _ensure_review_tab_exists(client):
    """Create the Mapping Review tab if it doesn't exist, with formatting."""
    sheet_meta = client.get(spreadsheetId=SPREADSHEET_ID).execute()
    existing_tabs = [s["properties"]["title"] for s in sheet_meta["sheets"]]

    if REVIEW_TAB in existing_tabs:
        return

    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": REVIEW_TAB}}}]},
    ).execute()

    print(f"  Created '{REVIEW_TAB}' tab.")
    _format_review_tab(client)


def _format_review_tab(client):
    """Apply formatting: header, freeze, column widths, dropdown, conditional formatting."""
    sheet_id = _get_review_sheet_id(client)
    if sheet_id is None:
        return

    # Write header
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{REVIEW_TAB}'!A1:I1",
        valueInputOption="USER_ENTERED",
        body={"values": [[
            "Status", "Vendor", "Category", "Raw Description",
            "Suggested Canonical", "Score", "Avg Price", "Times Seen", "Notes",
        ]]},
    ).execute()

    requests = [
        # Freeze header row
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Bold header row
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }},
        # Column widths
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},  # A: Status
            "properties": {"pixelSize": 90}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": 2},  # B: Vendor
            "properties": {"pixelSize": 120}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 2, "endIndex": 3},  # C: Category
            "properties": {"pixelSize": 100}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 3, "endIndex": 4},  # D: Raw Description
            "properties": {"pixelSize": 320}, "fields": "pixelSize",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 4, "endIndex": 5},  # E: Suggested Canonical
            "properties": {"pixelSize": 200}, "fields": "pixelSize",
        }},
        # Status dropdown validation (A2:A1000) — Y/N
        {"setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": "Y"},
                                         {"userEnteredValue": "N"}]},
                "showCustomUi": True,
                "strict": False,
            },
        }},
        # Conditional formatting: Y = green background
        {"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": '=$A2="Y"'}]},
                    "format": {"backgroundColor": {"red": 0.85, "green": 0.95, "blue": 0.85}},
                },
            },
            "index": 0,
        }},
        # Conditional formatting: N = red background
        {"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": '=$A2="N"'}]},
                    "format": {"backgroundColor": {"red": 0.95, "green": 0.85, "blue": 0.85}},
                },
            },
            "index": 1,
        }},
    ]

    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests},
    ).execute()

    print(f"  Applied formatting to '{REVIEW_TAB}' tab.")


def write_suggestions_to_review(suggestions: list[dict]):
    """
    Write suggested mappings to the Mapping Review tab for user approval.

    Features:
      - Filters out known mismatches (tortilla→Corn, pickle→Dill, etc.)
      - Auto-approves suggestions scoring >= 90%
      - Sorted by vendor then frequency (most seen first)
      - Includes category column for context
      - Dropdown for Status, conditional formatting for APPROVE/REJECT/DONE

    Columns:
      A: Status              — APPROVE (auto for 90%+), blank for review, dropdown
      B: Vendor
      C: Category            — product category for context
      D: Raw Description     — what DocAI extracted
      E: Suggested Canonical — editable, what it will map to
      F: Score               — fuzzy match confidence
      G: Avg Price           — average price seen across invoices
      H: Times Seen          — how many invoices this description appeared on
      I: Notes               — free-form
    """
    client = get_sheets_client()
    _ensure_review_tab_exists(client)

    # Check for existing rows to avoid duplicates (check cols C:D now)
    existing = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!B:D")
    existing_keys = set()
    for row in existing[1:]:
        if len(row) >= 3:
            existing_keys.add((row[0].strip(), row[2].strip()))

    # Load negative matches to avoid re-suggesting rejected pairs
    negatives = _load_negative_matches()

    # Filter and build rows
    rows = []
    filtered_mismatches = 0
    skipped_dupes = 0
    skipped_negatives = 0

    # Sort by vendor, then by count descending
    sorted_suggestions = sorted(suggestions,
                                 key=lambda s: (s["vendor"], -s["count"]))

    for s in sorted_suggestions:
        key = (s["vendor"], s["raw_description"])
        if key in existing_keys:
            skipped_dupes += 1
            continue

        # Skip previously rejected suggestions
        if key in negatives:
            skipped_negatives += 1
            continue

        # Filter known mismatches
        if _is_known_mismatch(s.get("cleaned", s["raw_description"]), s["suggested"]):
            filtered_mismatches += 1
            continue

        # Auto-approve high confidence
        status = "Y" if s["score"] >= AUTO_APPROVE_THRESHOLD else ""

        rows.append([
            status,                            # A: Status
            s["vendor"],                       # B: Vendor
            s.get("category", ""),             # C: Category
            s["raw_description"],              # D: Raw Description
            s["suggested"],                    # E: Suggested Canonical
            round(s["score"], 1),              # F: Score
            f"${s['avg_price']:.2f}" if s.get("avg_price") else "",  # G: Avg Price
            s["count"],                        # H: Times Seen
            "",                                # I: Notes
        ])

    if not rows:
        print(f"No new suggestions to write.")
        if skipped_dupes:
            print(f"  ({skipped_dupes} already in review tab)")
        if filtered_mismatches:
            print(f"  ({filtered_mismatches} known mismatches filtered)")
        return

    # Clear old DONE rows first
    existing_data = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:I")
    done_count = sum(1 for row in existing_data[1:] if row and row[0].strip().upper() == "DONE")

    if done_count > 0:
        # Delete DONE rows by rewriting the sheet (keep header + non-DONE rows)
        keep_rows = [existing_data[0]]  # header
        for row in existing_data[1:]:
            if row and row[0].strip().upper() != "DONE":
                keep_rows.append(row)

        # Clear and rewrite
        client.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{REVIEW_TAB}'!A2:I",
        ).execute()

        if len(keep_rows) > 1:
            client.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{REVIEW_TAB}'!A2:I",
                valueInputOption="USER_ENTERED",
                body={"values": keep_rows[1:]},
            ).execute()

        print(f"  Cleared {done_count} completed (DONE) rows.")

    # Append new suggestions
    client.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{REVIEW_TAB}'!A:I",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

    auto_approved = sum(1 for r in rows if r[0] == "Y")
    needs_review = len(rows) - auto_approved

    print(f"\nWrote {len(rows)} suggestions to '{REVIEW_TAB}' tab:")
    print(f"  Auto-approved (score >= {AUTO_APPROVE_THRESHOLD}%): {auto_approved}")
    print(f"  Needs manual review: {needs_review}")
    if skipped_dupes:
        print(f"  Skipped (already in tab): {skipped_dupes}")
    if skipped_negatives:
        print(f"  Skipped (previously rejected): {skipped_negatives}")
    if filtered_mismatches:
        print(f"  Filtered (known mismatches): {filtered_mismatches}")
    print(f"\nWorkflow:")
    print(f"  1. Open Google Sheet → '{REVIEW_TAB}' tab")
    print(f"  2. Review items without a status — edit Suggested Canonical if needed")
    print(f"  3. Set Status to APPROVE or REJECT (dropdown)")
    print(f"  4. Run: python discover_unmapped.py --apply-approved")


def apply_approved():
    """
    Read APPROVE rows from the Mapping Review tab and move them
    to the Item Mapping tab, then mark them as DONE in the review tab.

    Uses new column layout:
      A: Status, B: Vendor, C: Category, D: Raw Description,
      E: Suggested Canonical, F: Score, G: Avg Price, H: Times Seen, I: Notes
    """
    client = get_sheets_client()
    raw = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:I")

    if not raw or len(raw) <= 1:
        print("No rows in Mapping Review tab.")
        return

    approved = []
    approved_row_nums = []
    rejected_pairs = []

    for i, row in enumerate(raw[1:], start=2):  # skip header, 1-indexed
        while len(row) < 9:
            row.append("")
        status = row[0].strip().upper()

        if status in ("N", "REJECT"):
            rejected_pairs.append((row[1].strip(), row[3].strip()))

        if status in ("Y", "APPROVE"):
            approved.append({
                "vendor": row[1].strip(),
                "category": row[2].strip(),
                "raw_description": row[3].strip(),
                "canonical": row[4].strip(),
                "notes": row[8].strip(),
                "row_num": i,
            })
            approved_row_nums.append(i)

    if not approved:
        print("No approved rows found. Mark rows as Y in the Status column first.")
        return

    print(f"Found {len(approved)} approved mappings. Writing to Item Mapping...")

    # Separate SUPC updates (update existing row's column F) from new appends
    supc_updates = []
    new_appends = []

    for a in approved:
        notes = a.get("notes", "")
        category = a.get("category", "")

        # If category starts with "SUPC:" and notes has "Row N", this is a
        # SUPC code update — write canonical to column F of the existing row
        if category.startswith("SUPC:") and "Row " in notes:
            try:
                target_row = int(notes.replace("Row ", "").strip())
                supc_updates.append({
                    "canonical": a["canonical"],
                    "target_row": target_row,
                })
            except ValueError:
                new_appends.append(a)
        else:
            new_appends.append(a)

    # Write SUPC canonical updates (column F of existing Item Mapping rows)
    if supc_updates:
        batch_data = []
        for u in supc_updates:
            batch_data.append({
                "range": f"Item Mapping!F{u['target_row']}",
                "values": [[u["canonical"]]],
            })
        for chunk_start in range(0, len(batch_data), 100):
            chunk = batch_data[chunk_start:chunk_start+100]
            client.values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": chunk},
            ).execute()
        print(f"  [OK] {len(supc_updates)} SUPC canonical names updated in Item Mapping.")

    # Append new mapping rows
    if new_appends:
        mapping_rows = []
        for a in new_appends:
            mapping_rows.append([
                a["vendor"],           # A: Vendor
                a["raw_description"],  # B: Raw Description
                a["category"],         # C: Category
                "",                    # D: Primary descriptor
                "",                    # E: Secondary descriptor
                a["canonical"],        # F: Canonical name
                "",                    # G: SUPC Code
            ])

        client.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Item Mapping!A:G",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": mapping_rows},
        ).execute()
        print(f"  [OK] {len(mapping_rows)} new rows added to Item Mapping.")

    # Mark as DONE in review tab
    batch_data = []
    for row_num in approved_row_nums:
        batch_data.append({
            "range": f"'{REVIEW_TAB}'!A{row_num}",
            "values": [["DONE"]],
        })

    if batch_data:
        client.values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": batch_data},
        ).execute()

    print(f"  [OK] Marked {len(approved_row_nums)} rows as DONE in '{REVIEW_TAB}'.")

    # Save rejections to negative match memory
    if rejected_pairs:
        negatives = _load_negative_matches()
        new_negatives = set(tuple(p) for p in rejected_pairs) - negatives
        if new_negatives:
            negatives.update(new_negatives)
            _save_negative_matches(negatives)
            print(f"  [OK] Saved {len(new_negatives)} rejections to negative match memory ({len(negatives)} total)")

    # Count remaining
    remaining = sum(1 for row in raw[1:] if len(row) > 0 and row[0].strip().upper() not in ("Y", "N", "APPROVE", "REJECT", "DONE"))
    rejected = sum(1 for row in raw[1:] if len(row) > 0 and row[0].strip().upper() in ("N", "REJECT"))
    print(f"\n  Remaining to review: {remaining}")
    print(f"  Rejected: {rejected}")

    # Re-map unmapped DB items with the updated mappings
    print(f"\n  Re-mapping unmapped DB items with updated mappings...")
    remapped = _remap_unmapped_items()
    print(f"  [OK] {remapped} items linked to products in DB")

    # Show updated fidelity
    total = InvoiceLineItem.objects.count()
    mapped = InvoiceLineItem.objects.filter(product__isnull=False).count()
    mapped_price = InvoiceLineItem.objects.filter(product__isnull=False, unit_price__isnull=False).count()
    flagged = InvoiceLineItem.objects.filter(price_flagged=True).count()

    print(f"\n  === FIDELITY ===")
    print(f"  Mapped:         {mapped}/{total} ({mapped/total*100:.1f}%)")
    print(f"  Mapped + price: {mapped_price}/{total} ({mapped_price/total*100:.1f}%)")
    if flagged:
        print(f"  Price anomalies: {flagged}")

    # Confidence distribution
    from django.db.models import Count
    conf_dist = (InvoiceLineItem.objects
        .filter(product__isnull=False, match_confidence__gt="")
        .values("match_confidence")
        .annotate(cnt=Count("id"))
        .order_by("-cnt"))
    if conf_dist:
        print(f"\n  Match confidence breakdown:")
        for row in conf_dist:
            print(f"    {row['match_confidence']:<20} {row['cnt']:>5}")


def run_audit():
    """Show accuracy audit: confidence distribution, price anomalies, suspect matches."""
    from django.db.models import Count, Avg, F, Q

    total = InvoiceLineItem.objects.count()
    mapped = InvoiceLineItem.objects.filter(product__isnull=False).count()
    unmapped = total - mapped
    has_price = InvoiceLineItem.objects.filter(unit_price__isnull=False).count()
    mapped_price = InvoiceLineItem.objects.filter(product__isnull=False, unit_price__isnull=False).count()
    flagged = InvoiceLineItem.objects.filter(price_flagged=True).count()

    print(f"{'='*60}")
    print(f"ACCURACY AUDIT")
    print(f"{'='*60}")
    print(f"\n  Total items:      {total}")
    print(f"  Mapped:           {mapped} ({mapped/total*100:.1f}%)")
    print(f"  Unmapped:         {unmapped} ({unmapped/total*100:.1f}%)")
    print(f"  Has price:        {has_price} ({has_price/total*100:.1f}%)")
    print(f"  Mapped + price:   {mapped_price} ({mapped_price/total*100:.1f}%)")

    # Confidence distribution
    print(f"\n  --- Confidence Distribution ---")
    conf_dist = (InvoiceLineItem.objects
        .filter(product__isnull=False)
        .values("match_confidence")
        .annotate(cnt=Count("id"))
        .order_by("-cnt"))

    no_conf = InvoiceLineItem.objects.filter(product__isnull=False, match_confidence="").count()
    if no_conf:
        print(f"  {'(pre-tracking)' :<20} {no_conf:>5}  — matched before confidence was stored")
    for row in conf_dist:
        if row["match_confidence"]:
            print(f"  {row['match_confidence']:<20} {row['cnt']:>5}")

    # Price anomalies
    print(f"\n  --- Price Anomalies ({flagged} flagged) ---")
    if flagged:
        anomalies = (InvoiceLineItem.objects
            .filter(price_flagged=True)
            .select_related("product", "vendor")
            .order_by("-invoice_date")[:20])
        for item in anomalies:
            name = item.product.canonical_name if item.product else item.raw_description[:30]
            vendor = item.vendor.name if item.vendor else "?"
            print(f"  ${float(item.unit_price):>8.2f}  {name:<30} {item.invoice_date}  [{vendor}]")
        if flagged > 20:
            print(f"  ... and {flagged - 20} more")
    else:
        print(f"  None detected (anomaly detection runs on new invoice processing)")

    # Lowest-confidence fuzzy matches (most likely to be wrong)
    print(f"\n  --- Lowest Confidence Matches (most suspect) ---")
    suspect = (InvoiceLineItem.objects
        .filter(
            product__isnull=False,
            match_confidence__in=["vendor_fuzzy", "global_fuzzy", "stripped-fuzzy"],
            match_score__isnull=False,
        )
        .select_related("product", "vendor")
        .order_by("match_score")[:15])
    if suspect:
        for item in suspect:
            name = item.product.canonical_name if item.product else "?"
            desc = item.raw_description[:35] if item.raw_description else ""
            print(f"  {item.match_score:>3}%  {desc:<35} → {name:<25} [{item.match_confidence}]")
    else:
        print(f"  No fuzzy matches with scores recorded yet")

    # Per-vendor fidelity
    print(f"\n  --- Per-Vendor Fidelity ---")
    from myapp.models import Vendor
    for v in Vendor.objects.all().order_by("name"):
        vt = InvoiceLineItem.objects.filter(vendor=v).count()
        vm = InvoiceLineItem.objects.filter(vendor=v, product__isnull=False).count()
        if vt > 0:
            print(f"  {v.name:<35} {vm:>5}/{vt:<5} ({vm/vt*100:.1f}%)")


def _remap_unmapped_items() -> int:
    """Re-run the mapper on all unmapped DB items and link them to Products."""
    from mapper import load_mappings, resolve_item

    mappings = load_mappings(force_refresh=True)
    unmapped = InvoiceLineItem.objects.filter(product__isnull=True).exclude(raw_description="")

    remapped = 0
    for item in unmapped.iterator():
        vendor_name = item.vendor.name if item.vendor else ""
        item_dict = {"raw_description": item.raw_description, "sysco_item_code": ""}
        result = resolve_item(item_dict, mappings, vendor=vendor_name)
        canonical = result.get("canonical")

        if canonical and result.get("confidence") != "unmatched":
            product = Product.objects.filter(canonical_name=canonical).first()
            if not product:
                cat_info = mappings.get("category_map", {}).get(canonical, {})
                product = Product.objects.create(
                    canonical_name=canonical,
                    category=cat_info.get("category", ""),
                    primary_descriptor=cat_info.get("primary_descriptor", ""),
                    secondary_descriptor=cat_info.get("secondary_descriptor", ""),
                )
            item.product = product
            item.match_confidence = result.get("confidence", "")
            score = result.get("score")
            item.match_score = int(score) if score is not None else None
            item.save(update_fields=["product", "match_confidence", "match_score"])
            remapped += 1

    return remapped


def main():
    parser = argparse.ArgumentParser(
        description="Discover unmapped invoice items and suggest canonical mappings"
    )
    parser.add_argument("--write", action="store_true",
                        help="Write high-confidence suggestions to Mapping Review tab for approval")
    parser.add_argument("--apply-approved", action="store_true",
                        help="Move APPROVE rows from Mapping Review tab to Item Mapping")
    parser.add_argument("--audit", action="store_true",
                        help="Show accuracy audit: confidence distribution, price anomalies, suspect matches")
    parser.add_argument("--vendor", type=str, default=None,
                        help="Filter to one vendor")
    parser.add_argument("--min-count", type=int, default=2,
                        help="Only show items seen N+ times (default: 2)")
    parser.add_argument("--min-score", type=int, default=80,
                        help="Minimum fuzzy score for suggestions (default: 80)")
    args = parser.parse_args()

    # Load reference data
    print("Loading existing Product names from DB...")
    product_names = list(Product.objects.values_list("canonical_name", flat=True))
    print(f"  {len(product_names)} canonical products in DB")

    print("Loading Synergy sheet products...")
    synergy_products = load_synergy_products()
    synergy_names = [p["product"] for p in synergy_products]
    print(f"  {len(synergy_names)} products in Synergy sheet")

    # Query unmapped items
    print("\nQuerying unmapped items from DB...")
    qs = InvoiceLineItem.objects.filter(product__isnull=True).exclude(raw_description="")
    if args.vendor:
        qs = qs.filter(vendor__name__icontains=args.vendor)

    # Count by (description, vendor)
    desc_counts = Counter()
    desc_prices = defaultdict(list)
    for item in qs.values_list("raw_description", "vendor__name", "unit_price"):
        desc, vendor, price = item
        if is_junk(desc):
            continue
        key = (desc, vendor or "Unknown")
        desc_counts[key] += 1
        if price:
            desc_prices[key].append(float(price))

    # Filter by min count
    frequent = {k: v for k, v in desc_counts.items() if v >= args.min_count}
    total_junk = sum(desc_counts.values()) - sum(frequent.values())

    print(f"  {sum(desc_counts.values())} non-junk unmapped item occurrences")
    print(f"  {len(frequent)} unique descriptions seen {args.min_count}+ times")
    print(f"  {total_junk} occurrences below min-count threshold")

    # Generate suggestions
    print("\nGenerating suggestions...\n")

    suggestions = []
    no_match = []

    for (desc, vendor), count in sorted(frequent.items(), key=lambda x: -x[1]):
        suggestion = suggest_canonical(desc, vendor, product_names, synergy_names, synergy_products)

        prices = desc_prices[(desc, vendor)]
        avg_price = sum(prices) / len(prices) if prices else 0

        if suggestion and suggestion["score"] >= args.min_score:
            suggestions.append({
                "raw_description": desc,
                "vendor": vendor,
                "count": count,
                "avg_price": avg_price,
                **suggestion,
            })
        else:
            no_match.append({
                "raw_description": desc,
                "vendor": vendor,
                "count": count,
                "avg_price": avg_price,
                "cleaned": clean_description(desc),
                "best_score": suggestion["score"] if suggestion else 0,
                "best_match": suggestion["suggested"] if suggestion else "",
            })

    # ── Report: High-confidence suggestions ─────────────────────────────
    print(f"{'='*80}")
    print(f"HIGH-CONFIDENCE SUGGESTIONS (score >= {args.min_score})")
    print(f"{'='*80}")
    print(f"{'Seen':>4}  {'Vendor':<14} {'Raw Description':<40} → {'Suggested Canonical':<30} {'Score':>5}")
    print(f"{'-'*4}  {'-'*14} {'-'*40}   {'-'*30} {'-'*5}")

    by_vendor = defaultdict(list)
    for s in suggestions:
        by_vendor[s["vendor"]].append(s)

    for vendor in sorted(by_vendor):
        items = by_vendor[vendor]
        print(f"\n  [{vendor}]")
        for s in sorted(items, key=lambda x: -x["count"]):
            print(f"  {s['count']:>4}  {'':<14} {s['raw_description'][:40]:<40} → {s['suggested'][:30]:<30} {s['score']:>5}")

    total_occurrences = sum(s["count"] for s in suggestions)
    print(f"\n  {len(suggestions)} suggestions covering {total_occurrences} item occurrences")

    # ── Report: No match found ──────────────────────────────────────────
    if no_match:
        print(f"\n{'='*80}")
        print(f"NO MATCH FOUND (need manual mapping)")
        print(f"{'='*80}")
        print(f"{'Seen':>4}  {'Vendor':<14} {'Cleaned Description':<40} {'Best Match':<25} {'Score':>5}")
        print(f"{'-'*4}  {'-'*14} {'-'*40} {'-'*25} {'-'*5}")

        for item in sorted(no_match, key=lambda x: -x["count"])[:50]:
            bm = item["best_match"][:25] if item["best_match"] else "-"
            print(f"  {item['count']:>4}  {item['vendor']:<14} {item['cleaned'][:40]:<40} {bm:<25} {item['best_score']:>5}")

        no_match_occurrences = sum(n["count"] for n in no_match)
        print(f"\n  {len(no_match)} items ({no_match_occurrences} occurrences) need manual mapping")

    # ── Summary ─────────────────────────────────────────────────────────
    all_occurrences = sum(desc_counts.values())
    print(f"\n{'='*80}")
    print(f"SUMMARY")
    print(f"{'='*80}")
    print(f"  Total unmapped (non-junk): {all_occurrences} occurrences, {len(desc_counts)} unique")
    print(f"  Auto-suggestable:          {total_occurrences} occurrences ({len(suggestions)} unique)")
    print(f"  Need manual mapping:       {sum(n['count'] for n in no_match)} occurrences ({len(no_match)} unique)")
    if all_occurrences > 0:
        pct = total_occurrences / all_occurrences * 100
        print(f"  Coverage if all accepted:  {pct:.1f}% of unmapped items resolved")

    # ── Audit mode ──────────────────────────────────────────────────────
    if args.audit:
        run_audit()
        return

    # ── Apply approved mappings ─────────────────────────────────────────
    if args.apply_approved:
        print(f"\nApplying approved mappings from '{REVIEW_TAB}' tab...")
        apply_approved()
        return

    # ── Write to review tab if requested ─────────────────────────────
    if args.write and suggestions:
        print(f"\nWriting {len(suggestions)} suggestions to '{REVIEW_TAB}' tab...")
        write_suggestions_to_review(suggestions)
    elif suggestions and not args.write:
        print(f"\nRun with --write to send these {len(suggestions)} suggestions to the '{REVIEW_TAB}' tab for review.")


if __name__ == "__main__":
    main()
