import re
from difflib import get_close_matches

from django.core.management.base import BaseCommand

from myapp.models import Menu, Recipe


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse spaces — for fuzzy compare."""
    s = (s or '').lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


class Command(BaseCommand):
    help = "Link existing Menu rows to Recipe rows by name match (exact + fuzzy)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--cutoff", type=float, default=0.75,
                            help="Fuzzy match similarity cutoff (0..1).")

    def handle(self, *args, **opts):
        # Only match menu-level recipes — exclude sub-recipes/prep components
        # to prevent "Pesto" matching "Shrimp Pesto Pasta" (the long-standing
        # substring bug). composed_dish and meal are the valid match targets.
        recipes = list(Recipe.objects.filter(
            level__in=('composed_dish', 'meal'),
            is_current=True,
        ))
        by_norm = {_norm(r.name): r for r in recipes}
        norm_keys = list(by_norm.keys())

        unlinked = Menu.objects.filter(recipe__isnull=True).exclude(dish_freetext='')
        self.stdout.write(f"{unlinked.count()} unlinked Menu rows  |  {len(recipes)} recipes")

        exact, substring, fuzzy_suggestions, miss = 0, 0, [], []
        updates = []
        for m in unlinked:
            norm_dish = _norm(m.dish_freetext)
            # 1) Exact name match after normalization
            if norm_dish in by_norm:
                updates.append((m, by_norm[norm_dish], 'exact'))
                exact += 1
                continue
            # 2) Substring match — prefer longer recipe name if multiple match.
            #    Clip at " with " so "X with Y" only matches against X (the main dish),
            #    not Y (the side).  "Sausage Gravy and biscuits" still matches Sausage Gravy.
            primary = norm_dish.split(' with ', 1)[0]
            dish_padded = f" {primary} "
            hits = [
                (nk, by_norm[nk]) for nk in norm_keys
                if len(nk) >= 5 and f" {nk} " in dish_padded
            ]
            if hits:
                # Prefer the longest match (most specific)
                hits.sort(key=lambda x: len(x[0]), reverse=True)
                updates.append((m, hits[0][1], f"substring→{hits[0][0]}"))
                substring += 1
                continue
            # 3) Fuzzy — suggest but don't auto-apply
            candidates = get_close_matches(norm_dish, norm_keys, n=1, cutoff=opts['cutoff'])
            if candidates:
                fuzzy_suggestions.append((m, by_norm[candidates[0]]))
            else:
                miss.append(m)

        self.stdout.write(
            f"Matches: {exact} exact, {substring} substring, "
            f"{len(fuzzy_suggestions)} fuzzy suggestions (not applied), {len(miss)} unmatched"
        )

        if substring:
            self.stdout.write("\n=== Substring matches (auto-applied) ===")
            for m, r, how in updates:
                if how.startswith('substring'):
                    self.stdout.write(f"  {m.date} {m.meal_slot:15s} '{m.dish_freetext[:50]}' → {r.name}")

        if fuzzy_suggestions:
            self.stdout.write("\n=== Fuzzy suggestions (review manually in UI) ===")
            for m, r in fuzzy_suggestions:
                self.stdout.write(f"  {m.date} {m.meal_slot:15s} '{m.dish_freetext[:50]}' ≈ {r.name}")

        if miss:
            self.stdout.write(f"\n=== Unmatched ({len(miss)}) — pick recipe manually in UI ===")
            for m in miss:
                self.stdout.write(f"  {m.date} {m.meal_slot:15s} {m.dish_freetext}")

        if opts['dry_run']:
            return

        for m, r, how in updates:
            m.recipe = r
            m.save(update_fields=['recipe'])
        self.stdout.write(self.style.SUCCESS(f"Linked {len(updates)} Menu rows."))
