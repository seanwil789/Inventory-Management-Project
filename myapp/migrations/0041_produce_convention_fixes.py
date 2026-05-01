"""Produce convention fixes per locked naming taxonomy.

Per Sean (2026-04-30):
- Apples → singular convention (`Apple, X`)
- Oranges, Mandarin → singular (`Orange, Mandarin`)
- `Herb, Chives` and `Herb, Mint` → drop prefix (just `Chives` and `Mint`)
"""
from django.db import migrations


RENAMES = [
    # (current canonical, new canonical)
    ("Apples, Gala",          "Apple, Gala"),
    ("Apples, Granny Smith",  "Apple, Granny Smith"),
    ("Apples, Red Delicious", "Apple, Red Delicious"),
    ("Apples, Fuji",          "Apple, Fuji"),
    # Apple, Honeycrisp already singular
    ("Oranges, Mandarin",     "Orange, Mandarin"),
    ("Herb, Chives",          "Chives"),
    ("Herb, Mint",            "Mint"),
]


def apply_convention_fixes(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        # Avoid clobbering an existing canonical with the new name
        if Product.objects.filter(canonical_name=new).exists():
            continue
        Product.objects.filter(canonical_name=old).update(canonical_name=new)


def reverse_convention_fixes(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        if Product.objects.filter(canonical_name=old).exists():
            continue
        Product.objects.filter(canonical_name=new).update(canonical_name=old)


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0040_produce_taxonomy_remap"),
    ]

    operations = [
        migrations.RunPython(apply_convention_fixes, reverse_convention_fixes),
    ]
