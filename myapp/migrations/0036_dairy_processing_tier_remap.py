"""Remap Dairy primary_descriptor to the 12-tier processing-chain order.

Pedagogy: section order teaches dairy processing flow:
  Milk → Cream → Yogurt → Butter → Cheese-Fresh → Cheese-Soft-Ripened →
  Cheese-Semi-Soft → Cheese-Semi-Hard → Cheese-Hard → Cheese-Processed →
  Processed → Frozen

Mapping rules:
- Old Dairy/Milk          → primary=Milk
- Old Dairy/Cream         → primary=Cream
- Old Dairy/Yogurt        → primary=Yogurt
- Old Dairy/Butter        → primary=Butter
- Old Dairy/Frozen        → primary=Frozen
- Old Dairy/Processed     → primary=Processed
- Old Cheese/<milk>/Fresh → primary='Cheese, Fresh'   (milk→secondary if not Cow)
- Old Cheese/*/Soft-Ripened → primary='Cheese, Soft-Ripened'
- Old Cheese/*/Semi-Soft  → primary='Cheese, Semi-Soft'
- Old Cheese/Cow/Blue     → primary='Cheese, Semi-Soft' (Sean: Blue is semi-soft)
- Old Cheese/*/Semi-Hard  → primary='Cheese, Semi-Hard'
- Old Cheese/*/Hard       → primary='Cheese, Hard'
- Old Cheese/*/Processed  → primary='Cheese, Processed'

For cheese products: if old primary_descriptor (milk source) was Cow or
Processed, set new secondary_descriptor=''. Otherwise (Goat / Sheep),
keep milk source in new secondary_descriptor.

Depends on 0035 (Cheese → Dairy collapse already done).
"""
from django.db import migrations


# (old_primary, old_secondary) → (new_primary, secondary_keep_source?)
DAIRY_NON_CHEESE_MAP = {
    ("Milk", ""):       "Milk",
    ("Cream", ""):      "Cream",
    ("Yogurt", ""):     "Yogurt",
    ("Butter", ""):     "Butter",
    ("Frozen", ""):     "Frozen",
    ("Processed", ""):  "Processed",
}

# (old_secondary_texture) → new_primary cheese tier
CHEESE_TEXTURE_MAP = {
    "Fresh":         "Cheese, Fresh",
    "Soft-Ripened":  "Cheese, Soft-Ripened",
    "Semi-Soft":     "Cheese, Semi-Soft",
    "Blue":          "Cheese, Semi-Soft",  # Sean: Blue is semi-soft
    "Semi-Hard":     "Cheese, Semi-Hard",
    "Hard":          "Cheese, Hard",
    "Processed":     "Cheese, Processed",
}


def remap_dairy_processing_tiers(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for p in Product.objects.filter(category="Dairy"):
        old_primary = p.primary_descriptor
        old_secondary = p.secondary_descriptor

        # Cheese products: old structure was (milk_source, texture)
        # where milk_source was in primary_descriptor (e.g., "Cow", "Goat")
        if old_primary in ("Cow", "Goat", "Sheep", "Processed") and old_secondary in CHEESE_TEXTURE_MAP:
            p.primary_descriptor = CHEESE_TEXTURE_MAP[old_secondary]
            # Keep milk source in secondary if non-default (non-Cow)
            p.secondary_descriptor = old_primary if old_primary in ("Goat", "Sheep") else ""
            p.save(update_fields=["primary_descriptor", "secondary_descriptor"])
            continue

        # Non-cheese dairy: old structure was (kind, "")
        new_primary = DAIRY_NON_CHEESE_MAP.get((old_primary, old_secondary))
        if new_primary:
            p.primary_descriptor = new_primary
            p.secondary_descriptor = ""
            p.save(update_fields=["primary_descriptor", "secondary_descriptor"])


def reverse_dairy_processing_tiers(apps, schema_editor):
    """Best-effort reverse: cheese tiers split back to milk-source primary +
    texture secondary. Non-cheese dairy retains primary, secondary stays blank."""
    Product = apps.get_model("myapp", "Product")
    REVERSE_TEXTURE = {v: k for k, v in CHEESE_TEXTURE_MAP.items() if k != "Blue"}
    for p in Product.objects.filter(category="Dairy"):
        if p.primary_descriptor in REVERSE_TEXTURE:
            texture = REVERSE_TEXTURE[p.primary_descriptor]
            milk_source = p.secondary_descriptor or "Cow"
            p.primary_descriptor = milk_source
            p.secondary_descriptor = texture
            p.save(update_fields=["primary_descriptor", "secondary_descriptor"])


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0035_unify_dairy_cheese_category"),
    ]

    operations = [
        migrations.RunPython(remap_dairy_processing_tiers, reverse_dairy_processing_tiers),
    ]
