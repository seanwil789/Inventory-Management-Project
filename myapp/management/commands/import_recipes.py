import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import docx
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.models import Recipe, RecipeIngredient


# Files that aren't recipes
SKIP_NAMES = {'Recipe Card Formating.docx', 'To Do.docx'}

# Titles that signal a procedure-only doc (parent procedure, not a standalone recipe)
PROCEDURE_TITLES = {'additional ingredients', 'procedure', 'assembly'}

UNICODE_FRAC = {
    '½': Decimal('0.5'),  '⅓': Decimal('0.333'), '⅔': Decimal('0.667'),
    '¼': Decimal('0.25'), '¾': Decimal('0.75'),
    '⅛': Decimal('0.125'),'⅜': Decimal('0.375'), '⅝': Decimal('0.625'), '⅞': Decimal('0.875'),
}

KNOWN_UNITS = {
    'tsp', 'teaspoon', 'teaspoons',
    'tbsp', 'tablespoon', 'tablespoons', 'tbs',
    'cup', 'cups', 'c',
    'lb', 'lbs', 'pound', 'pounds',
    'oz', 'ounce', 'ounces',
    'g', 'gram', 'grams',
    'kg', 'kilogram', 'kilograms',
    'ml', 'milliliter', 'milliliters',
    'l', 'liter', 'liters',
    'qt', 'quart', 'quarts',
    'pt', 'pint', 'pints',
    'gal', 'gallon', 'gallons',
    'clove', 'cloves',
    'piece', 'pieces',
    'slice', 'slices',
    'can', 'cans',
    'pkg', 'package', 'packages',
    'bunch', 'bunches',
    'each',
}

PROCEDURE_MARKERS = re.compile(
    r'^\s*\*?\s*(procedure|preparation procedure|reheating procedure|preheat)\b',
    re.IGNORECASE,
)

# Version suffix patterns to strip from title:
#   "Sausage Gravy (V1 1/12/12/26)"
#   "Biscuts V2 4 13 2026"
#   "Pancakes V1 1 5 2026"
VERSION_SUFFIX_RE = re.compile(
    r'\s*\(?V\d+[\d \-./]*\)?\s*$',
    re.IGNORECASE,
)


def _sanitize(s: str) -> str:
    """Strip lone surrogates that come from mangled filesystem encodings."""
    return (s or '').encode('utf-8', 'replace').decode('utf-8')


def _clean_single(raw: str) -> str:
    title = _sanitize(raw).strip().split('\t', 1)[0].strip()
    title = VERSION_SUFFIX_RE.sub('', title).strip()
    # Collapse any run of dash-like garbage (U+FFFD, literal ?, en/em dashes, ASCII -) into a single " "
    title = re.sub(r'[\uFFFD?\u2013\u2014\-]+', ' ', title)
    # Collapse consecutive whitespace
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def clean_title(paragraph_raw: str, filename_stem: str) -> str:
    """Prefer the cleaned-title candidate with more info.

    Some docs (e.g. Sugar Cookies V1 1 6 2026.docx) have a truncated title in
    the first paragraph because formatting splits it across runs python-docx
    can't reassemble. Filename is often the authoritative source in those cases.
    """
    p_cleaned = _clean_single(paragraph_raw)
    f_cleaned = _clean_single(filename_stem)
    if not p_cleaned and not f_cleaned:
        return filename_stem or 'Untitled'
    if not p_cleaned:
        return f_cleaned
    if not f_cleaned:
        return p_cleaned
    # Longer wins; tiebreak to paragraph
    return f_cleaned if len(f_cleaned) > len(p_cleaned) else p_cleaned


def parse_quantity(text: str) -> tuple[Decimal | None, str]:
    """Parse '6 cups', '½ cups', '1 ½ cups', '3 Tbsp', 'to taste' → (qty, unit)."""
    t = (text or '').strip()
    if not t:
        return None, ''

    # Replace unicode fractions with decimal equivalents
    total = Decimal('0')
    remaining = t
    for char, val in UNICODE_FRAC.items():
        if char in remaining:
            total += val
            remaining = remaining.replace(char, ' ').strip()

    # Pull leading number(s) from remaining
    m = re.match(r'^\s*(\d+(?:\.\d+)?)\s*(.*)$', remaining)
    if m:
        try:
            total += Decimal(m.group(1))
        except InvalidOperation:
            pass
        unit_part = m.group(2).strip()
    else:
        unit_part = remaining.strip()

    # Normalize unit: take first word if it's a known unit, else keep whole string
    first_word = unit_part.split()[0].lower().rstrip('.,') if unit_part else ''
    if first_word in KNOWN_UNITS:
        unit = first_word
    else:
        unit = unit_part[:30]

    qty = total if total > 0 else None
    return qty, unit


def split_ingredient_line(text: str) -> tuple[str, str]:
    """Split 'Butter\t\t\t1 cup' → ('Butter', '1 cup'). Handles tab-separated and space-separated forms."""
    if '\t' in text:
        parts = [s.strip() for s in text.split('\t') if s.strip()]
        if len(parts) == 0:
            return '', ''
        if len(parts) == 1:
            return parts[0], ''
        return parts[0], parts[-1]
    # No tabs — try to split on trailing "{number or fraction} ..."
    m = re.match(
        r'^(.*?)\s+((?:\d+(?:\.\d+)?|[½¼¾⅓⅔⅛⅜⅝⅞]).*)$',
        text.strip(),
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ''


def parse_recipe_doc(path: Path) -> dict | None:
    """Parse one .docx into a dict with name, ingredients, notes, source_doc."""
    try:
        doc = docx.Document(str(path))
    except Exception as e:
        return {'error': f"open failed: {e}", 'path': str(path)}

    paragraphs = [p for p in doc.paragraphs if p.text.strip()]
    if not paragraphs:
        return {'error': 'empty doc', 'path': str(path)}

    fallback_name = path.stem
    title = clean_title(paragraphs[0].text, fallback_name)

    ingredients = []
    notes_lines = []
    mode = 'ingredients'

    for p in paragraphs[1:]:
        text = p.text.strip()
        stripped = text.lstrip('*').strip().lower()
        if PROCEDURE_MARKERS.match(text):
            mode = 'procedure'
            # Keep "preheat ..." as a note; drop the literal "Procedure:" / "Preparation Procedure:" headers
            if stripped.startswith('preheat'):
                notes_lines.append(text)
            continue
        # Only treat List Paragraph as procedure if we've already captured ingredients
        # (Potato Skins has a single List Paragraph ingredient as line 1.)
        if p.style.name == 'List Paragraph' and ingredients:
            mode = 'procedure'
            notes_lines.append(text)
            continue

        if mode == 'ingredients':
            name, qty_text = split_ingredient_line(text)
            if not name:
                continue
            qty, unit = parse_quantity(qty_text)
            ingredients.append({
                'name_raw': name[:300],
                'quantity': qty,
                'unit': unit,
            })
        else:
            notes_lines.append(text)

    try:
        source = str(path.relative_to(path.parents[3])) if len(path.parents) >= 4 else str(path)
    except ValueError:
        source = str(path)
    source = source.encode('utf-8', 'replace').decode('utf-8')  # strip surrogates from filesystem
    return {
        'name':        title[:200],
        'source_doc':  source[:500],
        'ingredients': ingredients,
        'notes':       '\n'.join(notes_lines),
        'path':        str(path).encode('utf-8', 'replace').decode('utf-8'),
    }


class Command(BaseCommand):
    help = "Recursively import recipe .docx files into Recipe + RecipeIngredient."

    def add_arguments(self, parser):
        parser.add_argument("root", type=str, help="Recipe Book root directory")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--verbose-errors", action="store_true")

    def handle(self, *args, **opts):
        root = Path(opts["root"])
        if not root.exists():
            raise CommandError(f"Not found: {root}")

        all_files = [
            p for p in root.rglob("*.docx")
            if p.name not in SKIP_NAMES and not p.name.startswith('~$')
        ]
        self.stdout.write(f"Found {len(all_files)} recipe .docx files under {root}")

        # Split into composed-meal files vs normal recipes.
        normal_files: list[Path] = []
        composed_by_meal: dict[str, list[Path]] = defaultdict(list)
        for p in all_files:
            parts = p.relative_to(root).parts
            if len(parts) >= 2 and parts[0] == 'Composed Meals':
                composed_by_meal[parts[1]].append(p)
            else:
                normal_files.append(p)

        self.stdout.write(
            f"  Normal recipes: {len(normal_files)}  |  "
            f"Composed meals: {len(composed_by_meal)} ({sum(len(v) for v in composed_by_meal.values())} files)"
        )

        # Parse everything first
        parsed_normal = []
        errors = []
        for path in sorted(normal_files):
            r = parse_recipe_doc(path)
            if not r or 'error' in r:
                errors.append(r)
                continue
            parsed_normal.append(r)

        if opts['verbose_errors']:
            for e in errors:
                self.stdout.write(self.style.WARNING(f"  ERR {e.get('path')}: {e.get('error')}"))

        if opts['dry_run']:
            self.stdout.write(f"\nParsed normal: {len(parsed_normal)} ok, {len(errors)} errors")
            self.stdout.write(f"Composed meals to build: {sorted(composed_by_meal.keys())}")
            return

        created, updated, ing_total = 0, 0, 0
        linked_by_name = 0

        with transaction.atomic():
            # Phase 1: normal recipes (canonical sources)
            for r in parsed_normal:
                recipe, was_created = Recipe.objects.update_or_create(
                    name=r['name'],
                    defaults={'source_doc': r['source_doc'], 'notes': r['notes']},
                )
                created += was_created
                updated += not was_created
                recipe.ingredients.all().delete()
                for ing in r['ingredients']:
                    RecipeIngredient.objects.create(recipe=recipe, **ing)
                    ing_total += 1

            # Phase 2: composed meals
            for meal_name, paths in composed_by_meal.items():
                parent, _ = Recipe.objects.update_or_create(
                    name=meal_name,
                    defaults={'source_doc': f'Composed Meals/{meal_name}/', 'notes': ''},
                )
                parent.ingredients.all().delete()
                extra_notes: list[str] = []

                for path in sorted(paths):
                    parsed = parse_recipe_doc(path)
                    if not parsed or 'error' in parsed:
                        continue
                    is_procedure = (
                        'procedure' in path.stem.lower()
                        or parsed['name'].strip().lower() in PROCEDURE_TITLES
                    )
                    if is_procedure:
                        # Absorb its leaf ingredients + notes into the parent
                        for ing in parsed['ingredients']:
                            RecipeIngredient.objects.create(recipe=parent, **ing)
                            ing_total += 1
                        if parsed['notes']:
                            extra_notes.append(parsed['notes'])
                    else:
                        # Sibling is a sub-recipe. Use canonical if one exists (same name).
                        sub, sub_created = Recipe.objects.get_or_create(
                            name=parsed['name'],
                            defaults={'source_doc': parsed['source_doc'], 'notes': parsed['notes']},
                        )
                        if sub_created:
                            created += 1
                            for ing in parsed['ingredients']:
                                RecipeIngredient.objects.create(recipe=sub, **ing)
                                ing_total += 1
                        # Link sub as a sub_recipe ingredient on parent
                        RecipeIngredient.objects.create(
                            recipe=parent,
                            sub_recipe=sub,
                            name_raw=parsed['name'],
                            quantity=Decimal('1'),
                            unit='batch',
                        )
                        ing_total += 1

                if extra_notes:
                    parent.notes = '\n\n'.join(n for n in extra_notes if n)
                    parent.save(update_fields=['notes'])

            # Phase 3: auto-link by name match (RecipeIngredient.name_raw → Recipe)
            recipes_by_name = {r.name.lower().strip(): r for r in Recipe.objects.all()}
            to_link = RecipeIngredient.objects.filter(sub_recipe__isnull=True, product__isnull=True)
            for ri in to_link:
                cand = recipes_by_name.get(ri.name_raw.lower().strip())
                if cand and cand.id != ri.recipe_id:
                    ri.sub_recipe = cand
                    ri.save(update_fields=['sub_recipe'])
                    linked_by_name += 1

        self.stdout.write(self.style.SUCCESS(
            f"Recipes: {created} created, {updated} updated. Ingredients: {ing_total}. "
            f"Auto-linked sub_recipes by name: {linked_by_name}"
        ))
