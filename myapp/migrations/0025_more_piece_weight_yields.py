"""Second round of piece-weight YieldReference additions. Unblocks the
long-tail of Phase 6 incompat RIs: Jalapeño, Eggs, Celery stalk,
Shallot small/large, Whole Cloves spice.

Sources cited per row; USDA FoodData Central for pieces BoY doesn't
publish, BoY-convention yield percentages when the produce class has
a comparable BoY row.
"""
from decimal import Decimal

from django.db import migrations


NEW_ROWS = [
    # Jalapeño — USDA FDC 168577: 1 medium ≈ 14g (0.49 oz), small ~9g,
    # large ~22g. Yield% ≈ 87 (hot pepper convention; stem + seeds loss).
    {
        'ingredient': 'Peppers', 'prep_state': 'Jalapeño,each medium',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('0.49'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FDC', 'source_ref': 'FDC 168577',
    },
    {
        'ingredient': 'Peppers', 'prep_state': 'Jalapeño,each small',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('0.32'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FDC', 'source_ref': 'FDC 168577',
    },
    {
        'ingredient': 'Peppers', 'prep_state': 'Jalapeño,each large',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('0.77'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FDC', 'source_ref': 'FDC 168577',
    },

    # Eggs — USDA grade conventions (weight per whole egg, in shell):
    # Small  = 18 oz/dozen = 1.50 oz each
    # Medium = 21 oz/dozen = 1.75 oz each
    # Large  = 24 oz/dozen = 2.00 oz each   (the default grade in
    #                                        professional recipes)
    # Yield% ≈ 87 (shell loss: ~50g edible from ~57g whole egg).
    # YR section is 'dairy' per Book of Yields convention (eggs lumped
    # with dairy products in Ch 8).
    {
        'ingredient': 'Eggs', 'prep_state': 'each medium',
        'section': 'dairy',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('1.75'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FSIS', 'source_ref': '21 oz/dozen grade',
    },
    {
        'ingredient': 'Eggs', 'prep_state': 'each large',
        'section': 'dairy',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('2.00'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FSIS', 'source_ref': '24 oz/dozen grade',
    },
    {
        'ingredient': 'Eggs', 'prep_state': 'each small',
        'section': 'dairy',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('1.50'),
        'yield_pct': Decimal('87'),
        'source': 'USDA FSIS', 'source_ref': '18 oz/dozen grade',
    },

    # Celery stalk — USDA FDC: 1 medium stalk ≈ 40g = 1.4 oz.
    # BoY only publishes bunch-based rows for celery; adding a per-stalk
    # row so 'ea Celery' in recipes resolves correctly. yield_pct from
    # BoY's celery bunch row (68.8).
    {
        'ingredient': 'Celery', 'prep_state': 'each medium stalk',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('1.4'),
        'yield_pct': Decimal('68.8'),
        'source': 'USDA FDC', 'source_ref': 'medium stalk ~40g',
    },

    # Shallots — completing the small/large set (medium added in 0024).
    # Interpolated from USDA FDC typical range (5-45g): small ~15g,
    # large ~42g.
    {
        'ingredient': 'Shallots', 'prep_state': 'each small',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('0.5'),
        'yield_pct': Decimal('80.21'),
        'source': 'USDA FDC', 'source_ref': 'small French ~15g',
    },
    {
        'ingredient': 'Shallots', 'prep_state': 'each large',
        'section': 'vegetables',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('1.5'),
        'yield_pct': Decimal('80.21'),
        'source': 'USDA FDC', 'source_ref': 'large French ~42g',
    },

    # Whole Cloves (the spice bud, not garlic cloves) — Sean's range
    # 0.5-0.8g per bud (midpoint 0.65g ≈ 0.023 oz). Previous 65mg
    # estimate was ~10x too light; also ap_weight_oz is decimal_places=2
    # so 0.002 would silently store as 0.00 anyway. yield_pct=100
    # (whole bud is the flavoring unit). Section 'herbs_spices'.
    {
        'ingredient': 'Cloves', 'prep_state': 'whole,each',
        'section': 'herbs_spices',
        'ap_unit': 'each', 'ap_weight_oz': Decimal('0.02'),
        'yield_pct': Decimal('100'),
        'source': 'Sean', 'source_ref': '0.5-0.8g per dried bud',
    },
]


def add_rows(apps, schema_editor):
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


def remove_rows(apps, schema_editor):
    YR = apps.get_model('myapp', 'YieldReference')
    for row in NEW_ROWS:
        YR.objects.filter(
            ingredient=row['ingredient'],
            prep_state=row['prep_state'],
            section=row['section'],
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0024_piece_weight_yield_references'),
    ]

    operations = [
        migrations.RunPython(add_rows, remove_rows),
    ]
