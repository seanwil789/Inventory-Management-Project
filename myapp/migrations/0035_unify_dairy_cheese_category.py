"""Collapse `Cheese` category → `Dairy`.

Per the dairy walkthrough decision (Sean 2026-04-30): dairy and cheese
are one continuous processing chain (raw milk → cream → cultured →
butter → fresh cheese → aged cheese → processed → frozen). The DB split
between two top-level categories was a hangover from earlier iterations.
The sheet already treats them as one section ("Dairy / Cheese").

After this migration, all cheese products live under category="Dairy",
ready for the 12-tier processing-chain primary_descriptor remap (0036).

Reverse: re-split products back to "Cheese" if their primary_descriptor
starts with "Cheese, " (which only happens after 0036). For products
that landed here pre-0036, no clean reverse exists, so we no-op.
"""
from django.db import migrations


def collapse_cheese_to_dairy(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(category__iexact="Cheese").update(category="Dairy")


def split_dairy_back_to_cheese(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    # Best-effort reverse: anything with primary_descriptor starting "Cheese, "
    # came from the old Cheese category. Move back.
    Product.objects.filter(
        category="Dairy",
        primary_descriptor__startswith="Cheese, ",
    ).update(category="Cheese")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0034_cheese_prep_state_populate"),
    ]

    operations = [
        migrations.RunPython(collapse_cheese_to_dairy, split_dairy_back_to_cheese),
    ]
