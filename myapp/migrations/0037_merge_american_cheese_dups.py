"""Merge #16 'American' (dormant) into #585 'American Cheese, Sliced'.

Per Sean (2026-04-30): "American is American is American, the only
exception is Cooper Sharp." Two existing canonicals for American cheese
were a convention-migration artifact. #16 is dormant w/ stale data;
#585 is the active SKU.

Merge path:
- 4 ProductMappings: re-point to #585
- 16 InvoiceLineItems: re-point to #585 (preserves purchase history under unified canonical)
- 2 RecipeIngredients: re-point to #585 (recipe ingredient names preserved on the row, so display is intact)
- Hard-delete #16

Reverse: not safe (data has been merged). No-op reverse — would need
manual rebuild from invoice history.
"""
from django.db import migrations


def merge_american_cheese_dups(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    ProductMapping = apps.get_model("myapp", "ProductMapping")
    InvoiceLineItem = apps.get_model("myapp", "InvoiceLineItem")
    RecipeIngredient = apps.get_model("myapp", "RecipeIngredient")
    ProductMappingProposal = apps.get_model("myapp", "ProductMappingProposal")

    src = Product.objects.filter(id=16, canonical_name="American").first()
    dst = Product.objects.filter(id=585, canonical_name="American Cheese, Sliced").first()
    if not src or not dst:
        # Idempotent: already merged or pre-existing rename. No-op.
        return

    ProductMapping.objects.filter(product=src).update(product=dst)
    InvoiceLineItem.objects.filter(product=src).update(product=dst)
    RecipeIngredient.objects.filter(product=src).update(product=dst)
    # Any pending proposals targeting src → redirect to dst
    ProductMappingProposal.objects.filter(suggested_product=src).update(suggested_product=dst)
    src.delete()


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0036_dairy_processing_tier_remap"),
    ]

    operations = [
        migrations.RunPython(merge_american_cheese_dups, reverse_noop),
    ]
