"""Bakery convention fixes — singular + comma format.

Per Sean (2026-04-30):
- Singular convention throughout
- Bread comma format (`Bread, Sourdough` etc.)
- Croissant variants → `Croissant, X`
- Hot Dog Roll (not Bun) — apply to canonical and secondary_descriptor
"""
from django.db import migrations


# (current canonical, new canonical)
RENAMES = [
    # Bagels singular
    ("Plain Bagels",          "Bagel, Plain"),
    ("Bagels, Cinnamon",      "Bagel, Cinnamon"),
    # Buns / rolls singular
    ("Burger Buns",           "Burger Bun"),
    ("Brioche Buns",          "Brioche Bun"),
    ("Brioche Sliders",       "Brioche Slider"),
    ("Hoagie Rolls",          "Hoagie Roll"),
    ("Seeded Hoagie Rolls",   "Seeded Hoagie Roll"),
    ("Hot Dog Rolls",         "Hot Dog Roll"),
    ("Kaiser Rolls",          "Kaiser Roll"),
    ("Dinner Rolls",          "Dinner Roll"),
    ("White Wraps",           "Wrap, White"),
    # Pastries
    ("Donuts",                "Donut"),
    ("Chocolate Croissant",   "Croissant, Chocolate"),
    # Quick breads
    ("Mini Muffins",          "Mini Muffin"),
    ("Blueberry Muffins",     "Blueberry Muffin"),
    # Bread comma convention
    ("Whole Wheat Bread",     "Bread, Whole Wheat"),
    ("Sour Dough",            "Bread, Sourdough"),
]


def apply_bakery_renames(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        if Product.objects.filter(canonical_name=new).exists():
            continue
        Product.objects.filter(canonical_name=old).update(canonical_name=new)
    # Also fix Hot Dog Bun secondary → Hot Dog Roll
    Product.objects.filter(
        category="Bakery", secondary_descriptor="Hot Dog Bun",
    ).update(secondary_descriptor="Hot Dog Roll")


def reverse_bakery_renames(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        if Product.objects.filter(canonical_name=old).exists():
            continue
        Product.objects.filter(canonical_name=new).update(canonical_name=old)
    Product.objects.filter(
        category="Bakery", secondary_descriptor="Hot Dog Roll",
    ).update(secondary_descriptor="Hot Dog Bun")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0048_proteins_retires_and_shrimp_rename"),
    ]

    operations = [
        migrations.RunPython(apply_bakery_renames, reverse_bakery_renames),
    ]
