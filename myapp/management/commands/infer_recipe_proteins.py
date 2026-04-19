"""Fill Recipe.protein for recipes Menu Guide didn't tag.

Uses two signals (priority order):
  1. source_doc path — 'Recipe Book/Proteins/Beef/...' → beef
  2. name keywords — 'Chicken Parm' → chicken, 'Shrimp Pesto Pasta' → seafood, etc.

Only fills where Recipe.protein is currently blank — never overrides Menu Guide / manual assignments.
"""
import re
from django.core.management.base import BaseCommand
from myapp.models import Recipe


# Path-based inference — first segment under "Recipe Book/" wins when it matches
PATH_PROTEIN = {
    'proteins/beef':    'beef',
    'proteins/chicken': 'chicken',
    'proteins/pork':    'pork',
    'proteins/turkey':  'turkey',
    'proteins/seafood': 'seafood',
    'baking':           'eggs',    # baked goods are eggs/breakfast-ish
    'breakfast':        'eggs',
}

# Name keyword rules — priority order
KEYWORD_RULES = [
    ('seafood',  ['shrimp', 'salmon', 'tilapia', 'crab', 'tuna', 'fish']),
    ('pork',     ['pork', 'bacon', 'sausage', 'ham', 'cheesesteak', 'carbonara', 'pulled']),
    ('beef',     ['beef', 'steak', 'burger', 'bolognaise', 'bolognese', 'beefaroni',
                  'meatball', 'gyro', 'short rib', 'brisket', 'cornbeef', 'taco meat',
                  'ground beef']),
    ('chicken',  ['chicken', 'buffalo', 'wings']),
    ('turkey',   ['turkey']),
    ('veg',      ['falafel', 'black bean', 'mushroom', 'tofu', 'seitan', 'tempeh',
                  'veggie', 'lentil', 'chickpea', 'hummus', 'vegan', 'marinara',
                  'queso', 'rice', 'grits', 'tomato soup', 'potato', 'spanish rice',
                  'pasta']),
    ('eggs',     ['waffle', 'pancake', 'biscuit', 'bread', 'muffin', 'cookie',
                  'brownie', 'crisp', 'frosting', 'cake', 'frittata', 'quiche',
                  'egg', 'stuffed bun', 'batter']),
]


def infer_from_path(source_doc: str) -> str:
    s = (source_doc or '').lower().replace('\\', '/')
    for path_key, protein in PATH_PROTEIN.items():
        if f"/{path_key}/" in s or s.startswith(path_key + '/'):
            return protein
    return ''


def infer_from_name(name: str) -> str:
    s = (name or '').lower()
    for protein, keys in KEYWORD_RULES:
        if any(k in s for k in keys):
            return protein
    return ''


class Command(BaseCommand):
    help = "Fill Recipe.protein for recipes Menu Guide didn't tag — uses source_doc path + name keywords."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        untagged = Recipe.objects.filter(protein='')
        total = untagged.count()
        self.stdout.write(f"Recipes without protein: {total}")

        updates: list[tuple[Recipe, str, str]] = []  # (recipe, protein, source_of_inference)
        for r in untagged:
            protein = infer_from_path(r.source_doc)
            src = 'path'
            if not protein:
                protein = infer_from_name(r.name)
                src = 'name'
            if protein:
                updates.append((r, protein, src))

        self.stdout.write(f"Will tag {len(updates)} recipes "
                          f"({total - len(updates)} remaining untagged).")

        if opts['dry_run']:
            from collections import Counter
            by_protein = Counter(p for _, p, _ in updates)
            self.stdout.write(f"\nDistribution: {dict(by_protein)}\n")
            for r, p, src in updates[:30]:
                self.stdout.write(f"  {r.name:<40} → {p:8s} (via {src})")
            return

        for r, p, _ in updates:
            r.protein = p
            r.save(update_fields=['protein'])
        self.stdout.write(self.style.SUCCESS(f"Tagged {len(updates)} recipes."))
