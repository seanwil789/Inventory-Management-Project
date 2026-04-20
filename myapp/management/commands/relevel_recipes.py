"""
Re-level recipes from the conservative default set by migration 0016.

After the initial backfill, most recipes sit at level='recipe' (component)
even when they are actually menu-level meals. This command promotes them
using folder + ingredient signals.

Rules (first match wins):
  1. source_doc under Prep Components/ or matches "Sauce/Stock/Dressing/
     Seasoning/Frosting/Marinade/Batter/Breading/Base/Salsa" → keep 'recipe'
  2. source_doc under Composed Meals/ OR has any sub_recipe ingredient
     → 'composed_dish'
  3. source_doc under Proteins/, Breakfast/, Events/, Side Dishes/
     → 'meal'
  4. Recipe has a non-empty Recipe.protein AND 5+ real (non-sub) ingredients
     → 'meal'
  5. Baking/ folder: left as 'recipe' (components like Frosting) unless a
     name in MEAL_BAKING_NAMES (standalone breakfast items) → 'meal'
  6. Otherwise: unchanged

Run:
    python manage.py relevel_recipes
    python manage.py relevel_recipes --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from myapp.models import Recipe


COMPONENT_NAME_SUFFIXES = (
    'sauce', 'stock', 'dressing', 'seasoning', 'frosting', 'marinade',
    'batter', 'breading', 'base', 'salsa', 'stuffing', 'glaze', 'syrup',
    'gravy',  # caveat: "Sausage Gravy" is debatable; let Sean override
)

COMPONENT_FOLDERS = ('Prep Components/',)

COMPOSED_FOLDERS = ('Composed Meals/',)

MEAL_FOLDERS = ('Proteins/', 'Breakfast/', 'Events/', 'Side Dishes/')

# Baking items that are standalone breakfast/meal components, not sauces/frostings
MEAL_BAKING_NAMES = {
    'biscuits', 'banana bread', 'blueberry crisp', 'apple crisp',
    'pancakes', 'traditional waffles', 'corn meal waffles', 'lemon ricotta pancakes',
    'lemon blueberry muffins', 'coffee cake', 'steamed buns',
    'brownies', 'chocolate cake', 'chocolate chip cookies',
    'double chocolate chip cookies', 'sugar cookies',  # desserts, technically meals
}


def _suggest_level(recipe: Recipe) -> str | None:
    """Return suggested level key, or None if the current level looks right."""
    src = (recipe.source_doc or '').replace('\\', '/').lower()
    name_lc = recipe.name.lower()

    # Rule 0: has sub_recipe ingredients → composed_dish (strongest signal).
    # Overrides folder/name — a sauce that references other recipes is really a
    # composed dish (e.g., Beef Bolognese references Tomato Sauce).
    has_sub = recipe.ingredients.filter(sub_recipe__isnull=False).exists()
    if has_sub:
        return 'composed_dish'

    # Rule 1: name-suffix indicates component
    if any(name_lc.endswith(suf) for suf in COMPONENT_NAME_SUFFIXES):
        # Even if folder says 'Proteins/' — name wins. E.g., 'Chicken Stock'.
        return 'recipe'

    # Rule 1b: folder under Prep Components/
    if any(f.lower() in src for f in COMPONENT_FOLDERS):
        return 'recipe'

    # Rule 2: Composed Meals folder
    if any(f.lower() in src for f in COMPOSED_FOLDERS):
        return 'composed_dish'

    # Rule 5: Baking/ folder with whitelisted name
    if 'baking/' in src:
        if name_lc in MEAL_BAKING_NAMES:
            return 'meal'
        return 'recipe'  # most baking/ items are components (Buttercream Frosting, etc.)

    # Rule 3: meal folders
    if any(f.lower() in src for f in MEAL_FOLDERS):
        return 'meal'

    # Rule 4: has protein + 5+ real ingredients → meal
    real_ing_count = recipe.ingredients.filter(sub_recipe__isnull=True).count()
    if recipe.protein and real_ing_count >= 5:
        return 'meal'

    # Rule 6: no change suggested
    return None


class Command(BaseCommand):
    help = 'Re-level recipes based on folder + protein + name heuristics.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write level changes to DB (default is dry-run)')

    def handle(self, *args, **opts):
        apply = opts['apply']

        changes = []
        for recipe in Recipe.objects.prefetch_related('ingredients'):
            suggested = _suggest_level(recipe)
            if suggested is None:
                continue
            if recipe.level == suggested:
                continue
            changes.append((recipe, recipe.level, suggested))

        # Group by (from → to) for the summary
        from collections import Counter
        transitions = Counter((old, new) for _, old, new in changes)

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Re-level heuristic ({len(changes)} recipes will change) ==='))

        self.stdout.write('Transitions:')
        for (old, new), n in sorted(transitions.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {old:14s} → {new:14s}  ({n} recipes)')

        self.stdout.write('\nPer-recipe detail:')
        for recipe, old, new in changes[:80]:
            src_short = (recipe.source_doc or '').split('/')[-2] if recipe.source_doc else ''
            self.stdout.write(f'  {recipe.name:42s}  {old:14s} → {new:14s}  [{src_short}]')
        if len(changes) > 80:
            self.stdout.write(f'  ... and {len(changes) - 80} more')

        self.stdout.write(self.style.SUCCESS(
            f'\nSummary: {len(changes)} recipes with proposed level changes.'))

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no DB writes. Re-run with --apply to save.'))
            return

        updated = 0
        for recipe, _old, new in changes:
            recipe.level = new
            recipe.save(update_fields=['level'])
            updated += 1
        self.stdout.write(self.style.SUCCESS(f'\n✔ Updated {updated} recipes.'))
