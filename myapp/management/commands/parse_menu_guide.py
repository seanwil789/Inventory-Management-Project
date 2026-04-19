"""Parse Menu Guide.docx and enrich matching Recipe rows with protein / fat_health / popularity.

Menu Guide structure (hierarchical; no tables):
  Breakfast              <- meal_slot header
    Cold Breakfast:      <- sub-slot (ends with ':')
      Parfait            <- dish
      Baking             <- nested protein-like category
        Banana bread
    Hot Breakfast:
      Proteins
        Beef             <- protein
          Braised Short Ribs (F)
  Lunch / Dinner / Sides / Sauces / ...

Heuristics:
  - Known protein category words flip the current protein context.
  - Dishes matched to Recipe rows by case-insensitive exact name (after stripping (F)/(H)).
  - (F) / (H) suffix → fat_health.
  - Highlight color on the run(s) → popularity.
"""
import re
from pathlib import Path

import docx
from django.core.management.base import BaseCommand, CommandError

from myapp.models import Recipe


PROTEIN_HEADERS = {
    'beef':    'beef',
    'chicken': 'chicken',
    'poultry': 'chicken',
    'pork':    'pork',
    'turkey':  'turkey',
    'seafood': 'seafood',
    'shrimp':  'seafood',
    'salmon':  'seafood',
    'veg':     'veg',
    'vegetarian': 'veg',
    'veggie':  'veg',
    'eggs':    'eggs',
    'egg dishes': 'eggs',
}

MEAL_SLOT_HEADERS = {'breakfast', 'lunch', 'dinner', 'sides', 'sauces', 'dressings', 'condiments'}

FH_RE = re.compile(r'\s*\(([FHfh])\)\s*$')


def _strip_fh(name: str) -> tuple[str, str]:
    """'Chicken And Waffles (F)' → ('Chicken And Waffles', 'F')."""
    m = FH_RE.search(name)
    if m:
        return name[:m.start()].strip(), m.group(1).upper()
    return name.strip(), ''


def _popularity_from_runs(paragraph) -> str:
    """Scan runs for a highlight color. Majority wins."""
    from collections import Counter
    c = Counter()
    for run in paragraph.runs:
        hl = run.font.highlight_color
        if hl is None:
            continue
        s = str(hl)
        if 'GREEN' in s:   c['high']   += len(run.text)
        elif 'YELLOW' in s: c['medium'] += len(run.text)
        elif 'RED' in s:    c['low']    += len(run.text)
    if not c:
        return ''
    return c.most_common(1)[0][0]


def _section_key(text: str) -> str:
    """Normalize section labels: strip trailing ':' and lowercase."""
    return text.rstrip(':').strip().lower()


class Command(BaseCommand):
    help = "Parse Menu Guide.docx and update Recipes with protein/fat_health/popularity."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str,
                            default=".kitchen_ops/Kitchen Operations/Kitchen Coordination/Menu Guide.docx",
                            nargs='?')
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        path = Path(opts['path'])
        if not path.exists():
            raise CommandError(f"Not found: {path}")

        doc = docx.Document(str(path))
        current_protein = ''
        current_meal_slot = ''

        # Candidate (name, protein, fh, popularity) triples per paragraph
        candidates: list[tuple[str, str, str, str]] = []

        for p in doc.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            key = _section_key(t)

            # Section header detection
            if key in MEAL_SLOT_HEADERS:
                current_meal_slot = key
                # reset protein context when changing meal slot
                current_protein = ''
                continue
            if key in PROTEIN_HEADERS:
                current_protein = PROTEIN_HEADERS[key]
                continue
            # "Cold Breakfast:" / "Hot Breakfast:" — sub-slot, ignored for protein but keep
            if key.endswith(' breakfast') or key in ('proteins', 'bread options', 'breakfast sides',
                                                     'meat sides', 'other', 'sandwiches', 'soups',
                                                     'starch', 'cold', 'all purpose', 'beans', 'potatoes',
                                                     'pasta', 'rice', 'veg', 'baking'):
                # Some of these signal protein context (veg, beans, starch). Set explicitly below:
                if key in ('veg', 'beans'):
                    current_protein = 'veg'
                elif key in ('meat sides', 'bacon', 'sausage'):
                    current_protein = 'pork'
                continue

            # Treat as a dish line
            name, fh = _strip_fh(t)
            if not name:
                continue
            popularity = _popularity_from_runs(p)
            candidates.append((name, current_protein, fh, popularity))

        self.stdout.write(f"Parsed {len(candidates)} dish candidates from Menu Guide.")

        # Match to Recipes by exact name (case-insensitive)
        recipes_by_norm = {r.name.lower(): r for r in Recipe.objects.all()}
        updates = []
        unmatched_count = 0

        for name, protein, fh, popularity in candidates:
            r = recipes_by_norm.get(name.lower())
            if not r:
                unmatched_count += 1
                continue
            changes = {}
            if protein and not r.protein:
                changes['protein'] = protein
            if fh and not r.fat_health:
                changes['fat_health'] = fh
            if popularity and not r.popularity:
                changes['popularity'] = popularity
            if changes:
                updates.append((r, changes))

        self.stdout.write(
            f"Match results: {len(updates)} recipes will get updates, "
            f"{len(candidates) - unmatched_count - len(updates)} matched but already tagged, "
            f"{unmatched_count} Menu Guide dishes have no Recipe yet."
        )

        if opts['dry_run']:
            for r, ch in updates[:20]:
                self.stdout.write(f"  {r.name}: {ch}")
            return

        updated = 0
        for r, ch in updates:
            for field, value in ch.items():
                setattr(r, field, value)
            r.save(update_fields=list(ch.keys()))
            updated += 1
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} recipes."))
