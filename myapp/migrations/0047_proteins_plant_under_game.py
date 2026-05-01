"""Plant proteins → Game/Plant.

Per Sean (2026-04-30): "plant can go under game". Plant proteins are
alternate proteins; Game is the secondary slot, Plant is the primal slot.

- Veggie Burger (#153)    → primary=Game, secondary=Plant, prep_state=Processed
- Veggie Egg Roll (#320)  → primary=Game, secondary=Plant, prep_state=Processed
- Black Bean Burger (#329)→ primary=Game, secondary=Plant, prep_state=Processed
"""
from django.db import migrations


PLANT_IDS = (153, 320, 329)


def plant_under_game(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=PLANT_IDS, category="Proteins").update(
        primary_descriptor="Game",
        secondary_descriptor="Plant",
        prep_state="Processed",
    )


def reverse_plant(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=PLANT_IDS).update(
        primary_descriptor="Plant",
        secondary_descriptor="Processed",
        prep_state="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0046_proteins_bases_fold"),
    ]

    operations = [
        migrations.RunPython(plant_under_game, reverse_plant),
    ]
