"""Smallwares convention fixes — singular + comma format throughout.

Per Sean (2026-04-30) singular convention rule + comma format for size/material.
"""
from django.db import migrations


RENAMES = [
    # Tumblers + cups
    ("8 oz Tumbler",         "Tumbler, 8 oz"),
    ("9 oz Rpet",            "Cup, 9 oz Rpet"),
    ("Plastic Cups 16 oz",   "Cup, Plastic, 16 oz"),
    ("Paper Cups 12 oz",     "Cup, Paper, 12 oz"),
    ("Paper Cups 8 oz",      "Cup, Paper, 8 oz"),
    # Bowls
    ("Bowls, 32oz",          "Bowl, 32 oz"),
    # Containers
    ("Cup Containers",       "Container, Cup"),
    ("Pint Containers",      "Container, Pint"),
    ("Quart Containers",     "Container, Quart"),
    # Utensils — both plastic and metal singular + comma
    ("Plastic Forks",        "Fork, Plastic"),
    ("Plastic Knives",       "Knife, Plastic"),
    ("Plastic Spoons",       "Spoon, Plastic"),
    ("Forks, Metal",         "Fork, Metal"),
    ("Spoons, Metal",        "Spoon, Metal"),
    # Bags / wraps
    ("Plastic Wrap",         "Wrap, Plastic"),
    ("Trash Bags",           "Bag, Trash"),
    ("Trash Liner",          "Liner, Trash"),
    ("Zip Lock Bags",        "Bag, Zip Lock"),
    ("Pastry Bags",          "Bag, Pastry"),
    ("Bags, Plastic Slide, Gallon", "Bag, Plastic Slide, Gallon"),
    # Plates
    ("Paper Plates",         "Plate, Paper"),
    # To-Go box typo fix
    ("Rectangular Box and Lid (To-oo box)", "Box, To-Go (Rectangular)"),
]


def apply_smallwares_renames(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        if Product.objects.filter(canonical_name=new).exists():
            continue
        Product.objects.filter(canonical_name=old).update(canonical_name=new)


def reverse_smallwares_renames(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old, new in RENAMES:
        if Product.objects.filter(canonical_name=old).exists():
            continue
        Product.objects.filter(canonical_name=new).update(canonical_name=old)


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0057_smallwares_restructure"),
    ]

    operations = [
        migrations.RunPython(apply_smallwares_renames, reverse_smallwares_renames),
    ]
