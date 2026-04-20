"""Auto-populate Recipe.valid_slots from name + source_doc heuristics.

For each recipe with empty valid_slots, pick a best-guess set of slots
based on signal words. Human review afterwards is expected — these are
seeds, not ground truth.

Heuristics (first match wins):
  - source_doc contains "Breakfast"  → cold_breakfast + hot_breakfast
  - name matches breakfast keywords → cold_breakfast + hot_breakfast
  - name matches dessert/sweet keywords → dinner (served as side/ending)
  - name matches soup/salad/sandwich/wrap → lunch + dinner
  - source_doc contains "Entree"/"Dinner"/"Lunch" folder → lunch + dinner
  - Fallback → lunch + dinner

Usage:
  python manage.py tag_meal_slots                  # dry-run preview
  python manage.py tag_meal_slots --apply          # write to DB
  python manage.py tag_meal_slots --apply --retag  # overwrite non-empty tags too
"""
from __future__ import annotations

import re
from django.core.management.base import BaseCommand
from myapp.models import Recipe

BREAKFAST_KEYWORDS = re.compile(
    r'\b(pancake|waffle|oatmeal|oats|granola|french toast|frittata|omelet|'
    r'scramble|bacon|sausage|breakfast|egg|crepe|muffin|danish|cereal|'
    r'biscuit|hash|sticky bun|roll|cinnamon roll|toast)\b',
    re.IGNORECASE,
)

LUNCH_DINNER_KEYWORDS = re.compile(
    r'\b(soup|salad|sandwich|wrap|burger|taco|burrito|pasta|pizza|'
    r'chili|stew|curry|chowder|pot pie|casserole|lasagna|entree|'
    r'stir.?fry|kebab|bbq|barbecue)\b',
    re.IGNORECASE,
)

# Items that show up in dinner contexts mainly
DINNER_LEAN = re.compile(
    r'\b(roast|fillet|filet|loin|chops?|steak|brisket|rack|tenderloin|'
    r'whole chicken|whole fryer|baked|grilled|braised|smoked)\b',
    re.IGNORECASE,
)


def _suggest_slots(recipe: Recipe) -> list[str]:
    src = (recipe.source_doc or '').lower()
    name = recipe.name or ''

    # Folder signal is strongest
    if 'breakfast' in src:
        return ['cold_breakfast', 'hot_breakfast']

    # Name-based breakfast detection
    if BREAKFAST_KEYWORDS.search(name):
        return ['cold_breakfast', 'hot_breakfast']

    # Lunch+dinner keywords
    if LUNCH_DINNER_KEYWORDS.search(name):
        return ['lunch', 'dinner']

    # Dinner-lean entree keywords
    if DINNER_LEAN.search(name):
        return ['lunch', 'dinner']

    # Folder fallback
    if any(x in src for x in ('entree', 'dinner', 'lunch', 'composed')):
        return ['lunch', 'dinner']

    # Default — permissive lunch+dinner
    return ['lunch', 'dinner']


class Command(BaseCommand):
    help = 'Seed Recipe.valid_slots from heuristics over name + source_doc.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB (default is dry-run)')
        parser.add_argument('--retag', action='store_true',
                            help='Overwrite existing valid_slots values too')

    def handle(self, *args, **opts):
        qs = Recipe.objects.all().order_by('level', 'name')
        if not opts['retag']:
            # Only recipes with no slots yet
            from django.db.models import Q
            qs = qs.filter(Q(valid_slots=[]) | Q(valid_slots__isnull=True))

        self.stdout.write(f'Candidate recipes: {qs.count()}')

        changes = []
        for r in qs:
            proposed = _suggest_slots(r)
            current = r.valid_slots or []
            if sorted(proposed) != sorted(current):
                changes.append((r, current, proposed))

        # Summary by proposed set
        from collections import Counter
        by_tag = Counter()
        for _, _, proposed in changes:
            by_tag[tuple(sorted(proposed))] += 1
        self.stdout.write('\nProposed tag sets:')
        for tags, n in by_tag.most_common():
            self.stdout.write(f'  {list(tags)}: {n} recipes')

        # Detail — recipes that differ from current
        self.stdout.write('\nSample (first 30):')
        for r, current, proposed in changes[:30]:
            self.stdout.write(
                f'  {r.name[:40]:<40}  {r.level:<14}  '
                f'[{", ".join(current) or "(empty)"}] → [{", ".join(proposed)}]'
            )

        if opts['apply']:
            for r, _, proposed in changes:
                r.valid_slots = proposed
                r.save(update_fields=['valid_slots'])
            self.stdout.write(self.style.SUCCESS(
                f'\nApplied to {len(changes)} recipes.'))
        else:
            self.stdout.write(self.style.WARNING(
                f'\n(dry run — {len(changes)} recipes would change. '
                'Re-run with --apply to write.)'))
