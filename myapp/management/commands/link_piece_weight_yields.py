"""Auto-link RecipeIngredient.yield_ref for Phase 6 piece-unit recipes.

Targets RecipeIngredient rows with:
  - recipe__is_current=True
  - product__isnull=False (i.e. mapped to a canonical Product)
  - quantity__isnull=False
  - yield_ref__isnull=True (not already linked)
  - unit in {medium, large, small, ea, each}

Matches by:
  1. product.canonical_name → YieldReference.ingredient via
     progressive front-strip, back-strip, and single-token with plural
     ("Red Onion" → "Onion(s)", "Tomato, Utility" → "Tomato(es)")
  2. Restricted to YR rows with ap_unit in {each, head} (not bunch —
     'ea Celery' means a stalk, not a whole bunch)
  3. Size-word recipe unit must match YR prep_state size tag
     ('medium' → 'each medium' / 'whole,medium'). 'large' falls back
     to 'jumbo'. Missing size → skip rather than guess.
  4. 'ea' / 'each' → prefer YR row with 'medium' size; else empty-prep
     single row; else ambiguous.

Dry-run by default. Run with --apply to write links.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import RecipeIngredient, YieldReference


PIECE_RECIPE_UNITS = {'medium', 'large', 'small', 'ea', 'each'}
PIECE_AP_UNITS = {'each', 'head'}  # 'bunch' intentionally excluded


def _infer_size_from_prep(prep_state: str) -> str | None:
    p = (prep_state or '').lower()
    for w in ('medium', 'large', 'small', 'jumbo'):
        if w in p:
            return w
    return None


def _is_simple_prep(yr: YieldReference) -> bool:
    p = (yr.prep_state or '').lower()
    blocked = ('peel', 'chop', 'dice', 'slice', 'cooked', 'grated',
               'purée', 'crush', 'ground', 'juice', 'elephant')
    return not any(x in p for x in blocked)


def _candidate_keys(canonical_name: str) -> list[str]:
    """Generate lookup keys from a product name: full → front-strip →
    back-strip → single tokens. Handles 'Red Onion', 'Tomato, Utility',
    'Bell Pepper, Red' uniformly."""
    base = canonical_name.lower().replace(',', ' ').split()
    keys = [' '.join(base)]
    for i in range(1, len(base)):
        keys.append(' '.join(base[i:]))   # front-strip
    for i in range(len(base) - 1, 0, -1):
        keys.append(' '.join(base[:i]))   # back-strip
    for t in base:
        keys.append(t)                    # single token
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _build_yr_index():
    """Index YR rows by ingredient (lowercased) → [(yr, inferred_size)]."""
    idx: dict[str, list[tuple[YieldReference, str | None]]] = {}
    qs = YieldReference.objects.filter(
        ap_weight_oz__isnull=False, ap_unit__in=PIECE_AP_UNITS,
    )
    for yr in qs:
        key = yr.ingredient.strip().lower()
        idx.setdefault(key, []).append((yr, _infer_size_from_prep(yr.prep_state)))
    return idx


def _lookup(canonical_name: str, idx: dict) -> list:
    for k in _candidate_keys(canonical_name):
        if k in idx:
            return idx[k]
        if k + 's' in idx:
            return idx[k + 's']
    return []


def _match(canonical_name: str, recipe_unit: str, idx: dict):
    """Return (yr, reason) — yr is None on no-match or ambiguous."""
    cands = _lookup(canonical_name, idx)
    if not cands:
        return None, 'no YR ingredient match'

    u = (recipe_unit or '').strip().lower()
    if u in ('medium', 'large', 'small'):
        same = [c for c in cands if c[1] == u]
        if not same and u == 'large':
            same = [c for c in cands if c[1] == 'jumbo']
        if not same:
            return None, f'no {u!r}-sized piece row (would be guessing)'
        cands = same
    elif u in ('ea', 'each'):
        med = [c for c in cands if c[1] == 'medium']
        if med:
            cands = med
        else:
            simple = [c for c in cands if c[1] is None and _is_simple_prep(c[0])]
            if simple:
                cands = simple

    if len(cands) == 1:
        return cands[0][0], 'unambiguous'
    empties = [c for c in cands if (c[0].prep_state or '') == '']
    if len(empties) == 1:
        return empties[0][0], 'empty-prep tie-break'
    return None, f'ambiguous: {len(cands)} candidates'


class Command(BaseCommand):
    help = 'Link RecipeIngredient.yield_ref to piece-weight YR rows for size-word/ea units.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write links (default is dry-run)')

    def handle(self, *args, **opts):
        idx = _build_yr_index()

        linkable, ambiguous, nomatch = [], [], []
        qs = (RecipeIngredient.objects
              .filter(recipe__is_current=True,
                      product__isnull=False,
                      quantity__isnull=False,
                      yield_ref__isnull=True)
              .select_related('product', 'recipe'))

        for ri in qs:
            u = (ri.unit or '').strip().lower()
            if u not in PIECE_RECIPE_UNITS:
                continue
            yr, reason = _match(ri.product.canonical_name, ri.unit, idx)
            if yr:
                linkable.append((ri, yr, reason))
            elif 'ambiguous' in reason:
                ambiguous.append((ri, reason))
            else:
                nomatch.append((ri, reason))

        self.stdout.write(self.style.HTTP_INFO(
            f'═══ Linkable: {len(linkable)} RIs ═══'))
        for ri, yr, why in linkable:
            self.stdout.write(
                f'  {ri.recipe.name[:25]:25s}  '
                f'{ri.quantity}{ri.unit!r:<8s}  '
                f'{ri.product.canonical_name[:22]:22s}  '
                f'→ {yr.ingredient}/{yr.prep_state!r} ({yr.ap_weight_oz}oz)  [{why}]')

        if ambiguous:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'═══ Ambiguous: {len(ambiguous)} RIs (manual review) ═══'))
            for ri, reason in ambiguous:
                self.stdout.write(
                    f'  {ri.recipe.name[:25]:25s}  '
                    f'{ri.quantity}{ri.unit!r:<8s}  '
                    f'{ri.product.canonical_name}: {reason}')

        if nomatch:
            self.stdout.write('')
            self.stdout.write(self.style.HTTP_INFO(
                f'═══ No match: {len(nomatch)} RIs (need YR row or recipe cleanup) ═══'))
            for ri, reason in nomatch:
                self.stdout.write(
                    f'  {ri.recipe.name[:25]:25s}  '
                    f'{ri.quantity}{ri.unit!r:<8s}  '
                    f'{ri.product.canonical_name[:25]:25s}  [{reason}]')

        if not opts['apply']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry run — no DB writes. Re-run with --apply to link.'))
            return

        with transaction.atomic():
            for ri, yr, _ in linkable:
                ri.yield_ref = yr
                ri.save(update_fields=['yield_ref'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'✔ Linked {len(linkable)} RecipeIngredient rows to piece-weight YR rows.'))
