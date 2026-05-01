"""Smallwares restructure: rename Paper/Disposable → Smallwares + cost-behavior tiers
+ land Water Filter from Coffee/Concessions + convention fixes.

Per Sean (2026-05-01): cost-behavior tiers preserve consumption-vs-census
analytics for the PD conversation. See feedback_categories_as_analytical_tools.md.

Three cost-behavior tiers:
- Paper Consumables   (uniform, ∝ census)
- Plastic Consumables (mixed elasticity — trash bags elastic, wrap/foil inelastic;
                       elasticity is an analytics-layer flag, not a structural split)
- Durable Smallwares  (event-driven replacement)
"""
from django.db import migrations


# Sub-tier assignment by old primary_descriptor + product ID for misclassifications

# Paper Consumables — what was in old Paper tier + items that fit (parchment, baking cups, pan liner)
PAPER_CONSUMABLES_IDS = {
    146,  # Towel, Paper
    335,  # Toilet Paper
    357,  # Napkins
    347,  # Paper Plates
    99,   # Paper Cups 12 oz
    612,  # Paper Cups 8 oz
    199,  # Parchment Paper
    410,  # Pastry Bags
    365,  # Baking Cups
    506,  # Bags (paper bags)
    605,  # Pan Liner, Quilon
    604,  # Container, Paper #3
}

# Durable Smallwares — items that aren't single-use
DURABLE_SMALLWARES_IDS = {
    606,  # Forks, Metal
    607,  # Spoons, Metal
    526,  # Aprons
    515,  # Rectangular Box and Lid (To-oo box) — if reusable
    615,  # Water Filter (incoming from Coffee/Concessions)
}


def restructure_smallwares(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")

    # ── 1. Rename Paper/Disposable category → Smallwares ────────────
    Product.objects.filter(category="Paper/Disposable").update(category="Smallwares")

    # ── 2. Land Water Filter from Coffee/Concessions ────────────
    Product.objects.filter(id=615, canonical_name="Water Filter").update(
        category="Smallwares",
    )

    # ── 3. Apply cost-behavior tiers ────────────
    smallwares = Product.objects.filter(category="Smallwares")
    for p in smallwares:
        if p.id in PAPER_CONSUMABLES_IDS:
            p.primary_descriptor = "Paper Consumables"
        elif p.id in DURABLE_SMALLWARES_IDS:
            p.primary_descriptor = "Durable Smallwares"
        else:
            p.primary_descriptor = "Plastic Consumables"
        p.secondary_descriptor = ""
        p.save(update_fields=["primary_descriptor", "secondary_descriptor"])


def reverse_smallwares_restructure(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    # Move Water Filter back to Coffee/Concessions/Beverages
    Product.objects.filter(id=615).update(category="Coffee/Concessions",
                                           primary_descriptor="Beverages")
    # Rename category back
    Product.objects.filter(category="Smallwares").update(category="Paper/Disposable")
    # Restore old primary_descriptor (best effort: Plastic Facility / Paper)
    Product.objects.filter(category="Paper/Disposable", id__in=PAPER_CONSUMABLES_IDS).update(
        primary_descriptor="Paper",
    )
    Product.objects.filter(category="Paper/Disposable").exclude(
        id__in=PAPER_CONSUMABLES_IDS,
    ).update(primary_descriptor="Plastic Facility")


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0056_chemicals_task_based_remap"),
    ]

    operations = [
        migrations.RunPython(restructure_smallwares, reverse_smallwares_restructure),
    ]
