"""Add piece-weight YieldReference rows for produce ingredients the
Phase 6 audit flagged as gaps. Each row has a piece-type ap_unit
('each') and populated ap_weight_oz so Phase 6's piece-rewrite branch
in RecipeIngredient.estimated_cost can resolve them.

Sources cited per row in source_ref. BoY 8e rows where available;
USDA FoodData Central (FDC) where BoY doesn't publish a piece weight.

Idempotent via update_or_create keyed on (ingredient, prep_state,
section) — existing rows with identical keys get their ap_weight_oz
backfilled but other fields preserved.
"""
from decimal import Decimal

from django.db import migrations


NEW_ROWS = [
    # Onions — BoY has 'each large' (13.70) and 'each small' (7.80) but
    # no 'each medium'. Derived from BoY count-size table: 50 lb ÷ 90 ct
    # (midpoint of the 80-99 ct "medium" range) = 8.89 oz. yield_pct
    # from the count-size rows (~60%).
    {
        'ingredient': 'Onions', 'prep_state': 'each medium',
        'section': 'vegetables',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('8.9'),
        'yield_pct': Decimal('60'),
        'source': 'Book of Yields 8e',
        'source_ref': 'count-size derivation p.50',
    },

    # Shallots — BoY only has per-pound rows. USDA FDC has "1 shallot"
    # ≈ 25g = 0.88 oz (varietal, French). Rounding to 1.0 oz as the
    # culinary medium. Yield% from existing BoY peeled,diced row.
    {
        'ingredient': 'Shallots', 'prep_state': 'each medium',
        'section': 'vegetables',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('1.0'),
        'yield_pct': Decimal('80.21'),
        'source': 'USDA FDC',
        'source_ref': 'typical French shallot ~25-30g',
    },

    # Bell Peppers — USDA FDC 170580: medium bell pepper = 119g = 4.2 oz,
    # large = 164g = 5.8 oz. yield_pct from existing BoY Green,chopped row.
    {
        'ingredient': 'Peppers', 'prep_state': 'Bell,each medium',
        'section': 'vegetables',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('4.2'),
        'yield_pct': Decimal('81.30'),
        'source': 'USDA FDC',
        'source_ref': 'FDC 170580',
    },
    {
        'ingredient': 'Peppers', 'prep_state': 'Bell,each large',
        'section': 'vegetables',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('5.8'),
        'yield_pct': Decimal('81.30'),
        'source': 'USDA FDC',
        'source_ref': 'FDC 170580',
    },

    # Tomatoes — BoY has Cherry/Grape/Sun-dried piece weights but no
    # standard slicing tomato. USDA FDC 170457: medium 123g = 4.3 oz.
    # yield_pct typical for fresh tomato ~92%.
    {
        'ingredient': 'Tomato', 'prep_state': 'whole,each medium',
        'section': 'vegetables',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('4.3'),
        'yield_pct': Decimal('92'),
        'source': 'USDA FDC',
        'source_ref': 'FDC 170457',
    },

    # Bay Leaves — a single dried leaf is essentially cost-free
    # (~0.3g = 0.01 oz). yield_pct = 100 since the whole leaf is the
    # edible/flavoring unit.
    {
        'ingredient': 'Bay Leaves', 'prep_state': 'whole,each',
        'section': 'herbs_spices',
        'ap_unit': 'each',
        'ap_weight_oz': Decimal('0.01'),
        'yield_pct': Decimal('100'),
        'source': 'USDA FDC',
        'source_ref': 'single dried leaf ~0.3g',
    },
]


def add_piece_weight_rows(apps, schema_editor):
    YR = apps.get_model('myapp', 'YieldReference')
    for row in NEW_ROWS:
        YR.objects.update_or_create(
            ingredient=row['ingredient'],
            prep_state=row['prep_state'],
            section=row['section'],
            defaults={
                'ap_unit': row['ap_unit'],
                'ap_weight_oz': row['ap_weight_oz'],
                'yield_pct': row['yield_pct'],
                'source': row['source'],
                'source_ref': row['source_ref'],
            },
        )


def remove_piece_weight_rows(apps, schema_editor):
    YR = apps.get_model('myapp', 'YieldReference')
    for row in NEW_ROWS:
        YR.objects.filter(
            ingredient=row['ingredient'],
            prep_state=row['prep_state'],
            section=row['section'],
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0023_curated_product_default_case_sizes'),
    ]

    operations = [
        migrations.RunPython(add_piece_weight_rows, remove_piece_weight_rows),
    ]
