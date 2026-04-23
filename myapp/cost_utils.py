"""
Cost calculation helpers. Converts invoice prices + recipe quantities into per-recipe $.

Returns None from any function when inputs are too ambiguous to compute — so callers
can show "—" in UI rather than lying with a fake number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


# ---- Case size parsing ----

@dataclass(frozen=True)
class CaseInfo:
    pack_count: int          # e.g., 12 bottles
    pack_size: Decimal       # e.g., 32 (oz each)
    pack_unit: str           # 'lb', 'oz', 'gal', 'fl_oz', 'ct', 'hd', 'bu'

    @property
    def total_in_base_unit(self) -> Decimal:
        """Total quantity in the canonical base unit for this type."""
        return Decimal(self.pack_count) * self.pack_size


_CASE_RE = re.compile(
    r'^\s*(?P<count>\d+)\s*/\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>LB|OZ|GAL|CT|HD|BU|PT|QT|FLOZ|FL_OZ|ML|L|KG|G|DOZ|DOZEN|DZ)\s*$',
    re.I,
)
_CASE_RE_SIMPLE = re.compile(
    r'^\s*(?P<count>\d+(?:\.\d+)?)\s*(?P<unit>LB|OZ|GAL|CT|HD|BU|PT|QT|FLOZ|FL_OZ|ML|L|KG|G|EACH|EA|DOZ|DOZEN|DZ)\s*$',
    re.I,
)
# '6/10CAN' is 6 × #10-size cans, not 6 packs of 10 items.
# #10 is a can-size designation (~109 oz each), so multiplying count×10 would
# overstate the pack by 10x. Treat as (count, 1, can) — N individual cans.
_TEN_CAN_RE = re.compile(r'^\s*(?P<count>\d+)\s*/\s*#?10\s*CAN\s*$', re.I)


def parse_case_size(s: str | None) -> Optional[CaseInfo]:
    """Parse invoice case_size strings like '12/32OZ' or '24CT'. None if unparseable."""
    if not s:
        return None
    s = s.strip()
    if not s or s in {'1', '2', '4'}:
        return None  # bare counts with no unit — too ambiguous
    m = _TEN_CAN_RE.match(s)
    if m:
        return CaseInfo(
            pack_count=int(m.group('count')),
            pack_size=Decimal('1'),
            pack_unit='can',
        )
    m = _CASE_RE.match(s)
    if m:
        return CaseInfo(
            pack_count=int(m.group('count')),
            pack_size=Decimal(m.group('size')),
            pack_unit=m.group('unit').lower().replace('floz', 'fl_oz'),
        )
    m2 = _CASE_RE_SIMPLE.match(s)
    if m2:
        unit = m2.group('unit').lower().replace('floz', 'fl_oz').replace('each', 'ct').replace('ea', 'ct')
        return CaseInfo(pack_count=1, pack_size=Decimal(m2.group('count')), pack_unit=unit)
    return None


# ---- Description-weight extraction (Phase 2A unlock for cost calc) ----

# "36/1#" or "1/40LB" — N packs of M pounds each.
# `#(?![\d.])` rejects '1#234' (# can't be followed by a digit/decimal —
# would be a SUPC code or part number, not a pound symbol). `LBS?\b`
# allows LB or LBS with word-boundary terminator.
_DESC_PER_LB = re.compile(
    r'(\d+\.?\d*)\s*/\s*(\d+\.?\d*)\s*(?:#(?![\d.])|LBS?\b)',
    re.IGNORECASE,
)
# "20 LB", "11LB", "5#", "50#" — single weight (the case total).
# Negative-lookbehind avoids capturing the "M" half of an N/M# pattern handled above.
_DESC_BARE_LB = re.compile(
    r'(?<![\d/])(\d+\.?\d*)\s*(?:#(?![\d.])|LBS?\b)',
    re.IGNORECASE,
)


def extract_weight_from_description(desc: str | None) -> Optional[str]:
    """Extract a `parse_case_size`-shaped weight string from a raw invoice
    description. Catches patterns the case_size column missed:
      - "CS Butter Prints 36/1# Unsalted Sweet" → '36/1LB'
      - "SWEET POTATO, YAMS, #1, 40 LB"        → '40LB'
      - "BROCCOLI, CROWNS, 20 LB"              → '20LB'
      - "RICOTTA, ITALIAN, 6/3 LB"             → '6/3LB'

    Returns None if no weight pattern is found. Used by
    `effective_case_size_for_cost` to fall back when the structured
    case_size column is bare-qty (e.g. '1', '4') or missing entirely.
    """
    if not desc:
        return None
    m = _DESC_PER_LB.search(desc)
    if m:
        return f'{m.group(1)}/{m.group(2)}LB'
    m = _DESC_BARE_LB.search(desc)
    if m:
        return f'{m.group(1)}LB'
    return None


# Bare 'N/M' (no unit) — interpreted as N packs of M lbs when nothing better is
# available. Sysco uses this format ('36/1' for butter prints, '6/12' for some
# packs). Sanity range guards against typos / non-weight ratios.
_BARE_N_OVER_M = re.compile(r'^(\d+)/(\d+(?:\.\d+)?)$')
_BARE_NM_MIN_LBS = Decimal('0.5')
_BARE_NM_MAX_LBS = Decimal('200')


# ---- Bushel-fraction extraction (Phase 6) ----
#
# Farm Art (and other produce vendors) express the container as a bushel
# fraction in the description, e.g. "CUCUMBERS, 1-1/9 BUSHEL". The case_size
# column only captures the bare quantity purchased ('1', '2', etc.), so the
# container weight must be recovered from the description. USDA PACA
# Handbook 28 publishes lb-per-bushel standards for commodity grains;
# AMS publishes conventional weights for produce. Values below are PACA
# container-net-weight minimums — vendors have no incentive to overfill.
_BUSHEL_TO_LB: dict[str, Decimal] = {
    # Produce (AMS conventional; lb/bushel)
    'cucumber':   Decimal('48'),
    'cucumbers':  Decimal('48'),
    'pepper':     Decimal('25'),   # bell, jalapeño, poblano — 1 1/9 bu ≈ 28 lb carton
    'peppers':    Decimal('25'),
    'eggplant':   Decimal('35'),
    'squash':     Decimal('44'),   # summer / zucchini
    'tomatillo':  Decimal('52'),
    'tomatillos': Decimal('52'),
    'tomato':     Decimal('53'),
    'tomatoes':   Decimal('53'),
    'collard':    Decimal('23'),
    'collards':   Decimal('23'),
    'greens':     Decimal('23'),   # generic leafy greens
    'bean':       Decimal('30'),   # green beans
    'beans':      Decimal('30'),
    'apple':      Decimal('42'),
    'apples':     Decimal('42'),
    'peach':      Decimal('48'),
    'peaches':    Decimal('48'),
}

# "1 1/9 BUSHEL", "1-1/9 BUSHEL", "1/2 BUSHEL" — fraction before BU/BUSHEL
_BUSHEL_FRAC_PREFIX = re.compile(
    r'(?:(\d+)[\s\-]+)?(\d+)\s*/\s*(\d+)\s*(?:BU|BUSHEL)\b',
    re.IGNORECASE,
)
# "BUSHEL 1-1/9" — fraction after BUSHEL keyword
_BUSHEL_FRAC_POSTFIX = re.compile(
    r'\b(?:BU|BUSHEL)\s+(?:(\d+)[\s\-]+)?(\d+)\s*/\s*(\d+)',
    re.IGNORECASE,
)
# Plain "BUSHEL" (spelled out) with no fraction — interpreted as 1 bushel.
# Critical: does NOT match bare "BU" to avoid colliding with bunch-count
# notation ("60 BU" = 60 bunches for cilantro/parsley/etc).
_BARE_BUSHEL = re.compile(r'\bBUSHEL\b', re.IGNORECASE)


def extract_bushel_fraction(desc: str | None) -> Optional[Decimal]:
    """Parse a bushel fraction from a raw invoice description.
    '1-1/9 BUSHEL' → 10/9 ≈ 1.111
    '1/2 BUSHEL'   → 0.5
    'BUSHEL'       → 1.0    (bare — spelled out, not 'BU')
    'HERB, CILANTRO, 60 BU' → None  (bunch-count, not bushel container)
    Returns None when no bushel notation is present.
    """
    if not desc:
        return None
    m = _BUSHEL_FRAC_PREFIX.search(desc) or _BUSHEL_FRAC_POSTFIX.search(desc)
    if m:
        whole = Decimal(m.group(1) or '0')
        num = Decimal(m.group(2))
        den = Decimal(m.group(3))
        if den == 0:
            return None
        return whole + num / den
    if _BARE_BUSHEL.search(desc):
        return Decimal('1')
    return None


def extract_bushel_case_size(desc: str | None, product_name: str | None) -> Optional[str]:
    """Convert a description-embedded bushel-fraction to a synthetic
    case_size string (e.g. '53LB' for 1-1/9 bushel cucumber @ 48 lb/bu).
    Returns None when description lacks bushel notation, or the product
    has no _BUSHEL_TO_LB entry."""
    if not desc or not product_name:
        return None
    fraction = extract_bushel_fraction(desc)
    if fraction is None:
        return None
    n = _normalize_name(product_name)
    lb_per_bu = _BUSHEL_TO_LB.get(n)
    if lb_per_bu is None:
        for part in reversed(n.split('_')):
            if part in _BUSHEL_TO_LB:
                lb_per_bu = _BUSHEL_TO_LB[part]
                break
    if lb_per_bu is None:
        return None
    total_lb = (fraction * lb_per_bu).quantize(Decimal('0.1'))
    return f'{total_lb}LB'


def effective_case_size_for_cost(case_size: str | None,
                                  raw_description: str | None,
                                  product_default: str | None = None) -> str:
    """Return the most-useful case_size string for cost calc, in priority order:
      1. The literal `case_size` if it already parses (no change).
      2. Weight extracted from the invoice description (`'5# BAG'`, `'36/1#'`).
      3. `Product.default_case_size` if it parses — populated by
         `infer_product_default_case_sizes` from the mode of historical
         invoice case_sizes. Used when the current invoice's case_size
         is bare-qty / OCR-mangled but the product has a known canonical
         pack (e.g. Milk default='4/1GAL', Garlic='4/1GAL').
      4. Bare 'N/M' (no unit) treated as 'N/MLB' if N×M is within a sane
         range (0.5–200 lbs). Last-resort heuristic for Sysco notation
         where the unit is conventionally LB.
    Returns the original `case_size` (possibly unparseable) when no
    strategy yields a better value, so the caller's downstream behavior
    is unchanged in that case.

    NOTE: this is the SINGLE-best-effort form. Callers that want to try
    multiple candidates against `ingredient_cost` (because the literal
    parses but is semantically wrong — e.g. AP Flour cs='30/85CT' parses
    to count but the product is sold by weight) should use
    `case_size_candidates_for_cost` instead and try each.
    """
    cs = (case_size or '').strip()
    # 1. Original parses?
    if parse_case_size(cs):
        return cs
    # 2. Description has weight?
    desc_w = extract_weight_from_description(raw_description)
    if desc_w and parse_case_size(desc_w):
        return desc_w
    # 3. Product default (the inferred canonical pack)?
    pd = (product_default or '').strip()
    if pd and parse_case_size(pd):
        return pd
    # 4. Bare N/M with sane total → assume lbs
    m = _BARE_N_OVER_M.match(cs)
    if m:
        n = Decimal(m.group(1))
        per = Decimal(m.group(2))
        total = n * per
        if _BARE_NM_MIN_LBS <= total <= _BARE_NM_MAX_LBS:
            synth = f'{m.group(1)}/{m.group(2)}LB'
            if parse_case_size(synth):
                return synth
    return cs


def case_size_candidates_for_cost(case_size: str | None,
                                   raw_description: str | None,
                                   product_default: str | None = None,
                                   product_name: str | None = None) -> list[str]:
    """Return all parseable case_size candidates in preference order.

    Unlike `effective_case_size_for_cost` which returns one string,
    this returns the full set so the cost calculator can try each
    against `ingredient_cost`. Important when the literal `case_size`
    parses but is semantically wrong — AP Flour invoice carries
    cs='30/85CT' which parses as count, but recipes ask cups (volume)
    and the product is actually sold by weight (50-lb bag). Trying the
    product default ('1/50LB') as a second candidate unlocks the cost.

    Order:
      1. Literal `case_size` (most invoice-specific)
      2. Description-extracted weight ('5# BAG', '36/1#')
      3. Bushel-fraction in description × product's lb/bushel
         ('1-1/9 BUSHEL' cucumber → '53.3LB'). Phase 6 Farm Art unlock.
      4. Product.default_case_size (inferred canonical pack)
      5. Bare 'N/M' treated as 'N/MLB' (last-resort heuristic)

    Caller iterates and returns the first candidate that produces a
    non-None cost from `ingredient_cost`.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(c):
        if not c:
            return
        c = c.strip()
        if not c or c in seen:
            return
        if parse_case_size(c):
            out.append(c)
            seen.add(c)

    cs = (case_size or '').strip()
    _add(cs)

    desc_w = extract_weight_from_description(raw_description)
    _add(desc_w)

    # Bushel-fraction extraction (Farm Art produce cartons)
    bushel_w = extract_bushel_case_size(raw_description, product_name)
    _add(bushel_w)

    _add((product_default or '').strip())

    m = _BARE_N_OVER_M.match(cs)
    if m:
        n = Decimal(m.group(1))
        per = Decimal(m.group(2))
        total = n * per
        if _BARE_NM_MIN_LBS <= total <= _BARE_NM_MAX_LBS:
            _add(f'{m.group(1)}/{m.group(2)}LB')

    return out


# ---- Unit conversion ----

# Grams of each weight unit → canonical oz (avoirdupois)
_WEIGHT_TO_OZ: dict[str, Decimal] = {
    'oz':  Decimal('1'),
    'lb':  Decimal('16'),
    'lbs': Decimal('16'),       # recipe plural
    '#':   Decimal('16'),       # pound symbol — common shorthand in recipe quantity columns
    'g':   Decimal('0.03527396195'),
    'kg':  Decimal('35.27396195'),
    'pound':  Decimal('16'),
    'pounds': Decimal('16'),
    'ounce':  Decimal('1'),
    'ounces': Decimal('1'),
    'gram':   Decimal('0.03527396195'),
    'grams':  Decimal('0.03527396195'),
}

# Fluid ounces of each volume unit → canonical fl_oz
_VOLUME_TO_FL_OZ: dict[str, Decimal] = {
    'fl_oz':  Decimal('1'),
    'floz':   Decimal('1'),
    'tsp':    Decimal('1') / Decimal('6'),
    'tsps':   Decimal('1') / Decimal('6'),
    'tbsp':   Decimal('0.5'),
    'tbsps':  Decimal('0.5'),
    'cup':    Decimal('8'),
    'cups':   Decimal('8'),
    'c':      Decimal('8'),
    'pt':     Decimal('16'),
    'pint':   Decimal('16'),
    'pints':  Decimal('16'),
    'qt':     Decimal('32'),
    'quart':  Decimal('32'),
    'quarts': Decimal('32'),
    'gal':    Decimal('128'),
    'gallon': Decimal('128'),
    'gallons':Decimal('128'),
    'ml':     Decimal('0.033814'),
    'l':      Decimal('33.814'),
    'liter':  Decimal('33.814'),
    'liters': Decimal('33.814'),
    # #10 can — standard food-service can size, ~13.625 cups = 109 fl_oz.
    # Recipes write '#10 Can' as the unit; case is "6/10CAN" pack.
    '#10_can': Decimal('109'),
    '#10can':  Decimal('109'),
    '10_can':  Decimal('109'),  # in case '#' is stripped
}

# Count-type units. Most resolve 1:1 to 'ct' (count). 'doz'/'dozen' is the
# exception — 1 dozen = 12 ct — handled by `to_base_unit` and is the unlock
# for products like Eggs sold by '15 DOZ' (= 180 ct).
# 'bu' is treated as count (bunch/bundle) per Farm Art's vendor convention
# — observed in Product.default_case_size like '60BU' for Cilantro (60 bunches).
# Does NOT mean USDA bushel here. If a vendor ever bills in USDA bushels
# (lb-based: wheat=60, corn=56, tomatoes=53, etc. — Handbook 28), a
# separate _BUSHEL_TO_LB lookup will be needed and 'bu' dispatch will
# have to disambiguate on product.
_COUNT_UNITS = {'ct', 'each', 'ea', 'hd', 'head', 'heads', 'bu', 'bunch', 'bunches',
                'bag', 'bags', 'bottle', 'bottles', 'jar', 'jars', 'can', 'cans',
                'pack', 'packs', 'bundle', 'bundles',
                'doz', 'dozen', 'dz'}
_COUNT_TO_CT: dict[str, Decimal] = {
    'doz':    Decimal('12'),
    'dozen':  Decimal('12'),
    'dz':     Decimal('12'),
}


def normalize_unit(u: str) -> str:
    """Strip punctuation and lowercase. 'Tbsp.' → 'tbsp'."""
    return (u or '').strip().strip('.').strip().lower().replace(' ', '_')


def unit_kind(u: str) -> str:
    """Return one of: 'weight', 'volume', 'count', 'unknown'."""
    u = normalize_unit(u)
    if u in _WEIGHT_TO_OZ:
        return 'weight'
    if u in _VOLUME_TO_FL_OZ:
        return 'volume'
    if u in _COUNT_UNITS:
        return 'count'
    return 'unknown'


def to_base_unit(qty: Decimal, u: str) -> Optional[tuple[Decimal, str]]:
    """Convert a (qty, unit) to its base — (oz, 'oz'), (fl_oz, 'fl_oz'), or (qty, 'ct')."""
    u = normalize_unit(u)
    if u in _WEIGHT_TO_OZ:
        return qty * _WEIGHT_TO_OZ[u], 'oz'
    if u in _VOLUME_TO_FL_OZ:
        return qty * _VOLUME_TO_FL_OZ[u], 'fl_oz'
    if u in _COUNT_UNITS:
        # 'doz' → 12 ct, 'each'/'ct' → 1 ct
        return qty * _COUNT_TO_CT.get(u, Decimal('1')), 'ct'
    return None


# ---- Ingredient density lookups (volume → weight) ----

# Fallback weight-per-cup for common ingredients (oz). Used when BoY YieldReference
# doesn't have the ingredient and a recipe uses a volume unit but invoice is weight-priced.
# Values from USDA / standard baking references.
_CUP_WEIGHT_OZ: dict[str, Decimal] = {
    'flour':           Decimal('4.25'),
    'ap_flour':        Decimal('4.25'),
    'all_purpose_flour': Decimal('4.25'),
    'bread_flour':     Decimal('4.5'),
    'cake_flour':      Decimal('3.75'),
    'whole_wheat_flour': Decimal('4.5'),
    'sugar':           Decimal('7'),
    'white_sugar':     Decimal('7'),
    'brown_sugar':     Decimal('7.5'),  # packed
    'powdered_sugar':  Decimal('4.5'),
    'butter':          Decimal('8'),
    'softened_butter': Decimal('8'),
    'oil':             Decimal('7.5'),
    'olive_oil':       Decimal('7.5'),
    'honey':           Decimal('12'),
    'milk':            Decimal('8.5'),
    'heavy_cream':     Decimal('8.5'),
    'buttermilk':      Decimal('8.5'),
    'water':           Decimal('8.35'),
    'cocoa':           Decimal('3'),
    'cornstarch':      Decimal('4.5'),
    'oats':            Decimal('3'),
    'rolled_oats':     Decimal('3'),
    'rice':            Decimal('6.5'),
    'raisins':         Decimal('5.5'),
    'chocolate_chips': Decimal('6'),
    # Condiments / fine-dispense
    'salt':            Decimal('10'),
    'kosher_salt':     Decimal('5'),
    'salt_kosher':     Decimal('5'),  # canonical-name variant ("Salt, Kosher")
    'baking_powder':   Decimal('8'),
    'baking_soda':     Decimal('8'),
    # Spices — ground powders and whole seeds.
    #
    # Values realigned 2026-04-23 to Book of Yields 8e p.10-13. Previous
    # values were USDA/"standard baking reference" rounded up to convenient
    # numbers, which systematically over-weighted ground powders by 15-35%.
    # BoY values are empirical and more granular; Sean (butcher/kitchen
    # experience) sense-checked the direction — ground powders run lighter
    # than the old hardcoded suggested.
    #
    # Pattern from the full BoY herbs_spices section:
    #   - Dried leafy herbs:  1.0-1.8 oz/cup (median 1.45) → hardcoded 1.5 ✓
    #   - Ground powders:     2.7-4.1 oz/cup (median 3.72) → was 4.0-5.0 ✗
    #   - Whole seeds:        3.0-4.0 oz/cup (median 3.56) → ~right, minor adj
    #   - Salt: brand-dependent; Diamond Crystal kosher = 4.70, hardcoded 5
    #
    # Ground powders
    'paprika':         Decimal('3.9'),   # BoY p.12
    'cinnamon':        Decimal('4.0'),   # BoY ground p.10
    'cinnamon_ground': Decimal('4.0'),
    'ground_cinnamon': Decimal('4.0'),
    'nutmeg':          Decimal('3.8'),   # BoY ground 3.76 p.11
    'ground_nutmeg':   Decimal('3.8'),
    'black_pepper':    Decimal('3.8'),   # BoY table grind 3.81 p.12
    'pepper':          Decimal('3.8'),
    'white_pepper':    Decimal('4.5'),   # BoY white ground 4.51 — denser than black
    'cumin':           Decimal('3.3'),   # BoY ground 3.33 p.10
    'ground_cumin':    Decimal('3.3'),
    'chili_powder':    Decimal('3.8'),   # BoY 3.76 p.10
    'curry_powder':    Decimal('3.6'),   # BoY 3.56 p.10
    'garlic_powder':   Decimal('3.7'),   # BoY 3.70 p.11 (was 5.0 — biggest fix)
    'onion_powder':    Decimal('3.7'),   # BoY 3.70 p.12 (was 5.0 — biggest fix)
    'allspice':        Decimal('3.7'),   # no specific BoY; ground-powder median
    'ground_allspice': Decimal('3.7'),
    'cloves':          Decimal('3.7'),   # BoY ground 3.72 p.10
    'ginger':          Decimal('3.8'),   # BoY ground 3.81 p.11
    'ground_ginger':   Decimal('3.8'),
    'turmeric':        Decimal('4.3'),   # BoY 0.27 × 16 (parser-error corrected; real ~4.3)
    'mustard_powder':  Decimal('3.1'),   # BoY ground 3.10 p.11
    'cayenne':         Decimal('3.8'),   # no BoY cayenne; ground-powder median
    # Whole seed spices
    'whole_black_pepper': Decimal('4.0'),  # BoY whole 4.00 p.12
    'black_pepper_whole': Decimal('4.0'),
    'whole_cumin':        Decimal('3.4'),  # BoY cumin seed whole 3.39 p.10
    'whole_cloves':       Decimal('3.0'),  # BoY cloves whole 3.00 p.10
    # Dried herbs — much lighter (~1.5 oz/cup)
    'oregano':         Decimal('1.5'),
    'basil':           Decimal('1.5'),
    'thyme':           Decimal('1.5'),
    'rosemary':        Decimal('1.5'),
    'parsley':         Decimal('1.5'),
    'dill':            Decimal('1.5'),
    'sage':            Decimal('1.5'),
    'bay_leaves':      Decimal('0.5'),
    'whole_bay_leaves': Decimal('0.5'),
    # Liquids / pourables
    'vanilla':         Decimal('8.5'),
    'vanilla_extract': Decimal('8.5'),
    'soy_sauce':       Decimal('9'),
    'fish_sauce':      Decimal('9'),
    'vinegar':         Decimal('8'),
    'maple_syrup':     Decimal('11'),
    'molasses':        Decimal('11'),
    'corn_syrup':      Decimal('11.5'),
    'mayonnaise':      Decimal('7.7'),
    'mayo':            Decimal('7.7'),
    'mustard':         Decimal('8.5'),     # prepared dijon/yellow
    'ketchup':         Decimal('8.5'),
    'tahini':          Decimal('9.5'),
    'peanut_butter':   Decimal('9'),
    # Yeast (instant/active dry — granulated)
    'yeast':           Decimal('5'),
    'active_dry_yeast': Decimal('5'),
    'dry_active_yeast': Decimal('5'),
    'yeast_dry_active': Decimal('5'),  # canonical-name variant
    # Dairy add-ons
    'sour_cream':      Decimal('8.5'),
    'yogurt':          Decimal('8.5'),
    'ricotta':         Decimal('8.7'),
    'cream_cheese':    Decimal('8'),
    # Grains / bulk dry
    'cornmeal':        Decimal('5'),
    'corn_meal':       Decimal('5'),
    'grits':           Decimal('5.6'),
    'bread_crumbs':    Decimal('4'),
    'breadcrumbs':     Decimal('4'),
    'panko':           Decimal('1.75'),
    # Produce — densities for chopped/diced (recipe context).
    # The cost calc treats the canonical name's density as approximate
    # for whatever prep state. These are USDA-derived approximations:
    'onion':           Decimal('5.6'),  # chopped
    'red_onion':       Decimal('5.6'),
    'yellow_onion':    Decimal('5.6'),
    'onion_yellow':    Decimal('5.6'),  # canonical-name variant
    'onion_red':       Decimal('5.6'),
    'white_onion':     Decimal('5.6'),
    'shallot':         Decimal('5.3'),  # chopped
    'carrot':          Decimal('4.4'),  # chopped/grated
    'celery':          Decimal('4.2'),  # chopped
    'potato':          Decimal('5.3'),  # diced raw
    'idaho_potato':    Decimal('5.3'),
    'potato_idaho':    Decimal('5.3'),
    'russet_potato':   Decimal('5.3'),
    'sweet_potato':    Decimal('4.6'),
    'tomato':          Decimal('6.3'),  # chopped
    'plum_tomato':     Decimal('6.3'),
    'plum_tomatoes':   Decimal('6.3'),
    'cherry_tomato':   Decimal('6.0'),
    'bell_pepper':     Decimal('5.3'),  # chopped
    'jalapeno':        Decimal('3.5'),  # chopped (small dice)
    'cucumber':        Decimal('4.7'),  # diced
    'mushroom':        Decimal('2.5'),  # sliced
    'mushrooms':       Decimal('2.5'),
    'cabbage':         Decimal('2.5'),  # shredded
    'broccoli':        Decimal('3.1'),  # florets
    'cauliflower':     Decimal('3.5'),
    'corn':            Decimal('5.8'),  # kernels
    'green_beans':     Decimal('3.9'),  # cut
    'spinach':         Decimal('1.0'),  # raw, packed loosely
    'lettuce':         Decimal('2.0'),  # shredded
    'parsley':         Decimal('1.4'),  # fresh chopped (separate from dried 1.5 above
                                         # — same value, kept in produce block)
    'cilantro':        Decimal('1.3'),  # fresh chopped
    'basil':           Decimal('1.5'),  # fresh chopped (overrides dried entry above —
                                         # most recipe contexts mean fresh; close enough)
    'garlic':          Decimal('4.8'),  # peeled / minced
    'ginger':          Decimal('4.0'),  # peeled / grated (~ same as ground)
}


def _normalize_name(name: str) -> str:
    return name.strip().lower().replace('-', '_').replace(' ', '_').replace(',', '')


# Per-piece weights for ingredients whose recipe unit names pieces rather
# than a standard unit. Keys are (normalized_ingredient_name, recipe_unit);
# value is oz-per-piece. Applied BEFORE the main dispatch in ingredient_cost
# — the qty+unit get rewritten to an equivalent weight in oz, and standard
# weight↔anything dispatch takes over from there.
#
# Curated from Book of Yields / USDA; each entry is specific enough that
# the unit is unambiguous for that ingredient (e.g. "cloves" means peeled
# garlic cloves at ~5g, not the spice).
_INGREDIENT_PIECE_OZ: dict[tuple[str, str], Decimal] = {
    ('garlic', 'clove'):  Decimal('0.18'),  # peeled, USDA ≈ 5g
    ('garlic', 'cloves'): Decimal('0.18'),
}


def _piece_weight_oz_for(ingredient_name: str, recipe_unit: str) -> Optional[Decimal]:
    """If (ingredient, unit) is in the piece-weight table, return oz-per-piece.

    Exact-match only. Does NOT fall back to last-token matching the way
    cup_weight_oz_for does — because modifier words ('Roasted Garlic',
    'Pickled Ginger') usually indicate a prep that's itself a sub-recipe
    (garlic + oil roasted together, ginger + brine, etc.), and naively
    using the base ingredient's piece weight would hide the missing
    sub_recipe link and under-cost the line. Better to return None and
    surface the data gap than silently approximate."""
    key = (_normalize_name(ingredient_name), normalize_unit(recipe_unit))
    return _INGREDIENT_PIECE_OZ.get(key)


def cup_weight_oz_for(ingredient_name: str) -> Optional[Decimal]:
    """Fallback density lookup. Returns oz per cup, or None if unknown."""
    n = _normalize_name(ingredient_name)
    if n in _CUP_WEIGHT_OZ:
        return _CUP_WEIGHT_OZ[n]
    # Try last token (e.g. "white sugar" → "sugar")
    parts = n.split('_')
    for p in parts[::-1]:
        if p in _CUP_WEIGHT_OZ:
            return _CUP_WEIGHT_OZ[p]
    return None


# ---- Main cost function ----

def ingredient_cost(
    recipe_qty: Decimal | None,
    recipe_unit: str,
    ingredient_name: str,
    case_price: Decimal | None,
    case_size_str: str | None,
    yield_pct: Decimal | None = None,
    ounce_weight_per_cup: Decimal | None = None,
    price_per_pound: Decimal | None = None,
) -> tuple[Optional[Decimal], str]:
    """
    Compute the estimated dollar cost of an ingredient line.

    Returns (cost_or_None, reason_note). `reason_note` explains why cost is None
    (for UI debugging) or describes any assumption made.

    When `price_per_pound` is provided (parser's direct $/lb, persisted on
    InvoiceLineItem.price_per_pound for Sysco catch-weight + Exceptional
    rows), the weight-unit recipe dispatch uses it directly instead of
    parsing case_size. This is the accuracy-load-bearing path for protein
    ingredients — avoids the entire case_size → pack_count × pack_size
    chain and produces a cost from a single multiplication.
    """
    if recipe_qty is None:
        return None, 'no quantity'
    if case_price is None and price_per_pound is None:
        return None, 'no recent invoice price'

    # Apply yield + piece-weight transforms first so they're available to
    # both the direct $/lb path and the case_size dispatch below.
    qty = Decimal(recipe_qty)
    if yield_pct:
        qty = qty / (yield_pct / Decimal('100'))

    piece_oz = _piece_weight_oz_for(ingredient_name, recipe_unit)
    if piece_oz is not None:
        qty = qty * piece_oz
        recipe_unit = 'oz'

    # Direct $/lb path. Parser emits price_per_pound for Sysco catch-weight
    # and Exceptional — both are per-lb billed. When the recipe asks in a
    # weight unit, a single multiplication ends the work; no case_size
    # dependency, no case_info parsing, no pack-count arithmetic.
    if price_per_pound is not None:
        if unit_kind(recipe_unit) == 'weight':
            oz_result = to_base_unit(qty, recipe_unit)
            if oz_result is not None:
                lb_value = oz_result[0] / Decimal('16')
                cost = Decimal(str(price_per_pound)) * lb_value
                return cost.quantize(Decimal('0.01')), 'direct $/lb'

    case_info = parse_case_size(case_size_str)
    if case_info is None:
        return None, f'unparseable case_size: {case_size_str!r}'

    r_kind = unit_kind(recipe_unit)
    c_kind = unit_kind(case_info.pack_unit)

    # Convert recipe qty + case size to a common base
    if r_kind == 'weight' and c_kind == 'weight':
        recipe_base = to_base_unit(qty, recipe_unit)       # oz
        case_base = to_base_unit(case_info.pack_size, case_info.pack_unit)  # oz
        if not recipe_base or not case_base:
            return None, 'weight conversion failed'
        total_case_oz = case_base[0] * case_info.pack_count
        cost = case_price * (recipe_base[0] / total_case_oz)
        return cost.quantize(Decimal('0.01')), 'weight↔weight'

    if r_kind == 'volume' and c_kind == 'volume':
        recipe_base = to_base_unit(qty, recipe_unit)       # fl_oz
        case_base = to_base_unit(case_info.pack_size, case_info.pack_unit)  # fl_oz
        if not recipe_base or not case_base:
            return None, 'volume conversion failed'
        total_case_floz = case_base[0] * case_info.pack_count
        cost = case_price * (recipe_base[0] / total_case_floz)
        return cost.quantize(Decimal('0.01')), 'volume↔volume'

    # Cross-domain: volume recipe qty, weight case, or vice versa
    if r_kind == 'volume' and c_kind == 'weight':
        density = ounce_weight_per_cup or cup_weight_oz_for(ingredient_name)
        if not density:
            return None, f'no density for volume→weight ({ingredient_name!r})'
        recipe_volume_cups = to_base_unit(qty, recipe_unit)  # fl_oz
        if not recipe_volume_cups:
            return None, 'volume conversion failed'
        recipe_oz_weight = (recipe_volume_cups[0] / Decimal('8')) * density
        case_base = to_base_unit(case_info.pack_size, case_info.pack_unit)  # oz
        if not case_base:
            return None, 'weight conversion failed'
        total_case_oz = case_base[0] * case_info.pack_count
        cost = case_price * (recipe_oz_weight / total_case_oz)
        return cost.quantize(Decimal('0.01')), f'volume→weight via density {density} oz/c'

    if r_kind == 'weight' and c_kind == 'volume':
        density = ounce_weight_per_cup or cup_weight_oz_for(ingredient_name)
        if not density:
            return None, f'no density for weight→volume ({ingredient_name!r})'
        recipe_oz = to_base_unit(qty, recipe_unit)  # oz
        if not recipe_oz:
            return None, 'weight conversion failed'
        recipe_volume_cups = recipe_oz[0] / density
        recipe_floz = recipe_volume_cups * Decimal('8')
        case_base = to_base_unit(case_info.pack_size, case_info.pack_unit)  # fl_oz
        if not case_base:
            return None, 'volume conversion failed'
        total_case_floz = case_base[0] * case_info.pack_count
        cost = case_price * (recipe_floz / total_case_floz)
        return cost.quantize(Decimal('0.01')), f'weight→volume via density {density} oz/c'

    # Container-unit ↔ weight/volume case: recipe asks for a specific
    # container (bag, bottle, jar, can, pack) and the case describes its
    # SIZE in weight or volume. The case's pack_count IS the container
    # count — recipe qty maps to that directly.
    #   '0.5 bag Mozzarella' case='12/4OZ' (12 bags × 4oz) → 0.5/12 × case
    #   '1 bag Yellow Onion' case='1/50LB' (1 bag × 50lb)  → 1/1  × case
    # Does NOT fire for generic count units like 'each'/'ea'/'ct' — those
    # are ambiguous ("1 ea carrot" can't be resolved this way).
    _container_units = {'bag', 'bags', 'bottle', 'bottles', 'jar', 'jars',
                        'can', 'cans', 'pack', 'packs'}
    if (normalize_unit(recipe_unit) in _container_units
            and c_kind in ('weight', 'volume')
            and case_info.pack_count >= 1):
        cost = case_price * (qty / Decimal(case_info.pack_count))
        return cost.quantize(Decimal('0.01')), f'container↔case (1 of {case_info.pack_count})'

    # Count vs something else — only works when matched exactly,
    # OR when recipe is a small integer with empty unit AND case is count
    # (the canonical "6 eggs" pattern: recipe says "6" with no unit,
    #  case says "15 DOZ" = 180 ct → cost = case_price × 6/180).
    if r_kind == 'count' and c_kind == 'count':
        case_total = to_base_unit(case_info.pack_size, case_info.pack_unit)
        total_case_ct = (case_total[0] if case_total else case_info.pack_size) * case_info.pack_count
        cost = case_price * (qty / total_case_ct)
        return cost.quantize(Decimal('0.01')), 'count↔count'

    if (r_kind == 'unknown' and not normalize_unit(recipe_unit)
            and c_kind == 'count' and qty == qty.to_integral_value()
            and Decimal('1') <= qty <= Decimal('200')):
        case_total = to_base_unit(case_info.pack_size, case_info.pack_unit)
        total_case_ct = (case_total[0] if case_total else case_info.pack_size) * case_info.pack_count
        cost = case_price * (qty / total_case_ct)
        return cost.quantize(Decimal('0.01')), 'count↔count (unitless recipe inferred as count)'

    return None, f'incompatible units: recipe={recipe_unit!r}({r_kind}) vs case={case_info.pack_unit!r}({c_kind})'
