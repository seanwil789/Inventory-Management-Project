"""Retire #601 'Whipped Topping' to Pseudo category.

Per Sean (2026-04-30): Whipped Topping (non-dairy Cool Whip-style) is
not actually dairy and not actively used in recipes. Retire path uses
the existing Pseudo pattern (precedent: #553 'Water (free, untracked)').

Why retire vs delete:
- 3 ProductMappings (raw descriptions still flow through if invoiced)
- 4 InvoiceLineItems (history preserved)
- 0 RecipeIngredients

Setting category=Pseudo removes from order guides + sheet sync (Pseudo
is excluded from SHEET_CATEGORIES) without orphaning historical data.
"""
from django.db import migrations


def retire_whipped_topping(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id=601, canonical_name="Whipped Topping").update(category="Pseudo")


def unretire_whipped_topping(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id=601, canonical_name="Whipped Topping").update(category="Dairy")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0037_merge_american_cheese_dups"),
    ]

    operations = [
        migrations.RunPython(retire_whipped_topping, unretire_whipped_topping),
    ]
