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
                                   product_default: str | None = None) -> list[str]:
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
      3. Product.default_case_size (inferred canonical pack)
      4. Bare 'N/M' treated as 'N/MLB' (last-resort heuristic)

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
    'tbsp':   Decimal('0.5'),
    'cup':    Decimal('8'),
    'cups':   Decimal('8'),
    'c':      Decimal('8'),
    'pt':     Decimal('16'),
    'pint':   Decimal('16'),
    'qt':     Decimal('32'),
    'quart':  Decimal('32'),
    'gal':    Decimal('128'),
    'gallon': Decimal('128'),
    'ml':     Decimal('0.033814'),
    'l':      Decimal('33.814'),
    'liter':  Decimal('33.814'),
}

# Count-type units. Most resolve 1:1 to 'ct' (count). 'doz'/'dozen' is the
# exception — 1 dozen = 12 ct — handled by `to_base_unit` and is the unlock
# for products like Eggs sold by '15 DOZ' (= 180 ct).
_COUNT_UNITS = {'ct', 'each', 'ea', 'hd', 'head', 'bu', 'bunch', 'bag',
                'bottle', 'jar', 'can', 'doz', 'dozen', 'dz'}
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
    # Spices (mostly ground, ~4 oz/cup) — Phase 2E unlock for no_density bucket
    'paprika':         Decimal('4.5'),
    'cinnamon':        Decimal('4.5'),
    'cinnamon_ground': Decimal('4.5'),
    'ground_cinnamon': Decimal('4.5'),
    'nutmeg':          Decimal('4'),
    'ground_nutmeg':   Decimal('4'),
    'black_pepper':    Decimal('4'),       # ground
    'pepper':          Decimal('4'),
    'white_pepper':    Decimal('4'),
    'whole_black_pepper': Decimal('4.5'),  # whole peppercorns
    'black_pepper_whole': Decimal('4.5'),
    'cumin':           Decimal('4'),
    'ground_cumin':    Decimal('4'),
    'whole_cumin':     Decimal('4'),
    'chili_powder':    Decimal('4.5'),
    'curry_powder':    Decimal('3.5'),
    'garlic_powder':   Decimal('5'),
    'onion_powder':    Decimal('5'),
    'allspice':        Decimal('4'),
    'ground_allspice': Decimal('4'),
    'cloves':          Decimal('3.5'),
    'whole_cloves':    Decimal('3.5'),
    'ginger':          Decimal('4'),
    'ground_ginger':   Decimal('4'),
    'turmeric':        Decimal('5'),
    'mustard_powder':  Decimal('3.5'),
    'cayenne':         Decimal('4'),
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
}


def _normalize_name(name: str) -> str:
    return name.strip().lower().replace('-', '_').replace(' ', '_').replace(',', '')


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
) -> tuple[Optional[Decimal], str]:
    """
    Compute the estimated dollar cost of an ingredient line.

    Returns (cost_or_None, reason_note). `reason_note` explains why cost is None
    (for UI debugging) or describes any assumption made.
    """
    if recipe_qty is None:
        return None, 'no quantity'
    if case_price is None:
        return None, 'no recent invoice price'

    case_info = parse_case_size(case_size_str)
    if case_info is None:
        return None, f'unparseable case_size: {case_size_str!r}'

    # Optionally scale qty up for yield loss (AP needed)
    qty = Decimal(recipe_qty)
    if yield_pct:
        qty = qty / (yield_pct / Decimal('100'))

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
