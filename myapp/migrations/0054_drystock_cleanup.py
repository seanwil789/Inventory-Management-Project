"""Drystock cleanup: cross-category moves + PreFabs case-by-case.

Per Sean (2026-05-01):
1. Cross-category moves to Coffee/Concessions:
   - All Drystock 'Beverages' primary → Coffee/Concessions
   - All Drystock 'Chips' primary → Coffee/Concessions
   - All Drystock 'Cereals' primary → Coffee/Concessions
   - All Drystock 'Coffee Dispenser Station' primary → Coffee/Concessions

2. PreFabs case-by-case decisions:
   RETIRE → Pseudo: #242 Corn Tortilla, #484 French Fries, #529 Hash Browns,
                    #596 Macaroni Salad, #325 Pasta Salad
   RECATEGORIZE: #230 Pizza Sauce already moved to Drystock/Sauces in 0052
   KEEP (stays in Drystock/PreFabs): Falafel, Fries Frozen, Puff Pastry,
                                      Pumpkin Pie, Stuffing Mix, Taco Shells

The cooking-stage primary_descriptor for the kept Drystock products is
already correct in the existing data (Grains/Legumes, Pastas, Flours
and Starches, Oils, Vinegars, Condiments, Sauces, Canned Vegetables,
Sugars/Sweeteners, Baking, Leaveners, PreFabs). The cooking-stage ORDER
is enforced by synergy_sync section_label dict (updated separately).
"""
from django.db import migrations


# Drystock primaries that move wholesale to Coffee/Concessions
MOVE_PRIMARIES = ("Beverages", "Chips", "Cereals", "Coffee Dispenser Station")

# PreFabs to retire (5 products with low/no usage)
RETIRE_PREFAB_IDS = (242, 484, 529, 596, 325)


def drystock_cleanup(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    # Cross-category moves: keep primary_descriptor as-is, only change category
    for prim in MOVE_PRIMARIES:
        Product.objects.filter(category="Drystock", primary_descriptor=prim).update(
            category="Coffee/Concessions",
        )
    # PreFabs retires
    Product.objects.filter(id__in=RETIRE_PREFAB_IDS, category="Drystock").update(
        category="Pseudo",
    )


def reverse_drystock_cleanup(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for prim in MOVE_PRIMARIES:
        Product.objects.filter(category="Coffee/Concessions", primary_descriptor=prim).update(
            category="Drystock",
        )
    Product.objects.filter(id__in=RETIRE_PREFAB_IDS).update(category="Drystock")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0053_extract_spices_category"),
    ]

    operations = [
        migrations.RunPython(drystock_cleanup, reverse_drystock_cleanup),
    ]
