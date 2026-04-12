"""
Parses raw OCR text from invoices into structured line items.
Handles Sysco, Colonial Meat, Exceptional, FarmArt, PBM formats.
Add a new _parse_<vendor> function as you onboard each vendor.
"""
import re
import json
from datetime import datetime


def detect_vendor(text: str) -> str:
    text_upper = text.upper()
    if "SYSCO" in text_upper:
        return "Sysco"
    if "COLONIAL" in text_upper or "VOLONIAL" in text_upper:
        return "Colonial Village Meat Markets"
    if "EXCEPTIONAL" in text_upper:
        return "Exceptional Foods"
    if "FARMART" in text_upper or "FARM ART" in text_upper:
        return "Farm Art"
    if "PBM" in text_upper or "PHILADELPHIA BAKERY MERCHANTS" in text_upper or "PHILABAKERY" in text_upper:
        return "Philadelphia Bakery Merchants"
    if "DELAWARE COUNTY LINEN" in text_upper:
        return "Delaware County Linen"
    return "Unknown"


def extract_date(text: str) -> str:
    patterns = [
        r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b",
        r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2})\b",
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            groups = match.groups()
            try:
                fmt = "%m/%d/%y" if len(groups[2]) == 2 else "%m/%d/%Y"
                date = datetime.strptime(f"{groups[0]}/{groups[1]}/{groups[2]}", fmt)
                return date.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


# ---------------------------------------------------------------------------
# Sysco parser
# ---------------------------------------------------------------------------

# Sysco item codes are 6-7 digit numbers. Prices end in .XX (two decimal places).
_PRICE_ANCHOR = re.compile(r'(\d{6,7})\s+(\d+\.\d{2})\s*$')

# Lines that look like product descriptions (contain 3+ consecutive letters
# and are not section headers or pure-number lines).
_SECTION_HEADER = re.compile(r'\*{2,}')
_SKIP_LINE = re.compile(
    r'^(\d+\s*(CS|BG|LB|OZ|CT|GAL?|EA|PK|CASE|ONLY)\s*$'   # bare qty/unit
    r'|\d+\.?\d*\s*(LB|OZ|CT|GAL?|#)\s*$'                  # bare pack size
    r'|[\d\s.]+$'                                            # pure numbers
    r'|GROUP\s+TOTAL'                                        # totals
    r'|OPEN:\s*\d'                                           # store hours
    r'|CLOSE:\s*\d'
    r'|CASES\b'
    r'|SPLIT\s+TOT'
    r'|CUBE\s+GROSS'
    r'|REMIT\s+TO'
    r'|PAYABLE\s+ON'
    r')',
    re.IGNORECASE,
)


def _is_description(line: str) -> bool:
    """True if the line looks like product description text."""
    line = line.strip()
    if not line:
        return False
    if _SECTION_HEADER.match(line):
        return False
    if _SKIP_LINE.match(line):
        return False
    if not re.search(r'[A-Za-z]{3,}', line):
        return False
    return True


# Matches case-size tokens embedded in Sysco descriptions.
# Captures formats like: 4/1GAL, 2/5LB, 3CT, 24CS, 6/32OZ, 16/20, 12OZ
# Must appear as a whole token (word boundary or start/end of string).
_CASE_SIZE_RE = re.compile(
    r'(?<!\w)'
    r'(\d+/\d+(?:LB|OZ|GAL|KG|CT|LTR|FL\s*OZ)?'   # N/M or N/MUNIT  e.g. 4/1GAL, 16/20
    r'|\d+(?:CT|CS|EA|PK|BG))'                       # NCT/NCS/NEA     e.g. 3CT, 24CS
    r'(?!\w)',
    re.IGNORECASE,
)


def _extract_case_size(text: str) -> str:
    """
    Pull the first case-size token from a raw Sysco description line.
    Returns the token uppercased, or "" if none found.
    Examples:
      "WHLFCLS ROMAINE HEARTS 3CT"     → "3CT"
      "SYSCLS MAYO 4/1GAL"             → "4/1GAL"
      "1 CS CHICKEN BREAST 2/5LB"      → "2/5LB"
      "SYSPAD TILAPIA FILLET 16/20"    → "16/20"
    """
    m = _CASE_SIZE_RE.search(text)
    return m.group(1).upper() if m else ""


def _clean_description(text: str) -> str:
    """
    Strip leading QTY/unit tokens and trailing barcodes/item codes
    from a Sysco description fragment, leaving just the product name.
    """
    # Remove leading qty+unit  e.g. "1 CS", "2 CS", "1 BG", "1s ONLY"
    text = re.sub(
        r'^\d+s?\s*(ONLY\s*)?\d*\.?\d*\s*'
        r'(?:CS|BG|LB|OZ|CT|GAL?|EA|PK|CASE)?\s*',
        '', text, flags=re.IGNORECASE,
    ).strip()
    # Remove leading pack-size  e.g. "150 LB", "612 CT", "482.6OZ"
    text = re.sub(
        r'^\d+\.?\d*\s*(?:LB|OZ|CT|GAL?|EA|PK|#)\s*',
        '', text, flags=re.IGNORECASE,
    ).strip()
    # Remove trailing long barcodes (12+ digits)
    text = re.sub(r'\s+\d{12,}\s*$', '', text).strip()
    # Remove trailing short reference codes (4-6 digits)
    text = re.sub(r'(\s+\d{4,6}){1,2}\s*$', '', text).strip()
    return text


def _parse_sysco(text: str) -> list[dict]:
    """
    Sysco invoices are printed in columns.  The OCR reads them top-to-bottom,
    so within each category block the item descriptions appear in order
    *before* the matching item-code/price pairs.

    Strategy:
    1. Find every price anchor  (7-digit item-code  +  price).
    2. For each anchor, walk backward through the lines to find the nearest
       unused description fragment.
    3. If the anchor line itself carries description text (inline items),
       use that directly.
    """
    lines = [l.strip() for l in text.splitlines()]
    items = []

    # ── Pass 1: locate every price anchor ──────────────────────────────────
    anchors = []   # (line_index, item_code, price, prefix_text)
    for i, line in enumerate(lines):
        m = _PRICE_ANCHOR.search(line)
        if m:
            prefix = line[:m.start()].strip()
            anchors.append((i, m.group(1), float(m.group(2)), prefix))

    # ── Pass 2: find a description for each anchor ─────────────────────────
    used_lines = set()

    for anchor_pos, (line_idx, item_code, price, prefix) in enumerate(anchors):

        # Option A: the price line itself has readable description text
        inline_desc = _clean_description(prefix)
        if inline_desc and len(inline_desc) >= 5 and re.search(r'[A-Za-z]{3,}', inline_desc):
            description = inline_desc
            case_size   = _extract_case_size(prefix)
        else:
            # Option B: scan backward for the nearest unused description line,
            # stopping at the previous anchor or a section header.
            prev_anchor_line = anchors[anchor_pos - 1][0] if anchor_pos > 0 else -1
            description = None
            case_size   = ""

            for look in range(line_idx - 1, prev_anchor_line, -1):
                if look in used_lines:
                    continue
                candidate = lines[look]
                if _is_description(candidate):
                    case_size   = _extract_case_size(candidate)
                    description = _clean_description(candidate)
                    used_lines.add(look)
                    break

            if not description:
                description = f"[Sysco #{item_code}]"

        items.append({
            "raw_description": description,
            "sysco_item_code": item_code,
            "unit_price":      price,
            "case_size_raw":   case_size,
        })

    return items


# ---------------------------------------------------------------------------
# Exceptional Foods parser
# ---------------------------------------------------------------------------

def _parse_exceptional(text: str) -> list[dict]:
    """
    Exceptional invoices are multi-column PDFs. Vision API OCR reads the columns
    independently, so the output interleaves item codes, quantities, descriptions,
    and prices in separate clusters rather than row-by-row.

    Strategy:
    1. Find the item block (after "Item ID" header, before "Sale Amount").
    2. Extract description lines — product name text that isn't a code, qty, or price.
       Also handle the occasional inline format: "2.00CS Pork Butt Bone In IBP".
    3. Extract line-item totals by anchoring on per-unit price lines
       (e.g. "4.6900 LE") and taking the next standalone XX.XX value.
    4. Zip descriptions with totals in order. If counts differ (e.g. per-CS items
       lack a per-unit anchor), fall back to collecting all standalone decimals
       above a minimum threshold.
    """
    lines = [l.strip() for l in text.splitlines()]

    # Per-unit price: 4 decimal places followed by optional unit abbreviation
    # e.g. "4.6900 LE", "6.2900 LB", "1.7800"
    per_unit_re = re.compile(r'^\d+\.\d{4}\s*(?:LE|LB|CS|EA|PK|OZ)?\s*$', re.IGNORECASE)
    standalone_re = re.compile(r'^\d+\.\d{2}$')
    qty_unit_re   = re.compile(r'^\d+\.?\d*\s*(?:CS|LB|EA|PK|OZ)\s*$', re.IGNORECASE)
    inline_re     = re.compile(r'^\d+\.?\d*\s*(?:CS|LB|EA|PK|OZ)\s+(.+)', re.IGNORECASE)

    SKIP = re.compile(
        r'^(INVOICE|EXCEPTIONAL|\*FOODS\*?|Est\s+\d{4}|U\.S\.|INSPECTED|PASSED|'
        r'DEPARTMENT|AGRICULTURE|EST\.\s*\d+|NC\.|INC\.|'
        r'SOLD|SHIP\b|Route\b|Stop\b|Sales\b|Customer\b|Order\b|P\.O\.|Terms\b|Ship Via|'
        r'Quantity Shipped|Qty Shipped|Qty Ordered|Qty\b|'
        r'Description\s*$|Weight\s*$|Price\s*$|Per\s*$|Total\s*$|'
        r'Sale Amount|Freight|Tax\s*$|Amount Paid|BALANCE|TOTAL PIECES|BOXES|DUE\s*$|'
        r'Received By|Print Name|CUSTOMER COPY|'
        r'Net \d+|Delivery\s*$|'
        r'Claims|service charge|HANDLING|returns|credit|minimum|'
        r'Ph:|Fx:|www\.|Invoice No\.|Invoice Date|Page\s*$|Item ID)',
        re.IGNORECASE,
    )

    def is_product_line(line: str) -> bool:
        if not line or len(line) < 6:
            return False
        if SKIP.match(line):
            return False
        if per_unit_re.match(line):
            return False
        if standalone_re.match(line):
            return False
        if qty_unit_re.match(line):
            return False
        if re.match(r'^[\d\s.,\-/]+$', line):   # pure numbers / punctuation
            return False
        if re.match(r'^[A-Z0-9]{1,10}$', line): # short item codes (all-caps/digits)
            return False
        return bool(re.search(r'[A-Za-z]{3,}', line))

    # ── Locate item block ────────────────────────────────────────────────────
    start = None
    for i, line in enumerate(lines):
        if re.search(r'\bItem\s+ID\b', line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return []

    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(
            r'^(Sale Amount|Received By|CUSTOMER COPY|'
            r'DUE TO RISING|NOTICE:|ALL ORDERS|'
            r'service charge|HANDLING CHARGE|No returns|No credit)',
            lines[i], re.IGNORECASE
        ):
            end = i
            break
        # Footer logo reprint (EXCEPTIONAL appears again after the item list)
        if lines[i].upper() == 'EXCEPTIONAL' and i > start + 5:
            end = i
            break

    block = lines[start:end]

    # ── Extract descriptions ─────────────────────────────────────────────────
    descriptions = []
    for line in block:
        m = inline_re.match(line)
        if m:
            desc = m.group(1).strip()
            if re.search(r'[A-Za-z]{3,}', desc) and not SKIP.match(desc):
                descriptions.append(desc)
        elif is_product_line(line):
            descriptions.append(line)

    # ── Extract totals anchored on per-unit price lines ──────────────────────
    # Per-LB/unit items: description … weight … PRICE.XXXX UNIT … TOTAL.XX
    totals = []
    for i, line in enumerate(block):
        if per_unit_re.match(line):
            for look in range(i + 1, min(i + 4, len(block))):
                if standalone_re.match(block[look]):
                    totals.append(float(block[look]))
                    break

    # ── Fallback: count mismatch (e.g. per-CS items lack a per-unit anchor) ──
    # Collect all standalone XX.XX values above a floor; take the last N.
    if len(totals) < len(descriptions):
        FLOOR = 10.0
        all_decimals = [float(l) for l in block if standalone_re.match(l) and float(l) >= FLOOR]
        if len(all_decimals) >= len(descriptions):
            totals = all_decimals[-len(descriptions):]
        else:
            totals = all_decimals

    # ── Zip together ─────────────────────────────────────────────────────────
    items = []
    for desc, price in zip(descriptions, totals):
        if price > 0:
            items.append({
                "raw_description": desc,
                "unit_price": price,
                "case_size_raw": "",
            })

    return items


# ---------------------------------------------------------------------------
# FarmArt parser
# ---------------------------------------------------------------------------

def _parse_farmart(text: str) -> list[dict]:
    """
    FarmArt invoices have two types of items:
    - Non-stock items prefixed with "zz"
    - Regular stock items (no prefix) that appear after the column headers

    Both formats follow: Description → "United States" → unit price → amount
    We skip items with a zero or missing amount (unavailable items).
    """
    items = []
    lines = [l.strip() for l in text.splitlines()]

    # Headers/footers to skip
    skip_patterns = re.compile(
        r'^(Bill To|Ship To|Received By|Invoice|Customer|Date|Purchase|Driver|'
        r'Route|Terms|Salesperson|Picker|Order|Quantity|U/M|Item|Description|'
        r'COOL|United States|Nontaxable|Taxable|Tax|Discount|Invoice Total|'
        r'Payments|Invoice Balance|Page|All returns|\*\*\*|zz BAKING|NOT AVAIL|'
        r'Unit Price|Amount|'       # column headers
        r'"zz"|'                    # "zz" non-stock delivery note lines
        r'\d+\.\d{3})',             # quantity lines (1.000 HALF, 4.000 EACH CAU, etc.)
        re.IGNORECASE
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        # Match both "zz ITEM NAME" and plain "ITEM NAME, details" descriptions
        is_zz   = line.upper().startswith("ZZ ")
        is_desc = (
            not is_zz
            and len(line) > 8
            and re.search(r'[A-Za-z]{4,}', line)
            and not skip_patterns.match(line)
            and not re.match(r'^[\d\s.,]+$', line)
        )

        if is_zz or is_desc:
            description = line[3:].strip() if is_zz else line
            description = re.sub(r'\s*\*+.*$', '', description).strip()

            # Look ahead for two consecutive standalone numbers (unit price, amount)
            prices_found = []
            for look in range(i + 1, min(i + 8, len(lines))):
                if re.match(r'^\d+\.\d{2}$', lines[look]):
                    prices_found.append(float(lines[look]))
                    if len(prices_found) == 2:
                        break

            # Use the extended amount (second number); skip if zero/missing
            amount = prices_found[1] if len(prices_found) == 2 else (
                     prices_found[0] if len(prices_found) == 1 else None)

            if amount and amount > 0:
                items.append({
                    "raw_description": description,
                    "unit_price": amount,
                    "case_size_raw": "",
                })
        i += 1

    return items


# ---------------------------------------------------------------------------
# PBM (Philadelphia Bakery Merchants) parser
# ---------------------------------------------------------------------------

def _parse_pbm(text: str) -> list[dict]:
    """
    PBM invoices come in two layouts depending on OCR column reading order:

    Layout A (row-by-row): Description | Unit Price | Amount headers appear
      together, then item rows: ItemCode | Qty | U/M | Description | UnitPrice | Amount

    Layout B (column format): Left column has item data (ItemCode/Qty/U/M/Desc),
      right column has prices (Unit Price | Amount pairs) read separately.

    Detection: if "Unit Price" appears within 5 lines of "Description", it's Layout A.
    Otherwise Layout B (prices come in a separate block later in the text).

    Descriptions are extracted by finding lines that immediately follow a U/M line
    (DZ, EA, LB, etc.) — this works for both layouts.
    """
    lines = [l.strip() for l in text.splitlines()]

    um_pattern   = re.compile(r'^(DZ|EA|LB|CS|OZ|PK|BG|CTN)$', re.IGNORECASE)
    stop_pattern = re.compile(
        r'^(Routeperson|QTY\s+Totals|s\s+Printed|Subtotal|Invoice\s+Total|Page\s+\d)',
        re.IGNORECASE
    )

    # Locate key header positions
    desc_idx       = None
    unit_price_idx = None
    amount_idx     = None
    for i, line in enumerate(lines):
        if re.match(r'^Description\s*$', line, re.IGNORECASE) and desc_idx is None:
            desc_idx = i
        elif desc_idx is not None and unit_price_idx is None and re.match(r'^Unit\s*Price\s*$', line, re.IGNORECASE):
            unit_price_idx = i
        elif desc_idx is not None and amount_idx is None and re.match(r'^Amount\s*$', line, re.IGNORECASE):
            amount_idx = i

    if desc_idx is None:
        return []

    # Layout detection: prices right after Description = row-by-row; far away = column
    is_column_format = (
        unit_price_idx is not None and unit_price_idx > desc_idx + 5
    )

    # ── Extract descriptions: lines following a U/M token in the item block ───
    descriptions = []
    for i in range(desc_idx + 1, len(lines)):
        line = lines[i]
        if stop_pattern.match(line):
            break
        if re.match(r'^\d+\.\d{2}$', line):   # skip standalone decimals
            continue
        if i > 0 and um_pattern.match(lines[i - 1]):
            # Exclude bare item codes (all uppercase alphanumeric, no spaces/symbols)
            # but allow product names like "100% Whole Wheat"
            if re.search(r'[A-Za-z]{3,}', line) and not re.match(r'^[A-Z0-9]+$', line):
                descriptions.append(line)

    # ── Collect extended amounts ───────────────────────────────────────────────
    if is_column_format:
        # Prices are in a right-column block that starts after the "Amount" header
        price_start = amount_idx if amount_idx is not None else unit_price_idx
        raw_amounts = []
        for i in range((price_start or 0) + 1, len(lines)):
            if re.match(r'^(Subtotal|Invoice\s+Total)', lines[i], re.IGNORECASE):
                break
            if re.match(r'^\d+\.\d{2}$', lines[i]):
                raw_amounts.append(float(lines[i]))
        # Pairs alternate: unit_price, extended_amount → take extended (odd indices)
        ext_amounts = raw_amounts[1::2]
    else:
        # Row-by-row: after the Amount header, numbers appear as qty/unit/extended triples
        raw_amounts = []
        for i in range((amount_idx or desc_idx) + 1, len(lines)):
            line = lines[i]
            if re.match(r'^(QTY\s+Totals|Subtotal|Invoice\s+Total)', line, re.IGNORECASE):
                break
            if re.match(r'^\d+\.\d{2}$', line):
                raw_amounts.append(float(line))
        # Pattern per item: qty, unit_price, extended → take extended (index 2, 5, 8, ...)
        ext_amounts = raw_amounts[2::3]

    items = []
    for desc, price in zip(descriptions, ext_amounts):
        if price > 0:
            items.append({
                "raw_description": desc,
                "unit_price": price,
                "case_size_raw": "",
            })

    return items


# ---------------------------------------------------------------------------
# Delaware County Linen parser
# ---------------------------------------------------------------------------

def _parse_delaware_linen(text: str) -> list[dict]:
    """
    Delaware County Linen invoices:
    Qty | Item Code | Description | Unit Price | Amount | Qty Adjustment

    The OCR reads Qty and Item Code columns top-to-bottom first, then reads
    Description/Unit Price/Amount in row order.  So the text looks like:
      300          ← all qtys
      25
      MOPS         ← all item codes (ALL CAPS single words)
      BAPSWTW
      Bar Mops     ← description row 1
      0.22         ← unit price row 1
      66.00T       ← taxable amount row 1
      Bib Aprons White  ← description row 2
      ...

    Strategy: after the "Amount" header, skip pure numbers and all-caps
    item codes.  For each description line, look ahead for two prices.
    """
    items = []
    lines = [l.strip() for l in text.splitlines()]

    skip = re.compile(
        r'(fuel|delivery|sales tax|pa sale|credit card|qty adjustment|'
        r'total due|payments|balance due)',
        re.IGNORECASE
    )
    stop = re.compile(
        r'^(Total|Subtotal|Payments|Balance|All items|Please|Thank|PLEASE|'
        r'PO BOX|www\.)',
        re.IGNORECASE
    )

    in_items = False
    i = 0
    while i < len(lines):
        line = lines[i]

        # Enter item block after "Amount" column header
        if re.match(r'^Amount\s*$', line, re.IGNORECASE):
            in_items = True
            i += 1
            continue

        if not in_items:
            i += 1
            continue

        if stop.match(line):
            break
        if skip.search(line):
            i += 1
            continue
        if re.match(r'^\d+$', line):          # pure integer = qty
            i += 1
            continue
        if re.match(r'^[A-Z]+$', line):        # all-caps single word = item code
            i += 1
            continue
        if len(line) <= 2:                     # dash or single char
            i += 1
            continue
        if re.match(r'^\d+\.?\d*%?$', line):  # percentage or bare number
            i += 1
            continue

        # Description: has letters and is not a bare amount (digits + optional T)
        if re.search(r'[A-Za-z]', line) and not re.match(r'^[\d.]+T?$', line):
            description = line
            unit_price  = None
            amount      = None

            for look in range(i + 1, min(i + 6, len(lines))):
                candidate = lines[look]
                price_m = re.match(r'^([\d.]+)T?$', candidate)
                if price_m:
                    val = float(price_m.group(1))
                    if unit_price is None:
                        unit_price = val
                    else:
                        amount = val
                        break

            if unit_price is not None and not skip.search(description):
                final_price = amount if amount is not None else unit_price
                if final_price > 0:
                    items.append({
                        "raw_description": description,
                        "unit_price": final_price,
                        "case_size_raw": "",
                    })

        i += 1

    return items


# ---------------------------------------------------------------------------
# Colonial Meat — handwritten, flag for manual review
# ---------------------------------------------------------------------------

def _parse_colonial(text: str) -> list[dict]:
    """
    Colonial Meat invoices are handwritten. OCR accuracy is limited.
    We extract what we can and flag everything for manual review.
    """
    items = []
    for line in text.splitlines():
        line = line.strip()
        price_match = re.search(r'(\d+\.\d{2})\s*$', line)
        if price_match and len(line) > 8:
            desc = line[:price_match.start()].strip()
            if re.search(r'[A-Za-z]{2,}', desc):
                items.append({
                    "raw_description": desc,
                    "unit_price": float(price_match.group(1)),
                    "case_size_raw": "",
                    "needs_review": True,
                })
    return items


# ---------------------------------------------------------------------------
# Generic fallback parser
# ---------------------------------------------------------------------------

def _fallback_parse(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        price_match = re.search(r"(\d+\.\d{2})\s*$", line)
        if price_match and len(line) > 10:
            items.append({
                "raw_description": line[:price_match.start()].strip(),
                "case_size_raw": "",
                "unit_price": float(price_match.group(1)),
                "needs_review": True,
            })
    return items


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_invoice(text: str, vendor: str = None) -> dict:
    vendor = vendor or detect_vendor(text)
    date   = extract_date(text)

    parsers = {
        "Sysco":                 _parse_sysco,
        "Exceptional":           _parse_exceptional,
        "FarmArt":               _parse_farmart,
        "PBM":                   _parse_pbm,
        "Colonial Meat":         _parse_colonial,
        "Delaware County Linen": _parse_delaware_linen,
    }

    parser_fn = parsers.get(vendor, _fallback_parse)
    items = parser_fn(text)

    if vendor not in parsers:
        for item in items:
            item["needs_review"] = True

    return {
        "vendor": vendor,
        "invoice_date": date,
        "items": items,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            raw = f.read()
        result = parse_invoice(raw)
        print(json.dumps(result, indent=2))
