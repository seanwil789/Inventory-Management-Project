"""Cross-category moves out of Dairy per format-intent + plant-milk policy.

Per Sean (2026-04-30):
- Chobani flavored cups (single-serve branded snack format) → Concessions
- Sweetened Condensed Milk + Powdered Milk (canned/dry, baking aisle) → Drystock
- Soy Milk (and any future plant milks) → Concessions (resident beverage,
  not recipe ingredient — plant milks aren't 1:1 dairy substitutes)

See feedback_format_intent_classification.md for the rule.
"""
from django.db import migrations


# Chobani flavored cups → Coffee/Concessions / Snack/Lunch Options
CHOBANI_IDS = (295, 296, 298, 299, 559, 560)
# Baking-aisle dry milk products → Drystock / Baking
DRYSTOCK_BAKING_IDS = (453, 454)  # Sweetened Condensed Milk, Powdered Milk
# Plant milks → Coffee/Concessions / Coffee Dispenser Station
PLANT_MILK_IDS = (253,)  # Milk, Soy


def move_dairy_cross_category(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=CHOBANI_IDS).update(
        category="Coffee/Concessions",
        primary_descriptor="Snack/Lunch Options",
        secondary_descriptor="",
    )
    Product.objects.filter(id__in=DRYSTOCK_BAKING_IDS).update(
        category="Drystock",
        primary_descriptor="Baking",
        secondary_descriptor="",
    )
    Product.objects.filter(id__in=PLANT_MILK_IDS).update(
        category="Coffee/Concessions",
        primary_descriptor="Coffee Dispenser Station",
        secondary_descriptor="",
    )


def reverse_cross_category(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=CHOBANI_IDS).update(
        category="Dairy", primary_descriptor="Yogurt", secondary_descriptor="",
    )
    Product.objects.filter(id__in=DRYSTOCK_BAKING_IDS).update(
        category="Dairy", primary_descriptor="Processed", secondary_descriptor="",
    )
    Product.objects.filter(id__in=PLANT_MILK_IDS).update(
        category="Dairy", primary_descriptor="Milk", secondary_descriptor="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0038_retire_whipped_topping"),
    ]

    operations = [
        migrations.RunPython(move_dairy_cross_category, reverse_cross_category),
    ]
