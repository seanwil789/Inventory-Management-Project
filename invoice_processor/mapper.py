"""
Maps invoice line items to canonical product names from your inventory sheet.

Reads from the "Item Mapping" tab in your Google Sheet.
Column structure (matches Sheet3 layout):
  A: vendor               — which supplier this description comes from
  B: item_description     — raw OCR/vendor description (what the invoice says)
  C: category             — top-level category (e.g. "Produce", "Proteins")
  D: primary_descriptor   — mid-level grouping (e.g. "Leaf", "Poultry")
  E: secondary_descriptor — fine-level grouping (e.g. "Cow", "Goat") — optional
  F: canonical_name       — your product name / tertiary descriptor (e.g. "Romaine")
  G: sysco_item_code      — Sysco 7-digit item code (optional, most reliable match)
"""
import json
import os
import re
from rapidfuzz import process, fuzz, utils as fuzz_utils
from sheets import get_sheet_values
from config import SPREADSHEET_ID, MAPPING_TAB

MAPPING_CACHE_PATH = "invoice_processor/mappings/item_mappings.json"
MAPPING_CACHE_TTL_SECONDS = 3600  # 1 hour
FUZZY_THRESHOLD = 90
STRIPPED_FUZZY_THRESHOLD = 90

# Sysco brand/vendor prefix codes that precede the actual product description.
# Multi-word patterns must come before single-word to avoid partial stripping.
# Leading noise patterns that appear before the actual brand prefix or description.
# Handles OCR artefacts like "ONLY 2 LB", "3085CT", ".SYS", "AVG", stray letters.
_LEADING_NOISE_RE = re.compile(
    r'^(?:'
    r'ONLY\s*[\d.]+\s*(?:LB|OZ|GAL|KG|CT|LTR|FL\s*OZ)?\s*'  # "ONLY 2 LB", "ONLY 16 OZ"
    r'|[\d]+(?:CT|CS|OZ|LTR|GAL|LB|KG)?\s+'                   # "3085CT ", "15024 OZ "
    r'|[#.*]\s*'                                                 # "#", ".", "*" artefacts
    r'|AVG\s*'                                                   # "AVG" before brand code
    r'|(?:[A-Z]\s){1,2}'                                        # stray single letters "L ", "G "
    r')',
    re.IGNORECASE,
)

_SYSCO_PREFIX_RE = re.compile(
    r'^(?:'
    # Multi-word prefixes
    r'SYS\s+(?:CLS|IMP|REL|PRM|GRD|CUP|TOWEL|GLOVE|PAD)'
    r'|LA\s+BAND'
    r'|KG\s+CAPTO?'
    r'|KING\s+MI'
    r'|IMP\s*/\s*MCC'
    r'|AP\s*/\s*MCC'
    r'|MP\s*/\s*MCC'
    r'|KAP\s*[&EZ]\s*ZUB'
    r'|KAPSZUB|KAPEZUB'
    r"|D\s*'?\s*ALLAS|DALLAS"                                   # D'ALLAS / DALLAS spice brand
    # Single-word all-caps brand codes
    r'|WHLFCLS|WHLFIMP|WHLF[A-Z0-9]+'
    r'|GRECOSN|GRECOS[A-Z0-9]*'
    r'|COOPR|PATRPCK|EMBASSA|KONTOS'
    r'|BBR(?:LIMP|LCLS|L[A-Z0-9]+)'
    r'|FLEISHM[A-Z0-9]*'
    r'|AREZ(?:CLS|IMP|SVS|[A-Z0-9]+)'
    r'|ARZ[A-Z0-9]+'
    r'|CALMINI|CALMIN[A-Z0-9]*'
    r'|DELMNT[A-Z0-9]*'
    r'|SPART[A-Z0-9]+'
    r'|INTL[A-Z0-9]*'
    r'|PACKER[A-Z0-9]*'
    r'|LEPORT[A-Z0-9]*|PORT(?:CLS|PRD|[A-Z0-9]+)'             # LEPORTCLS, PORTCLS, PORTPRD
    r'|ALTACUC|ALTA[A-Z0-9]+'
    r'|VERSTNR|VERS[A-Z0-9]+'
    r'|STERAMN|STERAM[A-Z0-9]*'
    r'|MILLBAK|HIGHBAK|SUPRPTZ|INAUGTHOM|THRCRAB|MAEPLOY'
    r'|MINMAID|JDMTCLS|CASACLS|SIMPLOT|PILLSBY|HORMEL'
    r'|SYSCO\s+(?:PAD|CUP|FILTER|GLOVE)'
    r'|KEYSTON|ECOLAB|HEINZ|REGINA|ROLAND|GATORADE|LABELLA'
    r'|[A-Z]{3,8}(?:CLS|IMP|REL|PRM|GRD|PRD)'
    r')\s+',
    re.IGNORECASE,
)

# Trailing quantity/size codes that add noise without helping a product match.
# Matches tokens like "3CT", "2/5LB", "16/20", "12OZ", or bare 5+ digit numbers.
# Does NOT strip plain English words like BONELESS, SHREDDED, UNSALTED.
_TRAILING_NOISE_RE = re.compile(
    r'\s+(?:#\w+|\d{5,}|\d+[A-Z/\-]+[A-Z0-9]*|\d+/\d+)$',
    re.IGNORECASE,
)


def _strip_sysco_prefix(text: str) -> str:
    """
    Remove Sysco brand-code prefix (and optional trailing quantity codes) from
    a raw invoice description, exposing the plain product name for fuzzy matching.

    Handles leading OCR artefacts ("ONLY 2 LB", "3085CT", stray letters) before
    applying the brand-prefix strip.

    Returns the stripped text if it is at least 5 characters long and shorter
    than the original; otherwise returns the original unchanged.
    """
    # Step 0: remove leading OCR noise (ONLY X LB, count prefix, stray chars)
    pre = _LEADING_NOISE_RE.sub('', text).strip()

    # Step 1: remove brand prefix
    stripped = _SYSCO_PREFIX_RE.sub('', pre).strip()

    # If nothing changed in steps 0+1, try from original (avoid over-stripping)
    if stripped == text:
        stripped = pre

    # Step 2: remove trailing quantity/size codes up to twice
    for _ in range(2):
        candidate = _TRAILING_NOISE_RE.sub('', stripped).strip()
        if len(candidate) >= 5:
            stripped = candidate
        else:
            break

    if len(stripped) >= 5 and len(stripped) < len(text):
        return stripped
    return text


def load_mappings(force_refresh: bool = False) -> dict:
    """
    Returns four dicts:
      code_map        — { "9213489": "Udon Noodles", ... }
      desc_map        — { "NOODLE UDON JAPNSE": "Udon Noodles", ... }  (all vendors)
      vendor_desc_map — { "FARM ART": { "LETTUCE, ICEBERG, 24 CT": "Lettuce, Iceberg" }, ... }
      category_map    — { "Udon Noodles": {"category": "Drystock", ...}, ... }
    Uses local cache unless force_refresh=True.
    """
    cache = {"code_map": {}, "desc_map": {}, "vendor_desc_map": {}, "category_map": {}}

    if not force_refresh and os.path.exists(MAPPING_CACHE_PATH):
        # Check cache age — refresh if older than TTL
        import time
        cache_age = time.time() - os.path.getmtime(MAPPING_CACHE_PATH)
        if cache_age < MAPPING_CACHE_TTL_SECONDS:
            with open(MAPPING_CACHE_PATH) as f:
                return json.load(f)
        else:
            print(f"  Mapping cache is {cache_age/60:.0f}m old — refreshing from Sheet...")

    try:
        rows = get_sheet_values(SPREADSHEET_ID, f"{MAPPING_TAB}!A:G")
    except Exception:
        rows = []

    for row in rows[1:]:  # skip header row
        # Pad row to 7 columns
        while len(row) < 7:
            row.append("")
        vendor     = row[0].strip()          # A: vendor
        raw_desc   = re.sub(r'\s+', ' ', re.sub(r'[/\\]', ' ', row[1].strip())).upper()  # B: item_description (normalized)
        category   = row[2].strip()          # C: category
        primary    = row[3].strip()          # D: primary_descriptor
        secondary  = row[4].strip()          # E: secondary_descriptor
        canonical  = row[5].strip()          # F: canonical_name
        item_code  = row[6].strip()          # G: sysco_item_code

        if not canonical:
            continue
        if item_code:
            cache["code_map"][item_code] = canonical
        if raw_desc:
            cache["desc_map"][raw_desc] = canonical
            # Also store under vendor-scoped map
            if vendor:
                vendor_key = vendor.upper()
                cache["vendor_desc_map"].setdefault(vendor_key, {})[raw_desc] = canonical
        # Build category lookup keyed by canonical name
        if category:
            cache.setdefault("category_map", {})[canonical] = {
                "category":            category,
                "primary_descriptor":  primary,
                "secondary_descriptor": secondary,
            }

    os.makedirs(os.path.dirname(MAPPING_CACHE_PATH), exist_ok=True)
    with open(MAPPING_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    return cache


def resolve_item(item: dict, mappings: dict, vendor: str = "") -> dict:
    """
    Attempt to map a line item to a canonical name.

    Matching priority:
      1. Sysco item code (most reliable — only Sysco invoices carry SUPC codes)
      2. Vendor-scoped exact description match
      3. Vendor-scoped fuzzy description match
      4. Global exact description match (all vendors)
      5. Global fuzzy description match (all vendors)
      6. Sysco brand-prefix stripping → fuzzy match against canonical names

    Returns the item dict enriched with:
      canonical             — resolved name or None
      confidence            — "code" | "vendor-exact" | "vendor-fuzzy" |
                              "exact" | "fuzzy" | "stripped-fuzzy" | "unmatched"
      score                 — 0-100
      category              — top-level category (e.g. "Produce") or ""
      primary_descriptor    — mid-level grouping (e.g. "Leaf") or ""
      secondary_descriptor  — fine-level grouping (e.g. "Cow") or ""
    """
    code_map        = mappings.get("code_map", {})
    desc_map        = mappings.get("desc_map", {})
    vendor_desc_map = mappings.get("vendor_desc_map", {})
    category_map    = mappings.get("category_map", {})

    # Guard: don't attempt fuzzy matching on items with no description.
    # Without a description, fuzzy matching produces garbage results.
    # Still allow Sysco item code matches (those are reliable without a description).
    item_code = item.get("sysco_item_code", "")
    raw_desc = item.get("raw_description", "").strip()
    if not raw_desc and not item_code:
        return {**item, "canonical": None, "confidence": "unmatched", "score": 0,
                "category": "", "primary_descriptor": "", "secondary_descriptor": ""}

    vendor_map = vendor_desc_map.get(vendor.upper(), {}) if vendor else {}

    def _attach_category(result: dict) -> dict:
        canonical = result.get("canonical") or ""
        cat_info  = category_map.get(canonical, {})
        return {
            **result,
            "category":              cat_info.get("category", ""),
            "primary_descriptor":    cat_info.get("primary_descriptor", ""),
            "secondary_descriptor":  cat_info.get("secondary_descriptor", ""),
        }

    # 1. Sysco item code (most reliable)
    if item_code and item_code in code_map:
        return _attach_category({**item, "canonical": code_map[item_code], "confidence": "code", "score": 100})

    normalized = re.sub(r'[/\\]', ' ', item.get("raw_description", "")).strip().upper()
    normalized = re.sub(r'\s+', ' ', normalized)

    # 2. Vendor-scoped exact match
    if vendor_map and normalized in vendor_map:
        return _attach_category({**item, "canonical": vendor_map[normalized],
                                 "confidence": "vendor-exact", "score": 100})

    # 3. Vendor-scoped fuzzy match
    if vendor_map and normalized:
        best_match, score, _ = process.extractOne(
            normalized,
            vendor_map.keys(),
            scorer=fuzz.token_sort_ratio,
        )
        if score >= FUZZY_THRESHOLD:
            return _attach_category({**item, "canonical": vendor_map[best_match],
                                     "confidence": "vendor-fuzzy", "score": score})

    # 4. Global exact match
    if normalized in desc_map:
        return _attach_category({**item, "canonical": desc_map[normalized],
                                 "confidence": "exact", "score": 100})

    # 5. Global fuzzy match
    if desc_map and normalized:
        best_match, score, _ = process.extractOne(
            normalized,
            desc_map.keys(),
            scorer=fuzz.token_sort_ratio,
        )
        if score >= FUZZY_THRESHOLD:
            return _attach_category({**item, "canonical": desc_map[best_match],
                                     "confidence": "fuzzy", "score": score})

    # 6. Strip Sysco brand prefix and fuzzy match against canonical names directly.
    #    Sysco descriptions like "WHLFCLS ROMAINE HEARTS 3CT" → "ROMAINE HEARTS 3CT"
    #    which can then match the canonical "Romaine" at a lower threshold.
    if category_map and normalized:
        stripped = _strip_sysco_prefix(normalized)
        if stripped != normalized:
            canonical_names = list(category_map.keys())
            result = process.extractOne(
                stripped,
                canonical_names,
                scorer=fuzz.token_set_ratio,
                processor=fuzz_utils.default_process,
            )
            if result and result[1] >= STRIPPED_FUZZY_THRESHOLD and \
                    fuzz.token_sort_ratio(stripped, result[0],
                                         processor=fuzz_utils.default_process) >= 75:
                best_canonical, score = result[0], result[1]
                return _attach_category({
                    **item,
                    "canonical":  best_canonical,
                    "confidence": "stripped-fuzzy",
                    "score":      score,
                })

    return {**item, "canonical": None, "confidence": "unmatched", "score": 0,
            "category": "", "primary_descriptor": "", "secondary_descriptor": ""}


_JUNK_RE = re.compile(
    r'^\s*$'
    r'|FUEL\s*SURCHARGE'
    r'|CREDIT\s*CARD\s*(?:SRCHRG|CHARGE)'
    r'|REMOTE.?STOCK'
    r'|GROUP\s*TOTAL'
    r'|ORDER\s*SUMMARY'
    r'|MISC\s*CHARGES?'
    r'|CHARGE\s+FOR'
    r'|SALES\s*TAX'
    r'|PA\s+SALES\s+TAX'
    r'|DELIVERY\s*FEE'
    r'|ASK\s+YOUR\s+MA'
    r'|\*{3,}'
    r'|T/WT='
    r'|DAIRY\s*\*{2}'
    r'|OUT/STOCK'
    r'|PART/ORD'
    r'|SUBSTITUTE\s*$'
    r'|^COM$'
    r'|^FS-\w+$'
    r'|^\d+$'
    r'|^REMIT\s+TO'
    r'|^UNITED\s+STATES'
    r'|^PRICE\s*$'
    r'|^TOTAL\s*$'
    r'|^AMOUNT\s*$'
    r'|^CLOSE:\s*$'
    r'|^OPEN:\s*$'
    r'|^P\.?O\.?\s*BOX'
    r'|^PHILADELPHIA,?\s+PA'
    r'|^QTY\s+ADJUSTMENT'
    r'|^INVOICE\s+ADJUSTMENT'
    r'|^GROSS\s+WT'
    r'|^SYSCO\s+(?:NATURAL|PRODUCE\s+CAN)'
    r'|Alley\.?\s+There\s+is'
    r'|leave\s+at\s+that\s+door'
    r'|no\s+longer\s+available'
    r'|can\s+send\s+\d+'
    r'|figure\s+something\s+else'
    r'|day\s+notice'
    r'|^oneless\s'
    r'|^\d+oz\s+Bulk\s+Pack'
    r'|DELIVERY\s+[Ss]ervice\s+fee'
    r'|NOT\s+AVAILABLE'
    r'|non-stock\s+item\s+delivered'
    r'|Our\s+Order\s+Number'
    r'|^1\.00\s+HALF$'
    r'|^IMP$'
    r'|^GAR\d+[A-Z]*$'
    r'|^B\d{3}[A-Z]+$'
    r'|^BKB\d+[A-Z]+$'
    r'|^SYR\d+$'
    r'|^FL-[A-Z]+-\d+$'
    r'|^6#10$'
    r'|CANNED\s*&\s*DRY\s*\*'
    r'|^[\d\s.,/]+$'
    r'|CHEMICAL\s+JANITORIAL\s+GROUP',
    re.IGNORECASE,
)


def _is_junk_item(item: dict) -> bool:
    """Return True if the item is a non-product line (surcharge, header, etc.)."""
    desc = item.get("raw_description", "")
    return bool(_JUNK_RE.search(desc))


def map_items(parsed_items: list[dict], force_refresh: bool = False,
              mappings: dict = None, vendor: str = "") -> list[dict]:
    """
    Enrich each parsed line item with its canonical name.
    Automatically filters out junk lines (surcharges, headers, totals)
    before mapping.

    Args:
        parsed_items:  list of item dicts from parse_invoice()
        force_refresh: reload mappings from Google Sheet
        mappings:      pre-loaded mappings dict (avoids reload)
        vendor:        canonical vendor name from parse_invoice() — used for
                       vendor-scoped matching before falling back to global
    """
    if mappings is None:
        mappings = load_mappings(force_refresh=force_refresh)

    # Filter junk lines before mapping
    clean_items = []
    junk_count = 0
    for item in parsed_items:
        if _is_junk_item(item):
            junk_count += 1
        else:
            clean_items.append(item)

    if junk_count:
        print(f"  Filtered {junk_count} non-product lines (surcharges, headers, etc.)")

    results = [resolve_item(item, mappings, vendor=vendor) for item in clean_items]

    unmatched = [r for r in results if r["confidence"] == "unmatched"]
    if unmatched:
        print(f"\n  {len(unmatched)} item(s) need mapping — add them to the '{MAPPING_TAB}' tab:")
        print(f"  {'Item Code':<12} {'OCR Description':<40} {'→ Your Name'}")
        print(f"  {'-'*12} {'-'*40} {'-'*20}")
        for u in unmatched:
            code = u.get("sysco_item_code", "")
            desc = u.get("raw_description", "")[:40]
            print(f"  {code:<12} {desc:<40}")
        print()

    return results
