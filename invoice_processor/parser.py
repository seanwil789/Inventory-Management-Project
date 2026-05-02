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
# Accept an optional trailing pack_price: "CODE UNIT_PRICE" or "CODE UNIT_PRICE PACK_PRICE".
_PRICE_ANCHOR = re.compile(r'(\d{6,7})\s+(\d+\.\d{2})(?:\s+\d+\.\d{2})?\s*$')

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
    if not line or len(line) < 5:
        return False
    if _SECTION_HEADER.match(line):
        return False
    if _SKIP_LINE.match(line):
        return False
    if not re.search(r'[A-Za-z]{3,}', line):
        return False
    # Reject single-word brand fragments (LAYS, KIND, KONTOS, etc.)
    # Real descriptions have at least 2 words with 3+ letters each
    letter_words = re.findall(r'[A-Za-z]{3,}', line)
    if len(letter_words) < 2:
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


# Phase 3 #6 (Sean 2026-05-02): count-per-lb extraction from raw_description.
#
# Industry "count per lb" tokens like SHRIMP 21/25, BACON 18/22, SCALLOPS
# U/15 are embedded in raw_descriptions. They tell you "21-25 shrimp per
# pound" / "18-22 bacon strips per pound", and recipe cost math needs
# them ("2 strips bacon" → 2 / avg_count_per_lb × $/lb).
#
# Phase 3b shipped the consumer side (cost_utils dispatch). This is the
# parser side: extract N/M tokens in protein context and populate
# count_per_lb_low / count_per_lb_high on the item dict so db_write
# threads them through to ILI columns.
#
# Conservative pattern:
#   - Protein keyword present in raw (SHRIMP/BACON/SCALLOP/etc.)
#   - N/M token where 1 ≤ low < high ≤ 100
#   - NOT followed by unit suffix (LB/OZ/GAL/CT/PT) — those are pack sizes
#   - First match wins
_COUNT_PER_LB_PROTEIN_KEYWORDS_RE = re.compile(
    r'\b('
    r'SHRIMP|PRAWN|SCALLOP|LOBSTER|CRAB|'
    r'BACON|'
    r'SALMON|TUNA|TILAPIA|HALIBUT|TROUT'
    r')\b',
    re.IGNORECASE,
)
_COUNT_PER_LB_TOKEN_RE = re.compile(
    # N/M not adjacent to letters or digits (so won't match dates 10/14/25
    # or pack formats 12/4OZ followed by letters), and the / itself isn't
    # part of a multi-slash group (L/O 10/14 still matches the 10/14 part).
    r'(?:^|[^/A-Za-z\d])'
    r'(\d{1,3})/(\d{1,3})'
    r'(?![/A-Za-z\d])',
)
# Pack-format units that disqualify an N/M from being count-per-lb (it's
# a pack size like 12/4OZ instead of a count range like 21/25).
_PACK_UOM_AFTER_NM_RE = re.compile(
    r'^(LB|LBS|OZ|GAL|GALLON|QT|PT|CT|KG|LTR|FL\s*OZ|FLOZ|CS|EA|PK|BG)\b',
    re.IGNORECASE,
)


def _extract_count_per_lb(raw_desc: str) -> tuple[int, int] | None:
    """Extract count-per-lb tokens (SHRIMP 21/25, BACON 18/22).

    Returns (low, high) when raw_description contains BOTH a protein
    keyword AND an N/M token with no immediately-following unit. Returns
    None otherwise — callers should leave count_per_lb_low/high unset.
    """
    if not raw_desc:
        return None
    if not _COUNT_PER_LB_PROTEIN_KEYWORDS_RE.search(raw_desc):
        return None
    for m in _COUNT_PER_LB_TOKEN_RE.finditer(raw_desc):
        low, high = int(m.group(1)), int(m.group(2))
        # Tail check: is this N/M actually a pack size (12/4OZ)? Look at
        # what follows the M digits.
        tail = raw_desc[m.end():]
        if _PACK_UOM_AFTER_NM_RE.match(tail):
            continue
        # Range sanity: low must be strictly less than high, both positive,
        # high not absurd. Excludes 21/2 (low > high typo) and 100+.
        if 0 < low < high <= 100:
            return (low, high)
    return None


# Farm Art raw_description pack extractor (Sean 2026-05-02).
#
# Farm Art's _parse_farmart and match_farmart_spatial both emit empty
# case_size_raw — pack info is buried in raw_description as natural-
# language tokens. This extractor pulls those tokens into the same
# canonical case_size string + structured fields the Sysco path produces,
# so synergy_sync / cost_utils / class_guard treat Farm Art rows
# identically to Sysco rows.
#
# Three patterns supported (most-specific first; first match wins):
#   1. N/M-UNIT  e.g. "4 / 1 - GAL", "12/1 QUART", "12 / 3LB", "6/1QT"
#   2. N-UNIT    e.g. "9CT", "15 - DOZ", "60 BU", "1/40LB" (degenerate N=1)
#   3. N#        pound symbol e.g. "5 # BAG", "11 # X FANCY"
#
# Skipped (too ambiguous):
#   * Bare numbers without unit ("XL FANCY 18", "POTATOES 50") — could be
#     LB, count, or vendor code; conservative don't-guess.
#   * BAG / HEAD / CASE alone ("3 BAG") — those are containers, not
#     measurements; Farm Art "3 BAG" might mean 3 paper bags or 3×5lb bags.
_FARMART_PACK_NM_RE = re.compile(
    r'\b(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*-?\s*'
    r'(GAL|GALLON|QT|QUART|PT|PINT|LB|LBS|OZ|CT|DOZ|EA|FL\s*OZ|DZ)\b',
    re.IGNORECASE,
)
_FARMART_PACK_BARE_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*-?\s*'
    r'(CT|DOZ|BU|GAL|GALLON|QT|QUART|PT|PINT|LB|LBS|OZ|FL\s*OZ|DZ)\b',
    re.IGNORECASE,
)
_FARMART_PACK_HASH_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*#'
)
# Map plain-English unit to canonical uom + LB factor. Includes BU
# (bushel) which has no LB conversion (~weight varies by produce) — we
# keep BU as a recognized uom but don't compute case_total_weight_lb.
_FARMART_UOM_NORMALIZE: dict[str, str] = {
    'GAL': 'GAL', 'GALLON': 'GAL',
    'QT':  'QT',  'QUART':  'QT',
    'PT':  'PT',  'PINT':   'PT',
    'LB':  'LB',  'LBS':    'LB',
    'OZ':  'OZ',
    'CT':  'CT',
    'DOZ': 'DOZ', 'DZ': 'DOZ',
    'EA':  'EA',
    'BU':  'BU',
    'FL OZ': 'FL_OZ', 'FLOZ': 'FL_OZ',
}


def _build_pack_dict(count: int, size_str: str, uom_raw: str) -> dict:
    """Assemble the structured fields + canonical case_size_raw."""
    uom_key = uom_raw.upper().replace('  ', ' ').strip()
    uom = _FARMART_UOM_NORMALIZE.get(uom_key, uom_key)
    out = {
        'case_size_raw': f'{count}/{size_str}{uom}' if count > 1 else f'{size_str}{uom}',
        'case_pack_count': count,
        'case_pack_unit_size': size_str,
        'case_pack_unit_uom': uom,
    }
    factor = _PACK_UOM_TO_LB.get(uom)
    if factor is not None:
        try:
            total_lb = float(size_str) * count * factor
            out['case_total_weight_lb'] = round(total_lb, 3)
        except ValueError:
            pass
    return out


def _extract_farmart_pack(raw_desc: str) -> dict:
    """Parse Farm Art raw_description for embedded pack tokens.

    Returns dict with case_size_raw + structured fields, or {} when no
    confident pack token found. Most-specific N/M pattern checked first.

    Examples:
      "JUICE ORANGE 4 / 1 - GAL"       → 4/1GAL, count=4, size=1, uom=GAL
      "DAIRY HEAVY CREAM 40% 12/1 QT"  → 12/1QT, count=12, size=1, uom=QT
      "MELONS, CANTALOUPES, 9CT"       → 9CT, count=1, size=9, uom=CT
      "EGGS XL LOOSE 15 - DOZ"         → 15DOZ, count=1, size=15, uom=DOZ
      "HERB CILANTRO 60 BU"            → 60BU, count=1, size=60, uom=BU
      "CARROT, 5 # BAG"                → 5LB, count=1, size=5, uom=LB
      "POTATOES 50"                    → {} (no unit, ambiguous)
    """
    if not raw_desc:
        return {}
    # Pattern 1: N/M-UNIT (most specific — count + size + uom)
    m = _FARMART_PACK_NM_RE.search(raw_desc)
    if m:
        count = int(m.group(1))
        size = m.group(2)
        uom = m.group(3)
        return _build_pack_dict(count, size, uom)
    # Pattern 2: bare N-UNIT (count=1, size in N)
    m = _FARMART_PACK_BARE_RE.search(raw_desc)
    if m:
        size = m.group(1)
        uom = m.group(2)
        # Skip if size has a leading zero of a fraction (e.g. 0.5) — those
        # are typically bagged subdivisions, not pack counts.
        return _build_pack_dict(1, size, uom)
    # Pattern 3: N# (pound symbol)
    m = _FARMART_PACK_HASH_RE.search(raw_desc)
    if m:
        size = m.group(1)
        return _build_pack_dict(1, size, 'LB')
    return {}


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


# Sysco PACK SIZE column: appears between QTY (e.g. "2 CS") and DESCRIPTION
# Formats: "124 OZ", "41GAL", "482.6OZ", "230 CT", "612 CT", "135LB"
_PACK_SIZE_COL_RE = re.compile(
    r'^\d+\s*(?:CS|S)\s+'                             # QTY + unit (consumed, not captured)
    r'(\d+\.?\d*\s*(?:[O0]Z|LB|GAL|CT|LTR|#|FL\s*[O0]Z))'  # PACK SIZE (OCR reads O as 0)
    r'\s+',                                             # space before description
    re.IGNORECASE,
)
# Handle ONLY-prefixed lines: "1S ONLY1LB ...", "1S ONLY 4.5LB ...",
# or standalone "ONLY1 GAL AREZCLS...", "ONLY5 LB SYS REL HONEY..."
_PACK_SIZE_ONLY_RE = re.compile(
    r'^(?:\d+\s*S\s+)?ONLY\s*(\d+\.?\d*\s*(?:LB|OZ|GAL|CT|#))',
    re.IGNORECASE,
)


# Sysco catch-weight / variable-weight patterns
# These indicate the item is priced per-pound, with weight in the pack column.
# Formats:
#   "42.5 LE..." or "42.5 LB..." — actual shipped weight (42.5 lbs)
#   "110#AVG..." — 1×10# average weight
#   "116#AVG..." — 1×16# average weight
#   "86-9#AV..." — 8 pieces, 6-9# average each
#   "27-9#AV..." — 2 pieces, 7-9# average each
def _extract_catch_weight(text: str) -> dict | None:
    """
    Extract catch-weight / variable-weight info from a Sysco description line.
    Returns {"weight_lbs": float, "is_catch_weight": True} or None.

    Formats:
      "42.5 LE..."  or "42.5 LB..."  → 42.5 lbs shipped weight
      "25 LB ..."                     → 25 lbs
      "115LB ..."                     → 1×15LB (first digit is qty, rest is weight)
      "110 LB ..."                    → 1×10LB
      "110#AVGPORTPRD SALMON..."      → 1×10# average weight
      "116#AVGBELGIO CHEESE..."       → 1×16# average
      "86-9#AVBCH PORK BUTT..."       → 8 pieces, 6-9# avg each
      "27-9#AVBTRBALL TURKEY..."      → 2 pieces, 7-9# avg each
    """
    # Merged qty+weight (no space between digits and LB): "115LB" = 1×15LB
    # First digit is case count (usually 1), rest is weight per case
    m = re.match(r'^(\d)(\d+\.?\d*)(?:LB|LE)\s', text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        weight = float(m.group(2))
        return {"weight_lbs": count * weight, "is_catch_weight": True}

    # Direct weight with space: "42.5 LE..." or "25 LB ..."
    # Only match as direct weight if ≤ 50 lbs (larger values are likely
    # merged qty+weight that the previous regex missed, e.g. "110 LB")
    m = re.match(r'^(\d+\.?\d*)\s+(?:LE|LB)\s*[A-Z]', text, re.IGNORECASE)
    if m:
        wt = float(m.group(1))
        if wt <= 50:
            return {"weight_lbs": wt, "is_catch_weight": True}
        # >50: try splitting as qty+weight (e.g. "110" → 1×10)
        wt_str = m.group(1)
        if len(wt_str) >= 2:
            qty = int(wt_str[0])
            per = float(wt_str[1:])
            if qty <= 3 and per <= 50:
                return {"weight_lbs": qty * per, "is_catch_weight": True}
        return {"weight_lbs": wt, "is_catch_weight": True}

    # Average weight: "110#AVGPORTPRD..." = 1 piece × 10# avg
    # The format is: first digit = count, remaining digits = weight, then #AVG or #AV
    m = re.match(r'^(\d)(\d+\.?\d*)\s*#\s*AV', text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        per_piece = float(m.group(2))
        return {"weight_lbs": count * per_piece, "is_catch_weight": True}

    # Range average: "86-9#AV..." = 8 pieces, 6-9# avg each
    m = re.match(r'^(\d)(\d)-(\d+)\s*#\s*AV', text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        low = int(m.group(2))
        high = int(m.group(3))
        avg = (low + high) / 2
        return {"weight_lbs": count * avg, "is_catch_weight": True}

    return None


def _extract_pack_size(text: str) -> str:
    """
    Extract the PACK SIZE column value from a Sysco invoice line.
    This is the column between QTY and DESCRIPTION that shows what's
    in each case (e.g. "124 OZ", "41GAL", "612 CT").

    Also handles OCR-mangled formats where count+size run together:
      "482.60Z"  → "48/2.6OZ"  (48 cups × 2.6oz)
      "203.80Z"  → "20/3.8OZ"  (20 packs × 3.8oz)
      "24200Z"   → "24/20OZ"   (24 bottles × 20oz)

    Returns the pack size uppercased, or "" if not found.
    """
    # Standard column format: "2 CS 124 OZ DESCRIPTION"
    m = _PACK_SIZE_COL_RE.match(text)
    if m:
        return m.group(1).strip().upper()

    # ONLY-prefixed: "ONLY1LB", "ONLY 4.5LB", "ONLY1#", "ONLY1 GAL"
    m = _PACK_SIZE_ONLY_RE.match(text)
    if m:
        return m.group(1).strip().upper()

    # Mangled OCR: digits+unit merged at start of line — e.g. "24200Z DESCRIPTION"
    # Extract the raw number+unit and let _normalize_pack_size split it correctly
    m = re.match(
        r'^(\d+\.?\d*(?:[O0]Z|LB|GAL|CT))\s+[A-Z]',
        text, re.IGNORECASE
    )
    if m:
        raw = m.group(1).strip().upper()
        return _normalize_pack_size(raw)

    return ""


def _normalize_pack_size(pack: str) -> str:
    """
    Normalize a raw pack size string. Handles Sysco's merged qty+size format
    where the OCR runs count and size together:
      "120 LB"   → "1/20LB"    (1 case × 20 lbs — e.g. black beans)
      "210 LB"   → "2/10LB"    (2 cases × 10 lbs)
      "123LB"    → "1/23LB"    (1 case × 23 lbs)
      "124 OZ"   → "12/4OZ"    (12 cups × 4 oz — e.g. yogurt)
      "434 OZ"   → "4/34OZ"    (4 bags × 34 oz — e.g. cereal)
      "2416 OZ"  → "24/16OZ"   (24 cans × 16 oz — e.g. Arizona tea)
      "605.3OZ"  → "60/5.3OZ"  (60 patties × 5.3 oz — Burgers, Phase 2a fix)
      "122.38OZ" → "12/2.38OZ" (12 × 2.38 oz — Pringles)
      "120.5OZ"  → "12/0.5OZ"  (12 × 0.5 oz mini packs — Pretzels)
      "230 CT"   → "2/30CT"    ... or leave as "230 CT"?
    """
    if not pack:
        return pack

    # Common Sysco pack counts (units per case) — used to split merged PACK+SIZE.
    # Phase 2a (2026-05-02): expanded to match case_size_decoder.COMMON_PACK_COUNTS
    # so Burgers 60/5.3OZ + similar large-pack items decompose correctly. Order
    # matters — longer prefixes first (120 before 12 etc.) so the right pack
    # count wins for ambiguous strings like "120.5".
    _COMMON_PACKS = [288, 240, 200, 192, 180, 160, 150, 144, 120, 100, 96, 80,
                     72, 60, 50, 48, 40, 36, 32, 30, 24, 20, 18, 16, 15, 12, 10,
                     8, 6, 5, 4, 3, 2, 1]

    # Check for LB packs that need splitting (> 50 lbs unlikely as single unit)
    m = re.match(r'^(\d+)\s*LB$', pack, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if val > 50:
            val_str = str(val)
            qty = int(val_str[0])
            per = int(val_str[1:])
            if qty <= 3 and per > 0:
                return f"{qty}/{per}LB"

    # Normalize 0Z → OZ (OCR misreads letter O as zero)
    pack = re.sub(r'(\d)0Z$', r'\1OZ', pack)

    # Check for OZ packs that need splitting (merged PACK+SIZE across column line).
    # Phase 2a: regex tolerates decimal sizes ("605.3OZ", "122.38OZ").
    # Integer cases keep existing behavior; decimal cases use a separate
    # int-part split so the decimal lands with the size.
    m = re.match(r'^(\d+(?:\.\d+)?)\s*OZ$', pack, re.IGNORECASE)
    if m:
        val_str = m.group(1)
        if '.' in val_str:
            # Decimal — split integer part as count, attach decimal to size.
            int_part, _, dec_part = val_str.partition('.')
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if int_part.startswith(pc_str) and len(int_part) > len(pc_str):
                    size_str = f"{int_part[len(pc_str):]}.{dec_part}"
                    try:
                        size = float(size_str)
                    except ValueError:
                        continue
                    if 0.5 <= size <= 64:
                        return f"{pack_count}/{size_str}OZ"
            # No split worked — return unchanged (single-unit decimal case)
            return pack
        elif len(val_str) >= 3:
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if val_str.startswith(pc_str) and len(val_str) > len(pc_str):
                    size = int(val_str[len(pc_str):])
                    if 1 <= size <= 64:
                        return f"{pack_count}/{size}OZ"

    # Check for CT packs: "230 CT" = PACK(2) + SIZE(30 CT), "612 CT" = PACK(6) + SIZE(12 CT)
    m = re.match(r'^(\d+)\s*CT$', pack, re.IGNORECASE)
    if m:
        val_str = m.group(1)
        if len(val_str) >= 3:
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if val_str.startswith(pc_str) and len(val_str) > len(pc_str):
                    size = int(val_str[len(pc_str):])
                    if 1 <= size <= 100:
                        return f"{pack_count}/{size}CT"

    # Check for GAL packs: "41GAL" = PACK(4) + SIZE(1 GAL)
    m = re.match(r'^(\d+)\s*GAL$', pack, re.IGNORECASE)
    if m:
        val_str = m.group(1)
        if len(val_str) >= 2:
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if val_str.startswith(pc_str) and len(val_str) > len(pc_str):
                    size = val_str[len(pc_str):]
                    if size and int(size) <= 5:
                        return f"{pack_count}/{size}GAL"

    # Check for PT packs (half-pints are common Sysco berry/produce):
    # "121PT" = PACK(12) + SIZE(1 PT), "62PT" = PACK(6) + SIZE(2 PT).
    m = re.match(r'^(\d+)\s*PT$', pack, re.IGNORECASE)
    if m:
        val_str = m.group(1)
        if len(val_str) >= 2:
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if val_str.startswith(pc_str) and len(val_str) > len(pc_str):
                    size = val_str[len(pc_str):]
                    if size and 1 <= int(size) <= 32:
                        return f"{pack_count}/{size}PT"

    # Check for DZ packs (eggs mostly): "115DZ" = PACK(1) + SIZE(15 DZ),
    # "130DZ" = PACK(1) + SIZE(30 DZ).
    m = re.match(r'^(\d+)\s*DZ$', pack, re.IGNORECASE)
    if m:
        val_str = m.group(1)
        if len(val_str) >= 2:
            for pack_count in _COMMON_PACKS:
                pc_str = str(pack_count)
                if val_str.startswith(pc_str) and len(val_str) > len(pc_str):
                    size = val_str[len(pc_str):]
                    if size and 1 <= int(size) <= 48:
                        return f"{pack_count}/{size}DZ"

    return pack


# ── Structured pack-size decomposition (Phase 2a, 2026-05-02) ──────────────
#
# After _normalize_pack_size produces "60/5.3OZ" or "12/4OZ", we want to
# emit the structured tuple (case_pack_count, case_pack_unit_size,
# case_pack_unit_uom) into each item dict so db_write can populate the
# new ILI columns. case_total_weight_lb is derived where the unit
# converts cleanly to lb.
#
# Bare "NUNIT" forms (e.g. "50LB" = 1 case × 50 lb, "12CT" = 1 case
# of 12 ct) are also decomposable as count=1.

_NORMALIZED_PACK_RE = re.compile(
    r'^(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*'
    r'(LB|OZ|GAL|CT|LTR|L|PT|DZ|QT|EA|CAN|FL_OZ|FLOZ|KG|G|ML)\s*$',
    re.IGNORECASE,
)

# Bare "NUNIT" — "50LB" / "12CT" / "1GAL"
_BARE_PACK_RE = re.compile(
    r'^(\d+(?:\.\d+)?)\s*'
    r'(LB|OZ|GAL|CT|LTR|L|PT|DZ|QT|EA|KG|G|ML)\s*$',
    re.IGNORECASE,
)

# Conversion factors to LB for case_total_weight_lb derivation. Count
# units (CT, EA, DZ) intentionally excluded — total weight isn't
# meaningful without per-unit weight from the canonical product side.
_PACK_UOM_TO_LB: dict[str, float] = {
    'LB':  1.0,
    'OZ':  1.0 / 16,
    'KG':  2.20462,
    'G':   0.00220462,
    'GAL': 8.345,
    'QT':  2.086,
    'PT':  1.043,
    'L':   2.205,
    'LTR': 2.205,
    'ML':  0.002205,
    'FL_OZ': 0.0651,
    'FLOZ':  0.0651,
}


def _structured_pack_from_case_size(case_size_str: str | None) -> dict:
    """Decompose a normalized case_size string into structured fields.

    Returns {} when not decomposable. Otherwise returns a dict with:
      case_pack_count     int
      case_pack_unit_size str (kept as string to preserve decimal precision)
      case_pack_unit_uom  str (uppercased, normalized)
      case_total_weight_lb float (only when unit converts to lb)

    Examples:
      "60/5.3OZ"  → {count: 60, size: "5.3", uom: "OZ", total_lb: 19.875}
      "12/4OZ"    → {count: 12, size: "4",   uom: "OZ", total_lb: 3.0}
      "4/1GAL"    → {count: 4,  size: "1",   uom: "GAL", total_lb: 33.38}
      "50LB"      → {count: 1,  size: "50",  uom: "LB", total_lb: 50.0}
      "12CT"      → {count: 1,  size: "12",  uom: "CT"}    (no total_lb)
      "1"         → {}                                      (bare qty, ambiguous)
      "10.0LB"    → {count: 1,  size: "10.0", uom: "LB", total_lb: 10.0}

    Threaded into _parse_sysco + match_sysco_spatial item dicts so db_write
    populates ILI.case_pack_count / case_pack_unit_size / case_pack_unit_uom
    / case_total_weight_lb columns.
    """
    if not case_size_str:
        return {}
    s = case_size_str.strip()
    if not s:
        return {}

    # First try N/M format (Burgers 60/5.3OZ, butter 36/1LB, etc.)
    m = _NORMALIZED_PACK_RE.match(s)
    if m:
        count = int(m.group(1))
        size_str = m.group(2)
        uom = m.group(3).upper().replace('FLOZ', 'FL_OZ')
        out = {
            'case_pack_count': count,
            'case_pack_unit_size': size_str,
            'case_pack_unit_uom': uom,
        }
        factor = _PACK_UOM_TO_LB.get(uom)
        if factor is not None:
            try:
                total_lb = float(size_str) * count * factor
                out['case_total_weight_lb'] = round(total_lb, 3)
            except ValueError:
                pass
        return out

    # Fall back to bare "NUNIT" form ("50LB", "12CT") — count = 1
    m = _BARE_PACK_RE.match(s)
    if m:
        size_str = m.group(1)
        uom = m.group(2).upper()
        out = {
            'case_pack_count': 1,
            'case_pack_unit_size': size_str,
            'case_pack_unit_uom': uom,
        }
        factor = _PACK_UOM_TO_LB.get(uom)
        if factor is not None:
            try:
                total_lb = float(size_str) * factor
                out['case_total_weight_lb'] = round(total_lb, 3)
            except ValueError:
                pass
        return out

    return {}


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
    # Remove leading stray single/double characters from OCR column bleed
    # e.g. "Z NABISCO...", "D SYS CLS...", "NS05C CHOBANI..."
    text = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Z]{3,})', '', text).strip()
    # No-space OCR bleed: only strip Z (rare as word-start) attached to 5+ char words
    text = re.sub(r'^Z(?=[A-Z]{5,})', '', text).strip()
    # Remove trailing long barcodes (12+ digits)
    text = re.sub(r'\s+\d{12,}\s*$', '', text).strip()
    # Remove trailing short reference codes (4-6 digits)
    text = re.sub(r'(\s+\d{4,6}){1,2}\s*$', '', text).strip()
    return text


def extract_sysco_metadata(text: str) -> dict:
    """
    Extract invoice-level metadata from a Sysco invoice page.
    Used for multi-page grouping and delivery date detection.

    Returns:
        invoice_number: str or None
        delivery_date: str or None (M/DD/YY format)
        manifest: str or None
        page: int or None
        is_last_page: bool
    """
    lines = [l.strip() for l in text.splitlines()]
    meta = {
        "invoice_number": None,
        "delivery_date": None,
        "manifest": None,
        "page": None,
        "is_last_page": "LAST PAGE" in text.upper(),
    }

    for i, line in enumerate(lines):
        # DELV. DATE → next line has the date
        if re.match(r'^DELV\.?\s*DATE', line, re.IGNORECASE) and meta["delivery_date"] is None:
            if i + 1 < len(lines):
                dm = re.match(r'^(\d{1,2}/\d{1,2}/\d{2,4})', lines[i + 1].strip())
                if dm:
                    meta["delivery_date"] = dm.group(1)

        # INVOICE NUMBER → next line has the number
        if re.match(r'^INVOICE\s+NUMBER', line, re.IGNORECASE) and meta["invoice_number"] is None:
            if i + 1 < len(lines):
                nm = re.match(r'^(\d{6,})', lines[i + 1].strip())
                if nm:
                    meta["invoice_number"] = nm.group(1)

        # MANIFEST# inline
        m = re.search(r'MANIFEST#?\s*(\d+)', line, re.IGNORECASE)
        if m and meta["manifest"] is None:
            meta["manifest"] = m.group(1)

    return meta


def _parse_sysco(text: str) -> list[dict]:
    """
    Sysco invoices are printed in columns.  The OCR reads them top-to-bottom,
    so descriptions appear in order BEFORE the matching item-code/price anchors.

    When the OCR reads left-column first (descriptions) then right-column
    (codes+prices), all the anchors can cluster together 20+ lines after
    the descriptions.  A simple backward walk from each anchor to the previous
    anchor misses descriptions that are further back.

    Strategy:
    1. Collect all price anchors (7-digit item-code + price).
    2. Collect all description lines (with pack size and line index).
    3. For anchors with inline descriptions, use those directly.
    4. For remaining anchors, zip with unclaimed descriptions in order.
    """
    lines = [l.strip() for l in text.splitlines()]

    # ── Pass 0: find section boundaries + GROUP TOTAL values ──────────────
    # GROUP TOTAL amounts appear as standalone numbers near the section
    # boundary. We identify these so they're excluded from item price data.
    sections = []       # list of {"name", "start_line", "end_line", "total"}
    group_total_lines = set()  # lines that are GROUP TOTAL markers or amounts
    current_section = None

    for i, line in enumerate(lines):
        if _SECTION_HEADER.match(line):
            if current_section:
                current_section["end_line"] = i - 1
                sections.append(current_section)
            current_section = {"name": line, "start_line": i, "end_line": None, "total": None}
        if re.search(r'GROUP\s*TOTAL', line, re.IGNORECASE):
            group_total_lines.add(i)
            # The group total VALUE appears as standalone numbers RIGHT near
            # the "GROUP TOTAL" text — typically within 1-2 lines. Walking
            # further ahead catches first-item prices of the NEXT section,
            # which blocks split-anchor pairing and loses items.
            # Use a tight window (1 before, 2 after) and stop at the first
            # non-matching line.
            for j in range(max(0, i - 1), min(len(lines), i + 3)):
                l = lines[j].strip()
                if re.match(r'^(\d+\.\d{2})\s*\*?$', l):
                    group_total_lines.add(j)

    if current_section:
        current_section["end_line"] = len(lines) - 1
        sections.append(current_section)

    # ── Pass 1: locate every price anchor ──────────────────────────────────
    # Primary: CODE PRICE on same line (e.g. "7250644 52.99")
    # Secondary: CODE on one line, PRICE on the next (OCR split)
    # Skip lines that are GROUP TOTAL amounts.
    anchors = []   # (line_index, item_code, price, prefix_text)
    anchor_lines = set()
    used_as_split_price = set()

    for i, line in enumerate(lines):
        if i in group_total_lines:
            continue  # skip GROUP TOTAL lines
        m = _PRICE_ANCHOR.search(line)
        if m:
            prefix = line[:m.start()].strip()
            anchors.append((i, m.group(1), float(m.group(2)), prefix))
            anchor_lines.add(i)

    # ── Catch-weight (3-decimal per-lb) anchor pass ───────────────────────
    # Sysco catch-weight (MEATS/POULTRY/SEAFOOD) items print per-lb prices
    # with 3 decimals, which the main _PRICE_ANCHOR (2-decimal-only)
    # rejects. Two OCR layout variants recovered here:
    #
    # Column-dump (code on its own line, prefix empty):
    #   Line N-1: 65.200              weight in lbs (3-decimal bare)
    #   Line N:   3124662 12.650      code + per-lb
    #   Line N+1: 824.78              extended (2-decimal bare)
    #
    # Inline-description (code+per-lb at END of a description line,
    # weight on next line, no adjacent extended):
    #   Line N:   BCH BLK PORK TENDER 1.5 DN FRESH 25140 5812534 3.299
    #   Line N+1: 15.100              weight in lbs
    #   Line N+2: T/WT= (or other non-numeric)
    # In this variant the extended lands further downstream in a price
    # stack; we derive extended = weight × per_lb from the adjacent weight.
    #
    # Either variant: cross-validate with weight before extracting so we
    # don't fabricate items from coincidental digit patterns.
    # Code+per-lb must be at end of line (^...$ or trailing position).
    _CATCH_WEIGHT_ANCHOR = re.compile(r'(?:^|\s)(\d{6,7})\s+(\d+\.\d{3,4})\s*$')
    _BARE_2DEC = re.compile(r'^(\d+\.\d{2})\s*$')
    _BARE_3DEC = re.compile(r'^(\d+\.\d{3,4})\s*$')

    catch_weight_info: dict[int, dict] = {}   # line_idx → {per_lb, weight_lbs, extended}
    for i, line in enumerate(lines):
        if i in anchor_lines or i in group_total_lines:
            continue
        m = _CATCH_WEIGHT_ANCHOR.search(line.strip())
        if not m:
            continue
        code = m.group(1)
        per_lb = float(m.group(2))

        # Extract prefix (inline description text before the code+per-lb pair)
        prefix = line.strip()[:m.start()].rstrip()

        # Scan nearby lines for candidate extended (2-decimal) and weight
        # (3-4 decimal). Collect BOTH if present — we use them as
        # corroborating signals, not alternatives.
        ext_candidate = None
        ext_line_idx = None
        for j in range(i + 1, min(i + 3, len(lines))):
            if j in group_total_lines:
                continue
            em = _BARE_2DEC.match(lines[j].strip())
            if em:
                ext_candidate = float(em.group(1))
                ext_line_idx = j
                break

        wt_candidate = None
        search_range = list(range(max(0, i - 2), i)) + list(range(i + 1, min(i + 4, len(lines))))
        for j in search_range:
            if j in (ext_line_idx, i) or j in group_total_lines:
                continue
            wm = _BARE_3DEC.match(lines[j].strip())
            if wm:
                c = float(wm.group(1))
                if 0.1 <= c <= 1000:
                    wt_candidate = c
                    break

        # Decision rules — extract only when the data corroborates:
        #   (both present + reconcile)                → accept
        #   (extended only, plausible vs per_lb)      → accept, derive weight
        #   (weight only, no extended candidate)      → accept, derive extended
        #   (both present but conflict)               → REJECT (safety)
        #   (neither)                                 → REJECT
        extended = None
        weight_lbs = None
        if ext_candidate is not None and wt_candidate is not None:
            if abs(wt_candidate * per_lb - ext_candidate) <= 1.0:
                extended = ext_candidate
                weight_lbs = wt_candidate
            # else: reconciliation failed — skip rather than guess
        elif ext_candidate is not None and wt_candidate is None:
            if ext_candidate > per_lb * 1.5:  # extended must exceed per-lb by plausible weight
                extended = ext_candidate
                weight_lbs = round(extended / per_lb, 3) if per_lb > 0 else None
        elif wt_candidate is not None and ext_candidate is None:
            weight_lbs = wt_candidate
            extended = round(weight_lbs * per_lb, 2)

        if extended is None:
            continue

        anchors.append((i, code, extended, prefix))
        anchor_lines.add(i)
        catch_weight_info[i] = {
            "per_lb": per_lb,
            "weight_lbs": weight_lbs,
            "extended": extended,
        }

    # Now find split anchors: standalone item-code line paired with a nearby price.
    # Codes may be 6-8 digits (OCR sometimes merges a leading digit with the code,
    # e.g. "12273758" is really item 2273758). A price must be a line whose only
    # content is a decimal number; inline "CODE PRICE" lines don't count as prices
    # for this purpose (they're their own anchor).
    found_codes = {a[1] for a in anchors}
    # Sysco SUPC codes are 7 digits. Accept 8-digit when OCR has merged a leading
    # digit from an adjacent column (normalize by dropping the leading digit).
    # Also accept a trailing 7-digit code on a line that begins with a
    # description/barcode/brand prefix — Sysco OCR often emits lines like
    # "LE CHIP POTATO SOUR CRM & ON 3800084555 1978309" or "GB100-SYS 5793963"
    # where the SUPC sits at the end of a mixed-content line.
    _STANDALONE_CODE_RE = re.compile(r'^(?:\d{8,}\s+)?(\d{7,8})\s*$')
    _TRAILING_CODE_RE = re.compile(
        r'(?:^|\s)(?:[A-Z0-9\-./]{3,}\s+)?(\d{7})\s*$'
    )
    _STANDALONE_PRICE_RE = re.compile(r'^(\d+\.\d{2})(?:\s+\d+\.\d{2})?\s*$')

    # Load code_map once so we only accept orphan codes that actually resolve
    # to a real product. This filters out barcode fragments like 401490 / 911123
    # that happen to be 7 digits alone.
    try:
        from mapper import load_mappings as _load_for_split_anchor
        _split_code_map = _load_for_split_anchor().get("code_map", {})
    except Exception:
        _split_code_map = {}

    def _normalize_code(raw_code: str) -> str:
        """Trim OCR-merged leading digit from 8-digit codes (Sysco codes are 7)."""
        if len(raw_code) == 8 and raw_code[0] in '12':
            return raw_code[1:]
        return raw_code

    # Phase 1: collect orphan standalone-code lines and standalone-price lines.
    # For codes NOT in the known code_map, tag them so pairing applies
    # stricter proximity (2 lines). Real unmapped SUPCs usually have a price
    # very close; barcode fragments in OCR noise rarely align that tightly.
    # Orphan-collection footer cutoff. Sysco invoices have a summary
    # region after the items where stray 7-digit OCR artifacts look like
    # SUPCs (e.g. 3974320 appearing under 'ORDER SUMMARY' or after 'CHGS
    # FOR FUEL SURCHARGE'). Self-contained pre-scan — can't rely on the
    # later block_end calc, which depends on anchors this loop populates.
    #
    # Only these markers (no 'INVOICE ADJUSTMENTS' which is a column
    # HEADER that appears early). And the cutoff must be at least 30
    # lines in — early hits are usually legalese/headers, not the
    # post-item footer.
    _EARLY_FOOTER = re.compile(
        r'^\s*(ORDER\s+SUMMARY|MISC\s+CHARGES?|CHGS?\s+FOR\s+FUEL)',
        re.IGNORECASE,
    )
    orphan_cutoff = len(lines)
    for j, line in enumerate(lines):
        if j < 30:
            continue
        if _EARLY_FOOTER.match(line):
            orphan_cutoff = j
            break

    orphan_code_lines = []   # (line_idx, normalized_code, raw_code, is_known, inline_prefix)
    standalone_price_lines = []  # (line_idx, price)
    for i, line in enumerate(lines):
        if i in anchor_lines or i in group_total_lines or i in used_as_split_price:
            continue
        if i >= orphan_cutoff:
            continue
        stripped = line.strip()
        cm = _STANDALONE_CODE_RE.match(stripped)
        raw_code = None
        inline_prefix = ''
        if cm:
            raw_code = cm.group(1)
        else:
            # Line that ENDS with a 7-digit SUPC (description+code or brand-prefix+code)
            tcm = _TRAILING_CODE_RE.search(stripped)
            if tcm and re.search(r'[A-Za-z]', stripped):  # require non-digit content before
                raw_code = tcm.group(1)
                # Capture the text BEFORE the code as the inline prefix — this
                # is the item's description when the OCR emits the pattern
                # 'SANITIZER OASIS 146 MULTI QU 6100536' (description + trailing
                # code). Without this, the orphan-pairing path feeds an empty
                # prefix to Step B, which then pulls a random unclaimed
                # description from the iterator and mis-pairs.
                inline_prefix = stripped[:tcm.start(1)].rstrip()
        if raw_code:
            norm = _normalize_code(raw_code)
            if norm in found_codes:
                continue
            # Known codes get wide pairing (6 lines). Unknown codes (not in
            # code_map) are accepted too but must pair with a price within
            # 2 lines — recovers legitimate unmapped SUPCs while filtering
            # out barcode fragments and stray numeric runs in OCR text.
            is_known = bool(_split_code_map and norm in _split_code_map) \
                       or not _split_code_map  # empty map = test fixture, accept all
            orphan_code_lines.append((i, norm, raw_code, is_known, inline_prefix))
            continue
        pm = _STANDALONE_PRICE_RE.match(stripped)
        if pm:
            price = float(pm.group(1))
            if 1.0 <= price <= 1000:
                standalone_price_lines.append((i, price))

    # Phase 2: pair orphan codes with standalone prices.
    # Column-dump layouts stack N codes together, then N prices together, so
    # a code at line 118 may pair with a price at line 124 (6 lines ahead).
    # Both known and unknown codes use a 6-line forward window. Only known
    # codes additionally accept backward-adjacent prices — that direction
    # carries more barcode false-positive risk, and the _split_code_map
    # membership acts as a safety gate.
    used_prices = set()
    for i, norm_code, raw_code, is_known, inline_prefix in orphan_code_lines:
        best = None
        # Forward window: 6 lines for everyone (catches column-dump blocks)
        for pi, (pline, price) in enumerate(standalone_price_lines):
            if pi in used_prices:
                continue
            if pline > i and pline - i <= 6:
                best = pi
                break
        # Known codes also accept backward-adjacent prices
        if best is None and is_known:
            best_dist = 999
            for pi, (pline, price) in enumerate(standalone_price_lines):
                if pi in used_prices:
                    continue
                dist = abs(pline - i)
                if dist <= 6 and dist < best_dist:
                    best_dist = dist
                    best = pi
        if best is not None:
            pline, price = standalone_price_lines[best]
            # Carry inline_prefix into the anchor tuple so Step B's inline-desc
            # path picks it up (the description was RIGHT THERE on the code
            # line, no need to consume a random unclaimed desc from the queue).
            anchors.append((i, norm_code, price, inline_prefix))
            anchor_lines.add(i)
            used_as_split_price.add(pline)
            used_prices.add(best)
            found_codes.add(norm_code)

    # Sort anchors by line position
    anchors.sort(key=lambda a: a[0])

    # ── Find item block boundaries ────────────────────────────────────────
    # Strategy: find the block that contains the actual product data.
    # 1. Best: first section header (**** DAIRY ****, **** FROZEN ****)
    # 2. Fallback: last column header line ("EXTENDED PRICE", "PRICE" at end)
    #    which appears right before items start
    # 3. Last resort: first anchor line minus a generous lookback window
    block_start = 0
    block_end = len(lines)

    # Try section headers first
    for i, line in enumerate(lines):
        if _SECTION_HEADER.match(line.strip()):
            block_start = i
            break

    # Fallback: find "EXTENDED" + "PRICE" column headers (appear right before items)
    if block_start == 0:
        for i, line in enumerate(lines):
            if line.strip().upper() == 'EXTENDED' and i + 1 < len(lines):
                if lines[i + 1].strip().upper() == 'PRICE':
                    block_start = i + 2
                    break
        # Also try "PRICE" as last header followed by non-header content
        if block_start == 0:
            for i, line in enumerate(lines):
                if (line.strip().upper() in ('PRICE', 'EXTENDED PRICE')
                        and i > 10):  # must be well into the page, not the first "PRICE"
                    block_start = i + 1
                    break

    # Last resort: use first anchor minus lookback window
    if block_start == 0 and anchors:
        block_start = max(0, anchors[0][0] - 30)

    _FOOTER_RE = re.compile(
        r'^(EQUAL\s+OPPORTUNITY|REMIT\s+TO|CONT\.\s+ON'
        r'|IMPORTANT\s+PACA|DRIVER.S\s+SIGN|PAYABLE\s+ON'
        r'|NO\.\s+PCS|CUST\.\s+SIGNED|CASES\s+SPLIT'
        r'|SYSCO\s+PHILADELPHIA|OPEN:\s*\d|SUB\s*$'
        r'|P\.O\.\s*BOX'
        r'|AUTHORIZED\s+BY\s+SECTION'
        r'|RETAINS\s+A\s+TRUST'
        r'|RECEIVABLES\s+OR\s+PROCEEDS'
        r'|REPRESENTATIVE\s+CAPACITY'
        r'|RESPECT\s+TO\s+ANY\s+DISPUTE'
        r'|PERISHABLE\s+AGRICULTURAL'
        r'|STATUTORY\s+TRUST'
        r'|INVENTORIES\s+OF\s+FOOD'
        r')',
        re.IGNORECASE,
    )
    for i in range(block_start, len(lines)):
        if _FOOTER_RE.match(lines[i]):
            block_end = i
            break

    # ── Pass 2: collect all description lines (within item block only) ────
    desc_entries = []  # list of { line, description, case_size }
    for i in range(block_start, block_end):
        line = lines[i]
        if i in anchor_lines:
            continue
        if not _is_description(line):
            continue
        # Extract catch weight BEFORE cleaning (variable-weight/per-lb items)
        # Only applies to MEATS, POULTRY, SEAFOOD sections — not dry goods
        current_section_name = ""
        for sec in sections:
            if sec["start_line"] <= i <= (sec["end_line"] or len(lines)):
                current_section_name = sec["name"].upper()
                break
        is_protein_section = any(k in current_section_name
                                 for k in ["MEAT", "POULTRY", "SEAFOOD"])
        catch_wt = _extract_catch_weight(line) if is_protein_section else None

        # Extract pack size BEFORE cleaning
        case_size = _extract_pack_size(line)
        if not case_size:
            m = re.match(
                r'^(\d+\.?\d*\s*(?:[O0]Z|LB|GAL|CT|LTR|#))\s+',
                line, re.IGNORECASE
            )
            if m:
                case_size = m.group(1).strip().upper()
        if not case_size:
            case_size = _extract_case_size(line)
        # Check line above for standalone pack size
        if not case_size and i > 0:
            above = lines[i - 1].strip()
            m = re.match(
                r'^(\d+\.?\d*\s*(?:[O0]Z|LB|GAL|CT|LTR|#|FL\s*[O0]Z))\s*(?:\w{0,3})?$',
                above, re.IGNORECASE
            )
            if m:
                case_size = m.group(1).strip().upper()

        # For catch-weight items, the "case_size" is actually the shipped weight
        if catch_wt:
            case_size = f"{catch_wt['weight_lbs']}LB"
        else:
            # Normalize pack size (e.g. "120 LB" → "1/20LB")
            case_size = _normalize_pack_size(case_size)

        desc_entries.append({
            "line": i,
            "description": _clean_description(line),
            "case_size": case_size,
            "catch_weight": catch_wt,
        })

    # ── Load code_map for reliable product identification ────────────────
    try:
        from mapper import load_mappings as _load_code_map
        _mappings = _load_code_map()
        _code_map = _mappings.get("code_map", {})
    except Exception:
        _code_map = {}

    # ── Pass 3: match anchors to descriptions ─────────────────────────────
    # Strategy: known-code-first matching.
    #   1. Anchors with known SUPC codes → use code_map canonical directly.
    #      Remove their OCR descriptions from the pool.
    #   2. Remaining anchors (unknown codes) → match against the remaining
    #      description pool by ordered position.
    # This avoids OCR column-order misalignment for known products and
    # gives unknown codes a cleaner pool to match against.

    items = []
    used_descs = set()
    anchor_matches = [None] * len(anchors)  # (description, case_size) per anchor

    # ── Step A: handle known-code anchors ─────────────────────────────────
    # For each anchor with a code in the code_map, we already know the product.
    # Find and consume the nearest OCR description by LINE PROXIMITY — the
    # item code and its description always appear near each other in the raw
    # text, regardless of column reading order. This is more reliable than
    # fuzzy matching canonical names against OCR descriptions.
    known_anchor_indices = set()

    # First, find where each item code appears in the raw text (not just
    # the anchor line — the code might also appear near the description)
    code_positions = {}  # item_code → list of line positions
    for i, line in enumerate(lines):
        for ai, (_, item_code, _, _) in enumerate(anchors):
            if item_code in line:
                code_positions.setdefault(item_code, []).append(i)

    # ── Step A0: anchor-run ordered pairing ───────────────────────────────
    # Sysco OCR often reads a column of descriptions top-to-bottom, then a
    # column of codes+prices. The result is a RUN of consecutive anchor lines
    # with no descriptions interleaved, preceded by a block of descriptions.
    # Per-anchor proximity matching fails here (every anchor grabs the last
    # desc in the block) and shifts case_sizes by one position.
    #
    # Strategy: detect anchor-runs (2+ consecutive anchors with no descs
    # between them), then pair the run with the N preceding descs in the
    # same section, in line order.
    def _section_of(line_idx: int) -> str:
        for sec in sections:
            end = sec["end_line"] or len(lines)
            if sec["start_line"] <= line_idx <= end:
                return sec["name"]
        return ""

    sorted_known_ais = sorted(
        [ai for ai, (_, code, _, _) in enumerate(anchors) if code in _code_map],
        key=lambda a: anchors[a][0],
    )

    runs: list[list[int]] = []
    current_run: list[int] = []
    prev_line = -10
    for ai in sorted_known_ais:
        line = anchors[ai][0]
        desc_between = any(prev_line < de["line"] < line for de in desc_entries)
        if current_run and not desc_between and line - prev_line <= 6:
            current_run.append(ai)
        else:
            if len(current_run) >= 2:
                runs.append(current_run)
            current_run = [ai]
        prev_line = line
    if len(current_run) >= 2:
        runs.append(current_run)

    for run in runs:
        first_anchor_line = anchors[run[0]][0]
        sec_name = _section_of(first_anchor_line)
        preceding = sorted(
            [di for di, de in enumerate(desc_entries)
             if _section_of(de["line"]) == sec_name
             and de["line"] < first_anchor_line
             and di not in used_descs],
            key=lambda d: desc_entries[d]["line"],
        )
        if len(preceding) < len(run):
            continue
        # Take the last N descs before the run — these are the tightest match
        matching = preceding[-len(run):]
        for idx, ai in enumerate(run):
            di = matching[idx]
            if di in used_descs:
                continue
            used_descs.add(di)
            known_anchor_indices.add(ai)
            de = desc_entries[di]
            # Preserve OCR description instead of substituting the code_map
            # canonical. Canonical flows through via sysco_item_code → mapper
            # → Product FK separately; the raw_description column keeps its
            # audit value (real OCR text) and case_size extraction works
            # against real pack info like "241.5 OZSTACYS CHIP PITA" instead
            # of a short canonical like "Pita".
            anchor_matches[ai] = (de["description"], de["case_size"], de.get("catch_weight"))

    # Precompute nearest-unknown-anchor line for each desc, so Step A's
    # proximity matching can yield descs to unknown anchors that need them
    # more. Known codes already have raw_description from code_map canonical
    # — they only need the desc for case_size / catch_weight info. Unknown
    # codes NEED the desc for raw_description. Without this guard, known
    # codes steal descriptions that should pair with adjacent unknowns,
    # producing '[Sysco #NNN]' placeholders even when the real OCR
    # description sits right next to the unknown code line.
    unknown_anchor_lines = [anchors[ai][0] for ai in range(len(anchors))
                             if anchors[ai][1] not in _code_map]

    def _nearest_unknown_dist(desc_line: int) -> int:
        if not unknown_anchor_lines:
            return 999
        return min(abs(desc_line - ul) for ul in unknown_anchor_lines)

    for ai, (line_idx, item_code, price, prefix) in enumerate(anchors):
        canonical = _code_map.get(item_code)
        if not canonical:
            continue  # unknown code — handle in step B
        if ai in known_anchor_indices:
            continue  # handled in Step A0

        known_anchor_indices.add(ai)

        # Find the nearest unclaimed description to any occurrence of this code.
        # Skip descs that are strictly closer to an unknown code — the unknown
        # needs the desc for raw_description, the known has canonical already.
        code_lines = code_positions.get(item_code, [line_idx])
        best_di = None
        best_dist = 999

        for di, de in enumerate(desc_entries):
            if di in used_descs:
                continue
            unk_dist = _nearest_unknown_dist(de["line"])
            for cl in code_lines:
                dist = abs(de["line"] - cl)
                if unk_dist < dist:
                    continue  # desc belongs to a closer unknown anchor
                if dist < best_dist:
                    best_dist = dist
                    best_di = di

        if best_di is not None and best_dist <= 40:
            # Found nearby OCR description — prefer it over canonical. Real
            # OCR text carries pack info ("241.5 OZSTACYS CHIP PITA") that
            # canonical ("Pita") loses, so case_size extraction works and
            # audits retain what the vendor actually printed. Canonical
            # still flows to Product FK via sysco_item_code downstream.
            used_descs.add(best_di)
            de = desc_entries[best_di]
            anchor_matches[ai] = (de["description"], de["case_size"], de.get("catch_weight"))
        else:
            # No proximity match — stash canonical as a backup raw_description
            # (better than placeholder since the SUPC maps to a known Product).
            # Step A2 still tries ordered-fallback and overrides if it finds a
            # better desc.
            anchor_matches[ai] = (canonical, "", None)

    # ── Step B.1: proximity pass for unknown-code anchors ────────────────
    # Run BEFORE Step A2's ordered fallback. Known anchors already have their
    # canonical from code_map — they only need desc for case_size info (and
    # Product.default_case_size is available as a further fallback downstream).
    # Unknown anchors NEED the desc for raw_description. If ordered fallback
    # for known runs first, it greedily consumes descs that belong to nearby
    # unknown anchors, leaving those unknowns as '[Sysco #NNN]' placeholders
    # even though their description is right next to them in the OCR.
    unknown_anchors = [ai for ai in range(len(anchors)) if ai not in known_anchor_indices]

    # Proximity pass — each unknown anchor tries to claim its nearest
    # unclaimed description within a 20-line window.
    for ai in unknown_anchors:
        line_idx, item_code, price, prefix = anchors[ai]

        # Check inline description first (caller's embedded desc wins)
        inline_desc = _clean_description(prefix)
        if inline_desc and len(inline_desc) >= 5 and re.search(r'[A-Za-z]{3,}', inline_desc):
            cs = _extract_pack_size(prefix) or _extract_case_size(prefix)
            anchor_matches[ai] = (inline_desc, cs, None)
            continue

        best_di = None
        best_dist = 21  # one more than the 20-line window
        for di, de in enumerate(desc_entries):
            if di in used_descs:
                continue
            dist = abs(de["line"] - line_idx)
            if dist < best_dist:
                best_dist = dist
                best_di = di

        if best_di is not None and best_dist <= 20:
            used_descs.add(best_di)
            de = desc_entries[best_di]
            anchor_matches[ai] = (de["description"], de["case_size"], de.get("catch_weight"))

    # ── Step A2: ordered fallback for known-code anchors without a match ──
    # When proximity fails (clustered anchors far from descriptions),
    # consume unclaimed descriptions in order — the OCR reads descriptions
    # top-to-bottom, and anchors top-to-bottom, so the ordering is preserved.
    # Eligible iff the anchor still carries its canonical-name backup rather
    # than a real OCR desc (Step A2 shouldn't override descs Step A found).
    _canonical_set = {_code_map.get(anchors[ai][1]) for ai in known_anchor_indices}
    unmatched_known = [ai for ai in range(len(anchors))
                       if ai in known_anchor_indices
                       and anchor_matches[ai] is not None
                       and anchor_matches[ai][0] in _canonical_set]
    remaining_for_known = [(di, de) for di, de in enumerate(desc_entries)
                           if di not in used_descs]
    remaining_for_known.sort(key=lambda x: x[1]["line"])

    known_desc_iter = iter(remaining_for_known)
    for ai in sorted(unmatched_known, key=lambda x: anchors[x][0]):
        pair = next(known_desc_iter, None)
        if pair:
            di, de = pair
            used_descs.add(di)
            # Use the OCR description (not the placeholder the earlier loop
            # stashed) so raw_description carries real text. Product FK
            # still resolves from sysco_item_code downstream.
            anchor_matches[ai] = (de["description"], de["case_size"], de.get("catch_weight"))

    # ── Step B.2: ordered fallback for unknown anchors still unmatched ────
    remaining_descs = [(di, de) for di, de in enumerate(desc_entries) if di not in used_descs]
    remaining_descs.sort(key=lambda x: x[1]["line"])
    desc_iter = iter(remaining_descs)

    for ai in unknown_anchors:
        if anchor_matches[ai] is not None:
            continue
        pair = next(desc_iter, None)
        if pair:
            di, de = pair
            used_descs.add(di)
            anchor_matches[ai] = (de["description"], de["case_size"], de.get("catch_weight"))

    # Build final items, tagging each with its section
    for ai, (line_idx, item_code, price, prefix) in enumerate(anchors):
        if anchor_matches[ai]:
            ocr_description, case_size, catch_wt = anchor_matches[ai]
        else:
            ocr_description = ""
            case_size = ""
            catch_wt = None

        # raw_description always preserves the OCR text when available.
        # The canonical (product resolution) comes from the mapper's code
        # tier via sysco_item_code — we don't need to bake it into the
        # description. This keeps the original text searchable for audits,
        # enables re-derivation of more-specific canonicals later (e.g.
        # distinguishing Chobani flavors that all share one code_map entry),
        # and lets the semantic audit flag mismatches.
        #
        # Column misalignment risk: when anchor is assigned a wrong-column
        # description, raw_description may not match the product FK set by
        # the code-tier mapper. That's a known minor cost; the code-tier
        # resolution still yields the correct product. Worth it for the
        # audit/recovery capability.
        if ocr_description:
            description = ocr_description
        else:
            description = f"[Sysco #{item_code}]"

        # Find which section this anchor belongs to
        section_name = ""
        for sec in sections:
            if sec["start_line"] <= line_idx <= (sec["end_line"] or len(lines)):
                section_name = sec["name"]
                break
        if not section_name and sections:
            section_name = sections[-1]["name"]

        # Column-dump catch-weight info takes priority when present — the
        # second pass cross-validated weight × per_lb ≈ extended, so it's
        # stronger than description-line regex extraction.
        cw_info = catch_weight_info.get(line_idx)
        if cw_info:
            effective_case_size = f"{cw_info['weight_lbs']}LB"
        else:
            effective_case_size = case_size

        item = {
            "raw_description": description,
            "sysco_item_code": item_code,
            "unit_price":      price,
            "extended_amount": price,  # Sysco prices are per-line totals (qty usually 1)
            "case_size_raw":   effective_case_size,
            "section":         re.sub(r'[*\s]+', ' ', section_name).strip(),
            # Sysco lines are always 1 case per anchor; quantity = 1 by convention.
            "quantity":        1,
            "unit_of_measure": "CASE",
        }

        # Phase 2a: structured pack-size decomposition into ILI columns.
        # Decomposes "60/5.3OZ" → (60, "5.3", "OZ") + total_weight_lb.
        # Empty dict when not decomposable; db_write coerces missing keys to NULL.
        item.update(_structured_pack_from_case_size(effective_case_size))

        # Phase 3 #6: count-per-lb extraction for protein items.
        # SHRIMP 21/25 → (21, 25); BACON 18/22 → (18, 22).
        cpl = _extract_count_per_lb(description)
        if cpl is not None:
            item['count_per_lb_low'] = cpl[0]
            item['count_per_lb_high'] = cpl[1]

        # For catch-weight items, the price on the invoice is the TOTAL
        # (weight × price_per_lb). Override quantity + UoM to the per-lb
        # convention so calc_price_per_lb sees the right shape.
        if cw_info:
            item["unit_of_measure"] = "LB"
            item["price_per_unit"] = cw_info["per_lb"]
            item["quantity"] = cw_info["weight_lbs"]
        elif catch_wt and catch_wt.get("weight_lbs") and catch_wt["weight_lbs"] > 0:
            item["unit_of_measure"] = "LB"
            item["price_per_unit"] = round(price / catch_wt["weight_lbs"], 4)
            item["quantity"] = catch_wt["weight_lbs"]

        items.append(item)

    # ── Extract invoice total ────────────────────────────────────────────
    # Sysco multi-page invoices: the LAST PAGE has the invoice total.
    # Extraction order:
    #   1. "INVOICE TOTAL" label pattern — dedicated labeled total, most
    #      reliable. Handles both "INVOICE TOTAL" on one line and "INVOICE"
    #      / "TOTAL" stacked on adjacent lines.
    #   2. "LAST PAGE" marker — take largest decimal in ±10 line window.
    #   3. GROUP TOTAL sum — for pages without either marker.
    invoice_total = None

    # Method 1: "INVOICE TOTAL" label — LAST occurrence in the page.
    # Sysco OCR often stacks multiple label pairs (TAX TOTAL, INVOICE
    # TOTAL) followed by a block of values. The value ordering matches
    # the label ordering, so "INVOICE TOTAL" (2nd label pair) pairs with
    # the 2nd value — which is the LARGEST value since invoice total
    # exceeds tax total. Take the largest decimal in the 8-line window
    # after the label to get the right one regardless of the number of
    # preceding stacked labels (TAX TOTAL, DISCOUNT TOTAL, etc.).
    label_positions = []
    for i, line in enumerate(lines):
        stripped = line.strip().upper()
        if re.match(r'^INVOICE\s+TOTAL\s*$', stripped):
            label_positions.append((i, i))
        elif stripped == 'INVOICE' and i + 1 < len(lines) and \
                lines[i + 1].strip().upper() == 'TOTAL':
            label_positions.append((i, i + 1))
    if label_positions:
        _, search_after = label_positions[-1]
        nums = []
        for j in range(search_after + 1, min(search_after + 9, len(lines))):
            m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
            if m:
                val = float(m.group(1).replace(",", ""))
                if val > 1.0:
                    nums.append(val)
        if nums:
            invoice_total = max(nums)
            print(f"  [✓] Sysco invoice total from INVOICE TOTAL label: ${invoice_total:.2f}")

    # Method 2: "LAST PAGE" fallback. Numbers may appear before or after
    # the marker; search both directions, take the largest.
    if invoice_total is None:
        for i, line in enumerate(lines):
            if re.match(r'^\s*LAST PAGE\s*$', line, re.IGNORECASE):
                nums = []
                for j in range(max(0, i - 10), i):
                    m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                    if m:
                        val = float(m.group(1).replace(",", ""))
                        if val > 1.0:
                            nums.append(val)
                for j in range(i + 1, min(i + 10, len(lines))):
                    m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                    if m:
                        val = float(m.group(1).replace(",", ""))
                        if val > 1.0:
                            nums.append(val)
                if nums:
                    invoice_total = max(nums)
                    print(f"  [✓] Sysco invoice total from LAST PAGE: ${invoice_total:.2f}")
                break

    # Method 2: Fall back to GROUP TOTAL sums (partial pages)
    if invoice_total is None:
        group_totals = []
        for i, line in enumerate(lines):
            if re.match(r'^\s*GROUP TOTAL', line):
                last_num = None
                for j in range(i + 1, min(i + 15, len(lines))):
                    if re.search(r'\*{3,}', lines[j]) and 'GROUP' not in lines[j]:
                        break
                    m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                    if m:
                        last_num = float(m.group(1).replace(",", ""))
                if last_num is not None:
                    group_totals.append(last_num)
        if group_totals:
            invoice_total = round(sum(group_totals), 2)
            print(f"  [~] Sysco partial total from GROUP TOTALs: ${invoice_total:.2f} "
                  f"(not last page — may be incomplete)")

    items_total = round(sum(it.get("extended_amount", 0) or 0 for it in items), 2)
    if invoice_total is not None:
        diff = abs(invoice_total - items_total)
        if diff > 0.50:
            print(f"  [!] Sysco total vs items gap: "
                  f"total=${invoice_total:.2f}, items=${items_total:.2f}, "
                  f"gap=${diff:.2f}")

    return items, invoice_total


# ---------------------------------------------------------------------------
# Exceptional Foods parser (v2 — DocAI OCR column-aware)
# ---------------------------------------------------------------------------

def _extract_exceptional_freight(lines: list[str]) -> float | None:
    """Extract the "Freight" delivery fee from Exceptional Foods invoices.

    Exceptional's footer uses two possible layouts:

    Grouped (labels block THEN values block):
       Sales Amt / Misc Amt / Freight / Sales Tax / Total
       298.95    / 0.00     / 5.00    / 0.00      / 303.95
       → Freight is the 3rd label; its value is the 3rd number after the block.

    Interleaved (label/value alternating):
       Sales Amt / 373.04 / Misc Amt / 0.00 / Freight / 0.00 / ...
       → Freight value is immediately after the label.

    Detection: interleaved layouts have decimals BETWEEN "Sales Amt" and
    "Freight"; grouped has only labels in that span.
    """
    for i, line in enumerate(lines):
        if not re.match(r'^\s*Freight\s*$', line, re.IGNORECASE):
            continue
        sales_amt_idx = None
        for j in range(max(0, i - 10), i):
            if re.match(r'^\s*Sales\s+Amt\s*$', lines[j], re.IGNORECASE):
                sales_amt_idx = j
                break
        if sales_amt_idx is None:
            continue

        # Check if any numbers appear BETWEEN Sales Amt and Freight labels
        num_between = False
        for j in range(sales_amt_idx + 1, i):
            if re.match(r'^\s*\d+[,\d]*\.\d{2}\s*$', lines[j]):
                num_between = True
                break

        if num_between:
            # Interleaved: freight value is on the next non-label line
            for j in range(i + 1, min(i + 3, len(lines))):
                m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if m:
                    val = float(m.group(1).replace(",", ""))
                    if 0 <= val < 100:  # freight is typically small
                        return val if val > 0 else None
        else:
            # Grouped: values follow the full label block. Freight's value is
            # the (i - sales_amt_idx)-th number after the last label.
            offset = i - sales_amt_idx
            nums = []
            for j in range(i + 1, min(i + 20, len(lines))):
                if re.match(r'^\s*Freight|^\s*Sales|^\s*Misc|^\s*Total|^\s*Amount',
                            lines[j], re.IGNORECASE):
                    continue
                m = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if m:
                    nums.append(float(m.group(1).replace(",", "")))
                if len(nums) > offset:
                    break
            if len(nums) > offset:
                val = nums[offset]
                # Sanity: freight must be small relative to the total
                if 0 < val < 100:
                    return val
    return None


def _extract_exceptional_invoice_total(lines: list[str]) -> float | None:
    """Extract invoice total from Exceptional Foods footer.

    Handles two OCR layouts:
      Layout A (grouped): Labels block → Numbers block
        Sales Amt / Misc Amt / Freight / Sales Tax / Total → 298.95 / 0.00 / 5.00 / 0.00 / 303.95
      Layout B (interleaved): Label → Value → Label → Value
        Sales Amt / 373.23 / Misc Amt / 0.00 / ... / Total / 378.23

    In both cases, the definitive total is the number immediately after
    a standalone "Total" label (not "Sales Amt Total" or "QTY Total").
    """
    # The footer has varying OCR layouts (grouped vs interleaved).
    # Most reliable: find "Balance Due" label, then take the LAST
    # standalone dollar amount between Balance Due and end of invoice
    # (or "T = Taxable" marker). That's always the invoice total.
    balance_due_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^Balance Due\s*$', line, re.IGNORECASE):
            balance_due_idx = i
            break  # take the LAST occurrence in case there are multiple

    # Search backwards from end to find the last "Balance Due"
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r'^Balance Due\s*$', lines[i], re.IGNORECASE):
            balance_due_idx = i
            break

    if balance_due_idx is not None:
        # Collect all dollar amounts after Balance Due until end/marker
        nums = []
        for j in range(balance_due_idx + 1, min(balance_due_idx + 10, len(lines))):
            if re.match(r'^T\s*=\s*Taxable', lines[j], re.IGNORECASE):
                break
            m = re.match(r'^(\d+[,\d]*\.\d{2})\s*$', lines[j])
            if m:
                nums.append(float(m.group(1).replace(",", "")))
        if nums:
            # The last non-zero number is the Balance Due (= invoice total)
            non_zero = [n for n in nums if n > 0]
            if non_zero:
                return non_zero[-1]

    return None


def _parse_exceptional(text: str) -> list[dict]:
    """
    Parse Exceptional Foods invoices from DocAI OCR text.

    Invoice columns: Item ID | Qty Ordered | Description | Qty Shipped | Price | Per | Total

    DocAI OCR reads columns somewhat separately, so we get:
      - Description lines: "1.00 CS Bacon Applewood L/O 10/14 Martins 30530"
      - Price+Per lines: "4.69 LB" or "4.69\nLB" (per-lb price)
      - Total lines: standalone "70.35"
      - Qty shipped lines: standalone numbers between descriptions and prices

    Strategy:
      1. Find item block (after "Item ID", before footer)
      2. Preprocess: merge "number\\nLB" into "number LB"
      3. Extract descriptions (lines starting with qty + CS/EA/LB + product name)
      4. Extract price+per patterns ("N.NN LB" = per-lb price)
      5. After each price+per, find the total (next standalone number)
      6. Zip descriptions with (price_per_unit, per, total)
      7. Store unit_price=per-unit price, extended_amount=total for budget sync
    """
    lines = [l.strip() for l in text.splitlines()]

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
            r'^(Sale Amount|Misc Amt|RECEIVED|CUSTOMER COPY|PLEASE CHECK|'
            r'NOTICE:|ALL ORDERS|'
            r'service charge|HANDLING CHARGE|No returns|No credit|'
            r'Claims for|DUE TO RISING)',
            lines[i], re.IGNORECASE
        ):
            end = i
            break
        if lines[i].strip().upper() == 'EXCEPTIONAL' and i > start + 5:
            end = i
            break

    block = lines[start:end]

    # ── Preprocess: merge "number\nLB" into "number LB" ──────────────────────
    merged = []
    i = 0
    while i < len(block):
        line = block[i]
        if (i + 1 < len(block)
                and re.match(r'^(LB|CS|EA)$', block[i + 1], re.IGNORECASE)
                and re.match(r'^\d+\.\d{2}$', line)):
            merged.append(f"{line} {block[i + 1]}")
            i += 2
            continue
        merged.append(line)
        i += 1
    block = merged

    # ── Extract descriptions ─────────────────────────────────────────────────
    desc_re = re.compile(
        r'^(\d+\.?\d*)\s+(CS|EA|LB)\s+(.+)', re.IGNORECASE
    )
    SKIP_DESC = re.compile(
        r'Qty|Description|Price|Per\b|Total|Ship Via|Terms|Route|Stop|'
        r'Sales ID|Customer ID|Order|PHONE|FAX|Invoice|Page|Powered|'
        r'SOLD TO|SHIP TO|Net \d|Delivery',
        re.IGNORECASE,
    )

    descriptions = []
    for line_idx, line in enumerate(block):
        m = desc_re.match(line)
        if m:
            qty = float(m.group(1))
            order_unit = m.group(2).upper()
            desc = m.group(3).strip()
            # Must look like a product (3+ letter word), not a header
            if (len(desc) >= 4
                    and re.search(r'[A-Za-z]{3,}', desc)
                    and not SKIP_DESC.search(desc)):
                descriptions.append({
                    "qty_ordered": qty,
                    "order_unit": order_unit,
                    "description": desc,
                    "line_idx": line_idx,
                })

    # ── Extract price-per-pound patterns ─────────────────────────────────────
    # "4.69 LB" — the per-unit price. One per item since Exceptional prices
    # everything by the pound (even cases are avg-weight at $/LB).
    price_per_re = re.compile(r'^(\d+\.\d{2})\s+(LB|CS|EA)$', re.IGNORECASE)
    standalone_re = re.compile(r'^(\d+\.\d{2})$')

    price_pers = []
    for idx, line in enumerate(block):
        m = price_per_re.match(line)
        if m:
            price = float(m.group(1))
            per = m.group(2).upper()
            # Try to find the total: next non-zero standalone number after this.
            # Skip 0.00 — it's typically a Qty Adjustment / tax placeholder dump,
            # not a line total. Without the skip, items that come right before
            # a zero placeholder bind to $0.00 instead of their real total.
            total = None
            for look in range(idx + 1, min(idx + 4, len(block))):
                tm = standalone_re.match(block[look])
                if tm:
                    val = float(tm.group(1))
                    if val > 0:
                        total = val
                        break
            price_pers.append({
                "price_per_unit": price,
                "per": per,
                "total": total,
                "line_idx": idx,
            })

    # ── Collect all standalone numbers for cross-multiply solving ────────────
    standalone_re = re.compile(r'^(\d+\.?\d*)$')
    number_pool = []
    for line in block:
        m = standalone_re.match(line)
        if m:
            val = float(m.group(1))
            if 0.5 <= val <= 1000:
                number_pool.append(val)

    # Known P/# values — can't also be weights
    known_ppus = {round(pp["price_per_unit"], 2) for pp in price_pers}

    # ── Match descriptions to nearest price by line proximity ──────────────
    items = []
    used_pool = set()
    used_prices = set()

    for desc in descriptions:
        # Find the nearest unused price_per pattern AFTER this description
        best_pp = None
        best_dist = float("inf")
        best_pp_idx = None
        for pp_idx, pp in enumerate(price_pers):
            if pp_idx in used_prices:
                continue
            dist = pp["line_idx"] - desc["line_idx"]
            if dist > 0 and dist < best_dist:
                best_dist = dist
                best_pp = pp
                best_pp_idx = pp_idx

        if best_pp is None:
            # No price found after this description — item has no price
            items.append({
                "raw_description": desc["description"],
                "unit_price": None,
                "case_size_raw": "",
                "quantity": desc["qty_ordered"],
                "unit_of_measure": "",
            })
            continue

        used_prices.add(best_pp_idx)
        price_per_unit = best_pp["price_per_unit"]
        per = best_pp["per"]

        # Cross-multiply: find (weight, total) pair where weight × P/# ≈ total.
        # Skip zero-valued candidates — they're OCR dumps of freight/discount
        # placeholders, not legitimate weight/total pairs. Without the skip,
        # the (0, 0) pair always matches (0 × ppu = 0) and the winning item
        # silently gets unit_price=$0.
        best = None
        best_diff = float("inf")
        for i, cw in enumerate(number_pool):
            if i in used_pool or round(cw, 2) in known_ppus:
                continue  # skip P/# values as weights
            if cw == 0:
                continue
            expected = round(cw * price_per_unit, 2)
            for j, ct in enumerate(number_pool):
                if j in used_pool or j == i:
                    continue
                if ct == 0:
                    continue
                diff = abs(ct - expected)
                if diff <= 0.10 and diff < best_diff:
                    best_diff = diff
                    best = {"wi": i, "ti": j, "w": cw, "t": ct}

        if best:
            used_pool.add(best["wi"])
            used_pool.add(best["ti"])
            total = best["t"]
            weight = best["w"]
        else:
            # Fallback: use the total found after price+per in the block
            total = best_pp.get("total")
            weight = round(total / price_per_unit, 2) if total and price_per_unit > 0 else None

        # Phase 2b (2026-05-02): structured fields for Exceptional.
        # Catch-weight rows (per == "LB"): the actual SHIPPED weight is in
        # `weight` (cross-multiplied from the invoice's bare-decimal pool).
        # That's the load-bearing quantity for $/lb math — matches Sysco
        # catch-weight convention from Phase 2a. Beef Chuck Flap fix:
        # quantity=42.7 (shipped lb) + purchase_uom='LB' + total_weight_lb=42.7
        # + price_per_pound=10.99 → consumers compute $/lb directly without
        # re-parsing case_size string.
        item = {
            "raw_description": desc["description"],
            "unit_price": total,           # case total (weight × $/lb) — for DB + sheet
            "extended_amount": total,      # same as unit_price for Exceptional (qty already in weight)
            "case_size_raw": f"{weight}LB" if weight and per == "LB" else "",
            "unit_of_measure": per,
            "price_per_unit": price_per_unit,  # $/lb — for P/# column
        }
        if per == "LB" and weight is not None:
            # Catch-weight: quantity = shipped lbs (Sysco convention)
            item["quantity"] = weight
            item["purchase_uom"] = "LB"
            item["case_total_weight_lb"] = round(weight, 3)
            # Also emit case_pack_count=1 to mark it as a single-unit shipment
            # rather than a packed case. case_pack_unit_size = weight, uom = LB.
            item["case_pack_count"] = 1
            item["case_pack_unit_size"] = str(round(weight, 3))
            item["case_pack_unit_uom"] = "LB"
        else:
            # Non-catch-weight (per == "CS" / "EA"): qty_ordered is the right
            # "quantity" — what the invoice ordered. UoM = the order-level unit.
            item["quantity"] = desc["qty_ordered"]
            item["purchase_uom"] = per

        # Phase 3 #6: count-per-lb extraction (BACON 18/22, SHRIMP 21/25)
        cpl = _extract_count_per_lb(desc["description"])
        if cpl is not None:
            item["count_per_lb_low"] = cpl[0]
            item["count_per_lb_high"] = cpl[1]
        items.append(item)

    # ── Extract and validate invoice total ────────────────────────────
    all_lines = [l.strip() for l in text.splitlines()]
    invoice_total = _extract_exceptional_invoice_total(all_lines)

    # Exceptional footer includes a "Freight" line (often $5.00) separate
    # from item lines. Capture it as a synthetic line so item totals
    # reconcile against invoice_total.
    freight = _extract_exceptional_freight(all_lines)
    if freight and freight > 0:
        items.append({
            "raw_description": "Freight",
            "unit_price":      freight,
            "extended_amount": freight,
            "case_size_raw":   "",
        })

    items_total = round(sum(it.get("extended_amount", 0) or 0 for it in items), 2)

    if invoice_total is not None:
        diff = abs(invoice_total - items_total)
        if diff > 0.50:
            print(f"  [!] Exceptional invoice total mismatch: "
                  f"parsed=${invoice_total:.2f}, items=${items_total:.2f}, "
                  f"gap=${diff:.2f}")
        else:
            print(f"  [✓] Exceptional invoice total verified: ${invoice_total:.2f}")

    return items, invoice_total


# ---------------------------------------------------------------------------
# FarmArt parser
# ---------------------------------------------------------------------------

def _extract_farmart_invoice_total(lines: list[str]) -> float | None:
    """Extract the invoice total from a Farm Art invoice OCR text.

    Farm Art footers vary: sometimes label + adjacent value, sometimes
    label-block then value-block (Taxable Subtotal / Tax / Invoice Total /
    Invoice Balance stacked, then all their values stacked below). The
    label may be up to 5 lines from its value.
    """
    for i, line in enumerate(lines):
        # Inline: "Nontaxable 222.07" or "Invoice Total 316.90"
        m = re.match(r'^(?:Nontaxable|Invoice Total)\s+(\d+[,\d]*\.\d{2})',
                     line, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))

        # Label-on-own-line: search forward for the first bare decimal.
        # Farm Art footer blocks put the value 2-5 lines after the label.
        if re.match(r'^Nontaxable\s*$', line, re.IGNORECASE) or \
                re.match(r'^Invoice Total\s*$', line, re.IGNORECASE):
            for j in range(i + 1, min(i + 6, len(lines))):
                # Accept only bare decimal-on-own-line (avoids matching
                # prices embedded in items/tax notices)
                bm = re.match(r'^\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if bm:
                    val = float(bm.group(1).replace(",", ""))
                    # Sanity: totals are usually $1+ (skip rogue $0.00 tax lines)
                    if val >= 1.0:
                        return val
    return None


def _parse_farmart(text: str) -> list[dict]:
    """
    FarmArt invoices have two types of items:
    - Non-stock items prefixed with "zz"
    - Regular stock items (no prefix) that appear after the column headers

    Both formats follow: Description → "United States" → unit price → amount
    OCR sometimes bunches all descriptions together, then all prices — so we
    use two-pass extraction with proximity matching (same as Exceptional).

    Returns items with both unit_price (per case) and extended_amount (qty × price).
    Also extracts invoice_total for budget sync validation.
    """
    lines = [l.strip() for l in text.splitlines()]

    # Headers/footers to skip
    skip_patterns = re.compile(
        r'^(Bill To|Ship To|Received By|Invoice|Customer|Date|Purchase|Driver|'
        r'Route|Terms|Salesperson|Picker|Order|Quantity|U/M|Item|Description|'
        r'COOL|United States|Peru|Mexico|Canada|Nontaxable|Taxable|Tax|Discount|Invoice Total|'
        r'Payments|Invoice Balance|Page|All returns|\*\*\*|NOT AVAIL|'
        r'Unit Price|Amount|'       # column headers
        r'"zz"|'                    # "zz" non-stock delivery note lines
        r'\d+\.\d{3})',             # quantity lines (1.000 HALF, 4.000 EACH CAU, etc.)
        re.IGNORECASE
    )

    # ── Pass 1: Extract all descriptions with line positions ──────────────
    # zz-prefixed items are FLAGGED non-stock on Farm Art's order tracking.
    # Sean (2026-05-02): zz signals "ordered but possibly not delivered" —
    # but the prefix alone doesn't determine delivery. Some zz items DO
    # ship (the prefix is set when the item was non-stock at order-take
    # time; substitution can still occur). The reliable signal is whether
    # the row has non-zero price + qty. We KEEP zz lines as descriptions
    # here so they participate in price-pair matching; db_write filters
    # the genuinely-zero rows downstream via _is_undelivered_farmart().
    descriptions = []
    for i, line in enumerate(lines):
        is_zz = line.upper().startswith("ZZ ")
        is_desc = (
            not is_zz
            and len(line) > 12
            and re.search(r'[A-Z]{3,}', line)
            and (re.search(r',', line) or re.search(r'[A-Z]{4,}\s+[A-Z]{2,}', line))
            and not skip_patterns.match(line)
            and not re.match(r'^[\d\s.,]+$', line)
        )
        if is_zz or is_desc:
            desc = line[3:].strip() if is_zz else line
            desc = re.sub(r'\s*\*+.*$', '', desc).strip()
            if desc:
                # Mark zz items so downstream can apply the zero-data filter
                # Farm-Art-specifically without scanning raw_description.
                entry = {"description": desc, "line_idx": i}
                if is_zz:
                    entry["nonstock_flag"] = True
                descriptions.append(entry)

    # ── Pass 2: Extract all price pairs (unit_price, amount) with positions ──
    price_pairs = []
    i = 0
    while i < len(lines):
        m = re.match(r'^\s*-?\d+\.\d{2}\s*$', lines[i])
        if m:
            price1 = float(lines[i].strip())
            # Look for second price within next 2 lines
            for j in range(i + 1, min(i + 3, len(lines))):
                m2 = re.match(r'^\s*-?\d+\.\d{2}\s*$', lines[j])
                if m2:
                    price2 = float(lines[j].strip())
                    price_pairs.append({
                        "unit_price": price1,
                        "amount": price2,
                        "line_idx": i,
                    })
                    i = j + 1  # skip past this pair
                    break
            else:
                i += 1
                continue
            continue
        i += 1

    # ── Pass 3: Match descriptions to nearest subsequent price pair ────────
    items = []
    used_prices = set()

    for desc in descriptions:
        best_pp = None
        best_dist = float("inf")
        best_idx = None
        for pp_idx, pp in enumerate(price_pairs):
            if pp_idx in used_prices:
                continue
            dist = pp["line_idx"] - desc["line_idx"]
            if dist > 0 and dist < best_dist:
                best_dist = dist
                best_pp = pp
                best_idx = pp_idx

        if best_pp and best_pp["amount"] > 0:
            used_prices.add(best_idx)
            item = {
                "raw_description": desc["description"],
                "unit_price": best_pp["unit_price"],
                "extended_amount": best_pp["amount"],
                "case_size_raw": "",
            }
            # Extract embedded pack tokens (4/1GAL, 9CT, 15DOZ, 5#) into
            # structured fields. Falls back to {} on ambiguous, leaving
            # case_size_raw empty for db_write's default_case_size fallback.
            item.update(_extract_farmart_pack(desc["description"]))
            items.append(item)

    # ── Pass 4: Extract and validate invoice total ────────────────────────
    invoice_total = _extract_farmart_invoice_total(lines)
    items_total = round(sum(it["extended_amount"] for it in items), 2)

    if invoice_total is not None:
        diff = abs(invoice_total - items_total)
        if diff > 0.50:
            print(f"  [!] Farm Art invoice total mismatch: "
                  f"parsed=${invoice_total:.2f}, items=${items_total:.2f}, "
                  f"gap=${diff:.2f}")
        else:
            print(f"  [✓] Farm Art invoice total verified: ${invoice_total:.2f}")

    return items, invoice_total


# ---------------------------------------------------------------------------
# PBM (Philadelphia Bakery Merchants) parser
# ---------------------------------------------------------------------------

def _parse_pbm_format1(text: str) -> tuple[list[dict], float | None]:
    """
    PBM old-style invoices (Jan-Feb 2026 and earlier).
    Items appear as: "2 0290/AsstDo... Assorted Donuts"
    Prices in "Price Each" / "Amount" block.
    Total as "$XX.XX".
    """
    lines = [l.strip() for l in text.splitlines()]

    # Find Description header
    desc_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^Description\s*$', line, re.IGNORECASE):
            desc_idx = i
            break
    if desc_idx is None:
        return [], None

    # Extract descriptions: lines matching "N code/abbrev... Product Name"
    # or just product name lines after the delivery instructions
    descriptions = []
    in_delivery_note = False
    for i in range(desc_idx + 1, len(lines)):
        line = lines[i]
        if re.match(r'^(Price Each|Amount|Total)\b', line, re.IGNORECASE):
            break
        if line.startswith('***'):
            in_delivery_note = True
            continue
        if in_delivery_note and '***' in line:
            in_delivery_note = False
            continue
        if in_delivery_note:
            continue

        # Pattern: "N code/abbrev... Product Name"
        m = re.match(r'^\d+\s+\S+/\S+\.{2,}\s*(.+)', line)
        if m:
            descriptions.append(m.group(1).strip())
            continue
        # Standalone product name (no code prefix)
        if (re.search(r'[A-Za-z]{3,}', line)
                and not re.match(r'^\d+\s+[A-Z]\d+$', line)
                and len(line) >= 4):
            descriptions.append(line)

    # Extract prices: after "Price Each" / "Amount", alternating (unit, ext)
    raw_amounts = []
    price_start = None
    for i, line in enumerate(lines):
        if re.match(r'^(Price Each|Amount)\s*$', line, re.IGNORECASE):
            price_start = i
    if price_start:
        for i in range(price_start + 1, len(lines)):
            # Break on "Total: $X" or bare "$X" (total value lines), NOT the
            # bare "Total" column header that appears before any prices.
            if re.match(r'^Total\s+\$?\d', lines[i], re.IGNORECASE):
                break
            if re.match(r'^\$\d', lines[i]):
                break
            m = re.match(r'^(\d+\.\d{2})$', lines[i])
            if m:
                raw_amounts.append(float(m.group(1)))

    # Parse total: "$XX.XX"
    invoice_total = None
    for line in lines:
        m = re.match(r'^\$(\d+[,\d]*\.\d{2})$', line)
        if m:
            invoice_total = float(m.group(1).replace(",", ""))

    # Pair descriptions with prices (alternating: unit, ext)
    n = len(descriptions)
    items = []
    if n > 0 and len(raw_amounts) >= n * 2:
        for i in range(n):
            unit = raw_amounts[i * 2]
            ext = raw_amounts[i * 2 + 1]
            items.append({
                "raw_description": descriptions[i],
                "unit_price": unit,
                "extended_amount": ext,
                "case_size_raw": "",
            })
    elif n > 0 and raw_amounts:
        # Fallback: just pair what we have
        for i, desc in enumerate(descriptions):
            if i < len(raw_amounts):
                items.append({
                    "raw_description": desc,
                    "unit_price": raw_amounts[i],
                    "extended_amount": raw_amounts[i],
                    "case_size_raw": "",
                })

    items_sum = round(sum(it.get("extended_amount", 0) for it in items), 2)
    if invoice_total is not None:
        diff = abs(invoice_total - items_sum)
        if diff > 0.50:
            print(f"  [!] PBM (old format) invoice total mismatch: "
                  f"parsed=${invoice_total:.2f}, items=${items_sum:.2f}, gap=${diff:.2f}")
        else:
            print(f"  [✓] PBM (old format) invoice total verified: ${invoice_total:.2f}")

    return items, invoice_total


def _parse_pbm(text: str) -> list[dict]:
    """
    PBM invoices come in two formats:

    Format 1 (old, Jan-Feb 2026): handwritten-style with "Price Each" header,
      items as "N code/abbrev... Product Name", total as "$XX.XX".

    Format 2 (new, March+ 2026): digital with "Unit Price"/"Amount" columns,
      DZ/EA U/M tokens, "Invoice Total($):" footer.

    Detection: presence of "Price Each" → Format 1, else Format 2.
    """
    # Detect format
    if re.search(r'Price Each', text, re.IGNORECASE):
        return _parse_pbm_format1(text)

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
        return [], None

    # Layout detection: prices right after Description = row-by-row; far away = column
    is_column_format = (
        unit_price_idx is not None and unit_price_idx > desc_idx + 5
    )

    # "Late header" layout signal: on some PBM digital invoices the
    # "Description" label is a FOOTER legend printed right before
    # "Unit Price" / "Amount" (all three consecutive). The actual
    # descriptions are above, near the U/M tokens. Detecting the exact
    # adjacent triple avoids false-positives on normal layouts where
    # "Description" is just a mid-document header.
    late_header = (
        unit_price_idx is not None and amount_idx is not None
        and unit_price_idx == desc_idx + 1
        and amount_idx == desc_idx + 2
    )

    # ── Extract descriptions ────────────────────────────────────────────────
    # PBM OCR layouts vary — U/M tokens may or may not precede descriptions.
    # Strategy: scan for ALL product-name-like lines after the Description header,
    # filtering out codes, numbers, and header text.
    skip_desc = re.compile(
        r'^(Item\s*Number|Order|Ship|Qty|U/M|PO\s*Number|Salesperson|'
        r'Routeperson|Contact|Reference|Sequence|Shift|Route|Terms|'
        r'Ship Via|DEF|Page|Vanessa|Kimberly|Unit\s*Price|Amount)\b',
        re.IGNORECASE
    )

    descriptions = []
    for i in range(desc_idx + 1, len(lines)):
        line = lines[i].strip()
        if stop_pattern.match(line):
            break
        if not line or len(line) < 4:
            continue
        if re.match(r'^[\d.]+$', line):            # pure number
            continue
        if re.match(r'^[\d.]+\s*$', line):         # number with whitespace
            continue
        if um_pattern.match(line):                  # bare U/M token (DZ, EA, etc.)
            continue
        if re.match(r'^[A-Z]\d{2,}$', line):       # item code like L202, H106, G105
            continue
        if re.match(r'^0\d{2,}$', line):           # item code like 0290, 0389
            continue
        if re.match(r'^[A-Z]{1,2}\d+$', line):     # item code like R1012
            continue
        if skip_desc.match(line):                   # header text
            continue
        if re.match(r'^[\u1780-\u17FF\s]+$', line): # OCR garbage (Khmer chars etc.)
            continue
        # Must contain a word with 3+ letters (product name, not code)
        if re.search(r'[A-Za-z]{3,}', line):
            # Strip leading "DZ " or "PACK " if merged into description
            clean = re.sub(r'^(DZ|EA|LB|CS|PACK)\s+', '', line, flags=re.IGNORECASE).strip()
            if clean and len(clean) >= 4:
                descriptions.append(clean)

    # Late-header fallback: primary scan found nothing AND the footer has
    # the Description/UnitPrice/Amount triple. Rescan the range between the
    # FIRST U/M token and desc_idx — this skips the invoice header/address
    # block and focuses on the actual item list.
    if not descriptions and late_header:
        first_um = None
        for i, line in enumerate(lines):
            if um_pattern.match(line.strip()):
                first_um = i
                break
        if first_um is not None:
            for i in range(first_um, desc_idx):
                line = lines[i].strip()
                if not line or len(line) < 4:
                    continue
                if re.match(r'^[\d.]+\s*$', line):
                    continue
                if um_pattern.match(line):
                    continue
                if re.match(r'^[A-Z]\d{2,}$', line):
                    continue
                if re.match(r'^0\d{2,}$', line):
                    continue
                if re.match(r'^[A-Z]{1,2}\d+$', line):
                    continue
                if skip_desc.match(line):
                    continue
                if re.search(r'[A-Za-z]{3,}', line):
                    clean = re.sub(r'^(DZ|EA|LB|CS|PACK)\s+', '', line, flags=re.IGNORECASE).strip()
                    if clean and len(clean) >= 4:
                        descriptions.append(clean)

    # ── Collect all standalone prices between Description and Subtotal ────────
    # PBM OCR has varying layouts — prices can be before/after descriptions,
    # alternating (unit, ext, unit, ext) or grouped (all units, all exts).
    # Strategy: collect ALL prices, then pair with descriptions using the
    # subtotal as a validation check.
    subtotal = None
    for i, line in enumerate(lines):
        m = re.match(r'^Subtotal\s*\(\$\)\s*:\s*$', line, re.IGNORECASE)
        if m:
            for j in range(i + 1, min(i + 3, len(lines))):
                nm = re.match(r'^(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if nm:
                    subtotal = float(nm.group(1).replace(",", ""))
                    break
            break

    # Collect prices — strategy depends on whether descriptions were found via U/M (method 1)
    # or fallback (method 2). Method 1 = row-by-row (prices between desc and Subtotal).
    # Method 2 = column format (prices may be AFTER Subtotal).
    used_fallback = len(descriptions) > 0 and not any(
        i > 0 and re.match(r'^(DZ|EA|LB|CS|OZ|PK|BG|CTN)$', lines[i - 1], re.IGNORECASE)
        for i in range(desc_idx + 1, len(lines))
        if lines[i].strip() in [d for d in descriptions]
    )

    raw_amounts = []
    # When PBM's explicit "Unit Price" header was detected well after the
    # Description header, the price column is separated from the item list.
    # Use the header position as the price-search anchor — otherwise the
    # row-by-row extractor mistakes QTY column values (0.50 DZ, 1.00 DZ)
    # for prices and produces nonsense totals.
    if unit_price_idx is not None and unit_price_idx > desc_idx + 5:
        search_start = unit_price_idx + 1
    else:
        search_start = desc_idx + 1
    if used_fallback:
        # Column format: prices are after Subtotal, up to Invoice Total
        for i in range(search_start, len(lines)):
            if re.match(r'^(Invoice\s+Total|Page\s+\d)', lines[i], re.IGNORECASE):
                break
            if re.match(r'^Subtotal', lines[i], re.IGNORECASE):
                continue
            if re.match(r'^\d+\.\d{2}$', lines[i]):
                val = float(lines[i])
                if subtotal and abs(val - subtotal) < 0.01:
                    continue
                raw_amounts.append(val)
    else:
        # Row-by-row: prices are between items, stop at Subtotal
        for i in range(search_start, len(lines)):
            if re.match(r'^(QTY\s+Totals|Subtotal|Invoice\s+Total)', lines[i], re.IGNORECASE):
                break
            if re.match(r'^\d+\.\d{2}$', lines[i]):
                raw_amounts.append(float(lines[i]))

    n_desc = len(descriptions)
    unit_prices = []
    ext_amounts = []

    if n_desc > 0 and len(raw_amounts) >= n_desc:
        # Try multiple pairing strategies, validate against subtotal
        candidates = []

        # Strategy 1a: triples (qty, unit, ext) — row-by-row, qty first
        if len(raw_amounts) >= n_desc * 3:
            tri_ext = raw_amounts[2::3][:n_desc]
            tri_unit = raw_amounts[1::3][:n_desc]
            tri_sum = round(sum(tri_ext), 2)
            candidates.append(("triples_que", tri_unit, tri_ext, tri_sum))

        # Strategy 1b: triples (unit, ext, qty) — row-by-row, prices first
        if len(raw_amounts) >= n_desc * 3:
            tri_ext2 = raw_amounts[1::3][:n_desc]
            tri_unit2 = raw_amounts[0::3][:n_desc]
            tri_sum2 = round(sum(tri_ext2), 2)
            candidates.append(("triples_ueq", tri_unit2, tri_ext2, tri_sum2))

        # Strategy 2: alternating (unit, ext, unit, ext)
        if len(raw_amounts) >= n_desc * 2:
            alt_ext = raw_amounts[1::2][:n_desc]
            alt_unit = raw_amounts[0::2][:n_desc]
            alt_sum = round(sum(alt_ext), 2)
            candidates.append(("alternating", alt_unit, alt_ext, alt_sum))

        # Strategy 3: grouped (unit1..unitN, ext1..extN)
        if len(raw_amounts) >= n_desc * 2:
            grp_unit = raw_amounts[:n_desc]
            grp_ext = raw_amounts[n_desc:n_desc * 2]
            grp_sum = round(sum(grp_ext), 2)
            candidates.append(("grouped", grp_unit, grp_ext, grp_sum))

        # Strategy 4: just the amounts (single column)
        single = raw_amounts[:n_desc]
        single_sum = round(sum(single), 2)
        candidates.append(("single", single, single, single_sum))

        # Pick the strategy that matches subtotal
        best = None
        for name, units, exts, total in candidates:
            if subtotal and abs(total - subtotal) < 0.50:
                best = (name, units, exts)
                break

        if best:
            _, unit_prices, ext_amounts = best
        else:
            # No subtotal match — pick the one with the largest sum (most likely extended)
            candidates.sort(key=lambda x: x[3], reverse=True)
            _, unit_prices, ext_amounts = candidates[0][1], candidates[0][1], candidates[0][2]

    items = []
    for i, desc in enumerate(descriptions):
        ext = ext_amounts[i] if i < len(ext_amounts) else 0
        up = unit_prices[i] if i < len(unit_prices) else ext
        if ext > 0:
            items.append({
                "raw_description": desc,
                "unit_price": up,
                "extended_amount": ext,
                "case_size_raw": "",
            })

    # ── Extract and validate invoice total ────────────────────────────
    invoice_total = None
    for i, line in enumerate(lines):
        m = re.match(r'^Invoice\s+Total\s*\(\$\)\s*:\s*$', line, re.IGNORECASE)
        if m:
            for j in range(i + 1, min(i + 3, len(lines))):
                nm = re.match(r'^(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if nm:
                    invoice_total = float(nm.group(1).replace(",", ""))
                    break
            break
        # Inline: "Invoice Total($): 142.55"
        m = re.match(r'^Invoice\s+Total\s*\(\$\)\s*:\s*(\d+[,\d]*\.\d{2})', line, re.IGNORECASE)
        if m:
            invoice_total = float(m.group(1).replace(",", ""))
            break

    items_total = round(sum(it.get("extended_amount", 0) for it in items), 2)
    if invoice_total is not None:
        diff = abs(invoice_total - items_total)
        if diff > 0.50:
            print(f"  [!] PBM invoice total mismatch: "
                  f"parsed=${invoice_total:.2f}, items=${items_total:.2f}, "
                  f"gap=${diff:.2f}")
        else:
            print(f"  [✓] PBM invoice total verified: ${invoice_total:.2f}")

    return items, invoice_total


# ---------------------------------------------------------------------------
# Delaware County Linen parser
# ---------------------------------------------------------------------------

def _parse_delaware_linen_column_dump(text: str) -> list[dict]:
    """Fallback column-dump parser for Delaware Linen invoices whose OCR
    emits full columns (all qtys, then all codes, then all descriptions,
    then all prices) rather than the row-interleaved layout the primary
    parser expects. Google Document AI produces this shape.

    Strategy:
      1. Scan the full text for positive-integer standalone lines → qty pool.
      2. Collect description-like lines (letters, not headers/footnotes).
      3. Collect price-like lines (N.NN optionally trailing T/*).
      4. For each description, look for a (qty, price) pair where
         qty × price ≈ some amount in the pool. That (qty, price, amount)
         triple becomes the item's quantity / unit_price / extended_amount.

    Descriptions without a matching triple are dropped — they're usually
    fee/footer text the description filter didn't catch.
    """
    lines = [l.strip() for l in text.splitlines()]

    # Skip lines that look like descriptions but aren't product items
    skip_desc = re.compile(
        r'^(Qty|Item Code|Description|Unit Price|Amount|Qty Adjustment|'
        r'Customer|Invoice|Date|Route|Driver|Terms|Bill To|Ship To|'
        r'Net \d|Net$|'                           # payment terms: "Net 7"
        r'Delaware County|Fuel\s?Surcharge|Delivery Cha|PA Sales Tax|'
        r'All items|Please|Thank|PLEASE|PO BOX|www\.|Total|Subtotal|'
        r'Payments|Balance|FuelSurcharge|P\.\s*O\.|Phone|Fax|'
        r'Synergy|David|Chester|Linwood|West Chester)',  # address/contact seen in real OCR
        re.IGNORECASE,
    )
    # Address-like patterns: street numbers (line starts with digit) and
    # "City, ST 12345" lines. Product descriptions at Delaware Linen never
    # start with digits and never match a city-state-zip shape.
    address_re = re.compile(
        r'^\d'                                      # starts with digit → street #
        r'|,\s*[A-Z]{2}\s+\d{5}'                    # ", PA 19013" anywhere in line
        r'|^\d{3}\s*-\s*\d{3}\s*-\s*\d{4}$',         # phone number
    )

    qty_candidates: list[tuple[int, int]] = []       # (line_idx, value)
    desc_candidates: list[tuple[int, str]] = []      # (line_idx, text)
    price_candidates: list[tuple[int, float]] = []   # (line_idx, value)

    for idx, line in enumerate(lines):
        if not line:
            continue
        # Standalone positive integer → qty candidate
        m = re.match(r'^(\d+)$', line)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 9999:
                qty_candidates.append((idx, val))
            continue
        # Standalone decimal (with optional trailing T or *) → price candidate
        m = re.match(r'^(\d+(?:\.\d+)?)[T*]?$', line)
        if m:
            val = float(m.group(1))
            if 0.01 <= val <= 10000:
                price_candidates.append((idx, val))
            continue
        # Description-like: has letters, not a header/footer, not an address
        if re.search(r'[A-Za-z]{3,}', line) and not skip_desc.match(line):
            # Drop all-caps single words (item codes like MOPS, BAPSWT)
            if re.match(r'^[A-Z]+$', line) and len(line) <= 8:
                continue
            # Drop street addresses, city-state-zip, phone numbers
            if address_re.search(line):
                continue
            desc_candidates.append((idx, line))

    prices_by_idx = {i: v for i, v in price_candidates}
    price_values = [v for _, v in price_candidates]

    items: list[dict] = []
    used_prices: set[int] = set()
    used_qtys: set[int] = set()

    # For each description, try to match to a (qty, unit_price, amount) triple.
    # qty × unit_price ≈ amount  within a $0.50 tolerance.
    for di, (d_idx, desc) in enumerate(desc_candidates):
        best = None
        for qi, (q_idx, qty) in enumerate(qty_candidates):
            if qi in used_qtys:
                continue
            for ui, (u_idx, unit) in enumerate(price_candidates):
                if ui in used_prices:
                    continue
                expected = round(qty * unit, 2)
                for ai, (a_idx, amt) in enumerate(price_candidates):
                    if ai in used_prices or ai == ui:
                        continue
                    if abs(amt - expected) <= 0.50:
                        best = (qi, ui, ai, qty, unit, amt)
                        break
                if best:
                    break
            if best:
                break
        if best:
            qi, ui, ai, qty, unit, amt = best
            used_qtys.add(qi)
            used_prices.add(ui)
            used_prices.add(ai)
            items.append({
                "raw_description": desc,
                "unit_price":      unit,
                "extended_amount": amt,
                "case_size_raw":   "",
                "quantity":        qty,
            })

    return items


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
                ext = amount if amount is not None else unit_price
                if ext > 0:
                    items.append({
                        "raw_description": description,
                        "unit_price": unit_price,
                        "extended_amount": ext,
                        "case_size_raw": "",
                    })

        i += 1

    # ── Extract and validate invoice total ────────────────────────────
    # Delaware Linen prints totals with a dollar sign on a separate line
    # below the "Total Due" label. The $ prefix is optional in the regex
    # (line reads as "$91.37" not "91.37"). Tolerate comma thousands too.
    invoice_total = None
    for idx, line in enumerate(lines):
        m = re.match(r'^Total\s+Due\s*$', line, re.IGNORECASE)
        if m:
            for j in range(idx + 1, min(idx + 3, len(lines))):
                nm = re.match(r'^\$?\s*(\d+[,\d]*\.\d{2})\s*$', lines[j])
                if nm:
                    invoice_total = float(nm.group(1).replace(",", ""))
                    break
            break
        m = re.match(r'^Total\s*:?\s*\$?\s*(\d+[,\d]*\.\d{2})',
                     line, re.IGNORECASE)
        if m:
            invoice_total = float(m.group(1).replace(",", ""))
            break

    # Fallback: DocAI OCR dumps full columns rather than row-interleaved
    # rows, so the primary parser's "Amount"-header anchor doesn't fire.
    # When we extracted zero items, retry with a column-dump strategy.
    if not items:
        items = _parse_delaware_linen_column_dump(text)

    items_total = round(sum(it.get("extended_amount", 0) for it in items), 2)
    if invoice_total is not None:
        diff = abs(invoice_total - items_total)
        if diff > 0.50:
            print(f"  [!] Delaware Linen invoice total mismatch: "
                  f"parsed=${invoice_total:.2f}, items=${items_total:.2f}, "
                  f"gap=${diff:.2f}")
        else:
            print(f"  [✓] Delaware Linen invoice total verified: ${invoice_total:.2f}")

    return items, invoice_total


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

def _try_spatial(vendor: str, pages: list[dict] | None,
                 min_items: int | None = None) -> list[dict] | None:
    """When DocAI layout data (pages[].tokens[]) is present, dispatch to
    the per-vendor spatial matcher. Returns extracted items if the count
    is plausible, else None so callers fall back to the 1D heuristic
    parser.

    `min_items` default = 1 for all vendors. Originally 3 for non-Delaware
    vendors to filter page-1-only partial dumps; lowered 2026-05-01 because
    Sysco's multi-photo workflow (one JPG per invoice page) loses pages
    whose spatial result has <3 items. Empty/header-only pages produce
    zero anchors → zero spatial items, so no noise is admitted at 1."""
    if not pages:
        return None
    try:
        from spatial_matcher import (
            match_sysco_spatial, match_pbm_spatial,
            match_exceptional_spatial, match_farmart_spatial,
            match_delaware_spatial,
        )
    except Exception:
        return None
    dispatch = {
        "Sysco": match_sysco_spatial,
        "Philadelphia Bakery Merchants": match_pbm_spatial,
        "PBM": match_pbm_spatial,
        "Exceptional Foods": match_exceptional_spatial,
        "Exceptional": match_exceptional_spatial,
        "Farm Art": match_farmart_spatial,
        "FarmArt": match_farmart_spatial,
        "Delaware County Linen": match_delaware_spatial,
    }
    fn = dispatch.get(vendor)
    if not fn:
        return None
    if min_items is None:
        min_items = 1
    items = fn(pages)
    if len(items) < min_items:
        return None
    return items


def _try_spatial_sysco(pages: list[dict] | None, text: str) -> list[dict] | None:
    """Back-compat shim — existing callers keep working."""
    return _try_spatial("Sysco", pages)


def _is_non_invoice_document(text: str) -> str | None:
    """Detect documents that look like invoices to OCR but aren't priced
    invoices (pick sheets, packing slips). Returns a short reason string
    when rejected, None when the document looks like a normal invoice.

    Surfaced 2026-04-26 from an Aramark pick sheet that landed 7 priceless
    ILI rows. The pattern is documents with "Sales Order Pick Sheet" /
    "Pick Qty" headers but no price column — DocAI extracts descriptions
    and quantities just fine, leaving unit_price=NULL across every line.
    Caller (batch.py) skips db_write when this returns non-None.
    """
    upper = (text or '').upper()
    if 'SALES ORDER PICK SHEET' in upper:
        return 'pick_sheet'
    # Belt-and-suspenders: documents with "Pick Qty" AND "Picked" headers
    # but no $/Price/Total column in the first 1500 chars are pick sheets
    # even when the title isn't captured (rotated/cropped photo).
    if 'PICK QTY' in upper and 'PICKED' in upper:
        head = upper[:1500]
        if 'PRICE' not in head and 'TOTAL' not in head and 'AMOUNT' not in head:
            return 'pick_sheet'
    return None


def parse_invoice(text: str, vendor: str = None,
                  pages: list[dict] | None = None) -> dict:
    """Parse invoice OCR. Optional `pages` is DocAI layout data
    (pages[].tokens[] with bounding boxes) — when present for Sysco we
    use spatial row-matching for more reliable anchor/desc pairing.
    Other vendors still use text-based parsing."""
    vendor = vendor or detect_vendor(text)
    date   = extract_date(text)

    # Reject pick sheets / packing slips before parser dispatch — they
    # OCR like invoices but carry no prices, so per-line ILI writes
    # produce only NULL-price rows that pollute reporting.
    rejected = _is_non_invoice_document(text)
    if rejected:
        return {
            'vendor':          vendor or 'Unknown',
            'invoice_date':    date,
            'items':           [],
            'rejected_reason': rejected,
        }

    parsers = {
        "Sysco":                 _parse_sysco,
        "Exceptional Foods":     _parse_exceptional,
        "Exceptional":           _parse_exceptional,
        "Farm Art":              _parse_farmart,
        "FarmArt":               _parse_farmart,
        "Philadelphia Bakery Merchants": _parse_pbm,
        "PBM":                   _parse_pbm,
        "Colonial Meat":         _parse_colonial,
        "Delaware County Linen": _parse_delaware_linen,
    }

    # Run BOTH spatial and text parsers, pick whichever extracts more
    # items. Spatial usually wins for clean OCR (Sysco column-dump,
    # Exceptional, PBM digital), but on layouts where the spatial
    # row-cluster or x-band detection misses items (Farm Art multi-page
    # produce invoices have shown 6/16 spatial vs 16/16 text), we fall
    # back to text. Text parsers are also where invoice_total comes from,
    # so we always need to run them anyway.
    invoice_total = None
    spatial_items = _try_spatial(vendor, pages)

    text_items: list[dict] = []
    parser_fn = parsers.get(vendor, _fallback_parse)
    try:
        result = parser_fn(text)
        if isinstance(result, tuple):
            text_items, invoice_total = result
        else:
            text_items = result
    except Exception:
        text_items = []

    # Pick the source with more items. Tie-breaker: prefer spatial (it's
    # the path tuned for clean DocAI bounding-box layouts).
    if spatial_items is not None and len(spatial_items) >= len(text_items):
        items = spatial_items
    else:
        items = text_items

    if vendor not in parsers:
        for item in items:
            item["needs_review"] = True

    parsed = {
        "vendor": vendor,
        "invoice_date": date,
        "items": items,
    }
    if invoice_total is not None:
        parsed["invoice_total"] = invoice_total

    return parsed


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            raw = f.read()
        result = parse_invoice(raw)
        print(json.dumps(result, indent=2))
