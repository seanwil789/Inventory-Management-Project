"""Fold Bases into their parent animal with anatomical-source secondary.

Per Sean (2026-04-30): "primal slot is bone for base" — pedagogically
consistent (secondary_descriptor = anatomical part throughout). Crab uses
Shell since crustaceans don't have bones.

- Beef Base (#233)    → primary=Beef,    secondary=Bone, prep_state=Base
- Chicken Base (#441) → primary=Poultry, secondary=Bone, prep_state=Base
- Crab Base (#442)    → primary=Seafood, secondary=Shell, prep_state=Base
"""
from django.db import migrations


BASE_REMAP = {
    233: ("Beef",    "Bone"),
    441: ("Poultry", "Bone"),
    442: ("Seafood", "Shell"),
}


def fold_bases_into_animals(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for pid, (animal, source) in BASE_REMAP.items():
        Product.objects.filter(id=pid, category="Proteins").update(
            primary_descriptor=animal,
            secondary_descriptor=source,
            prep_state="Base",
        )


def reverse_fold(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=BASE_REMAP.keys()).update(
        primary_descriptor="Base",
        secondary_descriptor="Processed",
        prep_state="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0045_proteins_schema_reshape"),
    ]

    operations = [
        migrations.RunPython(fold_bases_into_animals, reverse_fold),
    ]
