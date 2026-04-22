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
    r'^\s*(?P<count>\d+)\s*/\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>LB|OZ|GAL|CT|HD|BU|PT|QT|FLOZ|FL_OZ|ML|L|KG|G)\s*$',
    re.I,
)
_CASE_RE_SIMPLE = re.compile(
    r'^\s*(?P<count>\d+(?:\.\d+)?)\s*(?P<unit>LB|OZ|GAL|CT|HD|BU|PT|QT|FLOZ|FL_OZ|ML|L|KG|G|EACH|EA)\s*$',
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

# Count-type units
_COUNT_UNITS = {'ct', 'each', 'ea', 'hd', 'head', 'bu', 'bunch', 'bag', 'bottle', 'jar', 'can'}


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
        return qty, 'ct'
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
    'baking_powder':   Decimal('8'),
    'baking_soda':     Decimal('8'),
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

    # Count vs something else — only works when matched exactly
    if r_kind == 'count' and c_kind == 'count':
        total_case_ct = Decimal(case_info.pack_count) * case_info.pack_size
        cost = case_price * (qty / total_case_ct)
        return cost.quantize(Decimal('0.01')), 'count↔count'

    return None, f'incompatible units: recipe={recipe_unit!r}({r_kind}) vs case={case_info.pack_unit!r}({c_kind})'
