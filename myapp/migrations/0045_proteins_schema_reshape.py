"""Reshape Proteins schema to animal/primal/prep_state.

OLD: primary_descriptor = primal cut (Chuck, Shoulder, Breast, etc.)
     secondary_descriptor = Fresh / Cured / Processed
     animal = implied by canonical name
NEW: primary_descriptor = animal (Beef, Pork, Poultry, Seafood, Game)
     secondary_descriptor = primal cut OR species (for fish/shellfish) OR Plant
     prep_state = Fresh / Cured / Processed / Base

Custom secondary order (food-safety walk pattern, Sean 2026-04-30):
Beef → Pork → Poultry → Seafood → Game (with Plant under Game)

Per-product inference:
- Token-based animal detection: explicit animal token wins
- Disambiguation: Sausage default = Pork (Sean confirmed); Sausage, Turkey = Poultry
- Hot Dogs → Beef (Sean: "hot dogs are beef")
- Beef Frank → Beef
- Bases (Beef/Chicken/Crab Base) → handled in 0047 (Bone/Shell secondary)
- Plant proteins → Game/Plant (handled in 0048)
- Eggs → Poultry/Egg (handled in 0049)
- Whole bird → Poultry/Whole
- Fish/Shellfish secondary = species (Salmon, Shrimp, etc.)
"""
from django.db import migrations


# Animal inference: token in canonical → animal
ANIMAL_RULES = [
    # Explicit animal tokens (high confidence) — most specific first
    ("turkey",     "Poultry"),
    ("chicken",    "Poultry"),
    ("egg",        "Poultry"),    # eggs → Poultry
    ("eggs",       "Poultry"),
    ("duck",       "Game"),
    ("lamb",       "Game"),
    ("venison",    "Game"),
    ("rabbit",     "Game"),
    ("goat",       "Game"),
    ("salmon",     "Seafood"),
    ("tilapia",    "Seafood"),
    ("tuna",       "Seafood"),
    ("cod",        "Seafood"),
    ("shrimp",     "Seafood"),
    ("clam",       "Seafood"),
    ("clams",      "Seafood"),
    ("mussel",     "Seafood"),
    ("mussels",    "Seafood"),
    ("scallop",    "Seafood"),
    ("crab",       "Seafood"),
    ("lobster",    "Seafood"),
    ("fish",       "Seafood"),
    # Pork-specific tokens
    ("bacon",      "Pork"),
    ("ham",        "Pork"),
    ("prosciutto", "Pork"),
    ("salami",     "Pork"),
    ("pepperoni",  "Pork"),
    ("kielbasa",   "Pork"),
    ("sausage",    "Pork"),       # default; Sausage Turkey caught earlier
    # Beef-specific tokens
    ("brisket",    "Beef"),
    ("burger",     "Beef"),
    ("burgers",    "Beef"),
    ("hot dog",    "Beef"),       # Sean: hot dogs are beef
    ("hot dogs",   "Beef"),
    ("frank",      "Beef"),       # Beef Frank → Beef
    ("franks",     "Beef"),
    ("ribeye",     "Beef"),
    ("striploin",  "Beef"),
    ("strip loin", "Beef"),
    ("roast beef", "Beef"),
    ("ground beef","Beef"),
    ("beef",       "Beef"),
    ("pork",       "Pork"),
    # Plant tokens (will be re-routed to Game/Plant in 0048)
    ("veggie",     "Game"),
    ("plant",      "Game"),
    ("black bean", "Game"),
    ("tofu",       "Game"),
    ("seitan",     "Game"),
]

# Primal cut hint when no explicit animal token (medium confidence)
# Old primary_descriptor → likely animal
PRIMAL_HINT = {
    # Beef-leaning primals
    "Brisket":   "Beef",
    "Plate":     "Beef",
    "Round":     "Beef",
    "Rib":       "Beef",
    "Sirloin":   "Beef",
    "Chuck":     "Beef",     # default — pork shoulder usually said as "shoulder"
    # Pork-leaning primals
    "Belly":     "Pork",
    "Leg":       "Pork",
    # Ambiguous (Shoulder is both) — handled by canonical token check
    "Shoulder":  "Pork",     # default; explicit beef tokens caught earlier
    "Loin":      "Pork",     # ambiguous; default pork
    # Poultry-specific
    "Breast":    "Poultry",
    "Thigh":     "Poultry",
    "Wing":      "Poultry",
    "Whole":     "Poultry",  # whole birds (chicken/turkey) — only one in DB is Chicken Whole Fryer
    "Egg":       "Poultry",
    # Seafood
    "Fish":      "Seafood",
    "Shellfish": "Seafood",
    # Plant (will reroute to Game in 0048)
    "Plant":     "Game",
    # Bases handled separately in 0047
    "Base":      None,
}


def infer_animal(canonical_name, current_primary):
    """Return the new primary_descriptor (animal) for a protein product.
    Prefers explicit token in canonical name; falls back to primal hint."""
    name_lower = canonical_name.lower()
    for token, animal in ANIMAL_RULES:
        if token in name_lower:
            return animal
    # Fall back to primal hint
    return PRIMAL_HINT.get(current_primary)


def reshape_proteins_schema(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    for p in Product.objects.filter(category="Proteins"):
        old_primary = p.primary_descriptor    # primal cut OR Base/Plant
        old_secondary = p.secondary_descriptor  # Fresh/Cured/Processed

        # Skip Bases — handled in 0047
        if old_primary == "Base":
            continue

        new_animal = infer_animal(p.canonical_name, old_primary)
        if not new_animal:
            continue  # Unmappable — leave for manual review

        # New schema
        new_primal = old_primary if old_primary not in ("Plant", "Base", "Egg",
                                                        "Whole", "Fish", "Shellfish") else None
        new_prep_state = old_secondary if old_secondary in ("Fresh", "Cured", "Processed") else "Fresh"

        # Special handling per primal type
        if old_primary == "Plant":
            # Plant proteins handled in 0048
            continue
        elif old_primary == "Egg":
            new_primal = "Egg"
        elif old_primary == "Whole":
            new_primal = "Whole"
        elif old_primary == "Fish":
            # Use canonical name as species secondary
            new_primal = p.canonical_name  # Salmon, Tilapia, Tuna
        elif old_primary == "Shellfish":
            # Use canonical name first word as species
            first = p.canonical_name.split(",")[0].strip()
            # Special cases
            species_map = {"Clams": "Clam", "Mussels": "Mussel",
                           "Crab Meat": "Crab", "Shrimp": "Shrimp",
                           "Shrimp 21/25": "Shrimp"}
            new_primal = species_map.get(first, first)

        p.primary_descriptor = new_animal
        p.secondary_descriptor = new_primal or ""
        p.prep_state = new_prep_state
        p.save(update_fields=["primary_descriptor", "secondary_descriptor", "prep_state"])


def reverse_proteins_schema(apps, schema_editor):
    """No-op reverse — the old schema can't be reliably reconstructed
    once primal cut and prep state have been split apart."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0043_merge_red_bliss_potato"),
    ]

    operations = [
        migrations.RunPython(reshape_proteins_schema, reverse_proteins_schema),
    ]
