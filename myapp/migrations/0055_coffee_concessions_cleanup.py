"""Coffee/Concessions cleanup: Pringles audit + Cap'n Crunch merge + naming convention.

Three operations:

1. Pringles #235 audit + redirect:
   - 4 BBQ-flavor ILIs/PMs → #618 Pringles, BBQ
   - 1 Original PM + 2 ambiguous "Grab & Go" ILIs → #568 Pringles, Original
   - 1 wildly-misrouted Coconut Snowflake ILI → #478 Coconut, Dry Shredded (Drystock/Baking)
   - Then delete #235

2. Cap'n Crunch dup merge: #339 'Captn Crunch' → #597 "Cap'n Crunch" (apostrophe variant).

3. Convention fixes — comma format:
   - Salt Packets → Salt, Packets
   - Sugar Packets → Sugar, Packets
   - Equal Packets → Equal, Packets
   - Splenda Packets → Splenda, Packets
   - Sweet and Low Packets → Sweet and Low, Packets
   - Chip Variety Pack → Chips, Variety
"""
from django.db import migrations


PACKET_RENAMES = [
    ("Salt Packets",          "Salt, Packets"),
    ("Sugar Packets",         "Sugar, Packets"),
    ("Equal Packets",         "Equal, Packets"),
    ("Splenda Packets",       "Splenda, Packets"),
    ("Sweet and Low Packets", "Sweet and Low, Packets"),
    ("Chip Variety Pack",     "Chips, Variety"),
]


def coffee_concessions_cleanup(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    ProductMapping = apps.get_model("myapp", "ProductMapping")
    InvoiceLineItem = apps.get_model("myapp", "InvoiceLineItem")
    RecipeIngredient = apps.get_model("myapp", "RecipeIngredient")
    ProductMappingProposal = apps.get_model("myapp", "ProductMappingProposal")

    # ── 1. Pringles audit + redirect + delete ────────────
    pringles_generic = Product.objects.filter(id=235, canonical_name="Pringles").first()
    pringles_bbq     = Product.objects.filter(id=618, canonical_name="Pringles, BBQ").first()
    pringles_orig    = Product.objects.filter(id=568, canonical_name="Pringles, Original").first()
    coconut          = Product.objects.filter(id=478, canonical_name="Coconut, Dry Shredded").first()

    if pringles_generic and pringles_bbq and pringles_orig and coconut:
        # Route by raw description content
        for ili in InvoiceLineItem.objects.filter(product=pringles_generic):
            raw = (ili.raw_description or "").upper()
            if "BBQ" in raw:
                ili.product = pringles_bbq
            elif "COCONUT" in raw:
                ili.product = coconut
            else:
                ili.product = pringles_orig
            ili.save(update_fields=["product"])
        for pm in ProductMapping.objects.filter(product=pringles_generic):
            desc = (pm.description or "").upper()
            if "BBQ" in desc:
                pm.product = pringles_bbq
            else:
                pm.product = pringles_orig
            pm.save(update_fields=["product"])
        ProductMappingProposal.objects.filter(suggested_product=pringles_generic).update(
            suggested_product=pringles_orig,
        )
        # Recipe ingredients (none expected, but safe)
        RecipeIngredient.objects.filter(product=pringles_generic).update(product=pringles_orig)
        pringles_generic.delete()

    # ── 2. Cap'n Crunch merge ────────────
    src = Product.objects.filter(id=339, canonical_name="Captn Crunch").first()
    dst = Product.objects.filter(id=597, canonical_name="Cap'n Crunch").first()
    if src and dst:
        ProductMapping.objects.filter(product=src).update(product=dst)
        InvoiceLineItem.objects.filter(product=src).update(product=dst)
        RecipeIngredient.objects.filter(product=src).update(product=dst)
        ProductMappingProposal.objects.filter(suggested_product=src).update(suggested_product=dst)
        src.delete()

    # ── 3. Packet + Chips, Variety renames ────────────
    for old, new in PACKET_RENAMES:
        if Product.objects.filter(canonical_name=new).exists():
            continue
        Product.objects.filter(canonical_name=old).update(canonical_name=new)


def reverse_noop(apps, schema_editor):
    """No safe reverse for redirected ILIs/PMs. Renames could be reversed
    but with low value at this point."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0054_drystock_cleanup"),
    ]

    operations = [
        migrations.RunPython(coffee_concessions_cleanup, reverse_noop),
    ]
