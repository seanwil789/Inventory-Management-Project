"""Recipe-coverage Stock + Flow report — the Measure step of the recipe-coverage goal.

Stock (lagging): % of menu cells in the date window with COMPLETE coverage —
  a recipe whose full expansion has both qty and a product FK on every
  ingredient (sub-recipes recursed). A cell linked to a null-qty or
  unlinked-ingredient recipe is NOT covered (anti-gaming: measures real
  buy-list productivity, not raw FK count).
Flow (leading): % of cells AUTHORED in the recent window (Menu.created_at)
  that have a recipe linked.

Read-only. Intended to fire cycle-end Sundays via cron.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from myapp.models import Menu


class Command(BaseCommand):
    help = "Recipe-coverage Stock + Flow report (recipe-coverage goal Measure step)."

    def add_arguments(self, parser):
        parser.add_argument('--start', help='Stock window start (ISO date). Default: today.')
        parser.add_argument('--end', help='Stock window end (ISO date). Default: start + 13 days.')
        parser.add_argument('--flow-days', type=int, default=14,
                            help='Flow lookback over Menu.created_at, in days. Default: 14.')

    def handle(self, *args, **opts):
        # Reuse the order-guide expansion so coverage never diverges from the
        # buy-list the guide actually produces. Lazy import avoids any
        # circular import at command load.
        from myapp.views import _expand_recipe

        today = date.today()
        start = date.fromisoformat(opts['start']) if opts.get('start') else today
        end = date.fromisoformat(opts['end']) if opts.get('end') else start + timedelta(days=13)

        cells = list(
            Menu.objects.filter(date__gte=start, date__lte=end)
            .prefetch_related('additional_recipes', 'recipe', 'freetext_components')
        )
        covered = incomplete = freetext_only = unlinked = 0
        blockers: dict[str, int] = {}

        for m in cells:
            recipes = list(m.additional_recipes.all())
            if m.recipe_id and m.recipe not in recipes:
                recipes.append(m.recipe)

            if not recipes:
                if m.freetext_components.exists():
                    freetext_only += 1
                else:
                    unlinked += 1
                continue

            cell_complete = True
            for r in recipes:
                r_complete = all(
                    ing['qty'] is not None and ing['product'] is not None
                    for ing in _expand_recipe(r, 1.0)
                )
                if not r_complete:
                    cell_complete = False
                    blockers[r.name] = blockers.get(r.name, 0) + 1
            if cell_complete:
                covered += 1
            else:
                incomplete += 1

        total = len(cells)
        stock = (covered / total) if total else 0.0

        since = timezone.now() - timedelta(days=opts['flow_days'])
        authored = Menu.objects.filter(created_at__gte=since)
        authored_n = authored.count()
        authored_recipe = authored.filter(recipe__isnull=False).count()
        flow = (authored_recipe / authored_n) if authored_n else None

        w = self.stdout.write
        w(f"=== Recipe Coverage Report — {today} ===")
        w(f"STOCK window {start} .. {end}  ({total} cells)")
        w(f"  STOCK = {stock:.1%}  ({covered} covered / {total})")
        w(f"    covered {covered} | linked-incomplete {incomplete} | "
          f"freetext-only {freetext_only} | unlinked {unlinked}")
        if flow is None:
            w(f"FLOW (last {opts['flow_days']}d authoring) = n/a — 0 cells authored in window")
        else:
            w(f"FLOW (last {opts['flow_days']}d authoring) = {flow:.1%}  "
              f"({authored_recipe} recipe'd / {authored_n} authored)")
        if blockers:
            w("  top blockers (linked cells w/ null-qty or unlinked ingredient -> feed A4/A5):")
            for name, c in sorted(blockers.items(), key=lambda kv: -kv[1])[:10]:
                w(f"    {c:3d}  {name}")
