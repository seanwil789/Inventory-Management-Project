"""Extract Spices into own top-level category + populate sub-tier + botanical family + prep_state.

Per Sean (2026-05-01): "spices are spices, if there are other open slots
downstream then categorization would be possible." Split Spices into its
own category with cooking-stage sub-tiers in primary_descriptor and
botanical families in secondary_descriptor.

40 products affected (Spices=39 + Spice typo=1).

Sub-tier order (cooking-stage flow):
  Salt → Pepper → Heat → Aromatic → Earthy → Allium → Dried Herbs → Blends → Seeds

prep_state: Whole / Ground (extracted from existing canonical-name suffixes
where present; defaults to Whole for whole-form spices).
"""
from django.db import migrations


# canonical_name → (sub_tier, family, prep_state, new_canonical_name_if_renamed)
SPICE_REMAP = {
    # Salt
    "Salt, Iodized":               ("Salt", "Mineral", "Whole", None),
    "Salt, Kosher":                ("Salt", "Mineral", "Whole", None),
    "Salt, Maldon":                ("Salt", "Mineral", "Whole", None),
    # Pepper
    "Black Pepper, Whole":         ("Pepper", "Piperaceae", "Whole", "Black Pepper"),
    "Black Pepper, Ground, Fine":  ("Pepper", "Piperaceae", "Ground", "Black Pepper, Fine"),
    # Heat — capsicum-derived chiles (Solanaceae)
    "Cayenne Pepper":              ("Heat", "Solanaceae", "Ground", None),
    "Chili Peppers, Dried":        ("Heat", "Solanaceae", "Whole", None),
    "Pepper, Ancho, Dried":        ("Heat", "Solanaceae", "Whole", None),
    "Pepper, D'Arbol, Dried":      ("Heat", "Solanaceae", "Whole", None),
    "Chipotle, Powder":            ("Heat", "Solanaceae", "Ground", None),
    "Red Pepper, Crushed":         ("Heat", "Solanaceae", "Ground", None),
    "Chili Powder, Dark":          ("Heat", "Solanaceae", "Ground", None),
    "Chili Powder, Light":         ("Heat", "Solanaceae", "Ground", None),
    # Aromatic warm
    "Allspice, Ground":            ("Aromatic", "Myrtaceae", "Ground", "Allspice"),
    "Cardamom":                    ("Aromatic", "Zingiberaceae", "Whole", None),
    "Cinnamon, Ground":            ("Aromatic", "Lauraceae", "Ground", "Cinnamon"),
    "Cinnamon, Stick":             ("Aromatic", "Lauraceae", "Whole", "Cinnamon"),
    "Cloves, Whole":               ("Aromatic", "Myrtaceae", "Whole", "Cloves"),
    "Nutmeg":                      ("Aromatic", "Myristicaceae", "Whole", None),
    "Star Anise":                  ("Aromatic", "Schisandraceae", "Whole", None),
    "Sumac":                       ("Aromatic", "Anacardiaceae", "Ground", None),
    # Earthy
    "Coriander":                   ("Earthy", "Apiaceae", "Whole", None),
    "Cumin, Ground":               ("Earthy", "Apiaceae", "Ground", "Cumin"),
    "Cumin, Whole":                ("Earthy", "Apiaceae", "Whole", "Cumin"),
    "Paprika":                     ("Earthy", "Solanaceae", "Ground", None),
    "Saffron":                     ("Earthy", "Iridaceae", "Whole", None),
    # Allium
    "Garlic Powder":               ("Allium", "Amaryllidaceae", "Ground", None),
    "Garlic, Granulated":          ("Allium", "Amaryllidaceae", "Ground", None),
    "Onion Powder":                ("Allium", "Amaryllidaceae", "Ground", None),
    # Dried Herbs
    "Basil, Dried":                ("Dried Herbs", "Lamiaceae", "Whole", None),
    "Bay Leaves, Whole":           ("Dried Herbs", "Lauraceae", "Whole", "Bay Leaves"),
    "Oregano, Dried":              ("Dried Herbs", "Lamiaceae", "Whole", None),
    "Thyme, Dried":                ("Dried Herbs", "Lamiaceae", "Whole", None),
    "Parsley, Dried":              ("Dried Herbs", "Apiaceae", "Whole", None),
    # Blends
    "Curry Powder":                ("Blends", "", "Ground", None),
    "Garam Masala":                ("Blends", "", "Ground", None),
    "Old Bay":                     ("Blends", "", "Ground", None),
    "Montreal Steak Seasoning":    ("Blends", "", "Ground", None),
    # Seeds
    "Sesame Seeds, Black":         ("Seeds", "Pedaliaceae", "Whole", None),
    "Fennel Seed, Whole":          ("Seeds", "Apiaceae", "Whole", "Fennel Seed"),
}


def extract_spices(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for old_canonical, (sub_tier, family, prep, new_canonical) in SPICE_REMAP.items():
        # Find by canonical name (handles both "Spices" and "Spice" old primary)
        qs = Product.objects.filter(
            category="Drystock", canonical_name=old_canonical,
            primary_descriptor__in=("Spices", "Spice"),
        )
        # Move to Spices category, set new tier + family + prep_state
        for p in qs:
            p.category = "Spices"
            p.primary_descriptor = sub_tier
            p.secondary_descriptor = family
            p.prep_state = prep
            if new_canonical and not Product.objects.filter(canonical_name=new_canonical).exists():
                p.canonical_name = new_canonical
            p.save()


def merge_spices_back_to_drystock(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(category="Spices").update(
        category="Drystock", primary_descriptor="Spices",
        secondary_descriptor="", prep_state="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0052_unify_condiments_into_drystock"),
    ]

    operations = [
        migrations.RunPython(extract_spices, merge_spices_back_to_drystock),
    ]
