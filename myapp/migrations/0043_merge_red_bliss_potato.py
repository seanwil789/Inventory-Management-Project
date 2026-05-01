"""Merge #588 'Potato, Red A-Size' → #84 'Red Bliss', then rename to 'Potato, Red Bliss'.

Per Sean (2026-04-30): Red Bliss IS the cultivar of A-size red potatoes;
Sysco names by USDA grade, Farm Art names by cultivar — same product.
Rename to match `Potato, Idaho / Yukon Gold / Fingerling` convention.
"""
from django.db import migrations


def merge_and_rename_red_bliss(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    ProductMapping = apps.get_model("myapp", "ProductMapping")
    InvoiceLineItem = apps.get_model("myapp", "InvoiceLineItem")
    RecipeIngredient = apps.get_model("myapp", "RecipeIngredient")
    ProductMappingProposal = apps.get_model("myapp", "ProductMappingProposal")

    src = Product.objects.filter(id=588, canonical_name="Potato, Red A-Size").first()
    dst = Product.objects.filter(id=84, canonical_name="Red Bliss").first()
    if not src or not dst:
        # Idempotent — already merged or pre-renamed. No-op.
        return

    ProductMapping.objects.filter(product=src).update(product=dst)
    InvoiceLineItem.objects.filter(product=src).update(product=dst)
    RecipeIngredient.objects.filter(product=src).update(product=dst)
    ProductMappingProposal.objects.filter(suggested_product=src).update(suggested_product=dst)
    src.delete()

    # Rename to Potato convention
    Product.objects.filter(id=84).update(canonical_name="Potato, Red Bliss")


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0042_produce_prep_state_and_herb_family"),
    ]

    operations = [
        migrations.RunPython(merge_and_rename_red_bliss, reverse_noop),
    ]
