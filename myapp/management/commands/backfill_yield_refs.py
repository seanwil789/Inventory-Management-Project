"""
Conservative auto-backfill of RecipeIngredient.yield_ref.

Only applies matches where:
  - ingredient name_raw (lowercased) == YieldReference.ingredient (lowercased)
  - That YieldReference has yield_pct populated
  - Exactly one such YieldReference exists

Skips:
  - Pantry ingredients (salt, flour, sugar, oil, etc.)
  - Ambiguous matches (multiple candidates with same name but different prep states)
  - No-candidate ingredients

For ambiguous and no-match cases, prints a review list pointing at /yields/bridge/.

Run:
    python manage.py backfill_yield_refs --dry-run
    python manage.py backfill_yield_refs --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Lower

from myapp.models import RecipeIngredient, YieldReference
from myapp.views import _is_pantry_skip


class Command(BaseCommand):
    help = 'Auto-link RecipeIngredient.yield_ref on exact-name single-candidate matches only.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write links (default is dry-run)')

    def handle(self, *args, **opts):
        dist = (RecipeIngredient.objects
                .filter(yield_ref__isnull=True, sub_recipe__isnull=True)
                .annotate(lc=Lower('name_raw'))
                .values('lc')
                .annotate(n=Count('id'))
                .order_by('-n'))

        auto_matches = []      # (name_lc, yr, count)
        ambiguous = []         # (name_lc, [yrs], count)
        no_match = []          # (name_lc, count)
        pantry_skipped = 0

        for d in dist:
            name_lc = d['lc']
            count = d['n']
            if _is_pantry_skip(name_lc):
                pantry_skipped += count
                continue
            # Exact name match with yield_pct populated
            qs = YieldReference.objects.filter(
                ingredient__iexact=name_lc,
                yield_pct__isnull=False,
            )
            n_cands = qs.count()
            if n_cands == 1:
                auto_matches.append((name_lc, qs.first(), count))
            elif n_cands > 1:
                ambiguous.append((name_lc, list(qs), count))
            else:
                no_match.append((name_lc, count))

        total_auto = sum(c for _, _, c in auto_matches)
        total_ambig = sum(c for _, _, c in ambiguous)
        total_none = sum(c for _, c in no_match)

        self.stdout.write(self.style.HTTP_INFO('=== Safe auto-matches (exact name, single candidate) ==='))
        for name_lc, yr, count in auto_matches:
            self.stdout.write(
                f'  [{count:2d}]  {name_lc!r:30s} → {yr.ingredient!r} / {yr.prep_state!r}  '
                f'yield_pct={yr.yield_pct}')

        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Ambiguous (multiple candidates — needs manual review via /yields/bridge/) ==='))
        for name_lc, yrs, count in ambiguous[:25]:
            prep_states = [y.prep_state or '(none)' for y in yrs[:4]]
            more = f' +{len(yrs) - 4} more' if len(yrs) > 4 else ''
            self.stdout.write(
                f'  [{count:2d}]  {name_lc!r:30s} ({len(yrs)} candidates: {", ".join(prep_states)}{more})')
        if len(ambiguous) > 25:
            self.stdout.write(f'  ... and {len(ambiguous) - 25} more')

        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            f'=== No match (add to YieldReference manually or via admin) ==='))
        for name_lc, count in no_match[:20]:
            self.stdout.write(f'  [{count:2d}]  {name_lc!r}')
        if len(no_match) > 20:
            self.stdout.write(f'  ... and {len(no_match) - 20} more')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Summary:\n'
            f'  Auto-linkable:      {len(auto_matches):4d} names  /  {total_auto:4d} rows\n'
            f'  Ambiguous (review): {len(ambiguous):4d} names  /  {total_ambig:4d} rows\n'
            f'  No match:           {len(no_match):4d} names  /  {total_none:4d} rows\n'
            f'  Pantry-skipped:                   {pantry_skipped:4d} rows'))

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '\nDry run — no DB writes. Re-run with --apply to link the auto-matches.'))
            return

        with transaction.atomic():
            total_linked = 0
            for name_lc, yr, _ in auto_matches:
                linked = (RecipeIngredient.objects
                          .annotate(lc=Lower('name_raw'))
                          .filter(lc=name_lc, yield_ref__isnull=True, sub_recipe__isnull=True)
                          .update(yield_ref=yr))
                total_linked += linked

        self.stdout.write(self.style.SUCCESS(
            f'\n✔ Linked {total_linked} RecipeIngredient rows to {len(auto_matches)} YieldReferences.'))
