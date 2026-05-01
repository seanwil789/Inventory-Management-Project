"""Unify Condiments/Sauces → Drystock.

Per Sean (2026-04-30): "a unified condiments and sauces was the original idea."
Asian condiments (6 products) fold in under Drystock/Condiments tier.
"""
from django.db import migrations


def unify_condiments_to_drystock(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    # Asian condiments → Drystock/Condiments
    Product.objects.filter(category="Condiments/Sauces", primary_descriptor="Asian").update(
        category="Drystock", primary_descriptor="Condiments",
    )
    # Sweet sauces → Drystock/Sauces (new tier)
    Product.objects.filter(category="Condiments/Sauces", primary_descriptor="Sweet").update(
        category="Drystock", primary_descriptor="Sauces",
    )
    # Vinegars → Drystock/Vinegars (existing tier)
    Product.objects.filter(category="Condiments/Sauces", primary_descriptor="Vinegars").update(
        category="Drystock",
    )
    # Plain Condiments → Drystock/Condiments (existing tier)
    Product.objects.filter(category="Condiments/Sauces", primary_descriptor="Condiments").update(
        category="Drystock",
    )
    # PreFabs (Pizza Sauce #230) → Drystock/Sauces (per case-by-case decision)
    Product.objects.filter(category="Condiments/Sauces", primary_descriptor="PreFabs").update(
        category="Drystock", primary_descriptor="Sauces",
    )


def split_back_to_condiments(apps, schema_editor):
    """No-op reverse — once unified, can't reliably split back without
    knowing original primary_descriptor mappings per product."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0051_bakery_bagel_merge"),
    ]

    operations = [
        migrations.RunPython(unify_condiments_to_drystock, split_back_to_condiments),
    ]
