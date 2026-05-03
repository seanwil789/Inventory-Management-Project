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
# Phase 4 layout (Sean 2026-05-03): inserted COUNT_FLAG between UNIT and ON_HAND.
# F = case_pack_count (number of items in case)
# G = units of case size (e.g., "5.3 oz Container", "1 Gal")
# H = "Ea" (count) or "#" (weigh) — counter-time flag
# I = On Hand   J = IUP   K = P/#  (all shifted right by one column from prior layout)
COL_SUB_CATEGORY = 1   # A
COL_PRODUCT      = 2   # B
COL_VENDOR       = 3   # C
COL_LOCATION     = 4   # D
COL_UNIT_PRICE   = 5   # E  — case price (what was paid for the full case)
COL_CASE_SIZE    = 6   # F  — case_pack_count (count, not raw case_size string)
COL_UNIT         = 7   # G  — units of case size (per-item description)
COL_COUNT_FLAG   = 8   # H  — "Ea" or "#"
COL_ON_HAND      = 9   # I
COL_IUP          = 10  # J  — Individual Unit Price (case price ÷ units per case)
COL_PRICE_PER_LB = 11  # K  — Price per pound

# Rows to skip when building the product index (header/divider row markers).
_SKIP_PRODUCTS = {"product", "sub category", ""}

# Fuzzy threshold for matching canonical names to Synergy product names.
SYNC_FUZZY_THRESHOLD = 90


# ── Case size parser ──────────────────────────────────────────────────────────

def _looks_like_date(s: str) -> bool:
    """Return True if the case size string is actually a date, not a real size."""
    # "4/11/2026", "3/15/22" — M/D/Y with 3 components is always a date
    if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', s):
        return True
    # M/YYYY — e.g. "3/2022", "3/2026"
    if re.match(r'^\d{1,2}/\d{4}$', s):
        return True
    # Leading-zero month: "09/05", "08/03" — real case sizes never zero-pad
    if re.match(r'^0\d/', s):
        return True
    # MM/DD where the second number is > 12 (can't be a sub-unit size)
    m = re.match(r'^(\d{1,2})/(\d{1,2})$', s)
    if m:
        first, second = int(m.group(1)), int(m.group(2))
        if first <= 12 and second > 12:
            return True
    return False


def parse_unit_count(case_size: str) -> int | None:
    """
    Extract the number of individual units per case from a case size string.

    Handles formats:
      "N/X..."  — N is the unit count  (e.g. "6/1GAL" → 6, "4/1" → 4, "12/2" → 12)
      "NCT/CS"  — N is the unit count  (e.g. "24CT" → 24, "160CT" → 160)
      "N"       — bare number (e.g. "20", "80") — the case quantity itself

    Returns None for values that look like dates, product specs, or unparseable.
    """
    if not case_size:
        return None

    s = case_size.strip().upper()

    if _looks_like_date(s):
        return None

    # Format 1: N/M or N/MUNIT — the number before the slash is the unit count
    m = re.match(r'^(\d+)\s*/\s*(\d+\.?\d*)\s*(?:LB|OZ|GAL|KG|CT|LTR)?\s*$', s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None

    # Format 2: NCT, NCS, NEA, NPK, NBG — N units of that type per case
    m = re.match(r'^(\d+)\s*(?:CT|CS|EA|PK|BG)$', s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None

    # Format 3: bare number — the total count or weight per case
    m = re.match(r'^(\d+)$', s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None

    # Bare measure with unit (e.g. "12OZ", "5LB", "1GAL") — single item
    return None


def parse_total_weight_lbs(case_size: str) -> float | None:
    """
    Extract total weight in pounds from a case size string.

    Handles:
      "N/MLB"  → N × M lbs  (e.g. "2/5LB" → 10, "4/1LB" → 4)
      "N/MOZ"  → N × M oz / 16  (e.g. "6/32OZ" → 12)
      "NLB"    → N lbs  (e.g. "50LB" → 50)
      "N"      → N lbs when combined with unit="#" (caller decides)

    Returns None if weight cannot be determined.
    """
    if not case_size:
        return None

    s = case_size.strip().upper()

    if _looks_like_date(s):
        return None

    # N/M LB — e.g. "2/5LB" → 2 × 5 = 10 lbs
    m = re.match(r'^(\d+)\s*/\s*(\d+\.?\d*)\s*LB\s*$', s)
    if m:
        count = int(m.group(1))
        per_unit = float(m.group(2))
        return count * per_unit if count > 0 else None

    # N/M OZ — e.g. "6/32OZ" → 6 × 32 / 16 = 12 lbs
    m = re.match(r'^(\d+)\s*/\s*(\d+\.?\d*)\s*OZ\s*$', s)
    if m:
        count = int(m.group(1))
        per_unit_oz = float(m.group(2))
        return round(count * per_unit_oz / 16, 4) if count > 0 else None

    # N/M KG — e.g. "2/5KG" → 2 × 5 × 2.205 lbs
    m = re.match(r'^(\d+)\s*/\s*(\d+\.?\d*)\s*KG\s*$', s)
    if m:
        count = int(m.group(1))
        per_unit_kg = float(m.group(2))
        return round(count * per_unit_kg * 2.205, 4) if count > 0 else None

    # NLB — e.g. "50LB" → 50 lbs
    m = re.match(r'^(\d+\.?\d*)\s*LB\s*$', s)
    if m:
        return float(m.group(1))

    # NOZ — e.g. "12OZ" → 0.75 lbs
    m = re.match(r'^(\d+\.?\d*)\s*OZ\s*$', s)
    if m:
        return round(float(m.group(1)) / 16, 4)

    return None


def calc_iup(unit_price: float, case_size: str,
             case_pack_count: int | None = None,
             purchase_uom: str | None = None) -> float | None:
    """
    Compute Individual Unit Price = price per individual unit (per-jar,
    per-bottle, per-piece — the smallest unit Sean tracks at month-end).

    Phase 3b (Sean 2026-05-03): the `purchase_uom` signal gates whether
    unit_price is case-level or per-unit:
      - purchase_uom in ('CASE', 'CS') → unit_price is CASE-level.
        IUP = unit_price / case_pack_count.
        Examples: Sysco yogurt $36 case / 12 cups = $3/cup;
                  Farm Art milk $46.73 case / 4 gal = $11.68/gal.
      - purchase_uom in ('LB', 'EACH', 'EA', 'GAL', 'QT', 'PT', 'DZ',
        'DOZ', 'BU', 'OZ', etc.) → unit_price is ALREADY per-unit.
        IUP = unit_price (no division).
        Examples: Farm Art shallot $20/gal (purchase_uom=GAL);
                  Farm Art bunches $1.98/case-of-60-bunches but
                  invoiced per-bunch when purchase_uom=EACH.

    When purchase_uom is missing/unknown:
      - Falls back to legacy behavior (divide by case_pack_count if > 1)
        for backward compat. This produced wrong IUP for per-unit-priced
        items where U/M signal was missing — once spatial U/M extraction
        reaches high coverage, the fallback should rarely fire.

    Returns None when count cannot be determined.
    """
    uom = (purchase_uom or '').upper().strip()

    # Weighed U/M (LB / KG / OZ / #): IUP isn't meaningful — these items
    # don't have a fixed "individual unit." Their per-unit price is per-lb
    # (P/# column), not per-piece. Return None so col I stays empty.
    _WEIGHED_UOMS = {'LB', 'LBS', '#', 'KG', 'OZ'}
    if uom in _WEIGHED_UOMS:
        return None

    # Per-unit countable U/M: unit_price IS the individual-unit price.
    # No division needed (Farm Art shallot $20/gal, cilantro $1/bunch).
    _PER_UNIT_UOMS = {'EACH', 'EA',
                      'GAL', 'GALLON', 'QT', 'QUART', 'PT', 'PINT',
                      'DZ', 'DOZ', 'DOZEN', 'BU', 'BUNCH'}
    if uom in _PER_UNIT_UOMS:
        return round(unit_price, 4)

    # Case-level U/M: divide by case_pack_count for per-unit price.
    _CASE_UOMS = {'CASE', 'CS', 'CTN', 'PK', 'BG'}
    if uom in _CASE_UOMS:
        if case_pack_count is not None and case_pack_count > 1:
            return round(unit_price / case_pack_count, 4)
        # Case U/M but no pack count — fall through to legacy parse
        count = parse_unit_count(case_size)
        if count is not None and count > 1:
            return round(unit_price / count, 4)
        return None

    # No purchase_uom signal — legacy behavior (assume case-level).
    # This produces wrong IUP for per-unit-priced items where U/M is
    # missing; closing the U/M coverage gap is the real fix.
    if case_pack_count is not None and case_pack_count > 1:
        return round(unit_price / case_pack_count, 4)
    count = parse_unit_count(case_size)
    if count is None or count <= 1:
        return None
    return round(unit_price / count, 4)


def calc_price_per_lb(unit_price: float, case_size: str,
                      unit_col: str = "",
                      stored_price_per_lb=None,
                      case_total_weight_lb=None) -> float | None:
    """
    Compute Price per Pound = unit_price / total_weight_in_lbs.

    Priority order (Phase 3a, 2026-05-02):
      1. `stored_price_per_lb` — parser's direct $/lb computation on
         Sysco catch-weight / Exceptional rows. Most authoritative.
      2. `case_total_weight_lb` — structured field set by parser/spatial
         + persisted on ILI. unit_price / case_total_weight_lb gives
         the canonical $/lb for any weighed product without re-parsing
         strings. Closes the Beef Chuck Flap cascade — when parser
         emits case_total_weight_lb=42.7, calc_price_per_lb returns
         469.31 / 42.7 = $10.99/lb (correct), NOT 469.31 / 17.99
         (Product.default_case_size, the wrong fallback).
      3. Legacy `parse_total_weight_lbs(case_size)` string parsing.
      4. Bare-number `unit_col='#'` fallback (per-Sean inventory
         convention for weighed items in the Synergy sheet).

    Returns None if weight cannot be determined or item isn't sold by weight.
    """
    # Parser's direct value — when present, it's authoritative.
    if stored_price_per_lb is not None:
        try:
            val = float(stored_price_per_lb)
            if val > 0:
                return round(val, 4)
        except (TypeError, ValueError):
            pass

    # Structured weight path — Phase 3a unlock.
    if case_total_weight_lb is not None:
        try:
            tw = float(case_total_weight_lb)
            if tw > 0:
                return round(unit_price / tw, 4)
        except (TypeError, ValueError):
            pass

    # Legacy string path
    weight = parse_total_weight_lbs(case_size)
    if weight and weight > 0:
        return round(unit_price / weight, 4)

    # If case size is a bare number and unit is "#" (pounds),
    # the bare number IS the total weight
    if unit_col.strip() == "#" and case_size:
        s = case_size.strip()
        if _looks_like_date(s):
            return None
        # Bare number — only trust as lbs when >= 2.
        # Bare '1' with unit='#' produced J = case_price ÷ 1 = case_price, the
        # misleading E==J symptom on the Synergy sheet (Yellow Onion $32.50 /
        # $32.50, Pork Butt $40.50 / $40.50, etc.). No real product is sold as
        # a 1-lb case marked with weight unit; '1' here is almost always
        # "1 case" from OCR, not "1 pound". Same for fractional values < 2.
        m = re.match(r'^(\d+\.?\d*)$', s)
        if m:
            lbs = float(m.group(1))
            if lbs >= 2:
                return round(unit_price / lbs, 4)
        # N/M with unit=# → N × M total pounds.
        # Same threshold as bare numbers: total weight must be >= 2 lbs.
        # Catches '1/1' (Prosciutto) where 1×1 = 1 lb total reproduces the
        # E==J symptom via N/M path instead of bare-number path.
        m = re.match(r'^(\d+)\s*/\s*(\d+\.?\d*)$', s)
        if m:
            n, per = int(m.group(1)), float(m.group(2))
            total = n * per
            if total >= 2:
                return round(unit_price / total, 4)

    return None

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
    # Produce — botanical-family taxonomy (locked 2026-04-30; vegetables-before-fruits)
    ("Produce",   "Herb"):           "Herb",
    ("Produce",   "Allium"):         "Allium",
    ("Produce",   "Capsicum"):       "Capsicum",
    ("Produce",   "Solanaceae"):     "Solanaceae",
    ("Produce",   "Cucurbit"):       "Cucurbit",
    ("Produce",   "Brassica"):       "Brassica",
    ("Produce",   "Leaf/Greens"):    "Leaf/Greens",
    ("Produce",   "Legume"):         "Legume",
    ("Produce",   "Lily"):           "Lily",
    ("Produce",   "Polygonaceae"):   "Polygonaceae",
    ("Produce",   "Root"):           "Root",
    ("Produce",   "Tuber"):          "Tuber",
    ("Produce",   "Rhizome"):        "Rhizome",
    ("Produce",   "Corn"):           "Corn",
    ("Produce",   "Fungus"):         "Fungus",
    ("Produce",   "Lauraceae"):      "Lauraceae",
    ("Produce",   "Berry"):          "Berry",
    ("Produce",   "Citrus"):         "Citrus",
    ("Produce",   "Drupe"):          "Drupe",
    ("Produce",   "Melon"):          "Melon",
    ("Produce",   "Pome"):           "Pome",
    ("Produce",   "Vitaceae"):       "Vitaceae",
    ("Produce",   "Musa"):           "Musa",
    ("Produce",   "Bromeliaceae"):   "Bromeliaceae",
    # Unified Dairy 12-tier processing chain (locked 2026-04-30)
    ("Dairy",     ""):                       "dairy",
    ("Dairy",     "Milk"):                   "Milk",
    ("Dairy",     "Cream"):                  "Cream",
    ("Dairy",     "Yogurt"):                 "Yogurt",
    ("Dairy",     "Butter"):                 "Butter",
    ("Dairy",     "Cheese, Fresh"):          "Cheese, Fresh",
    ("Dairy",     "Cheese, Soft-Ripened"):   "Cheese, Soft-Ripened",
    ("Dairy",     "Cheese, Semi-Soft"):      "Cheese, Semi-Soft",
    ("Dairy",     "Cheese, Semi-Hard"):      "Cheese, Semi-Hard",
    ("Dairy",     "Cheese, Hard"):           "Cheese, Hard",
    ("Dairy",     "Cheese, Processed"):      "Cheese, Processed",
    ("Dairy",     "Processed"):              "Processed",
    ("Dairy",     "Frozen"):                 "Frozen",
    # Eggs moved out of Dairy → Proteins/Poultry/Egg (per protein remap, ships separately)
    # Drystock cooking-stage flow (locked 2026-05-01)
    ("Drystock",  ""):                       "dry goods",
    ("Drystock",  "Grains/Legumes"):         "Grains/Legumes",
    ("Drystock",  "Pastas"):                 "Pastas",
    ("Drystock",  "Flours and Starches"):    "Flours and Starches",
    ("Drystock",  "Oils"):                   "Oils",
    ("Drystock",  "Vinegars"):               "Vinegars",
    ("Drystock",  "Condiments"):             "Condiments",
    ("Drystock",  "Sauces"):                 "Sauces",
    ("Drystock",  "Canned Vegetables"):      "Canned Vegetables",
    ("Drystock",  "Sugars/Sweeteners"):      "Sugars/Sweeteners",
    ("Drystock",  "Baking"):                 "Baking",
    ("Drystock",  "Leaveners"):              "Leaveners",
    ("Drystock",  "PreFabs"):                "PreFabs",
    # Spices (own top-level category, cooking-stage flow)
    ("Spices",    "Salt"):                   "Salt",
    ("Spices",    "Pepper"):                 "Pepper",
    ("Spices",    "Heat"):                   "Heat",
    ("Spices",    "Aromatic"):               "Aromatic",
    ("Spices",    "Earthy"):                 "Earthy",
    ("Spices",    "Allium"):                 "Allium",
    ("Spices",    "Dried Herbs"):            "Dried Herbs",
    ("Spices",    "Blends"):                 "Blends",
    ("Spices",    "Seeds"):                  "Seeds",
    ("Beverage",  ""):         "beverages",
    ("Bakery",    ""):         "bakery",
    # Chemicals task-based tiers (locked 2026-05-01)
    ("Chemicals", ""):                "chemicals",
    ("Chemicals", "Dish"):            "Dish",
    ("Chemicals", "Floor"):           "Floor",
    ("Chemicals", "Equipment"):       "Equipment",
    ("Chemicals", "Bathroom"):        "Bathroom",
    ("Chemicals", "General"):         "General",
    # Smallwares cost-behavior tiers (locked 2026-05-01)
    ("Smallwares",""):                       "smallwares",
    ("Smallwares","Paper Consumables"):      "Paper Consumables",
    ("Smallwares","Plastic Consumables"):    "Plastic Consumables",
    ("Smallwares","Durable Smallwares"):     "Durable Smallwares",
}


# ─────────────────────────────────────────────────────────────────────────────
# F/G/H column derivation (Sean 2026-05-03)
# ─────────────────────────────────────────────────────────────────────────────
# Sheet layout post-migration:
#   F = case size count        (case_pack_count, e.g., 12 for "12/5.3OZ")
#   G = units of case size     (e.g., "5.3 oz Container", "1 Gal", "")
#   H = count-or-weigh flag    ("Ea" for counted_*, "#" for weighed)
# Counter at inventory time reads H to know whether to count or weigh.

def derive_f_count(item: dict) -> str | int:
    """F column = number of items per case (case_pack_count)."""
    cnt = item.get("case_pack_count")
    return cnt if cnt is not None else ""


def derive_g_units(item: dict) -> str:
    """G column = descriptive units of one item in the case.
    Blank for weighed products (no per-case unit; H='#' carries the signal).
    """
    if item.get("inventory_class") == "weighed":
        return ""
    return item.get("inventory_unit_descriptor") or ""


def derive_h_flag(item: dict) -> str:
    """H column = 'Ea' (count) or '#' (weigh). Drives counter behavior."""
    klass = item.get("inventory_class") or ""
    if klass == "weighed":
        return "#"
    if klass.startswith("counted_"):
        return "Ea"
    return ""


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
    # Read through col H to capture new COUNT_FLAG column (Phase 4 layout).
    raw = get_sheet_values(SPREADSHEET_ID, f"'{sheet_tab}'!A:H")

    products: list[dict] = []
    sections: list[dict] = []
    current_section = ""

    for i, row in enumerate(raw, start=1):
        # Pad to at least 8 columns (A..H)
        while len(row) < 8:
            row.append("")

        sub_cat   = row[COL_SUB_CATEGORY - 1].strip()
        product   = row[COL_PRODUCT  - 1].strip()
        vendor    = row[COL_VENDOR   - 1].strip()
        case_size = row[COL_CASE_SIZE - 1].strip() if len(row) >= COL_CASE_SIZE else ""
        unit      = row[COL_UNIT - 1].strip() if len(row) >= COL_UNIT else ""
        count_flag = row[COL_COUNT_FLAG - 1].strip() if len(row) >= COL_COUNT_FLAG else ""

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
            "case_size": case_size,
            "unit":    unit,
            "count_flag": count_flag,
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
        # Phase 3c (2026-05-02): -extended_amount tiebreaker per the bug
        # register umbrella entry's "Three new variants" #3. Butter row 182
        # had two same-date ILIs ($1.40 fragment vs $97.39 case total); the
        # default -invoice_date / -imported_at order picked the fragment
        # (whichever was inserted last). Sorting by -extended_amount within
        # date pushes the real case-price row to the top, so first-wins
        # dedup picks the right one. Backward compat for non-conflict cases:
        # only tiebreaks when multiple rows share (canonical, vendor, date),
        # which is uncommon outside the path-divergence dup case.
        .order_by("-invoice_date", "-extended_amount", "-unit_price",
                   "-imported_at")
    )

    # Deduplicate: keep the latest price per (canonical, vendor)
    seen: dict[tuple, dict] = {}
    for ili in qs:
        canonical   = ili.product.canonical_name
        vendor_name = ili.vendor.name if ili.vendor else ""
        key = (canonical, vendor_name)
        if key not in seen:
            # Sean 2026-05-03: per-unit price = extended_amount / quantity.
            # This is the ACTUAL paid per-unit price (accounts for vendor
            # discounts, billing adjustments, etc.). Falls back to
            # ili.unit_price (U/P column) only when quantity is missing or
            # zero — that path mostly applies to Sysco/Exceptional rows
            # which always invoice as qty=1.
            #
            # Why prefer ext/qty over U/P column: Farm Art's U/P column is
            # the catalog list price; line amount has a consistent ~1%
            # vendor discount applied. ext/qty captures what Sean actually
            # paid. For costing accuracy (recipe COGs, IUP, P/#), actual
            # paid > catalog list.
            ext = float(ili.extended_amount) if ili.extended_amount else 0
            qty = float(ili.quantity) if ili.quantity and ili.quantity > 0 else 0
            up_list = float(ili.unit_price) if ili.unit_price else 0
            if qty > 0 and ext > 0:
                per_unit_price = round(ext / qty, 4)
            else:
                per_unit_price = up_list  # fallback for qty-missing rows
            line_total = ext if ext > 0 else per_unit_price
            seen[key] = {
                "canonical":             canonical,
                "vendor_name":           vendor_name,
                # 'unit_price' here = per-unit price (drives calc_iup).
                # Sheet col E "case price" = line_total (separate field below).
                "unit_price":            per_unit_price,
                "line_total":            line_total,
                "case_size_raw":         ili.case_size or "",
                "category":              ili.product.category,
                "primary_descriptor":    ili.product.primary_descriptor,
                "secondary_descriptor":  ili.product.secondary_descriptor,
                # Parser's direct $/lb — used by calc_price_per_lb in
                # preference over reverse-engineering.
                "price_per_pound":       (float(ili.price_per_pound)
                                          if ili.price_per_pound is not None
                                          else None),
                # Phase 3a structured fields — calc_iup_v2 + calc_price_per_lb_v2
                # read these directly when populated, bypassing case_size string parse.
                "case_pack_count":       ili.case_pack_count,
                "case_total_weight_lb":  (float(ili.case_total_weight_lb)
                                          if ili.case_total_weight_lb is not None
                                          else None),
                # Phase 3b (Sean 2026-05-03): purchase_uom gates calc_iup
                # case-level vs per-unit interpretation. CASE → divide by
                # case_pack_count; LB/EACH/GAL/QT/etc. → unit_price already
                # per-unit (no division).
                "purchase_uom":          ili.purchase_uom or "",
                # Phase 4 (Sean 2026-05-03): F+G+H sheet layout. F=count,
                # G=units of case size, H=Ea/# count-or-weigh flag.
                "inventory_class":            ili.product.inventory_class or "",
                "inventory_unit_descriptor":  ili.product.inventory_unit_descriptor or "",
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
    invoice_date: str = "",
    sheet_tab: str = None,
    dry_run: bool = False,
) -> dict:
    """
    Update prices from an in-memory list of mapped invoice items (e.g. from a
    live batch run).  Items must have 'canonical' and 'unit_price' keys.
    vendor is a single vendor name applied to all items when items lack a
    'vendor_name' key.

    Date guard: if invoice_date is provided, only syncs prices when the invoice
    month matches the active Synergy tab's month.  This prevents old invoices
    from overwriting current prices during archive reprocessing.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB

    # Date guard — skip sync if invoice is from a different month than the tab
    if invoice_date:
        try:
            tab_year, tab_month = parse_tab_month(tab)
            inv_parts = invoice_date.split("-")
            inv_year, inv_month = int(inv_parts[0]), int(inv_parts[1])
            if (inv_year, inv_month) != (tab_year, tab_month):
                print(f"   [skip] Invoice {invoice_date} is not in tab month "
                      f"({tab_year}-{tab_month:02d}) — skipping price sync")
                return {
                    "updated": 0, "skipped_no_price": 0,
                    "skipped_no_match": 0, "failed": 0,
                    "skipped_wrong_month": len(mapped_items),
                }
        except (ValueError, IndexError):
            # Can't parse date — skip sync rather than risk overwriting with wrong-month data
            print(f"   [!] Could not parse invoice_date '{invoice_date}' — skipping price sync")
            return {
                "updated": 0, "skipped_no_price": 0,
                "skipped_no_match": 0, "failed": 0,
                "skipped_wrong_month": len(mapped_items),
            }

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
    result = _sync_prices_core(normalised, sheet_tab=tab, dry_run=dry_run)
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
        # Sean 2026-05-03: per-unit price drives calc_iup;
        # line_total drives sheet col E "case price". For Sysco/Exceptional
        # qty=1 rows the two are equal. For Farm Art qty>1 they differ.
        unit_price  = item.get("unit_price")
        line_total  = item.get("line_total", unit_price)
        case_size   = item.get("case_size_raw", "")
        stored_ppp  = item.get("price_per_pound")
        # Phase 3a structured-field path
        case_pack_count = item.get("case_pack_count")
        case_total_weight_lb = item.get("case_total_weight_lb")
        # Phase 3b — purchase_uom drives calc_iup case-vs-per-unit gate
        purchase_uom = item.get("purchase_uom", "")

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
                                      processor=fuzz_utils.default_process) >= 75
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

        variant_names = [name for name, score in good_matches if score == 100]

        if exact_names:
            # (a) Exact match — only write to this row, ignore longer variants
            for name in exact_names:
                for row_entry in [p for p in products if p["product"] == name]:
                    target_rows.append(row_entry)

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
            sheet_unit = row_entry.get("unit", "")

            # Use invoice case size if available, otherwise keep the sheet's
            effective_case_size = case_size or row_entry.get("case_size", "")

            if dry_run:
                print(f"   [DRY RUN] row {row_num}: '{canonical}' → '{row_entry['product']}'"
                      f" [{vendor_col}] @ ${line_total:.2f}")
                summary["updated"] += 1
                continue

            # calc_iup gets per-unit price (drives IUP math).
            # calc_price_per_lb gets per-unit too (it derives $/lb correctly
            # when given the per-unit price + total weight).
            iup = calc_iup(unit_price, effective_case_size,
                           case_pack_count=case_pack_count,
                           purchase_uom=purchase_uom)
            pplb = calc_price_per_lb(unit_price, effective_case_size, sheet_unit,
                                     stored_price_per_lb=stored_ppp,
                                     case_total_weight_lb=case_total_weight_lb)

            # Col E gets line_total (case price = what Sean paid for the line).
            batch_data.append({
                "range": f"'{tab}'!E{row_num}",
                "values": [[line_total]],
            })
            # Phase 4 layout: F=count, G=units of case size, H=Ea/# flag.
            # item dict carries case_pack_count + inventory_class +
            # inventory_unit_descriptor (added in load_items_for_month).
            batch_data.append({
                "range": f"'{tab}'!F{row_num}",
                "values": [[derive_f_count(item)]],
            })
            batch_data.append({
                "range": f"'{tab}'!G{row_num}",
                "values": [[derive_g_units(item)]],
            })
            batch_data.append({
                "range": f"'{tab}'!H{row_num}",
                "values": [[derive_h_flag(item)]],
            })
            # Always write J (IUP) and K (P/#) — clear the cell when calc
            # returns None. Otherwise stale derived values from a prior sync
            # (back when calc_price_per_lb was over-eager on bare '1' / '1/1'
            # case sizes) persist on the sheet and reproduce the misleading
            # E==K symptom. Manually-entered J/K on rows that get a fresh
            # price write are also wiped — preserving their accuracy is the
            # caller's job (re-enter after sync, or skip the sync for that row).
            batch_data.append({
                "range": f"'{tab}'!J{row_num}",
                "values": [[iup if iup is not None else ""]],
            })
            batch_data.append({
                "range": f"'{tab}'!K{row_num}",
                "values": [[pplb if pplb is not None else ""]],
            })

            calc_strs = []
            if iup is not None:
                calc_strs.append(f"IUP=${iup:.4f}")
            if pplb is not None:
                calc_strs.append(f"P/#=${pplb:.4f}")
            calc_str = "  " + "  ".join(calc_strs) if calc_strs else ""

            print(f"   [✓] row {row_num}: '{canonical}' → '{row_entry['product']}'"
                  f" [{vendor_col}] @ ${unit_price:.2f}"
                  + (f" [{case_size}]" if case_size else "")
                  + calc_str)
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
# 2b.  Metadata sync (vendor + case size from DB → sheet)
# ─────────────────────────────────────────────────────────────────────────────

def sync_metadata_to_sheet(sheet_tab: str = None, dry_run: bool = False) -> dict:
    """
    Push vendor (col C), case-pack count (col F), units of case size (col G),
    and Ea/# count-or-weigh flag (col H) from DB to the Synergy sheet.
    DB is the source of truth.

    For each product row on the sheet:
      - Vendor: most frequent vendor from InvoiceLineItem for that product
      - F (count): latest ILI's case_pack_count
      - G (units): product.inventory_unit_descriptor (blank for weighed)
      - H (flag):  product.inventory_class → 'Ea' / '#'

    Only overwrites blank/bad vendor cells. F/G/H are always pushed (DB
    is authoritative; manual edits to those columns are not preserved).

    Returns summary dict with counts.
    """
    _bootstrap_django()
    from myapp.models import InvoiceLineItem, Product
    from django.db.models import Count

    tab = sheet_tab or ACTIVE_SHEET_TAB
    print(f"   Loading Synergy sheet index from '{tab}'...")
    products, _ = build_sheet_index(tab)

    client = get_sheets_client()
    batch_data = []
    vendors_updated = 0
    case_sizes_updated = 0
    skipped = 0

    # Known bad case size values to overwrite on the sheet
    _BAD_CASE_SIZE_SHEET_RE = re.compile(
        r'^\d{5,}$'       # 5+ digit numbers (SUPC codes)
        r'|^0+[A-Z]+$'    # 00EA etc.
        r'|^\d{2}/\d{2}$' # dates like 04/06
    )

    # Known bad case size values in the DB (don't push these to the sheet)
    _BAD_CASE_SIZE_DB_RE = re.compile(
        r'^90/10$'         # cooking oil blend ratio, not case size
        r'|^UNIT$'         # generic "unit", not a real case size
        r'|^CS$'           # just "case" with no count
        r'|^0+(?:EA|CS)$'  # 00EA, 00CS etc.
        r'|^0/\d+$'        # 0/31, 0/25 — leading zero
        r'|^\d{5,}$'       # SUPC codes
    , re.IGNORECASE)

    for entry in products:
        product_name = entry["product"]
        row_num = entry["row"]
        current_vendor = entry["vendor"]
        current_case_size = entry["case_size"]

        # --- Vendor sync ---
        if not current_vendor:
            # Find most frequent vendor for this product
            top_vendor = (
                InvoiceLineItem.objects
                .filter(
                    product__canonical_name__iexact=product_name,
                    vendor__isnull=False,
                )
                .values('vendor__name')
                .annotate(c=Count('id'))
                .order_by('-c')
                .first()
            )
            if top_vendor:
                vendor_name = top_vendor['vendor__name']
                if dry_run:
                    print(f"   [DRY RUN] row {row_num}: '{product_name}' vendor → {vendor_name}")
                else:
                    batch_data.append({
                        "range": f"'{tab}'!C{row_num}",
                        "values": [[vendor_name]],
                    })
                vendors_updated += 1

        # Normalize existing vendor names
        _VENDOR_NORMALIZE = {
            "farmart":       "Farm Art",
            "exceptional":   "Exceptional Foods",
        }
        if current_vendor and current_vendor.lower() in _VENDOR_NORMALIZE:
            corrected = _VENDOR_NORMALIZE[current_vendor.lower()]
            if dry_run:
                print(f"   [DRY RUN] row {row_num}: vendor '{current_vendor}' → '{corrected}'")
            else:
                batch_data.append({
                    "range": f"'{tab}'!C{row_num}",
                    "values": [[corrected]],
                })
            vendors_updated += 1

        # --- F/G/H sync: case-pack count + units + Ea/# flag ---
        product = (Product.objects
                   .filter(canonical_name__iexact=product_name).first())
        if product:
            # F: latest ILI's case_pack_count
            latest_pack = (InvoiceLineItem.objects
                           .filter(product=product,
                                   case_pack_count__isnull=False)
                           .order_by('-invoice_date', '-imported_at')
                           .first())
            f_val = (latest_pack.case_pack_count
                     if latest_pack and latest_pack.case_pack_count is not None
                     else "")
            g_val = ("" if product.inventory_class == "weighed"
                     else (product.inventory_unit_descriptor or ""))
            klass = product.inventory_class or ""
            if klass == "weighed":
                h_val = "#"
            elif klass.startswith("counted_"):
                h_val = "Ea"
            else:
                h_val = ""

            if dry_run:
                print(f"   [DRY RUN] row {row_num}: '{product_name}' "
                      f"F={f_val} G={g_val!r} H={h_val!r}")
            else:
                batch_data.append({
                    "range": f"'{tab}'!F{row_num}",
                    "values": [[f_val]],
                })
                batch_data.append({
                    "range": f"'{tab}'!G{row_num}",
                    "values": [[g_val]],
                })
                batch_data.append({
                    "range": f"'{tab}'!H{row_num}",
                    "values": [[h_val]],
                })
            case_sizes_updated += 1

    # Flush writes
    if batch_data and not dry_run:
        try:
            client.values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": batch_data},
            ).execute()
        except Exception as e:
            print(f"   [!] Batch metadata update failed: {e}")
            return {"vendors_updated": 0, "case_sizes_updated": 0, "failed": True}

    summary = {
        "vendors_updated": vendors_updated,
        "case_sizes_updated": case_sizes_updated,
        "failed": False,
    }
    print(f"   [✓] Vendors updated: {vendors_updated}, Case sizes updated: {case_sizes_updated}")
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
        # Phase 4 layout (Sean 2026-05-03):
        #   F: case_pack_count (number of items in case)
        #   G: inventory_unit_descriptor (units of each item)
        #   H: 'Ea' or '#' (count-or-weigh flag from inventory_class)
        #   I: On Hand (was H)  J: IUP (was I)  K: P/# (was J)
        f_val = item.get("case_pack_count") or ""
        g_val = ("" if item.get("inventory_class") == "weighed"
                 else (item.get("inventory_unit_descriptor") or ""))
        klass = item.get("inventory_class") or ""
        if klass == "weighed":
            h_val = "#"
        elif klass.startswith("counted_"):
            h_val = "Ea"
        else:
            h_val = ""
        new_row_values = [
            "",          # A: Sub Category (blank for non-header rows)
            canonical,   # B: Product
            vendor,      # C: Vendor
            "",          # D: Location
            unit_price,  # E: Unit Price
            f_val,       # F: Case Size (count)
            g_val,       # G: Unit (units of case size)
            h_val,       # H: Ea / # flag
            "",          # I: On Hand
            "",          # J: IUP
            "",          # K: P/#
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
            range_addr = f"'{tab}'!A{insert_row}:K{insert_row}"
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

    # Clear Unit Price (E), On Hand (I), IUP (J), and P/# (K) — skip row 1 (header)
    # Phase 4 layout: I=On Hand, J=IUP, K=P/#
    print(f"   Clearing prices, IUP, P/#, and on-hand quantities...")
    client.values().batchClear(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "ranges": [
                f"'{new_tab_name}'!E2:E",
                f"'{new_tab_name}'!I2:I",
                f"'{new_tab_name}'!J2:J",
                f"'{new_tab_name}'!K2:K",
            ]
        },
    ).execute()

    # Carry forward last known prices from DB for every product on the sheet
    print(f"   Carrying forward last known prices from DB...")
    _bootstrap_django()
    from myapp.models import InvoiceLineItem

    # Build sheet index to get product names + rows
    products, _ = build_sheet_index(new_tab_name)

    batch_data = []
    carried = 0
    for entry in products:
        product_name = entry["product"]
        row_num = entry["row"]

        # Find most recent price for this product from any month
        latest = (
            InvoiceLineItem.objects
            .filter(
                product__canonical_name__iexact=product_name,
                unit_price__isnull=False,
                unit_price__gt=0,
            )
            .order_by("-invoice_date", "-imported_at")
            .first()
        )
        if not latest:
            continue

        unit_price = float(latest.unit_price)
        case_size = latest.case_size or entry.get("case_size", "")
        sheet_unit = entry.get("unit", "")

        batch_data.append({
            "range": f"'{new_tab_name}'!E{row_num}",
            "values": [[unit_price]],
        })

        # Phase 3a structured fields from latest ILI — bypass case_size string
        # parse when populated. Phase 3b — purchase_uom gates calc_iup.
        iup = calc_iup(unit_price, case_size,
                       case_pack_count=latest.case_pack_count,
                       purchase_uom=latest.purchase_uom)
        pplb = calc_price_per_lb(unit_price, case_size, sheet_unit,
                                 stored_price_per_lb=latest.price_per_pound,
                                 case_total_weight_lb=latest.case_total_weight_lb)

        # Phase 4 layout: J=IUP, K=P/#
        if iup is not None:
            batch_data.append({
                "range": f"'{new_tab_name}'!J{row_num}",
                "values": [[iup]],
            })
        if pplb is not None:
            batch_data.append({
                "range": f"'{new_tab_name}'!K{row_num}",
                "values": [[pplb]],
            })
        carried += 1

    if batch_data:
        client.values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": batch_data},
        ).execute()

    print(f"   [✓] Carried forward prices for {carried} products")

    # Sync vendor + case size metadata from DB
    print(f"   Syncing vendor and case size metadata from DB...")
    sync_metadata_to_sheet(sheet_tab=new_tab_name, dry_run=dry_run)

    print(f"   [✓] Created '{new_tab_name}'")
    return new_tab_name


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Carry-forward refresh — update stale rows that have no current-month invoice
# ─────────────────────────────────────────────────────────────────────────────

def refresh_stale_carryover(sheet_tab: str = None, dry_run: bool = False) -> dict:
    """For each product on the sheet that has NO invoice in the tab's month,
    pull the latest historical invoice (any prior month, prefer matching
    vendor) and refresh E + recompute I/J. Skips rows that have a current-
    month invoice — `sync_prices_for_tab` already handles those.

    Closes the gap where stale prices inherited at sheet-creation time
    persist on the sheet because no fresh invoice arrives for that product
    during the active month. Without this, regular sync silently leaves
    those rows alone and the operator has no signal that the price might
    be months out of date.

    Returns: { refreshed, skipped_current_month, skipped_no_history,
               skipped_anomalous }.
    """
    _bootstrap_django()
    from myapp.models import InvoiceLineItem
    from datetime import date
    from calendar import monthrange

    tab = sheet_tab or ACTIVE_SHEET_TAB
    year, month = parse_tab_month(tab)
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    # Anomaly guard: skip refresh when the candidate price exceeds this
    # ceiling — almost certainly an extended_amount leak (e.g., Ribs Feb 10
    # Colonial Village row at $497,803.16, which is the line total leaking
    # into unit_price). Real cases top out around $200-300; setting the
    # ceiling at $2,000 catches leaks while allowing high-end legitimate
    # cases (Pepperoni 25-lb cases, bulk specialty items, etc.).
    PRICE_SANITY_CEILING = 2000.0

    print(f"   Loading sheet index for '{tab}'...")
    products, _ = build_sheet_index(tab)
    if not products:
        print(f"   [!] No products in '{tab}'")
        return {"refreshed": 0, "skipped_current_month": 0,
                "skipped_no_history": 0, "skipped_anomalous": 0}

    client = get_sheets_client()
    batch_data = []
    summary = {"refreshed": 0, "skipped_current_month": 0,
               "skipped_no_history": 0, "skipped_anomalous": 0}

    for entry in products:
        product_name = entry["product"]
        row_num = entry["row"]
        sheet_vendor = entry.get("vendor", "")
        sheet_unit = entry.get("unit", "")

        # Skip if a current-month invoice exists — regular sync handles it.
        current = (InvoiceLineItem.objects
                   .filter(product__canonical_name__iexact=product_name,
                           invoice_date__gte=month_start,
                           invoice_date__lte=month_end,
                           unit_price__isnull=False)
                   .exists())
        if current:
            summary["skipped_current_month"] += 1
            continue

        # Find latest historical invoice (prefer matching vendor).
        # `unit_price__gt=0` excludes zero-price rows from the
        # extended_amount-leak / parser-glitch era — refreshing the sheet
        # to $0 is worse than leaving the prior value alone.
        if sheet_vendor:
            latest = (InvoiceLineItem.objects
                      .filter(product__canonical_name__iexact=product_name,
                              vendor__name__iexact=sheet_vendor,
                              invoice_date__lte=month_end,
                              unit_price__gt=0)
                      .select_related('vendor')
                      .order_by("-invoice_date", "-imported_at")
                      .first())
        else:
            latest = None
        if latest is None:
            latest = (InvoiceLineItem.objects
                      .filter(product__canonical_name__iexact=product_name,
                              invoice_date__lte=month_end,
                              unit_price__gt=0)
                      .select_related('vendor')
                      .order_by("-invoice_date", "-imported_at")
                      .first())

        if not latest:
            summary["skipped_no_history"] += 1
            continue

        unit_price = float(latest.unit_price)

        # Anomaly guard — skip extended_amount leaks rather than
        # propagating them to the sheet.
        if unit_price > PRICE_SANITY_CEILING:
            print(f"   [!] row {row_num}: '{product_name}' candidate "
                  f"${unit_price:,.2f} from {latest.invoice_date} exceeds sanity "
                  f"ceiling — skipping (probable extended_amount leak; clear "
                  f"the sheet cell manually if needed)")
            summary["skipped_anomalous"] += 1
            continue

        case_size = latest.case_size or entry.get("case_size", "")
        latest_vendor = latest.vendor.name if latest.vendor else "?"
        # Phase 3a structured-field path. Phase 3b — purchase_uom gate.
        iup = calc_iup(unit_price, case_size,
                       case_pack_count=latest.case_pack_count,
                       purchase_uom=latest.purchase_uom)
        pplb = calc_price_per_lb(unit_price, case_size, sheet_unit,
                                 stored_price_per_lb=latest.price_per_pound,
                                 case_total_weight_lb=latest.case_total_weight_lb)

        if dry_run:
            print(f"   [DRY RUN] row {row_num}: '{product_name}' → ${unit_price:.2f} "
                  f"from {latest.invoice_date} ({latest_vendor})")
        else:
            # Phase 4 layout: F=count, J=IUP, K=P/#. F written from
            # latest.case_pack_count; raw case_size string is no longer
            # pushed to F.
            batch_data.append({
                "range": f"'{tab}'!E{row_num}",
                "values": [[unit_price]],
            })
            if latest.case_pack_count is not None:
                batch_data.append({
                    "range": f"'{tab}'!F{row_num}",
                    "values": [[latest.case_pack_count]],
                })
            batch_data.append({
                "range": f"'{tab}'!J{row_num}",
                "values": [[iup if iup is not None else ""]],
            })
            batch_data.append({
                "range": f"'{tab}'!K{row_num}",
                "values": [[pplb if pplb is not None else ""]],
            })
            print(f"   [✓] row {row_num}: '{product_name}' → ${unit_price:.2f} "
                  f"from {latest.invoice_date} ({latest_vendor})")
        summary["refreshed"] += 1

    if batch_data and not dry_run:
        try:
            client.values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": batch_data},
            ).execute()
        except Exception as e:
            print(f"   [!] Batch carryover refresh failed: {e}")

    return summary


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
    parser.add_argument("--sync-metadata", metavar="TAB",
                        help="Sync vendor + case size from DB into this tab "
                             "(e.g. 'Synergy Apr 2026')")
    parser.add_argument("--find-new", action="store_true",
                        help="List canonical names that have no row in the tab")
    parser.add_argument("--insert-new", action="store_true",
                        help="Find canonicals with no row in the tab AND insert them "
                             "at the end of their target section. Routes by "
                             "(category, primary_descriptor) → section via CATEGORY_TO_SECTION.")
    parser.add_argument("--refresh-carryover", metavar="TAB",
                        help="For products with no current-month invoice on this tab, "
                             "pull the latest historical invoice and refresh E/I/J. "
                             "Closes the stale-carryover gap (sheet inherits an old "
                             "price at creation, no new invoice arrives, sync never "
                             "updates it). Pair with --dry-run to preview.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing anything")
    args = parser.parse_args()

    if args.sync_metadata:
        _bootstrap_django()
        tab = args.sync_metadata
        print(f"\nSyncing metadata for '{tab}'...")
        summary = sync_metadata_to_sheet(sheet_tab=tab, dry_run=args.dry_run)
        print(f"\nDone — Vendors: {summary['vendors_updated']}  |  "
              f"Case sizes: {summary['case_sizes_updated']}")
        return

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

    if args.refresh_carryover:
        _bootstrap_django()
        tab = args.refresh_carryover
        try:
            parse_tab_month(tab)
        except ValueError as e:
            print(f"[!] {e}")
            return
        print(f"\nRefreshing stale carryover for '{tab}'...")
        summary = refresh_stale_carryover(sheet_tab=tab, dry_run=args.dry_run)
        print(f"\nDone — Refreshed: {summary['refreshed']}  |  "
              f"Skipped (current-month invoice): {summary['skipped_current_month']}  |  "
              f"No history: {summary['skipped_no_history']}  |  "
              f"Skipped anomalous: {summary['skipped_anomalous']}")
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

    if args.insert_new:
        _bootstrap_django()
        items = load_items_for_month(*parse_tab_month(tab))
        new = find_new_items(items, sheet_tab=tab)
        if not new:
            print(f"All canonicals already present in '{tab}'. Nothing to insert.")
            return
        print(f"\n{len(new)} canonical(s) to insert into '{tab}':")
        for it in new:
            print(f"  {it['canonical']:<35} → "
                  f"[{it.get('category','')} / {it.get('primary_descriptor','')}]")
        summary = insert_new_items(new, sheet_tab=tab, dry_run=args.dry_run)
        print(f"\nDone — Inserted: {summary['inserted']}  |  "
              f"Skipped (no section): {summary['skipped_no_section']}")
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
