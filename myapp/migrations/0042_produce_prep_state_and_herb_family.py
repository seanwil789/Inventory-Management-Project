"""Populate produce prep_state (Frozen / Juice / Dried + Fresh default)
and populate herb botanical family in secondary_descriptor.

Per Sean (2026-04-30):
- Frozen and Juice are prep_states (extracted from secondary_descriptor)
- Mushroom, Dried Shiitake → prep_state=Dried (canonical name kept per cheese precedent)
- Herb tier kept as culinary unit, botanical family in secondary_descriptor
  (Lamiaceae for basil/oregano/rosemary/thyme/mint, Apiaceae for cilantro/parsley/dill,
  Allium for chives)
"""
from django.db import migrations


HERB_FAMILY = {
    "Basil":    "Lamiaceae",
    "Mint":     "Lamiaceae",
    "Oregano":  "Lamiaceae",
    "Rosemary": "Lamiaceae",
    "Thyme":    "Lamiaceae",
    "Cilantro": "Apiaceae",
    "Dill":     "Apiaceae",
    "Parsley, Italian": "Apiaceae",
    "Chives":   "Allium",
}


def populate_produce_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")

    # Default all Produce products to prep_state=Fresh (only if currently blank)
    Product.objects.filter(category="Produce", prep_state="").update(prep_state="Fresh")

    # Frozen items — prep_state=Frozen, clear secondary_descriptor where it was "Frozen"
    for cn in ("Berry, Frozen", "Berry Mix, Frozen", "Corn, Frozen"):
        Product.objects.filter(category="Produce", canonical_name=cn).update(
            prep_state="Frozen", secondary_descriptor="",
        )

    # Juice items — prep_state=Juice, clear secondary_descriptor where it was "Juice"
    for cn in ("Lemon Juice", "Lime Juice", "Orange Juice"):
        Product.objects.filter(category="Produce", canonical_name=cn).update(
            prep_state="Juice", secondary_descriptor="",
        )

    # Dried items
    Product.objects.filter(
        category="Produce", canonical_name="Mushroom, Dried Shiitake",
    ).update(prep_state="Dried")

    # Herb botanical family in secondary_descriptor
    for canonical, family in HERB_FAMILY.items():
        Product.objects.filter(
            category="Produce", canonical_name=canonical,
        ).update(secondary_descriptor=family)


def reverse_produce_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(category="Produce").update(prep_state="")
    # Restore Frozen/Juice secondaries
    for cn in ("Berry, Frozen", "Berry Mix, Frozen"):
        Product.objects.filter(category="Produce", canonical_name=cn).update(
            secondary_descriptor="Frozen",
        )
    for cn in ("Lemon Juice", "Lime Juice", "Orange Juice"):
        Product.objects.filter(category="Produce", canonical_name=cn).update(
            secondary_descriptor="Juice",
        )
    # Clear herb families
    for canonical in HERB_FAMILY:
        Product.objects.filter(
            category="Produce", canonical_name=canonical,
        ).update(secondary_descriptor="")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0041_produce_convention_fixes"),
    ]

    operations = [
        migrations.RunPython(populate_produce_prep_state, reverse_produce_prep_state),
    ]
