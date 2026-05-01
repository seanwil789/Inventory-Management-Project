"""Populate prep_state for Cheese category products from canonical-name suffixes.

Per-suffix mapping:
  ", Shredded" → Shredded
  ", Sliced"   → Sliced
  ", Loaf"     → Loaf
  ", Balls"    → Balls
  ", Wedge"    → Wedge
  ", Crumbled" → Crumbled
  (anything else in Cheese category) → Whole (default form)

Other categories left untouched — Proteins migration ships separately.
"""
from django.db import migrations


SUFFIX_MAP = [
    (", Shredded", "Shredded"),
    (", Sliced", "Sliced"),
    (", Loaf", "Loaf"),
    (", Balls", "Balls"),
    (", Wedge", "Wedge"),
    (", Crumbled", "Crumbled"),
]


def populate_cheese_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for p in Product.objects.filter(category__iexact="Cheese"):
        new_state = "Whole"
        for suffix, state in SUFFIX_MAP:
            if p.canonical_name.endswith(suffix):
                new_state = state
                break
        if p.prep_state != new_state:
            p.prep_state = new_state
            p.save(update_fields=["prep_state"])


def clear_cheese_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(category__iexact="Cheese").update(prep_state="")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0033_product_prep_state"),
    ]

    operations = [
        migrations.RunPython(populate_cheese_prep_state, clear_cheese_prep_state),
    ]
