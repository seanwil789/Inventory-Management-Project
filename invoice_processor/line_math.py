"""Per-line math validation: qty × price ≈ extended.

Catch-weight aware. When `price_per_pound` is set on the item (Exceptional
catch-weight rows, Sysco MEATS/POULTRY/SEAFOOD rows), validate against ppp
rather than unit_price — those vendors store the line total in unit_price
because per-lb is the true unit price. Schema overload trap caught
2026-05-08; see project_parser_accuracy_goal.md and feedback_event_driven_pricing.md.

Used by all 3 extraction paths (text parsers in parser.py, rank-pair
extractors in rank_pair.py, spatial matchers in spatial_matcher.py) for
uniform line-level data quality enforcement.

Public API:
    validate_line_math(item, *, vendor='', try_self_correct=False) -> None

Mutates item in place. Sets math_flagged + math_diff_abs + math_diff_pct
when validation fails outside tolerance. Optionally self-corrects qty
when ext/price rounds to a clean integer.
"""
from __future__ import annotations


_DEFAULT_TOLERANCE_PCT = 5.0
_DEFAULT_TOLERANCE_ABS = 2.0


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate_line_math(item: dict, *,
                       vendor: str = '',
                       try_self_correct: bool = False,
                       tolerance_pct: float = _DEFAULT_TOLERANCE_PCT,
                       tolerance_abs: float = _DEFAULT_TOLERANCE_ABS) -> None:
    """Validate qty × price ≈ extended; mutate item with math_flagged on fail.

    Catch-weight semantic: when item['price_per_pound'] is populated AND
    qty is positive, the expected value is qty × ppp. unit_price in catch-
    weight vendors stores the line total, so qty × unit_price would falsely
    flag every row (Exceptional 52/52, Sysco MEATS, etc.).

    Standard semantic: qty × unit_price ≈ extended.

    Tolerance: BOTH diff_pct > tolerance_pct AND diff_abs > tolerance_abs
    must be true to flag. Single-bar drift (rounding/discount) doesn't
    trigger. Defaults: 5% / $2.

    Mutations on flag:
        item['math_flagged'] = True
        item['math_diff_abs'] = round(diff_abs, 2)
        item['math_diff_pct'] = round(diff_pct, 2)

    Self-correction (when try_self_correct=True): derives qty from
    ext / price; if it rounds to clean small integer (1-50, within 0.10
    of integer) and the rounded value reconciles to within tolerance,
    updates item['quantity'] in place and skips flagging.

    No-ops (returns silently without flagging):
        - Missing or zero qty / price / extended
        - Negative values
        - Insufficient data to compute expected
    """
    # Field-name compatibility:
    #   - quantity vs qty: rank_pair Farm Art uses 'qty'; Sysco uses
    #     'quantity'; spatial uses 'quantity'; parser text path varies.
    #   - price_per_pound vs price_per_unit: parsed items use
    #     'price_per_unit' (parser convention); DB rows use
    #     'price_per_pound' (model field name). Read both — first non-None
    #     wins so this works on both pre-write parsed items and post-write
    #     DB rows (backfill).
    if item.get('quantity') is not None:
        qty_key = 'quantity'
    elif item.get('qty') is not None:
        qty_key = 'qty'
    else:
        qty_key = 'quantity'  # default for self-correct insert
    qty = _to_float(item.get(qty_key))
    unit_price = _to_float(item.get('unit_price'))
    extended = _to_float(item.get('extended_amount'))
    ppp = _to_float(item.get('price_per_pound')
                    if item.get('price_per_pound') is not None
                    else item.get('price_per_unit'))
    desc = (item.get('description')
            or item.get('raw_description')
            or item.get('canonical')
            or '')

    # Need extended to validate at all
    if extended is None or extended <= 0:
        return
    # Need qty to validate
    if qty is None or qty <= 0:
        return

    # Catch-weight path takes priority when ppp is present
    if ppp is not None and ppp > 0:
        price = ppp
        price_kind = 'ppp'
    elif unit_price is not None and unit_price > 0:
        price = unit_price
        price_kind = 'unit'
    else:
        return  # neither ppp nor unit_price usable

    expected = qty * price
    if expected <= 0:
        return

    diff_abs = abs(extended - expected)
    diff_pct = (diff_abs / expected) * 100

    # Both bars must be exceeded for a flag (avoids noise on rounding/discount)
    if diff_pct <= tolerance_pct or diff_abs <= tolerance_abs:
        return

    # Try self-correction before flagging
    if try_self_correct:
        derived_raw = extended / price
        derived = round(derived_raw)
        if (1 <= derived <= 50
                and float(derived) != qty
                and abs(derived_raw - derived) < 0.10):
            corrected_expected = derived * price
            corrected_diff_abs = abs(extended - corrected_expected)
            corrected_diff_pct = (
                (corrected_diff_abs / corrected_expected) * 100
                if corrected_expected > 0 else 100.0
            )
            if (corrected_diff_pct <= tolerance_pct
                    or corrected_diff_abs <= tolerance_abs):
                item[qty_key] = float(derived)
                print(f"  [✓] {vendor} qty self-corrected: "
                      f"{desc[:40]!r} qty {qty}→{derived} "
                      f"(ext=${extended:.2f} / {price_kind}=${price:.2f} "
                      f"= {derived_raw:.2f})")
                return

    # Real anomaly — flag it
    item['math_flagged'] = True
    item['math_diff_abs'] = round(diff_abs, 2)
    item['math_diff_pct'] = round(diff_pct, 2)
    print(f"  [!] {vendor} line-math anomaly: {desc[:40]!r} "
          f"qty={qty} × {price_kind}=${price:.2f} = ${expected:.2f} "
          f"but extended=${extended:.2f} "
          f"(Δ={diff_pct:.0f}%, ${diff_abs:.2f})")
