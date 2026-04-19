"""Auto-match RecipeIngredient rows to Products.

Three-tier matching (applied in order):
  1. EXACT — normalized strings match (ignoring case/punct)
  2. TOKEN_SUBSET — every word in the ingredient appears in the product name
     (e.g. 'Brown Sugar' ⊆ 'Sugar, Dark Brown' → match)
  3. FUZZY — rapidfuzz WRatio above cutoff

Tier 1 and 2 auto-apply. Tier 3 is reported only (not applied) so Sean
can review before committing low-confidence matches. Re-running is idempotent:
only ingredients without a product are touched.

Usage:
  python manage.py map_recipe_ingredients --dry-run
  python manage.py map_recipe_ingredients
  python manage.py map_recipe_ingredients --include-fuzzy  (auto-apply fuzzy too)
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand
from rapidfuzz import fuzz, process

from myapp.models import Product, RecipeIngredient


FUZZY_CUTOFF = 85


def _normalize(s: str) -> str:
    s = (s or '').lower()
    s = re.sub(r'[^\w\s,]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _tokens(s: str) -> set[str]:
    """Token set, lowercased, singularized (crude), min length 2."""
    s = re.sub(r'[^\w\s]', ' ', (s or '').lower())
    toks = {t for t in s.split() if len(t) >= 2}
    # crude singular
    toks = {t[:-1] if len(t) > 3 and t.endswith('s') else t for t in toks}
    return toks


def match_ingredient(name: str, products: list[Product]) -> tuple[Product | None, str, int]:
    """Return (product, confidence_label, fuzz_score_if_any)."""
    norm_ing = _normalize(name)
    ing_toks = _tokens(name)
    if not norm_ing or not ing_toks:
        return None, '', 0

    # Tier 1: exact normalized
    for p in products:
        if _normalize(p.canonical_name) == norm_ing:
            return p, 'exact', 100

    # Tier 2: ingredient tokens are a subset of product tokens
    subset_matches: list[tuple[int, Product]] = []
    for p in products:
        p_toks = _tokens(p.canonical_name)
        if ing_toks and ing_toks.issubset(p_toks):
            extra = len(p_toks - ing_toks)
            subset_matches.append((extra, p))
    if subset_matches:
        subset_matches.sort(key=lambda x: (x[0], x[1].id))
        return subset_matches[0][1], 'token_subset', 95

    # Tier 3: fuzzy
    names = [p.canonical_name for p in products]
    result = process.extractOne(name, names, scorer=fuzz.WRatio, score_cutoff=FUZZY_CUTOFF)
    if result:
        _matched_name, score, idx = result
        return products[idx], 'fuzzy', int(score)

    return None, '', 0


class Command(BaseCommand):
    help = "Auto-match RecipeIngredient rows to Products (exact/token/fuzzy tiers)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--include-fuzzy", action="store_true",
                            help="Auto-apply fuzzy matches too (not just exact/token_subset)")

    def handle(self, *args, **opts):
        products = list(Product.objects.all())
        unmatched_qs = RecipeIngredient.objects.filter(product__isnull=True, sub_recipe__isnull=True)
        total = unmatched_qs.count()
        self.stdout.write(f"Candidates: {total} RecipeIngredients · {len(products)} Products")

        tier_counts: Counter = Counter()
        tier_buckets: dict[str, list[tuple[RecipeIngredient, Product, int]]] = {
            'exact': [], 'token_subset': [], 'fuzzy': [], 'none': [],
        }

        for ri in unmatched_qs:
            product, label, score = match_ingredient(ri.name_raw, products)
            if product:
                tier_counts[label] += 1
                tier_buckets[label].append((ri, product, score))
            else:
                tier_counts['none'] += 1
                tier_buckets['none'].append((ri, None, 0))

        self.stdout.write(f"\nTier results:")
        for tier in ('exact', 'token_subset', 'fuzzy', 'none'):
            self.stdout.write(f"  {tier:15s} {tier_counts.get(tier, 0)}")

        # Show samples of each tier
        for tier in ('exact', 'token_subset', 'fuzzy', 'none'):
            if not tier_buckets[tier]:
                continue
            self.stdout.write(f"\n=== {tier.upper()} (first 15) ===")
            for ri, p, score in tier_buckets[tier][:15]:
                arrow = f"→ {p.canonical_name} [{score}]" if p else "→ (no match)"
                self.stdout.write(f"  '{ri.name_raw}'  {arrow}")

        if opts['dry_run']:
            return

        # Apply: exact + token_subset always; fuzzy only with --include-fuzzy
        to_apply = tier_buckets['exact'] + tier_buckets['token_subset']
        if opts['include_fuzzy']:
            to_apply += tier_buckets['fuzzy']

        updated = 0
        for ri, product, _ in to_apply:
            ri.product = product
            ri.save(update_fields=['product'])
            updated += 1
        self.stdout.write(self.style.SUCCESS(f"\nApplied: {updated} RecipeIngredient → Product links."))
        remaining = RecipeIngredient.objects.filter(product__isnull=True, sub_recipe__isnull=True).count()
        self.stdout.write(f"Remaining unlinked: {remaining}")
