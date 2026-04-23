"""Curated Product.default_case_size values for pantry items whose
invoice history contains only blank case_size rows.

These products have a single ILI row (or all rows blank) in the DB, so
`infer_product_default_case_sizes` can't derive a pack from invoice mode.
Recipe cost calc bails with 'unparseable case_size' on all RIs using
these products. Manual defaults unblock ~16 RIs.

Values are typical Sysco food-service pack sizes. Sean can refine any
that don't match his actual receiving after demo.
"""
from django.db import migrations


PANTRY_DEFAULTS = [
    ('Artichokes',            '6/10CAN'),   # #10 cans, typical foodservice
    ('Baking Powder',         '1/10LB'),    # 10-lb bag
    ('Baking Soda',           '1/10LB'),    # 10-lb bag
    ('Bread Crumbs',          '1/5LB'),     # 5-lb bag
    ('Fennel',                '1/10LB'),    # fresh bulbs, weight-based
    ('Lentils',               '1/10LB'),    # dry
    ('Provolone',             '2/5LB'),     # 2 × 5 lb loaves
    ('Sugar, Confectioners',  '1/25LB'),    # 25-lb bag
]


def apply_defaults(apps, schema_editor):
    Product = apps.get_model('myapp', 'Product')
    for canonical, pack in PANTRY_DEFAULTS:
        Product.objects.filter(
            canonical_name=canonical, default_case_size='',
        ).update(default_case_size=pack)


def revert_defaults(apps, schema_editor):
    Product = apps.get_model('myapp', 'Product')
    for canonical, pack in PANTRY_DEFAULTS:
        Product.objects.filter(
            canonical_name=canonical, default_case_size=pack,
        ).update(default_case_size='')


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0026_invoicelineitem_price_per_pound'),
    ]

    operations = [
        migrations.RunPython(apply_defaults, revert_defaults),
    ]
