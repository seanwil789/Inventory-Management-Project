"""Set Product.default_case_size for products in recipes whose
inferred-from-history default was missing or wrong.

The 2026-04-22 cost-coverage audit identified 26 products in recipes
where the absence (or staleness) of a curated default was the only
thing blocking the cost calc — the product's invoice case_size was
bare-qty / OCR-mangled and `infer_product_default_case_sizes` couldn't
catch it (single-occurrence, sub-50% mode share, etc.).

Each value below is sourced from one of:
  - The most-common parseable historical case_size for the product
  - The invoice description (e.g. "JARS 4/1-GAL" → '4/1GAL')
  - Industry-standard pack sizes (Sysco AP Flour 50-lb bag, lemon 95ct)

Idempotent: only writes when the existing default_case_size is empty.
Skips silently if the product doesn't exist (fresh DB before product
catalog seeding).

Coverage impact at the time of writing:
  Before: 194/398 RIs priced (48.7%)
  After:  222/398 RIs priced (55.8%)  — +28 RIs, +7.1pp
"""
from django.db import migrations


CURATED_DEFAULTS = {
    'AP Flour':              '1/50LB',
    'Apples, Gala':          '88CT',
    'Basmati Rice':          '1/20LB',
    'Bell Pepper, Green':    '11LB',
    'Blueberries':           '12/1PT',
    'Bottled Water':         '24/16.9OZ',
    'Carrot':                '1/50LB',
    'Chipotle, Can':         '6/1GAL',
    'Chips, Potato':         '24/12OZ',
    'Cilantro':              '60BU',
    'Corn':                  '1/21LB',
    'Garlic':                '4/1GAL',
    'Ground Beef':           '40LB',
    'Lemon':                 '95CT',
    'Mustard, Dijon':        '6/1GAL',
    'Onion Powder':          '5.5LB',
    'Pepper, Jalapeno':      '11LB',
    'Plum Tomatoes, Whole':  '1/25LB',
    'Potato, Idaho':         '1/50LB',
    'Red Onion':             '1/25LB',
    'Ribs':                  '40LB',
    'Rosemary':              '1KG',
    'Shallot':               '4/1GAL',
    'Sugar, Light Brown':    '1/25LB',
    'Whole Bay Leaves':      '5OZ',
    'Yellow Onion':          '1/50LB',
    'Ziti':                  '1/35OZ',
}


def set_curated_defaults(apps, schema_editor):
    Product = apps.get_model('myapp', 'Product')
    for canonical, default in CURATED_DEFAULTS.items():
        # Only fill empty defaults — don't overwrite later manual curation
        Product.objects.filter(
            canonical_name=canonical,
            default_case_size='',
        ).update(default_case_size=default)


def unset_curated_defaults(apps, schema_editor):
    Product = apps.get_model('myapp', 'Product')
    for canonical, default in CURATED_DEFAULTS.items():
        Product.objects.filter(
            canonical_name=canonical,
            default_case_size=default,
        ).update(default_case_size='')


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0022_product_default_case_size'),
    ]

    operations = [
        migrations.RunPython(set_curated_defaults, unset_curated_defaults),
    ]
