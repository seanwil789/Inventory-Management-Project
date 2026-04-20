"""
Auto-tag Recipe.conflicts based on RecipeIngredient name_raw keyword matches.

Conservative keyword rules. Sean reviews / overrides the output via the recipe
edit form. Designed as a "get 80% of the way there" pass.

Run:
    python manage.py auto_tag_conflicts                # dry-run, shows diff
    python manage.py auto_tag_conflicts --apply        # writes conflicts
    python manage.py auto_tag_conflicts --apply --overwrite  # also clears existing
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models.functions import Lower

from myapp.models import Recipe, RecipeIngredient


# Keyword → conflict-key rules. Order doesn't matter; all rules are unioned.
# Matching: substring-in-name_raw, case-insensitive.
KEYWORD_RULES = [
    # Gluten
    (['flour', 'wheat', 'bread', 'breadcrumb', 'pasta', 'noodle', 'ziti',
      'spaghetti', 'tortilla', 'cracker', 'pancake', 'waffle', 'biscuit',
      'bun', 'roll', 'dough', 'crust', 'pita', 'lasagna'],
     ['gluten']),
    # Dairy
    (['milk', 'butter', 'cream', 'cheese', 'yogurt', 'parmesan', 'mozzarella',
      'ricotta', 'feta', 'cheddar', 'buttermilk', 'sour cream', 'heavy cream',
      'half and half', 'condensed milk', 'evaporated milk', 'whey'],
     ['dairy', 'animal_products']),
    # Egg
    (['egg', 'mayo', 'mayonnaise', 'aioli', 'meringue'],
     ['egg', 'animal_products']),
    # Peanut
    (['peanut', 'peanut butter'],
     ['peanut']),
    # Tree nuts
    (['almond', 'walnut', 'pecan', 'cashew', 'pistachio', 'hazelnut',
      'brazil nut', 'macadamia', 'pine nut'],
     ['tree_nut']),
    # Fish
    (['salmon', 'tuna', 'tilapia', 'cod', 'haddock', 'halibut', 'trout',
      'mahi', 'flounder', 'anchovy', 'sardine', 'bass', 'fish sauce'],
     ['fish', 'animal_products']),
    # Shellfish
    (['shrimp', 'crab', 'lobster', 'clam', 'mussel', 'oyster', 'scallop',
      'calamari', 'squid', 'crawfish', 'crayfish'],
     ['shellfish', 'animal_products']),
    # Soy
    (['soy', 'tofu', 'tempeh', 'edamame', 'miso', 'tamari'],
     ['soy']),
    # Sesame
    (['sesame', 'tahini'],
     ['sesame']),
    # Meat / animal products
    (['beef', 'pork', 'chicken', 'turkey', 'lamb', 'bacon', 'sausage',
      'ham', 'prosciutto', 'pepperoni', 'salami', 'chorizo', 'ground beef',
      'ground pork', 'ground turkey', 'ribs', 'brisket', 'steak', 'duck',
      'veal', 'meatball', 'pastrami'],
     ['meat', 'animal_products']),
    # Honey / animal products only
    (['honey', 'gelatin', 'lard', 'tallow'],
     ['animal_products']),
    # Pork/meat crossover — explicit not-kosher/halal signals from pork
    (['bacon', 'pork', 'ham', 'prosciutto', 'pepperoni', 'chorizo', 'lard'],
     ['not_kosher', 'not_halal']),
    # Mixing meat + dairy → not kosher
    # (handled post-pass once per-recipe sets are computed)
]


class Command(BaseCommand):
    help = 'Auto-tag Recipe.conflicts from ingredient keyword matches.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write conflicts to DB (default is dry-run)')
        parser.add_argument('--overwrite', action='store_true',
                            help='Clear existing conflicts before applying. '
                                 'Without this flag, existing conflicts are preserved '
                                 'and auto-detected ones are unioned in.')

    def handle(self, *args, **opts):
        apply = opts['apply']
        overwrite = opts['overwrite']

        changes: list[tuple[Recipe, set[str], set[str]]] = []  # (recipe, old, new)
        for recipe in Recipe.objects.prefetch_related('ingredients'):
            detected: set[str] = set()
            # Scan all ingredient names
            for ing in recipe.ingredients.all():
                name_lc = (ing.name_raw or '').lower()
                if not name_lc:
                    continue
                for keywords, conflict_keys in KEYWORD_RULES:
                    if any(kw in name_lc for kw in keywords):
                        detected.update(conflict_keys)

            # Post-pass: mixing meat + dairy → not kosher
            if 'meat' in detected and 'dairy' in detected:
                detected.add('not_kosher')

            old = set(recipe.conflicts or [])
            new = detected if overwrite else (old | detected)

            if new != old:
                changes.append((recipe, old, new))

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Auto-tag review ({len(changes)} recipes will change) ==='))

        for recipe, old, new in changes[:50]:
            added = sorted(new - old)
            removed = sorted(old - new) if overwrite else []
            parts = []
            if added:
                parts.append(f'+{",".join(added)}')
            if removed:
                parts.append(f'-{",".join(removed)}')
            self.stdout.write(f'  {recipe.name:40s}  {" ".join(parts)}')
        if len(changes) > 50:
            self.stdout.write(f'  ... and {len(changes) - 50} more')

        self.stdout.write(self.style.SUCCESS(
            f'\nSummary: {len(changes)} recipes with proposed conflict changes.'))

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no DB writes. Re-run with --apply to save.'))
            return

        updated = 0
        for recipe, _old, new in changes:
            recipe.conflicts = sorted(new)
            recipe.save(update_fields=['conflicts'])
            updated += 1
        self.stdout.write(self.style.SUCCESS(f'\n✔ Updated {updated} recipes.'))
