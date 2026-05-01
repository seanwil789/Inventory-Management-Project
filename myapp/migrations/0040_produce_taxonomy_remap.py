"""Produce taxonomy cleanup — kill non-scientific tiers, add botanical solo tiers.

Per Sean (2026-04-30 + later): "tropical is not a categorization scientifically."
Same logic kills `Vine` (growth-habit, not family) and `Stone Fruit` (Drupe is the
botanical name). `Fruits` tier was a convention-migration artifact (only Lime + Orange
which belong in Citrus). `PreFabs` for Guacamole retires (Sean: "If I want guacamole,
I will make it").

Mapping:
- Tropical (#401 Mango)         → Drupe (Mango is botanically a drupe)
- Tropical (#79 Pineapple)      → Bromeliaceae (new solo tier)
- Tropical (#116 Banana)        → Musa (new solo tier)
- Stone Fruit (#401 Mango legacy alias) → Drupe (just in case)
- Vine (#115 Avocado)           → Lauraceae (new solo tier)
- Vine (#266 Grapes Green)      → Vitaceae (new solo tier)
- Vine (#39 Grapes Red)         → Vitaceae
- Fruits (#122 Lime)            → Citrus
- Fruits (#123 Orange)          → Citrus
- PreFabs (#358 Guacamole)      → category=Pseudo (retire)
"""
from django.db import migrations


PRIMARY_REMAP = {
    # Mango (currently Stone Fruit, possibly Tropical) → Drupe
    "Stone Fruit":  "Drupe",
    "Tropical":     None,   # multiple destinations — handled per-product below
    "Vine":         None,   # multiple destinations
    "Fruits":       "Citrus",
}

# Per-product overrides for the multi-destination tiers
TROPICAL_DESTINATIONS = {
    "Mango":      "Drupe",
    "Banana":     "Musa",
    "Pineapple":  "Bromeliaceae",
}

VINE_DESTINATIONS = {
    "Avocado":              "Lauraceae",
    "Grapes, Green Seedless": "Vitaceae",
    "Grapes, Red Seedless":   "Vitaceae",
}


def remap_produce_taxonomy(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")

    # Tropical → per-canonical destinations
    for p in Product.objects.filter(category="Produce", primary_descriptor="Tropical"):
        new_primary = TROPICAL_DESTINATIONS.get(p.canonical_name)
        if new_primary:
            p.primary_descriptor = new_primary
            p.save(update_fields=["primary_descriptor"])

    # Vine → per-canonical destinations
    for p in Product.objects.filter(category="Produce", primary_descriptor="Vine"):
        new_primary = VINE_DESTINATIONS.get(p.canonical_name)
        if new_primary:
            p.primary_descriptor = new_primary
            p.save(update_fields=["primary_descriptor"])

    # Stone Fruit → Drupe (Mango if it landed here)
    Product.objects.filter(
        category="Produce", primary_descriptor="Stone Fruit",
    ).update(primary_descriptor="Drupe")

    # Fruits → Citrus (Lime, Orange)
    Product.objects.filter(
        category="Produce", primary_descriptor="Fruits",
    ).update(primary_descriptor="Citrus")

    # PreFabs/Guacamole → retire to Pseudo
    Product.objects.filter(
        id=358, canonical_name="Guacamole", category="Produce",
    ).update(category="Pseudo")


def reverse_produce_taxonomy(apps, schema_editor):
    """Best-effort reverse — restore the old non-scientific tiers."""
    Product = apps.get_model("myapp", "Product")
    for canonical, _new in TROPICAL_DESTINATIONS.items():
        Product.objects.filter(
            category="Produce", canonical_name=canonical,
        ).update(primary_descriptor="Tropical")
    for canonical, _new in VINE_DESTINATIONS.items():
        Product.objects.filter(
            category="Produce", canonical_name=canonical,
        ).update(primary_descriptor="Vine")
    Product.objects.filter(
        category="Produce", canonical_name__in=("Lime", "Orange"),
    ).update(primary_descriptor="Fruits")
    Product.objects.filter(id=358, canonical_name="Guacamole").update(category="Produce")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0039_dairy_cross_category_moves"),
    ]

    operations = [
        migrations.RunPython(remap_produce_taxonomy, reverse_produce_taxonomy),
    ]
