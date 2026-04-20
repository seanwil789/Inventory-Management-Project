"""
Auto-fill Recipe.protein from RecipeIngredient.name_raw keyword matches.

Conservative: only fills recipes with blank protein. Use --overwrite to
replace existing values. Sean reviews via the recipe edit form.

Run:
    python manage.py auto_tag_protein
    python manage.py auto_tag_protein --apply
    python manage.py auto_tag_protein --apply --overwrite
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from myapp.models import Recipe, PROTEIN_CHOICES


# Keyword → protein-key. Multi-word first so "ground turkey" doesn't collide
# with "turkey bacon" or "ground beef".
PROTEIN_KEYWORDS = [
    # Seafood — most specific first
    (['salmon', 'tuna', 'tilapia', 'cod', 'halibut', 'flounder', 'trout',
      'mahi', 'haddock', 'snapper', 'bass', 'anchovy', 'sardine'],          'seafood'),
    (['shrimp', 'crab', 'lobster', 'scallop', 'clam', 'mussel', 'oyster',
      'calamari', 'squid', 'crayfish'],                                     'seafood'),
    (['fish sauce', 'fish stock'],                                          None),   # condiments, not protein
    # Beef
    (['ground beef', 'beef', 'brisket', 'ribeye', 'sirloin', 'tenderloin',
      'chuck roast', 'flank', 'pastrami'],                                  'beef'),
    # Pork
    (['ground pork', 'pork', 'bacon', 'sausage', 'ham', 'prosciutto',
      'pepperoni', 'chorizo'],                                              'pork'),
    # Chicken
    (['chicken', 'poultry', 'drumstick', 'wing'],                           'chicken'),
    # Turkey (after chicken since overlap is low but turkey is less common)
    (['ground turkey', 'turkey'],                                           'turkey'),
    # Eggs — as primary protein when no meat/fish present
    (['egg', 'eggs'],                                                       'eggs'),
    # Vegetarian protein cues (for explicitly-veg dishes)
    (['tofu', 'tempeh', 'seitan', 'lentil', 'chickpea', 'edamame',
      'black bean', 'kidney bean', 'pinto bean', 'falafel'],                'veg'),
]

# Priority order when multiple match — meat > seafood > poultry > eggs > veg.
# "Chicken breast salad with bacon bits" should flag as chicken (main) not bacon.
# We use the one with highest ingredient-count match, with this as tiebreaker.
PRIORITY = ['beef', 'pork', 'chicken', 'turkey', 'seafood', 'eggs', 'veg']


# Recipe-name suffixes that indicate a component, not a protein-bearing dish.
# Sauces, stocks, dressings, etc. shouldn't get a protein label even if they
# use animal-derived ingredients (e.g., Apple Onion Mustard Sauce uses pork
# stock but isn't a pork dish).
COMPONENT_NAME_SUFFIXES = (
    'sauce', 'suace',  # 'suace' is a typo variant in the existing recipe set
    'stock', 'dressing', 'seasoning', 'frosting', 'marinade',
    'batter', 'breading', 'base', 'salsa', 'stuffing', 'glaze', 'syrup',
    'gravy',
)


def _is_component_recipe(recipe) -> bool:
    """Component-type recipes shouldn't be auto-protein-tagged."""
    name_lc = recipe.name.lower()
    return any(name_lc.endswith(suf) for suf in COMPONENT_NAME_SUFFIXES)


def _detect_protein_for_recipe(recipe: Recipe) -> tuple[str, dict]:
    """Return (protein_key_or_empty, counts_by_protein) from ingredient scan."""
    counts: Counter = Counter()
    for ing in recipe.ingredients.all():
        name_lc = (ing.name_raw or '').lower()
        if not name_lc:
            continue
        for keywords, protein_key in PROTEIN_KEYWORDS:
            if protein_key is None:
                continue  # skip condiments
            for kw in keywords:
                if kw in name_lc:
                    counts[protein_key] += 1
                    break  # count each ingredient once per protein group

    if not counts:
        # No protein keywords → check if the recipe has any ingredients at all.
        # If it has ingredients but none are proteins, infer 'veg'.
        if recipe.ingredients.exclude(name_raw='').exists():
            return 'veg', dict(counts)
        return '', dict(counts)

    # Pick the highest-count protein; tiebreak by PRIORITY list
    max_count = max(counts.values())
    top = [p for p, c in counts.items() if c == max_count]
    if len(top) == 1:
        return top[0], dict(counts)
    # Tiebreak
    for p in PRIORITY:
        if p in top:
            return p, dict(counts)
    return top[0], dict(counts)


class Command(BaseCommand):
    help = 'Auto-fill Recipe.protein from ingredient keywords.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write protein values to DB (default is dry-run)')
        parser.add_argument('--overwrite', action='store_true',
                            help='Replace existing Recipe.protein values. '
                                 'Without this flag, only blank proteins are filled.')

    def handle(self, *args, **opts):
        apply = opts['apply']
        overwrite = opts['overwrite']

        changes = []
        skipped_components = 0
        for recipe in Recipe.objects.prefetch_related('ingredients'):
            if _is_component_recipe(recipe):
                skipped_components += 1
                continue
            detected, counts = _detect_protein_for_recipe(recipe)
            old = recipe.protein
            if not detected:
                continue
            if old and not overwrite:
                continue
            if old == detected:
                continue
            changes.append((recipe, old, detected, counts))

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Auto-detect protein ({len(changes)} recipes will change) ==='))

        for recipe, old, new, counts in changes[:60]:
            old_disp = old or '(blank)'
            count_str = ' '.join(f'{p}={n}' for p, n in sorted(counts.items(), key=lambda x: -x[1])) or '-'
            self.stdout.write(f'  {recipe.name:42s}  {old_disp:10s} → {new:10s}  [{count_str}]')
        if len(changes) > 60:
            self.stdout.write(f'  ... and {len(changes) - 60} more')

        self.stdout.write(self.style.SUCCESS(
            f'\nSummary: {len(changes)} recipes with proposed protein changes. '
            f'{skipped_components} component recipes skipped (sauces, stocks, dressings, etc.).'))

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no DB writes. Re-run with --apply to save.'))
            return

        updated = 0
        for recipe, _old, new, _counts in changes:
            recipe.protein = new
            recipe.save(update_fields=['protein'])
            updated += 1
        self.stdout.write(self.style.SUCCESS(f'\n✔ Updated {updated} recipes.'))
