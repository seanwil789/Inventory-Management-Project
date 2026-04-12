"""
Synergy monthly sheet automation.

Capabilities:
  1. sync_prices_for_tab()    — pull invoice data for the month named in the tab,
                                fuzzy-find each product row, update Unit Price + Case Size
  2. sync_prices_from_items() — lower-level: update from an in-memory list of items
  3. find_new_items()         — return canonicals in invoices with no row in the tab
  4. insert_new_items()       — insert new items into the correct section
  5. create_month_sheet()     — duplicate the previous month's tab, clear prices/on-hand

Usage (standalone):
  python synergy_sync.py --sync-tab "Synergy Apr 2026"    # DB-driven price sync
  python synergy_sync.py --sync-tab "Synergy Apr 2026" --dry-run
  python synergy_sync.py --create-month 2026 4            # create April sheet
  python synergy_sync.py --find-new --tab "Synergy Apr 2026"

Typical batch.py integration (live invoice prices):
  from synergy_sync import sync_prices_from_items
  sync_prices_from_items(mapped_items, vendor=parsed["vendor"])
"""

import os
import sys
import re
import argparse
from calendar import month_name

sys.path.insert(0, os.path.dirname(__file__))

from rapidfuzz import process, fuzz, utils as fuzz_utils
from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID, ACTIVE_SHEET_TAB

# ── Column indices (1-indexed, matching Synergy sheet layout) ─────────────────
COL_SUB_CATEGORY = 1   # A
COL_PRODUCT      = 2   # B
COL_VENDOR       = 3   # C
COL_LOCATION     = 4   # D
COL_UNIT_PRICE   = 5   # E  — case price (what was paid for the full case)
COL_CASE_SIZE    = 6   # F
COL_UNIT         = 7   # G
COL_ON_HAND      = 8   # H
COL_IUP          = 9   # I  — Individual Unit Price (case price ÷ units per case)
COL_PRICE_PER_LB = 10  # J  — Price per pound

# Rows to skip when building the product index (header/divider row markers).
_SKIP_PRODUCTS = {"product", "sub category", ""}

# Fuzzy threshold for matching canonical names to Synergy product names.
SYNC_FUZZY_THRESHOLD = 78


# ── Case size parser ──────────────────────────────────────────────────────────

def parse_unit_count(case_size: str) -> int | None:
    """
    Extract the number of individual units per case from a case size string.

    Handles the two most common formats:
      "N/X..."  — N is the unit count  (e.g. "6/1GAL" → 6, "2/5LB" → 2)
      "NCT/CS"  — N is the unit count  (e.g. "24CT" → 24, "6CS" → 6)

    Returns None for single-measure strings like "12OZ" or "5LB" where there
    is no meaningful per-unit subdivision, and for anything unparseable.
    """
    if not case_size:
        return None

    s = case_size.strip().upper()

    # Format 1: N/X  — the number before the slash is always the unit count
    m = re.match(r'^(\d+)\s*/', s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None

    # Format 2: NCT, NCS, NEA, NPK, NBG  — N units of that type per case
    m = re.match(r'^(\d+)\s*(?:CT|CS|EA|PK|BG)$', s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None

    # Bare measure (e.g. "12OZ", "5LB", "1GAL") — single item, no subdivision
    return None


def calc_iup(unit_price: float, case_size: str) -> float | None:
    """
    Compute Individual Unit Price = unit_price / units_per_case.
    Returns None if the case size cannot be parsed or unit count is 1
    (no subdivision meaningful).
    """
    count = parse_unit_count(case_size)
    if count is None or count <= 1:
        return None
    return round(unit_price / count, 4)

# ── Category → Synergy section mapping ───────────────────────────────────────
# Maps (category, primary_descriptor) pairs to the section sub-category label
# that appears in col A of the Synergy sheet.  Values are lowercase for
# case-insensitive comparison against the sheet.
# Add entries here as new sections / categories appear in the sheet.
CATEGORY_TO_SECTION: dict[tuple[str, str], str] = {
    ("Proteins",  "Beef"):     "beef",
    ("Proteins",  "Poultry"):  "poultry",
    ("Proteins",  "Pork"):     "pork",
    ("Proteins",  "Seafood"):  "seafood",
    ("Produce",   "Leaf"):     "leaf/greens",
    ("Produce",   "Vegetable"):"vegetables",
    ("Produce",   "Fruit"):    "fruit",
    ("Produce",   "Herb"):     "herbs",
    ("Dairy",     ""):         "dairy",
    ("Dairy",     "Cheese"):   "cheese",
    ("Dairy",     "Egg"):      "eggs",
    ("Drystock",  ""):         "dry goods",
    ("Drystock",  "Spice"):    "spices",
    ("Drystock",  "Oil"):      "oils/vinegars",
    ("Drystock",  "Sauce"):    "sauces/condiments",
    ("Beverage",  ""):         "beverages",
    ("Bakery",    ""):         "bakery",
    ("Chemicals", ""):         "chemicals",
    ("Smallwares",""):         "smallwares",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Sheet index builder
# ─────────────────────────────────────────────────────────────────────────────

def build_sheet_index(sheet_tab: str) -> tuple[list[dict], list[dict]]:
    """
    Read a Synergy tab and return:
      products — list of dicts:
        { row: int (1-based), section: str, product: str, vendor: str }
      sections — ordered list of dicts:
        { name: str, start_row: int, end_row: int | None }

    Skips header rows (row 1) and blank / sub-category-only divider rows.
    Fills section names forward so every product row knows its section.
    """
    raw = get_sheet_values(SPREADSHEET_ID, f"'{sheet_tab}'!A:G")

    products: list[dict] = []
    sections: list[dict] = []
    current_section = ""

    for i, row in enumerate(raw, start=1):
        # Pad to at least 3 columns
        while len(row) < 3:
            row.append("")

        sub_cat = row[COL_SUB_CATEGORY - 1].strip()
        product = row[COL_PRODUCT  - 1].strip()
        vendor  = row[COL_VENDOR   - 1].strip()

        # Row 1 is always the column header row — skip
        if i == 1:
            continue

        # A non-empty col A value that isn't "Sub Category" starts a new section
        if sub_cat and sub_cat.lower() != "sub category":
            # Close previous section
            if sections:
                sections[-1]["end_row"] = i - 1
            current_section = sub_cat
            sections.append({"name": sub_cat, "start_row": i, "end_row": None})
            # This row may also have a product (some sections have the header and
            # first product on the same row)

        # Skip rows with no product name or known header text
        if not product or product.lower() in _SKIP_PRODUCTS:
            continue

        products.append({
            "row":     i,
            "section": current_section,
            "product": product,
            "vendor":  vendor,
        })

    # Close last section
    if sections:
        sections[-1]["end_row"] = len(raw)

    return products, sections


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Month parsing + DB loader
# ─────────────────────────────────────────────────────────────────────────────

# Abbreviated month names as they appear in Synergy tab names
_MONTH_ABBREVS = {mn[:3].lower(): i for i, mn in enumerate(month_name) if i > 0}


def parse_tab_month(tab_name: str) -> tuple[int, int]:
    """
    Extract (year, month) from a Synergy tab name.
    "Synergy Apr 2026" → (2026, 4)
    Raises ValueError if the name doesn't match the expected pattern.
    """
    m = re.match(r'Synergy\s+(\w{3,})\s+(\d{4})$', tab_name.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(
            f"Cannot parse month/year from tab name '{tab_name}'. "
            f"Expected format: 'Synergy Mon YYYY' (e.g. 'Synergy Apr 2026')"
        )
    month_abbrev = m.group(1)[:3].lower()
    month_num    = _MONTH_ABBREVS.get(month_abbrev)
    if month_num is None:
        raise ValueError(f"Unrecognised month abbreviation '{m.group(1)}' in tab '{tab_name}'")
    return int(m.group(2)), month_num


def load_items_for_month(year: int, month: int) -> list[dict]:
    """
    Query the database for invoice line items with invoice_date in year/month.

    Returns one dict per (canonical_name, vendor) pair — the most recent price
    seen in that month when a product was invoiced more than once.

    Format matches what sync_prices_from_items() expects:
      { canonical, vendor_name, unit_price, case_size_raw,
        category, primary_descriptor, secondary_descriptor }
    """
    _bootstrap_django()
    from myapp.models import InvoiceLineItem

    qs = (
        InvoiceLineItem.objects
        .filter(
            invoice_date__year=year,
            invoice_date__month=month,
            product__isnull=False,
            unit_price__isnull=False,
        )
        .select_related("product", "vendor")
        .order_by("-invoice_date", "-imported_at")   # latest first
    )

    # Deduplicate: keep the latest price per (canonical, vendor)
    seen: dict[tuple, dict] = {}
    for ili in qs:
        canonical   = ili.product.canonical_name
        vendor_name = ili.vendor.name if ili.vendor else ""
        key = (canonical, vendor_name)
        if key not in seen:
            seen[key] = {
                "canonical":             canonical,
                "vendor_name":           vendor_name,
                "unit_price":            float(ili.unit_price),
                "case_size_raw":         ili.case_size or "",
                "category":              ili.product.category,
                "primary_descriptor":    ili.product.primary_descriptor,
                "secondary_descriptor":  ili.product.secondary_descriptor,
            }

    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Price sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_prices_for_tab(sheet_tab: str = None, dry_run: bool = False) -> dict:
    """
    High-level entry point: derive the month from the tab name, load invoice
    data from the database for that month, and update Unit Price + Case Size
    for every matched row in the sheet.

    Returns the same summary dict as sync_prices_from_items().
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    year, month = parse_tab_month(tab)

    print(f"   Loading invoices from DB for {month_name[month]} {year}...")
    items = load_items_for_month(year, month)
    print(f"   {len(items)} distinct (product, vendor) price(s) found in DB")

    if not items:
        print(f"   [!] No invoice data for {month_name[month]} {year} — nothing to sync")
        return {"updated": 0, "skipped_no_price": 0, "skipped_no_match": 0, "failed": 0}

    return _sync_prices_core(items, sheet_tab=tab, dry_run=dry_run)


def sync_prices_from_items(
    mapped_items: list[dict],
    vendor: str = "",
    sheet_tab: str = None,
    dry_run: bool = False,
) -> dict:
    """
    Update prices from an in-memory list of mapped invoice items (e.g. from a
    live batch run).  Items must have 'canonical' and 'unit_price' keys.
    vendor is a single vendor name applied to all items when items lack a
    'vendor_name' key.
    """
    # Normalise to the internal format used by _sync_prices_core
    normalised = []
    for it in mapped_items:
        if not it.get("canonical") or it.get("unit_price") in (None, "", 0):
            continue
        normalised.append({
            "canonical":   it["canonical"],
            "vendor_name": it.get("vendor_name") or vendor,
            "unit_price":  it["unit_price"],
            "case_size_raw": it.get("case_size_raw", ""),
        })

    skipped_no_price = len(mapped_items) - len(normalised)
    result = _sync_prices_core(normalised, sheet_tab=sheet_tab, dry_run=dry_run)
    result["skipped_no_price"] += skipped_no_price
    return result


def _sync_prices_core(
    items: list[dict],
    sheet_tab: str = None,
    dry_run: bool = False,
) -> dict:
    """
    Shared engine used by both sync_prices_for_tab() and sync_prices_from_items().

    Each item dict must have: canonical, vendor_name, unit_price, case_size_raw.

    Matching strategy:
      1. Fuzzy-match canonical name against all product names (token_set_ratio).
      2. Among all rows whose product name matches well enough, prefer the row
         whose vendor column also matches the item's vendor (case-insensitive).
      3. Fall back to the best product-name match if no vendor row found.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    summary = {"updated": 0, "skipped_no_price": 0, "skipped_no_match": 0, "failed": 0}

    if not items:
        return summary

    print(f"   Loading Synergy sheet index from '{tab}'...")
    products, _ = build_sheet_index(tab)

    if not products:
        print(f"   [!] No products found in '{tab}' — skipping price sync")
        summary["skipped_no_match"] = len(items)
        return summary

    product_names = [p["product"] for p in products]
    client = get_sheets_client()
    batch_data = []

    for item in items:
        canonical   = item["canonical"]
        vendor_name = item.get("vendor_name", "")
        unit_price  = item.get("unit_price")
        case_size   = item.get("case_size_raw", "")

        # Step 1: find all sheet rows whose product name fuzzy-matches
        all_matches = process.extract(
            canonical,
            product_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
            limit=10,
        )
        good_matches = [
            (name, score) for name, score, _ in all_matches
            if score >= SYNC_FUZZY_THRESHOLD
            and fuzz.token_sort_ratio(canonical, name,
                                      processor=fuzz_utils.default_process) >= 45
        ]
        if not good_matches:
            summary["skipped_no_match"] += 1
            continue

        # Step 2: collect all target rows to write.
        #
        # Priority order:
        #   a) Exact name match — use it exclusively (prevents "Honey" from
        #      also writing to "Honey Dew" when a "Honey" row exists).
        #   b) Variant case — canonical has no exact match but IS a word-subset
        #      of multiple sheet names (e.g. "Gatorade" → "Gatorade Yellow" AND
        #      "Gatorade Orange" both score 100).  Write to ALL such rows.
        #   c) Normal case — single best match with vendor preference.
        target_rows: list[dict] = []

        canonical_lower = canonical.lower().strip()
        exact_names = [
            name for name, score in good_matches
            if name.lower().strip() == canonical_lower
        ]

        if exact_names:
            # (a) Exact match — only write to this row, ignore longer variants
            for name in exact_names:
                for row_entry in [p for p in products if p["product"] == name]:
                    target_rows.append(row_entry)
        else:
            variant_names = [name for name, score in good_matches if score == 100]

        if not target_rows and len(variant_names) > 1:
            # (b) Multiple 100-score subset matches — all are product variants
            for name in variant_names:
                for row_entry in [p for p in products if p["product"] == name]:
                    target_rows.append(row_entry)
        elif not target_rows:
            # Normal single-target path with vendor preference
            chosen_name, chosen_score = good_matches[0]
            if vendor_name:
                vendor_lower = vendor_name.lower()
                for name, score in good_matches:
                    candidate_rows = [p for p in products if p["product"] == name]
                    for row_entry in candidate_rows:
                        if row_entry["vendor"].lower() == vendor_lower:
                            chosen_name  = name
                            chosen_score = score
                            break
                    else:
                        continue
                    break

            matched_rows = [p for p in products if p["product"] == chosen_name]
            if not matched_rows:
                summary["skipped_no_match"] += 1
                continue

            row_entry = matched_rows[0]
            if vendor_name and len(matched_rows) > 1:
                vendor_lower = vendor_name.lower()
                for r in matched_rows:
                    if r["vendor"].lower() == vendor_lower:
                        row_entry = r
                        break
            target_rows.append(row_entry)

        if not target_rows:
            summary["skipped_no_match"] += 1
            continue

        for row_entry in target_rows:
            row_num    = row_entry["row"]
            vendor_col = row_entry["vendor"]

            if dry_run:
                print(f"   [DRY RUN] row {row_num}: '{canonical}' → '{row_entry['product']}'"
                      f" [{vendor_col}] @ ${unit_price:.2f}")
                summary["updated"] += 1
                continue

            iup = calc_iup(unit_price, case_size)

            batch_data.append({
                "range": f"'{tab}'!E{row_num}",
                "values": [[unit_price]],
            })
            if case_size:
                batch_data.append({
                    "range": f"'{tab}'!F{row_num}",
                    "values": [[case_size]],
                })
            if iup is not None:
                batch_data.append({
                    "range": f"'{tab}'!I{row_num}",
                    "values": [[iup]],
                })

            iup_str = f"  IUP=${iup:.4f}" if iup is not None else ""
            print(f"   [✓] row {row_num}: '{canonical}' → '{row_entry['product']}'"
                  f" [{vendor_col}] @ ${unit_price:.2f}"
                  + (f" [{case_size}]" if case_size else "")
                  + iup_str)
            summary["updated"] += 1

    # Flush all writes in one API call
    if batch_data:
        try:
            client.values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": batch_data},
            ).execute()
        except Exception as e:
            print(f"   [!] Batch price update failed: {e}")
            summary["failed"] = summary["updated"]
            summary["updated"] = 0

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# 3.  New-item detection
# ─────────────────────────────────────────────────────────────────────────────

def find_new_items(
    mapped_items: list[dict],
    sheet_tab: str = None,
) -> list[dict]:
    """
    Return items whose canonical name has no fuzzy match in the Synergy tab.
    Each returned item is the original mapped_item dict, unchanged.

    Useful for generating a report of items that need to be added to the sheet.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    products, _ = build_sheet_index(tab)
    product_names = [p["product"] for p in products]

    new_items = []
    seen_canonicals: set[str] = set()

    for item in mapped_items:
        canonical = item.get("canonical")
        if not canonical or canonical in seen_canonicals:
            continue
        seen_canonicals.add(canonical)

        result = process.extractOne(
            canonical,
            product_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
        )
        if result is None or result[1] < SYNC_FUZZY_THRESHOLD:
            new_items.append(item)
        else:
            # Apply same secondary sort filter used in sync_prices
            sort_score = fuzz.token_sort_ratio(
                canonical, result[0], processor=fuzz_utils.default_process
            )
            if sort_score < 45:
                new_items.append(item)

    return new_items


# ─────────────────────────────────────────────────────────────────────────────
# 4.  New-item insertion
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_target_section(item: dict, sections: list[dict]) -> dict | None:
    """
    Map an item's category/primary_descriptor to a Synergy section.
    Falls back to fuzzy-matching the category name against section names.
    Returns the matching section dict or None.
    """
    category   = item.get("category", "")
    primary    = item.get("primary_descriptor", "")

    # Try exact mapping first
    key = (category, primary)
    target_section_name = CATEGORY_TO_SECTION.get(key)
    if target_section_name is None:
        target_section_name = CATEGORY_TO_SECTION.get((category, ""))

    if target_section_name:
        for sec in sections:
            if sec["name"].lower() == target_section_name:
                return sec
        # Fuzzy fallback against actual section names
        section_names = [s["name"] for s in sections]
        result = process.extractOne(
            target_section_name,
            section_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
        )
        if result and result[1] >= 70:
            return next(s for s in sections if s["name"] == result[0])

    # Last resort: fuzzy match category directly against section names
    if category:
        section_names = [s["name"] for s in sections]
        result = process.extractOne(
            category,
            section_names,
            scorer=fuzz.token_set_ratio,
            processor=fuzz_utils.default_process,
        )
        if result and result[1] >= 60:
            return next(s for s in sections if s["name"] == result[0])

    return None


def insert_new_items(
    new_items: list[dict],
    vendor: str = "",
    sheet_tab: str = None,
    dry_run: bool = False,
) -> dict:
    """
    Insert new items into the correct section of the Synergy tab.
    Each new item is appended at the end of its section (before the blank
    divider row that separates sections).

    Returns { inserted: int, skipped_no_section: int }.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    _, sections = build_sheet_index(tab)
    summary = {"inserted": 0, "skipped_no_section": 0}

    if not sections:
        print(f"   [!] No sections found in '{tab}' — cannot insert new items")
        summary["skipped_no_section"] = len(new_items)
        return summary

    client = get_sheets_client()

    # Process insertions in reverse row order so that earlier row numbers
    # remain valid after each insert.
    # First pass: determine target row for each item.
    insertions: list[tuple[int, list]] = []

    for item in new_items:
        canonical  = item.get("canonical") or item.get("raw_description", "")
        unit_price = item.get("unit_price", "")
        case_size  = item.get("case_size_raw", "")

        section = _resolve_target_section(item, sections)
        if section is None:
            print(f"   [!] No section found for '{canonical}' "
                  f"(category={item.get('category','')}) — skipping")
            summary["skipped_no_section"] += 1
            continue

        # Insert after the last product row of the section
        insert_row = (section["end_row"] or section["start_row"]) + 1
        new_row_values = [
            "",          # A: Sub Category (blank for non-header rows)
            canonical,   # B: Product
            vendor,      # C: Vendor
            "",          # D: Location
            unit_price,  # E: Unit Price
            case_size,   # F: Case Size
            "",          # G: Unit
            "",          # H: On Hand
            "",          # I: IUP
            "",          # J: P/#
        ]
        insertions.append((insert_row, new_row_values))

        if dry_run:
            print(f"   [DRY RUN] Would insert '{canonical}' into section "
                  f"'{section['name']}' at row {insert_row}")

    if dry_run:
        summary["inserted"] = len(insertions)
        return summary

    # Sort descending by row so later rows are inserted first (preserves indices)
    insertions.sort(key=lambda x: x[0], reverse=True)

    for insert_row, row_values in insertions:
        try:
            # Insert a blank row, then write values into it
            _insert_row_in_sheet(client, tab, insert_row)
            range_addr = f"'{tab}'!A{insert_row}:J{insert_row}"
            client.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=range_addr,
                valueInputOption="USER_ENTERED",
                body={"values": [row_values]},
            ).execute()
            print(f"   [✓] Inserted '{row_values[1]}' into sheet at row {insert_row}")
            summary["inserted"] += 1
        except Exception as e:
            print(f"   [!] Failed to insert '{row_values[1]}': {e}")

    return summary


def _insert_row_in_sheet(client, sheet_tab: str, row_index: int):
    """
    Insert a blank row BEFORE row_index (1-based) by sending a batchUpdate
    insertDimension request.  Requires the numeric sheetId.
    """
    # Look up the sheetId by tab name
    meta = client.get(spreadsheetId=SPREADSHEET_ID,
                      fields="sheets(properties(sheetId,title))").execute()
    sheet_id = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet_tab:
            sheet_id = s["properties"]["sheetId"]
            break
    if sheet_id is None:
        raise ValueError(f"Tab '{sheet_tab}' not found in spreadsheet")

    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId":    sheet_id,
                        "dimension":  "ROWS",
                        "startIndex": row_index - 1,  # 0-based
                        "endIndex":   row_index,
                    },
                    "inheritFromBefore": True,
                }
            }]
        },
    ).execute()


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Monthly sheet creation
# ─────────────────────────────────────────────────────────────────────────────

def _get_sheet_id(client, tab_name: str) -> int | None:
    meta = client.get(spreadsheetId=SPREADSHEET_ID,
                      fields="sheets(properties(sheetId,title))").execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    return None


def _list_synergy_tabs(client) -> list[str]:
    """Return tab titles that match 'Synergy [Month] [Year]'."""
    meta = client.get(spreadsheetId=SPREADSHEET_ID,
                      fields="sheets(properties(title))").execute()
    pattern = re.compile(r'^Synergy\s+\w+\s+\d{4}$', re.IGNORECASE)
    return [
        s["properties"]["title"]
        for s in meta.get("sheets", [])
        if pattern.match(s["properties"]["title"])
    ]


def create_month_sheet(year: int, month: int, dry_run: bool = False) -> str:
    """
    Create a new Synergy tab for the given year/month by duplicating the most
    recent existing Synergy tab, then clearing Unit Price (col E) and
    On Hand (col H).

    Returns the new tab name, e.g. "Synergy Apr 2026".
    Raises ValueError if the tab already exists.
    """
    new_tab_name = f"Synergy {month_name[month][:3]} {year}"
    client = get_sheets_client()

    # Check the tab doesn't already exist
    existing_tabs = _list_synergy_tabs(client)
    if new_tab_name in existing_tabs:
        raise ValueError(f"Tab '{new_tab_name}' already exists")

    if dry_run:
        print(f"   [DRY RUN] Would create tab '{new_tab_name}' from most recent Synergy tab")
        return new_tab_name

    # Find the most recent Synergy tab to copy from
    if not existing_tabs:
        raise ValueError("No existing Synergy tabs found to copy from")

    # Sort by parsing month/year from title; fall back to lexicographic order
    def _tab_sort_key(title: str):
        m = re.match(r'Synergy\s+(\w+)\s+(\d{4})$', title, re.IGNORECASE)
        if not m:
            return (0, 0)
        try:
            month_num = list(month_name).index(
                next(mn for mn in month_name if mn.lower().startswith(m.group(1).lower()))
            )
            return (int(m.group(2)), month_num)
        except (StopIteration, ValueError):
            return (0, 0)

    source_tab = sorted(existing_tabs, key=_tab_sort_key)[-1]
    source_id  = _get_sheet_id(client, source_tab)
    if source_id is None:
        raise ValueError(f"Cannot find sheet ID for '{source_tab}'")

    print(f"   Copying '{source_tab}' → '{new_tab_name}'...")

    # Duplicate the sheet
    resp = client.sheets().copyTo(
        spreadsheetId=SPREADSHEET_ID,
        sheetId=source_id,
        body={"destinationSpreadsheetId": SPREADSHEET_ID},
    ).execute()

    new_sheet_id = resp["sheetId"]

    # Rename it
    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [{
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": new_sheet_id,
                        "title":   new_tab_name,
                    },
                    "fields": "title",
                }
            }]
        },
    ).execute()

    # Clear Unit Price (E), IUP (I), and On Hand (H) — skip row 1 (header)
    print(f"   Clearing prices, IUP, and on-hand quantities...")
    client.values().batchClear(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "ranges": [
                f"'{new_tab_name}'!E2:E",
                f"'{new_tab_name}'!H2:H",
                f"'{new_tab_name}'!I2:I",
            ]
        },
    ).execute()

    print(f"   [✓] Created '{new_tab_name}'")
    return new_tab_name


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Synergy monthly sheet automation"
    )
    parser.add_argument("--sync-tab", metavar="TAB",
                        help="Sync prices from DB into this tab using invoice data "
                             "for the month named in the tab title "
                             "(e.g. 'Synergy Apr 2026')")
    parser.add_argument("--tab", default=None,
                        help="Synergy tab name for --find-new (default: ACTIVE_SHEET_TAB)")
    parser.add_argument("--create-month", nargs=2, metavar=("YEAR", "MONTH"),
                        help="Create a new Synergy tab for YEAR MONTH (e.g. 2026 4)")
    parser.add_argument("--find-new", action="store_true",
                        help="List canonical names that have no row in the tab")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing anything")
    args = parser.parse_args()

    if args.sync_tab:
        _bootstrap_django()
        tab = args.sync_tab
        try:
            year, month = parse_tab_month(tab)
        except ValueError as e:
            print(f"[!] {e}")
            return
        print(f"\nSyncing prices for '{tab}' ({month_name[month]} {year})...")
        summary = sync_prices_for_tab(sheet_tab=tab, dry_run=args.dry_run)
        print(f"\nDone — Updated: {summary['updated']}  |  "
              f"No sheet match: {summary['skipped_no_match']}  |  "
              f"No price in DB: {summary['skipped_no_price']}"
              + (f"  |  Failed: {summary['failed']}" if summary['failed'] else ""))
        return

    if args.create_month:
        year, month = int(args.create_month[0]), int(args.create_month[1])
        new_tab = create_month_sheet(year, month, dry_run=args.dry_run)
        print(f"New tab: '{new_tab}'")
        return

    tab = args.tab or ACTIVE_SHEET_TAB

    if args.find_new:
        _bootstrap_django()
        items = load_items_for_month(*parse_tab_month(tab))
        new = find_new_items(items, sheet_tab=tab)
        if new:
            print(f"\n{len(new)} canonical(s) not found in '{tab}':")
            for it in new:
                print(f"  {it['canonical']:<35} [{it.get('category','')} / "
                      f"{it.get('primary_descriptor','')}]")
        else:
            print(f"All canonicals already present in '{tab}'.")
        return

    parser.print_help()


def _bootstrap_django():
    if not os.environ.get('DJANGO_SETTINGS_MODULE'):
        os.environ['DJANGO_SETTINGS_MODULE'] = 'myproject.settings'
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        import django
        django.setup()


if __name__ == "__main__":
    main()
