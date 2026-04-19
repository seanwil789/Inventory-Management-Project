from decimal import Decimal
from fractions import Fraction

from django import template

register = template.Library()


# Common cooking fractions: (Fraction value, display string).
COMMON_FRACTIONS = [
    (Fraction(1, 8), '1/8'),
    (Fraction(1, 4), '1/4'),
    (Fraction(1, 3), '1/3'),
    (Fraction(3, 8), '3/8'),
    (Fraction(1, 2), '1/2'),
    (Fraction(5, 8), '5/8'),
    (Fraction(2, 3), '2/3'),
    (Fraction(3, 4), '3/4'),
    (Fraction(7, 8), '7/8'),
]
TOLERANCE = 0.02  # Accept values within 0.02 of a common fraction (handles stored 0.333 etc.)


@register.filter(name='pretty_qty')
def pretty_qty(value):
    """Render a Decimal quantity as a line-cook-readable fraction.

    0.500 → 1/2
    0.250 → 1/4
    1.500 → 1 1/2
    2.000 → 2
    3.100 → 3.1   (no common fraction match)
    None  → ''
    """
    if value is None:
        return ''
    try:
        q = float(value)
    except (TypeError, ValueError):
        return str(value)

    if q == int(q):
        return str(int(q))

    whole = int(q)
    frac_part = q - whole

    closest = min(COMMON_FRACTIONS, key=lambda f: abs(float(f[0]) - frac_part))
    if abs(float(closest[0]) - frac_part) <= TOLERANCE:
        if whole:
            return f"{whole} {closest[1]}"
        return closest[1]

    # Fall back to a trimmed decimal
    return f"{q:.2f}".rstrip('0').rstrip('.')
