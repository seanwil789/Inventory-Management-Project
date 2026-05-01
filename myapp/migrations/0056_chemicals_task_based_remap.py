"""Chemicals task-based primary_descriptor remap.

Per Sean (2026-05-01): organize chemicals by TASK (Dish/Floor/Equipment/
Bathroom/General) with chemicals + tools paired together. Old structure
(Cleaning/Sanitizing/Scrubbing) collapses; tools sit alongside the
chemicals they're used with.

5 task tiers:
  Dish      → Dish Detergent, Sanitizer, Sponge, Scrub Pad, Scour Pads,
              Scouring Pad, Steel Wool
  Floor     → Floor Cleaner, Mop Heads
  Equipment → ScotchBrite Flattop Cleaner, Fryer Cleaner, Grill Cleaner,
              Grill Brick, Degreaser, Broom (Sean: broom is equipment)
  Bathroom  → HandSoap, Disinfectant
  General   → Bleach, All Purpose Cleaner, Windex
"""
from django.db import migrations


# product_id → new task tier
TASK_REMAP = {
    # Dish
    105: "Dish",  # Dish Detergent
    562: "Dish",  # Sanitizer
    137: "Dish",  # Sponge
    136: "Dish",  # Scrub Pad
    518: "Dish",  # Scour Pads
    417: "Dish",  # Scouring Pad
    517: "Dish",  # Steel Wool
    # Floor
    156: "Floor", # Floor Cleaner
    8:   "Floor", # Mop Heads
    # Equipment
    523: "Equipment",  # ScotchBrite Flattop Cleaner
    372: "Equipment",  # Fryer Cleaner
    371: "Equipment",  # Grill Cleaner
    138: "Equipment",  # Grill Brick
    520: "Equipment",  # Degreaser
    412: "Equipment",  # Broom (Sean: broom is equipment)
    # Bathroom
    521: "Bathroom",   # HandSoap
    563: "Bathroom",   # Disinfectant
    # General
    176: "General",    # Bleach
    370: "General",    # All Purpose Cleaner
    522: "General",    # Windex
}


def remap_chemicals_by_task(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for pid, task in TASK_REMAP.items():
        Product.objects.filter(id=pid, category="Chemicals").update(
            primary_descriptor=task,
            secondary_descriptor="",
        )


def reverse_chemicals_remap(apps, schema_editor):
    """Best-effort reverse: rebuild old chemical-type tiers from current task tiers."""
    Product = apps.get_model("myapp", "Product")
    # Sponges/scrub/steel wool back to Scrubbing
    for pid in (137, 136, 518, 417, 517, 138, 8, 412):
        Product.objects.filter(id=pid).update(primary_descriptor="Scrubbing")
    # Sanitizer / Disinfectant back to Sanitizing
    for pid in (105, 562, 563):
        Product.objects.filter(id=pid).update(primary_descriptor="Sanitizing")
    # Rest back to Cleaning
    for pid in (156, 523, 372, 371, 520, 521, 176, 370, 522):
        Product.objects.filter(id=pid).update(primary_descriptor="Cleaning")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0055_coffee_concessions_cleanup"),
    ]

    operations = [
        migrations.RunPython(remap_chemicals_by_task, reverse_chemicals_remap),
    ]
