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
from abbreviations import expand_abbreviations

MAPPING_CACHE_PATH = "invoice_processor/mappings/item_mappings.json"
MAPPING_CACHE_TTL_SECONDS = 3600  # 1 hour
FUZZY_THRESHOLD = 90
STRIPPED_FUZZY_THRESHOLD = 90
CHAR_RATIO_THRESHOLD = 95  # tighter than token because char-level is more
                           # sensitive to short strings and noise


# Discriminator tokens: if a canonical name contains any of these AND the
# raw description doesn't, the match is rejected. Prevents the stemmed and
# char tiers from collapsing semantically distinct forms (fresh vs dried
# shiitake, raw vs cooked beef, etc.) that happen to share most tokens.
_CANONICAL_QUALIFIERS = {
    'dried', 'frozen', 'canned', 'cooked', 'raw',
    'powdered', 'powder', 'smoked', 'pickled', 'jarred',
    'bottled', 'concentrated', 'condensed', 'decaf', 'instant',
    'whole', 'ground', 'minced', 'shredded', 'grated',
    'sliced', 'diced', 'chopped', 'crushed', 'pureed',
    'roasted', 'toasted', 'baked', 'fried', 'steamed',
    'fresh',  # only catches "fresh X" canonical vs generic raw, not the reverse
}


def _has_missing_qualifier(raw_stemmed: str, canonical_stemmed: str) -> bool:
    """Return True if the canonical has a qualifier token that the raw
    description doesn't. When True, the match should be rejected — the
    canonical is semantically more specific and the raw probably means
    something different."""
    raw_tokens = set(raw_stemmed.lower().split())
    canon_tokens = set(canonical_stemmed.lower().split())
    missing = (canon_tokens & _CANONICAL_QUALIFIERS) - raw_tokens
    return bool(missing)


# Token-overlap gate — applied to fuzzy tiers as a pre-commit sanity check.
# Fuzzy scoring (token_sort_ratio, token_set_ratio) can produce semantically
# nonsense matches that pass the threshold (e.g. SPICE GARLIC PWDR →
# Cinnamon, Ground at score >=90 because of shared structural tokens). This
# gate enforces: if the matched canonical doesn't share at least one
# meaningful stemmed 3+letter token with the raw description, reject and
# try the next tier. Replicates the after-the-fact logic in
# audit_suspect_mappings as a pre-commit guard so the bad FK never lands
# in the DB.
#
# Uses STEMMED comparison (via _stem_text) so legitimate plural/singular
# matches (PINEAPPLES → Pineapple) are still allowed. NOT applied to
# tier 6c (char-level fallback) because that tier exists specifically to
# catch spelling variants (Canteloupe → Cantaloupe) where stems differ.


def _has_token_overlap(raw: str, canonical: str) -> bool:
    """True if raw_description and canonical share at least one
    stemmed 3+letter token. Used as a fuzzy-tier pre-commit gate to
    reject zero-overlap nonsense matches. Stemming via _stem_text
    means PINEAPPLES → Pineapple still passes (both stem to 'pineapple')
    while SPICE GARLIC PWDR → Cinnamon, Ground does not."""
    raw_tokens = set(_stem_text(raw).split())
    can_tokens = set(_stem_text(canonical).split())
    return bool(raw_tokens & can_tokens)


# Per-vendor threshold overrides for the vendor_fuzzy tier.
# Exceptional Foods has catch-weight descriptions that lose tokens faster
# than typical vendors (e.g. "1.00 CS Bacon Applewood Slice Martins 30530"
# has vendor codes + item numbers that reduce fuzzy scores). Loosening to
# 85 catches legitimate matches without materially increasing false positives.
_VENDOR_FUZZY_THRESHOLDS = {
    'EXCEPTIONAL FOODS': 85,
    'DELAWARE COUNTY LINEN': 85,  # low-volume, high variability
    'COLONIAL VILLAGE MEAT MARKETS': 85,  # handwritten OCR
}


def _fuzzy_threshold_for(vendor: str) -> int:
    """Return the fuzzy-match threshold for a vendor, falling back to the
    global default. Case-insensitive lookup."""
    return _VENDOR_FUZZY_THRESHOLDS.get(vendor.upper(), FUZZY_THRESHOLD)


# Token stemmer: lowercases, strips punctuation, and crudely removes
# trailing 's' for simple plurals (4+ char words, not ending in 'ss').
# Matches what audit_suspect_mappings does — brings those semantics into
# the mapper so pluralization no longer depends on token_set_ratio luck.
_STEM_TOKEN_RE = re.compile(r'[A-Za-z]{3,}')


def _stem_text(text: str) -> str:
    """Lowercase + strip punctuation + stem plural tokens. Returns a
    space-joined string of stemmed tokens, suitable for fuzzy comparison.

    Handles common food-domain plural patterns (longest suffix wins):
      'rries' → 'rry'  (berries → berry, raspberries → raspberry)
      'ovies' → 'ovy'  (anchovies → anchovy)
      'atoes' → 'ato'  (tomatoes → tomato, potatoes → potato)
      'goes'  → 'go'   (mangoes → mango)
      'ches'  → 'ch'   (peaches → peach)
      'shes'  → 'sh'   (dishes → dish)
      'xes'   → 'x'    (boxes → box)
      's'     → ''     (fallback — bagels → bagel, apples → apple)

    Patterns are suffix-specific so cookies/brownies/movies (singulars
    end in 'ie') and shoes (singular ends in 'oe') don't get
    over-stemmed. Adding new patterns is safe — keep them suffix-anchored
    and long enough that incidental collisions are unlikely."""
    tokens = []
    for t in _STEM_TOKEN_RE.findall(text or ''):
        low = t.lower()
        if low.endswith('rries') and len(low) >= 6:
            low = low[:-3] + 'y'
        elif low.endswith('ovies') and len(low) >= 7:
            low = low[:-3] + 'y'
        elif low.endswith('atoes') and len(low) >= 6:
            low = low[:-2]
        elif low.endswith('goes') and len(low) >= 5:
            low = low[:-2]
        elif low.endswith('ches') and len(low) >= 5:
            low = low[:-2]
        elif low.endswith('shes') and len(low) >= 5:
            low = low[:-2]
        elif low.endswith('xes') and len(low) >= 4:
            low = low[:-2]
        elif len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
            low = low[:-1]
        tokens.append(low)
    return ' '.join(tokens)


# Sysco section header → Product.category candidates. When the Sysco
# parser tags an item with a section (DAIRY, PRODUCE, MEATS, etc.),
# we can narrow fuzzy-match candidate pool to canonicals whose Product
# category aligns. Two-pass: if nothing matches in the restricted pool,
# fall back to unrestricted (preserves recall when Product.category is
# wrong or empty in the DB).
#
# Sections map to MULTIPLE categories where ambiguous. Keys are substring-
# matched against the section header UPPERcased, so "**** CANNED & DRY ****"
# matches both "CANNED" and "DRY" — union of both target sets is used.
_SYSCO_SECTION_TO_CATEGORIES = {
    'DAIRY':      ['Dairy'],                  # Cheese collapsed into Dairy in 0035
    'PRODUCE':    ['Produce'],
    'MEATS':      ['Proteins'],
    'MEAT':       ['Proteins'],
    'POULTRY':    ['Proteins'],
    'SEAFOOD':    ['Proteins'],
    'CANNED':     ['Drystock'],  # Condiments/Sauces unified into Drystock in 0052
    'DRY':        ['Drystock', 'Spices'],
    'PAPER':      ['Smallwares'],   # Paper/Disposable renamed in 0057
    'DISPOSABLE': ['Smallwares'],
    'JANITORIAL': ['Chemicals'],
    'CHEMICAL':   ['Chemicals'],
    'BEVERAGE':   ['Coffee/Concessions'],   # Beverages cat doesn't exist in DB
    'BAKERY':     ['Bakery'],
    'DELI':       ['Proteins', 'Dairy'],      # Deli case carries cured meats + sliced cheese
    'SPICES':     ['Spices'],
    'GROCERY':    ['Drystock'],
    # FROZEN intentionally excluded — too ambiguous (frozen meat / frozen
    # produce / frozen bakery all plausible). No filter applied.
}


def _candidates_for_section(section: str, category_map: dict) -> list[str]:
    """Return canonical names restricted to categories matching the
    Sysco section. Empty list when section is unknown/empty or when
    the filter would leave an untenable pool (<3 candidates — fall
    through to full pool in that case)."""
    if not section:
        return []
    section_upper = section.upper()
    target_categories: set[str] = set()
    for key, cats in _SYSCO_SECTION_TO_CATEGORIES.items():
        if key in section_upper:
            target_categories.update(cats)
    if not target_categories:
        return []
    restricted = [c for c, info in category_map.items()
                  if info.get('category') in target_categories]
    # If the restricted pool is too small, fuzzy will be noisy. Signal
    # "no useful filter" so caller falls back to unrestricted.
    return restricted if len(restricted) >= 3 else []

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
    r'|PROPACK|PROPAK|QUAKER'
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


# OCR concatenation patterns common in Sysco invoice descriptions, where
# the OCR pass loses spacing between numeric quantities and unit codes
# or unit codes and brand-prefix letters. Cleaned BEFORE the mapper
# tokenizes/fuzzy-matches so the noise tokens become separable.
#
# Patterns (longest applied first within each step):
#   1. '<digits>0Z'  → '<digits> OZ'   — '0Z' is the OCR misread of 'OZ'
#                                        when zero-vs-O confusion happens
#                                        (9620Z → 962 OZ, 16810Z → 1681 OZ,
#                                         ONLY180Z → ONLY18 OZ)
#   2. 'OZCITVCLS'-style — known unit prefix flowing into 4+ caps run
#      (Sysco brand prefix). Conservative whitelist of 2-3-letter units.
_OCR_0Z_RE = re.compile(r'(\d+)0Z\b')
_OCR_UNIT_PREFIX_RE = re.compile(r'\b(OZ|LB|GAL|CT|CS|DZ|EA|KG|ML)([A-Z]{4,})')


def _ocr_cleanup(text: str) -> str:
    """Insert spaces at OCR concatenation boundaries. Idempotent."""
    if not text:
        return text
    text = _OCR_0Z_RE.sub(r'\1 OZ', text)
    text = _OCR_UNIT_PREFIX_RE.sub(r'\1 \2', text)
    return text


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


def _bootstrap_django_if_needed():
    """Ensure Django is configured. Idempotent — no-op if already set up.
    Mirrors the pattern in db_write.py so mapper can run from either a
    cron script context (no Django at import time) or a Django mgmt-
    command context (Django already bootstrapped)."""
    if not os.environ.get('DJANGO_SETTINGS_MODULE'):
        import sys as _sys
        import django as _django
        os.environ['DJANGO_SETTINGS_MODULE'] = 'myproject.settings'
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        _django.setup()


def _load_from_db() -> dict:
    """Build the 4-dict mapping shape from the ProductMapping DB table.

    After Step 1 of the sheet→DB migration ran, ProductMapping is the
    canonical source of truth (~1,445 rows mirroring the Item Mapping
    sheet). This eliminates the sheet-read on every cron cycle and the
    forward-looking damage class from upstream Product renames.

    Same return shape as the legacy sheet path so resolve_item is
    unchanged."""
    _bootstrap_django_if_needed()
    from myapp.models import ProductMapping

    cache = {"code_map": {}, "desc_map": {}, "vendor_desc_map": {}, "category_map": {}}

    rows = (ProductMapping.objects
            .select_related('vendor', 'product')
            .filter(product__isnull=False)
            .values('description', 'supc',
                    'vendor__name',
                    'product__canonical_name',
                    'product__category',
                    'product__primary_descriptor',
                    'product__secondary_descriptor'))

    for r in rows:
        canonical = r['product__canonical_name']
        if not canonical:
            continue
        # Normalize description the same way the sheet path did, so
        # vendor_exact / fuzzy keys remain comparable.
        raw_desc = re.sub(r'\s+', ' ',
                          re.sub(r'[/\\]', ' ', (r['description'] or '').strip())).upper()
        supc = (r['supc'] or '').strip()
        vendor_name = (r['vendor__name'] or '').strip()

        if supc:
            cache["code_map"][supc] = canonical
        if raw_desc:
            cache["desc_map"][raw_desc] = canonical
            if vendor_name:
                cache["vendor_desc_map"].setdefault(vendor_name.upper(), {})[raw_desc] = canonical

        # category_map keyed by canonical — Product table is the source of truth
        # after the convention migration. Each canonical has exactly one
        # taxonomy triple (no row-level ambiguity like the sheet had).
        cache["category_map"][canonical] = {
            "category":             r['product__category'] or '',
            "primary_descriptor":   r['product__primary_descriptor'] or '',
            "secondary_descriptor": r['product__secondary_descriptor'] or '',
        }

    return cache


# _load_from_sheet() retired 2026-05-02 — the Google Sheet's "Item Mapping"
# tab was deleted. ProductMapping table (DB) is the sole source of truth.
# load_mappings() emits a clear error when the table is empty rather than
# silently falling back to a deleted sheet tab.


def load_mappings(force_refresh: bool = False) -> dict:
    """
    Returns four dicts:
      code_map        — { "9213489": "Udon Noodles", ... }
      desc_map        — { "NOODLE UDON JAPNSE": "Udon Noodles", ... }  (all vendors)
      vendor_desc_map — { "FARM ART": { "LETTUCE, ICEBERG, 24 CT": "Lettuce, Iceberg" }, ... }
      category_map    — { "Udon Noodles": {"category": "Drystock", ...}, ... }

    Source of truth: DB ProductMapping table (after Step 1 backfill).
    Sheet path is a fallback when ProductMapping is empty.

    Uses local file cache unless force_refresh=True. The on-disk JSON
    cache remains useful for non-Django scripts and for hot-reload speed
    in the cron path; TTL is the same as before (1 hour).
    """
    if not force_refresh and os.path.exists(MAPPING_CACHE_PATH):
        import time
        cache_age = time.time() - os.path.getmtime(MAPPING_CACHE_PATH)
        if cache_age < MAPPING_CACHE_TTL_SECONDS:
            with open(MAPPING_CACHE_PATH) as f:
                return json.load(f)
        else:
            print(f"  Mapping cache is {cache_age/60:.0f}m old — refreshing from DB...")

    cache = _load_from_db()

    # ProductMapping empty (pre-backfill, fresh install, DB corruption).
    # Sean 2026-05-02: the sheet's Item Mapping tab has been retired, so
    # the legacy sheet fallback is no longer viable — _load_from_sheet
    # would read a deleted tab and return empty. Better to fail loud with
    # a recovery hint than silently produce zero mappings.
    if not cache["desc_map"] and not cache["code_map"]:
        print("  [!] ProductMapping table is empty. Mapping will not work "
              "until restored. Recovery options:\n"
              "      1. Restore from a recent DB backup\n"
              "      2. Re-run sync_item_mapping_from_sheet against an old\n"
              "         sheet snapshot (if you have one)\n"
              "      3. Re-curate from invoice history via discover_unmapped\n"
              "         + /mapping-review/")

    os.makedirs(os.path.dirname(MAPPING_CACHE_PATH), exist_ok=True)
    with open(MAPPING_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    return cache


# Non-product line patterns — surcharges, fees, credits, page-summary noise
# that appear in invoice OCR as line items but aren't recipe-costing material.
# Matched by substring in the raw_description (uppercase-compared). Tagged
# with confidence='non_product' so they don't pollute the mapping % metric
# and don't clutter Mapping Review. Still retain dollar value for budget
# tracking — we just route them out of the product-catalog flow.
_NON_PRODUCT_PATTERNS = (
    "CHGS FOR FUEL",
    "FUEL SURCHARGE",
    "DELIVERY FEE",
    "DELIVERY CHG",
    "CREDIT MEMO",
    "ORDER SUMMARY",
    "MISC CHARGES",
    "MISC CHARGE",
    "ADMIN FEE",
    "SERVICE CHARGE",
    "ENV CHARGE",
    "ENVIRONMENTAL FEE",
    "SURCHARGE",
    "FREIGHT",
)


def _is_non_product(raw_desc: str) -> bool:
    """True when the raw description looks like a vendor surcharge, fee,
    credit, or invoice-footer noise — none of which should go through the
    product-mapping flow."""
    if not raw_desc:
        return False
    upper = raw_desc.upper()
    return any(p in upper for p in _NON_PRODUCT_PATTERNS)


def resolve_item(item: dict, mappings: dict, vendor: str = "") -> dict:
    """
    Attempt to map a line item to a canonical name.

    Matching priority:
      0. Non-product classifier (surcharges, fees, credits) — short-circuit
         out of product mapping entirely; these don't belong to the recipe
         catalog and shouldn't count against mapping metrics.
      1. Sysco item code (most reliable — only Sysco invoices carry SUPC codes)
      2. Vendor-scoped exact description match
      3. Vendor-scoped fuzzy description match
      4. Global exact description match (all vendors)
      5. Global fuzzy description match (all vendors)
      6. Sysco brand-prefix stripping → fuzzy match against canonical names

    Returns the item dict enriched with:
      canonical             — resolved name or None
      confidence            — "code" | "vendor_exact" | "vendor_fuzzy" |
                              "exact" | "fuzzy" | "stripped_fuzzy" |
                              "keyword_batch" | "manual_review" |
                              "non_product" | "unmatched"
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

    # Priority 0: non-product classifier. Fuel surcharges, delivery fees,
    # credit memos, and invoice-footer noise shouldn't run through the
    # product catalog. Tag and return with empty canonical so budget tools
    # can still count the dollar value while recipe/mapping tools skip it.
    if _is_non_product(raw_desc):
        return {**item, "canonical": None, "confidence": "non_product",
                "score": 100, "category": "", "primary_descriptor": "",
                "secondary_descriptor": ""}

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
                                 "confidence": "vendor_exact", "score": 100})

    # 3. Vendor-scoped fuzzy match (with per-vendor threshold).
    # Token-overlap gate is intentionally NOT applied here — vendor_desc_map
    # entries are Sean's curated sheet mappings. A fuzzy hit means the raw
    # description is similar to a description Sean explicitly assigned to a
    # canonical, even when the canonical is an abbreviation (AMER →
    # American), plural (PEACH → Peaches), or category synonym (BEEF PATTY
    # → Burgers). Trust the curation at this layer.
    if vendor_map and normalized:
        vendor_threshold = _fuzzy_threshold_for(vendor)
        best_match, score, _ = process.extractOne(
            normalized,
            vendor_map.keys(),
            scorer=fuzz.token_sort_ratio,
        )
        if score >= vendor_threshold:
            return _attach_category({**item, "canonical": vendor_map[best_match],
                                     "confidence": "vendor_fuzzy", "score": score})

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
            candidate_canonical = desc_map[best_match]
            if _has_token_overlap(normalized, candidate_canonical):
                return _attach_category({**item, "canonical": candidate_canonical,
                                         "confidence": "fuzzy", "score": score})
            # else: fall through to stripped-fuzzy tier

    # 6. Strip Sysco brand prefix and fuzzy match against canonical names directly.
    #    Sysco descriptions like "WHLFCLS ROMAINE HEARTS 3CT" → "ROMAINE HEARTS 3CT"
    #    which can then match the canonical "Romaine" at a lower threshold.
    #
    # Two-pass when the Sysco parser tagged a section: first try against
    # canonicals in matching Product categories (higher precision), then
    # fall back to full pool if no match found. Preserves recall when
    # Product.category is empty/wrong in the DB.
    if category_map and normalized:
        canonical_names_full = list(category_map.keys())
        section_pool = _candidates_for_section(item.get('section', ''),
                                               category_map)
        # Pools to try in order. Section-filtered first when available.
        pools_to_try = []
        if section_pool:
            pools_to_try.append(section_pool)
        pools_to_try.append(canonical_names_full)

        # OCR cleanup (9620Z → 962 OZ, OZCITVCLS → OZ CITVCLS) is scoped
        # to tier 6 ONLY — tiers 2-5 lookup against sheet-curated keys
        # that include the original OCR garble; cleaning the input here
        # would break those exact-match dictionaries. At tier 6 we're
        # matching against English canonical names, so cleanup is a net
        # win — it exposes 'OZ' and other unit/word boundaries that the
        # tokenizer was missing. Surfaced from 2026-04-25 audit.
        ocr_cleaned = _ocr_cleanup(normalized)
        # Expand abbreviations BEFORE fuzzy matching against canonical names —
        # canonicals are English words ('Chicken Breast', 'Pork Shoulder',
        # 'Heavy Cream') so expansion bridges the OCR-shorthand gap.
        # ('BRST CHKN BNLS' → 'Breast Chicken Boneless' now matches
        # 'Chicken Breast' canonical at high score; previously zero overlap.)
        # Limited to tier 6 — tiers 2-5 match against sheet entries which
        # share the same abbreviations as raw input.
        stripped = _strip_sysco_prefix(ocr_cleaned)
        expanded = expand_abbreviations(stripped)
        stripped_for_stem = expanded if expanded != normalized else normalized
        stemmed_desc = _stem_text(stripped_for_stem)
        # The token-overlap gate also operates on expanded text so post-strip
        # English words can satisfy the gate without abbreviations defeating it.
        gate_text = expanded

        for canonical_names in pools_to_try:
            # 6a. Stripped prefix + token_set_ratio.
            # Use EXPANDED for the fuzzy match (bridges abbreviations) but
            # STRIPPED (no expansion) for the secondary 75-threshold shape
            # check — expansion lengthens the string and unfairly trips it.
            # Token-overlap gate still uses expanded so abbreviations don't
            # defeat the gate.
            if expanded != normalized:
                result = process.extractOne(
                    expanded,
                    canonical_names,
                    scorer=fuzz.token_set_ratio,
                    processor=fuzz_utils.default_process,
                )
                if result and result[1] >= STRIPPED_FUZZY_THRESHOLD and \
                        fuzz.token_sort_ratio(stripped, result[0],
                                             processor=fuzz_utils.default_process) >= 75 and \
                        _has_token_overlap(gate_text, result[0]):
                    return _attach_category({
                        **item,
                        "canonical":  result[0],
                        "confidence": "stripped_fuzzy",
                        "score":      result[1],
                    })

            # 6b. Stemmed fuzzy with qualifier gate.
            # Token-overlap gate is intentionally NOT applied here — this
            # tier's token_set_ratio can catch single-token typos like
            # 'Canteloupe' → 'Cantaloupe' where stems differ. The
            # existing safeguards (90 threshold + token_sort_ratio>=75 +
            # qualifier check) are sufficient at this layer.
            if stemmed_desc:
                stemmed_canonicals = {_stem_text(c): c for c in canonical_names if c}
                stem_result = process.extractOne(
                    stemmed_desc,
                    list(stemmed_canonicals.keys()),
                    scorer=fuzz.token_set_ratio,
                )
                if stem_result and stem_result[1] >= STRIPPED_FUZZY_THRESHOLD and \
                        fuzz.token_sort_ratio(stemmed_desc, stem_result[0]) >= 75 and \
                        not _has_missing_qualifier(stemmed_desc, stem_result[0]):
                    best_canonical = stemmed_canonicals[stem_result[0]]
                    return _attach_category({
                        **item,
                        "canonical":  best_canonical,
                        "confidence": "stripped_fuzzy",
                        "score":      stem_result[1],
                    })

            # 6c. Char-level fallback with qualifier gate.
            # Token-overlap gate is intentionally NOT applied here — this
            # tier exists specifically to catch spelling variants
            # (Canteloupe → Cantaloupe) where stems differ. The high
            # CHAR_RATIO_THRESHOLD (95) + token_sort_ratio>=60 constraints
            # are the safeguard against nonsense matches at this layer.
            char_result = process.extractOne(
                stripped_for_stem,
                canonical_names,
                scorer=fuzz.ratio,
                processor=fuzz_utils.default_process,
            )
            if char_result and char_result[1] >= CHAR_RATIO_THRESHOLD:
                canon = char_result[0]
                if fuzz.token_sort_ratio(stripped_for_stem, canon,
                                         processor=fuzz_utils.default_process) >= 60 and \
                        not _has_missing_qualifier(_stem_text(stripped_for_stem),
                                                   _stem_text(canon)):
                    return _attach_category({
                        **item,
                        "canonical":  canon,
                        "confidence": "stripped_fuzzy",
                        "score":      char_result[1],
                    })

            # 6d. Subset-match tier — canonical's tokens are ALL contained
            # in the (expanded) raw description's stemmed token set. Catches
            # the 'X Danish' → 'Danish' / 'BRST CHKN BNLS' → 'Chicken Breast'
            # / 'Apple Cider Vinegar' → 'Apple Cider Vinegar' class that
            # token_set/sort scorers miss when the raw has many extra
            # modifier tokens dragging down the ratio. Prefers most-specific
            # (longest canonical by token count). Returns None if multiple
            # equally-specific matches (ambiguous → human reviews via the
            # quarantine queue). Routed through quarantine in db_write — the
            # match never auto-attaches an FK.
            subset_canon = _find_subset_canonical_in_pool(
                gate_text, canonical_names)
            if subset_canon:
                return _attach_category({
                    **item,
                    "canonical":  subset_canon,
                    "confidence": "subset_match",
                    "score":      95,
                })

    return {**item, "canonical": None, "confidence": "unmatched", "score": 0,
            "category": "", "primary_descriptor": "", "secondary_descriptor": ""}


# Packaging / quantity-noise tokens. A canonical that consists *only*
# of these is rejected as a subset-match candidate — matching "Bags"
# against "DRIED, APRICOT, 3 LB BAG" is locked onto the package, not
# the product. Stemmed forms (no trailing 's' since _stem_text strips).
_SUBSET_NOISE_TOKENS = frozenset({
    'bag', 'box', 'case', 'carton', 'pack', 'container', 'pouch',
    'jar', 'bottle', 'can', 'piece', 'each', 'unit', 'crate', 'tray',
    'lid', 'cup',  # too generic on their own — rely on multi-token canonicals
})

# Food-form HEAD nouns. When raw description contains one of these,
# a subset-match candidate must ALSO contain at least one head — else
# the candidate is matching only modifiers (Blueberry inside "Blueberry
# Muffin", Butter inside "Butter Croissant"), which is the wrong
# resolution. Stemmed forms.
_SUBSET_FOOD_FORM_HEADS = frozenset({
    # Bakery — locked 5-bucket taxonomy heads
    'muffin', 'scone', 'biscuit',
    'cookie', 'brownie', 'blondie',
    'cake', 'cupcake', 'cheesecake',
    'pie', 'cobbler', 'tart', 'turnover',
    'donut', 'doughnut',
    'croissant', 'danish',
    'eclair', 'strudel',
    'bread', 'loaf', 'baguette', 'roll', 'bun',
    'bagel', 'tortilla', 'wrap',
    'pita', 'naan', 'focaccia', 'ciabatta',
    # Dish forms — same head-noun logic applies (the FORM is the product
    # identity; flavor/filling tokens are modifiers)
    'soup', 'salad', 'sandwich',
    'patty', 'burger',
    'eggroll',  # post-abbreviation expansion this stays one token; also
    # gets split to 'egg' + 'roll' if expansion adds a space, in which
    # case 'roll' covers it.
    'pizza', 'calzone', 'stromboli',
})


def _find_subset_canonical_in_pool(raw: str, canonicals) -> str | None:
    """Return the most-specific canonical whose stemmed tokens are all
    contained in raw's stemmed tokens. None if no match or if multiple
    equally-specific matches (ambiguous).

    Rejection rules layered on top of pure subset:
      1. NOISE-ONLY canonicals — if every canonical token is a packaging
         noise word (Bag, Box, Cup), reject. The match is locking onto
         the container, not the product identity.
      2. HEAD-NOUN MISMATCH — if raw contains a food-form head noun
         (muffin, croissant, bun, pie, ...), the candidate must also
         contain at least one of the same heads. Otherwise the candidate
         is matching only modifier/filling tokens (Blueberry, Butter,
         Corn) and producing wrong-product suggestions.

    These rules surfaced from 2026-04-25 mapping-review queue audit:
      - "Blueberry Muffins" → "Blueberries"   (head=muffin, candidate=modifier)
      - "Butter Croissant"  → "Butter"        (head=croissant, candidate=modifier)
      - "BUN HOT DOG"       → "Hot Dogs"      (head=bun, candidate=filling)
      - "DRIED APRICOT 3 LB BAG" → "Bags"     (noise-only candidate)
    """
    raw_tokens = set(_stem_text(raw).split())
    if not raw_tokens:
        return None
    raw_heads = raw_tokens & _SUBSET_FOOD_FORM_HEADS

    matches = []   # [(canonical, n_tokens)]
    for canon in canonicals:
        ctokens = set(_stem_text(canon).split())
        if not ctokens:
            continue
        if not ctokens.issubset(raw_tokens):
            continue
        # Rule 1: reject candidates made entirely of packaging noise.
        if ctokens.issubset(_SUBSET_NOISE_TOKENS):
            continue
        # Rule 2: when raw has a food-form head, candidate must share one.
        if raw_heads and not (ctokens & raw_heads):
            continue
        matches.append((canon, len(ctokens)))
    if not matches:
        return None
    matches.sort(key=lambda x: -x[1])
    top_n = matches[0][1]
    top = [c for c, n in matches if n == top_n]
    return top[0] if len(top) == 1 else None


_JUNK_RE = re.compile(
    r'^\s*$'
    r'|^\[Sysco\s*#\d+\]$'   # unknown-SUPC placeholder from parser
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
    r'|CHEMICAL\s+JANITORIAL\s+GROUP'
    # Additional Sysco header/footer OCR artifacts surfaced 2026-04-21
    r'|CONFIDENTIAL\s+PROPERTY\s+OF\s+SYSCO'
    r'|^CUBE\s+QUOPSTOCK$'
    r'|^DELV\.?\s*DATE\s*$'
    r'|^DFL\d+\w+$'
    r'|FRESH["\'\s]*MENU\s+ITEM'
    r'|^INVOICE\s+NUMBER\s*$'
    r'|^ITEM\s+DESCRIPTION\s*$'
    r'|^MA:\s+\w+'                       # 'MA: T4CBZ DAVID CIANFARO'
    r'|MANIFEST#\s*\d+'                  # 'MANIFEST# 1238296 NORMAL DELIVERY'
    r'|^ONLY\s*\d+\s*KILOROLAND$'        # odd pickup-truncation artifact
    r'|^ONLY\d+GAL\s*$'                  # 'ONLY1GAL' — size orphan
    r'|^PURCHASE\s+ORDER\s*$'
    r'|^RIBEYE\d+\w*$'                   # 'RIBEYE00STH' — item-code orphan
    r'|TERMS\s*-?\s*PAST\s+DUE'
    r'|^YP\d+\w+$',                      # 'YP160CSYSA' — code orphan
    re.IGNORECASE,
)


_PLACEHOLDER_DESC_RE = re.compile(r'^\[Sysco\s*#\d+\]$')


def _is_junk_item(item: dict) -> bool:
    """Return True if the item is a non-product line (surcharge, header, etc.).

    Exception: items whose description is the '[Sysco #NNN]' placeholder —
    emitted by the parser when a real anchor has no inline OCR description
    (common on column-dump catch-weight items) — AND that carry a valid
    sysco_item_code + non-trivial price ARE real products, just unmapped.
    They should surface in the unmapped-review queue so a human or SUPC
    CSV import can assign a canonical name. Without this guard, ~$800+
    catch-weight items silently vanish between parser and DB.

    Narrow scope: this exception ONLY bypasses the placeholder pattern.
    Other junk patterns (FUEL SURCHARGE, section headers, etc.) still
    filter normally even when a spurious code gets attached to them.
    """
    desc = item.get("raw_description", "")
    if not _JUNK_RE.search(desc):
        return False

    # If the junk match is specifically the placeholder AND the item has
    # a real code + price, override to keep it.
    code = item.get("sysco_item_code", "")
    price = item.get("unit_price") or 0
    if (_PLACEHOLDER_DESC_RE.match(desc)
            and code and len(str(code)) >= 6
            and price > 5):
        return False

    return True


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
