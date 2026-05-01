"""Bakery prep_state populate.

Per Sean (2026-04-30): prep_state = cut state. For bread:
- Sandwich Loaf → Sliced (kitchen norm)
- Bagels, Buns, Rolls, Tortillas, Wraps, Pita → Whole (already individual portions)

Schema consistency: secondary_descriptor = SHAPE (Sandwich Loaf / Bagel / Bun / etc.),
prep_state = CUT STATE (Whole / Sliced / Half-Sliced).
"""
from django.db import migrations


# Secondary_descriptor → default prep_state for that shape
SHAPE_PREP_DEFAULT = {
    "Sandwich Loaf":  "Sliced",  # bread loaves come pre-sliced from vendor
    "Bagel":          "Whole",
    "Brioche Bun":    "Whole",
    "Hamburger Bun":  "Whole",
    "Hoagie Roll":    "Whole",
    "Hot Dog Roll":   "Whole",
    "Kaiser Roll":    "Whole",
    "Dinner Roll":    "Whole",
    "Pita":           "Whole",
    "Tortilla":       "Whole",
    "Wrap":           "Whole",
    "Breadstick":     "Whole",
    # Pastries / quick breads
    "Croissant":      "Whole",
    "Danish":         "Whole",
    "Donut":          "Whole",
    "Muffin":         "Whole",
}


def populate_bakery_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for shape, default_prep in SHAPE_PREP_DEFAULT.items():
        Product.objects.filter(
            category="Bakery",
            secondary_descriptor=shape,
            prep_state="",
        ).update(prep_state=default_prep)


def clear_bakery_prep_state(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(category="Bakery").update(prep_state="")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0049_bakery_convention_fixes"),
    ]

    operations = [
        migrations.RunPython(populate_bakery_prep_state, clear_bakery_prep_state),
    ]
