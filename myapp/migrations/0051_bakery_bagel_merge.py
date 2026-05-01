"""Merge `Bagel` (#415) + `Bagel, Plain` (#7, renamed from `Plain Bagels` in 0049).

Per Sean (2026-04-30): same SKU, drift artifact. Merge into the more-anchored
ID. After 0049 the canonical names are `Bagel` (#415, no variant) and
`Bagel, Plain` (#7, renamed). Treat `Bagel, Plain` as canonical for the
generic bagel; redirect #415 → #7.
"""
from django.db import migrations


def merge_bagel_dups(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    ProductMapping = apps.get_model("myapp", "ProductMapping")
    InvoiceLineItem = apps.get_model("myapp", "InvoiceLineItem")
    RecipeIngredient = apps.get_model("myapp", "RecipeIngredient")
    ProductMappingProposal = apps.get_model("myapp", "ProductMappingProposal")

    src = Product.objects.filter(id=415, canonical_name="Bagel").first()
    dst = Product.objects.filter(id=7, canonical_name="Bagel, Plain").first()
    if not src or not dst:
        return  # idempotent

    ProductMapping.objects.filter(product=src).update(product=dst)
    InvoiceLineItem.objects.filter(product=src).update(product=dst)
    RecipeIngredient.objects.filter(product=src).update(product=dst)
    ProductMappingProposal.objects.filter(suggested_product=src).update(suggested_product=dst)
    src.delete()


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0050_bakery_prep_state"),
    ]

    operations = [
        migrations.RunPython(merge_bagel_dups, reverse_noop),
    ]
