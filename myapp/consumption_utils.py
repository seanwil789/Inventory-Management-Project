"""
Consumption math: given a date range, compute how much of each Product
was consumed across all Menus in that range.

Output is per-Product totals in physical units (oz for weight, fl_oz for
volume, ct for count). Variance reporter is responsible for converting
those to inventory-friendly units (lb, gal, etc.) when comparing to
Sean's Synergy column H counts.

Mirrors `cost_utils.py` patterns — same unit dispatch (weight↔weight,
volume↔volume, density-bridged, count) but emits qty consumed instead
of dollar cost.

Returns from `compute_consumption()`:
  {
    'by_product': {product_id: {'oz': X, 'fl_oz': Y, 'ct': Z, ...}},
    'caveats': [str, ...],       # menus/RIs skipped + reason
    'menus_processed': int,
    'menus_skipped': int,
    'menus_unlinked': int,        # freetext-only menus, no recipe to consume
  }

Caveats are critical — variance reports must surface them so Sean knows
which products' computed consumption is incomplete (vs which are fully
trustworthy). A clean number with caveats is honest; a clean number
without caveats hides data gaps.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from .cost_utils import (
    normalize_unit,
    to_base_unit,
    cup_weight_oz_for,
    _piece_weight_oz_for,
)

DEFAULT_CENSUS = 30          # fallback when no Census row exists for a date
DEFAULT_YIELD_SERVINGS = 40  # fallback when Recipe.yield_servings is null/0
MAX_SUB_RECIPE_DEPTH = 4     # cycle guard for sub_recipe recursion


def _scale_qty_to_servings(ri_qty: Decimal, recipe_yield: int,
                           target_servings: float) -> Decimal:
    """Scale a recipe ingredient quantity from its native yield to a target headcount.

    Recipe yields 40 portions, ingredient is 5 cups, target is 30 portions
    → return 5 × 30/40 = 3.75 cups.
    """
    yield_n = recipe_yield if recipe_yield else DEFAULT_YIELD_SERVINGS
    return Decimal(ri_qty) * (Decimal(str(target_servings)) / Decimal(yield_n))


def _apply_yield_pct(qty: Decimal, yield_pct: Optional[Decimal]) -> Decimal:
    """Convert edible-portion (recipe) quantity to as-purchased (raw) quantity.

    Recipe says 1 lb edible carrot, yield 81% → AP needed = 1 / 0.81 = 1.235 lb.
    Inventory was depleted by the AP amount, not the EP amount.
    """
    if not yield_pct or yield_pct <= 0:
        return qty
    return qty / (Decimal(yield_pct) / Decimal('100'))


def _accumulate(by_product: dict, product_id: int, base_unit: str,
                qty: Decimal) -> None:
    """Sum into per-product per-unit dict."""
    if product_id not in by_product:
        by_product[product_id] = {}
    by_product[product_id][base_unit] = by_product[product_id].get(
        base_unit, Decimal('0')) + qty


def _ri_consumption(ri, target_servings: float, recipe_yield: int,
                    by_product: dict, caveats: list, depth: int = 0) -> None:
    """Walk one RecipeIngredient, accumulate consumption into by_product.

    Sub-recipes recurse with their own scale.
    Skips silently with caveat when:
      - quantity is null (data gap)
      - product is null AND no sub_recipe (orphan ingredient)
      - unit is unparseable into a base unit (need density / piece weight that's missing)

    Sub-recipe recursion: ingredient says "2 batch Marinara"; Marinara's
    yield is 8 servings; we scale Marinara's ingredients by (2 / 8) ×
    parent's target_servings, recursing.
    """
    if depth > MAX_SUB_RECIPE_DEPTH:
        caveats.append(f'sub_recipe depth limit hit at RI #{ri.pk}')
        return

    if ri.sub_recipe_id:
        if ri.quantity is None:
            caveats.append(
                f'sub_recipe RI "{ri.name_raw or ri.sub_recipe.name}" has null qty — skipped'
            )
            return
        sub = ri.sub_recipe
        sub_yield = sub.yield_servings or DEFAULT_YIELD_SERVINGS
        # Parent says "2 batches"; each batch yields sub_yield servings;
        # so consumed sub-servings = 2 × sub_yield. We then walk sub's RIs
        # at their normal scale.
        sub_target = float(ri.quantity) * sub_yield
        for sub_ri in sub.ingredients.all():
            _ri_consumption(sub_ri, sub_target, sub_yield,
                            by_product, caveats, depth + 1)
        return

    if not ri.product_id:
        caveats.append(f'RI "{ri.name_raw}" has no product link — skipped')
        return
    if ri.quantity is None:
        caveats.append(
            f'RI "{ri.name_raw}" (Product #{ri.product_id}) has null qty — skipped'
        )
        return

    # Scale to target headcount
    scaled = _scale_qty_to_servings(ri.quantity, recipe_yield, target_servings)

    # Apply yield_pct to convert EP → AP (inventory depletes in AP units)
    eff_yield = ri.yield_pct
    if not eff_yield and ri.yield_ref and ri.yield_ref.yield_pct:
        eff_yield = ri.yield_ref.yield_pct
    ap_qty = _apply_yield_pct(scaled, eff_yield)

    # Convert to canonical base unit
    unit = ri.unit or ''

    # Piece-weight rewrite for size-word units (medium, large, each, ea)
    # Mirrors cost_utils dispatch — yield_pct must NOT be re-applied here
    # because ap_weight_oz is already AP weight.
    piece_oz = _piece_weight_oz_for(ri.name_raw, unit)
    if piece_oz is not None:
        ap_qty = scaled * piece_oz  # use unscaled-by-yield (piece IS AP)
        unit = 'oz'
    elif (ri.yield_ref and ri.yield_ref.ap_weight_oz
          and (ri.yield_ref.ap_unit or '').strip().lower() in ('each', 'head', 'bunch')
          and normalize_unit(unit) in ('medium', 'large', 'small', 'ea', 'each')):
        ap_qty = scaled * Decimal(ri.yield_ref.ap_weight_oz)
        unit = 'oz'

    base = to_base_unit(ap_qty, unit)
    if base is None:
        # Unit unrecognized (e.g. 'rib', 'slice', 'pinch'). Try density
        # bridge if recipe asks volume and ingredient has known density —
        # but for inventory consumption we just emit native qty and tag.
        caveats.append(
            f'RI "{ri.name_raw}" unit {unit!r} unparseable — qty {ap_qty} '
            f'left in native unit'
        )
        _accumulate(by_product, ri.product_id, unit or '?', ap_qty)
        return

    qty_in_base, base_unit = base
    _accumulate(by_product, ri.product_id, base_unit, qty_in_base)


def compute_consumption(start_date: date, end_date: date,
                         census_default: int = DEFAULT_CENSUS) -> dict:
    """Compute per-Product consumption across all Menus in [start_date, end_date].

    Args:
        start_date, end_date: inclusive date range.
        census_default: headcount to use when a date has no Census row.

    Returns:
        {
            'by_product': {product_id: {base_unit: total_qty_decimal, ...}},
            'caveats': [str, ...],
            'menus_processed': int,
            'menus_unlinked': int,
            'menus_skipped': int,
            'date_range': (start, end),
        }

    Walks Menu.recipe + Menu.additional_recipes. Freetext-only menus
    (no recipe link) contribute nothing to consumption — counted in
    menus_unlinked so the caller can warn the operator.
    """
    from .models import Menu, Census

    by_product: dict = {}
    caveats: list = []
    menus_processed = 0
    menus_unlinked = 0
    menus_skipped = 0

    # Preload census map
    census_map = {c.date: c.headcount for c in
                  Census.objects.filter(date__gte=start_date, date__lte=end_date)}

    menus = (Menu.objects
             .filter(date__gte=start_date, date__lte=end_date)
             .select_related('recipe')
             .prefetch_related('additional_recipes'))

    for menu in menus:
        recipes = []
        if menu.recipe_id:
            recipes.append(menu.recipe)
        recipes.extend(menu.additional_recipes.all())

        if not recipes:
            menus_unlinked += 1
            continue

        headcount = census_map.get(menu.date, census_default)

        try:
            for recipe in recipes:
                rec_yield = recipe.yield_servings or DEFAULT_YIELD_SERVINGS
                for ri in recipe.ingredients.all():
                    _ri_consumption(ri, target_servings=headcount,
                                     recipe_yield=rec_yield,
                                     by_product=by_product, caveats=caveats)
            menus_processed += 1
        except Exception as e:
            caveats.append(f'menu {menu.date} {menu.meal_slot} raised: {e!r}')
            menus_skipped += 1

    return {
        'by_product': by_product,
        'caveats': caveats,
        'menus_processed': menus_processed,
        'menus_unlinked': menus_unlinked,
        'menus_skipped': menus_skipped,
        'date_range': (start_date, end_date),
    }
