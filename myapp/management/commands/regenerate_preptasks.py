"""Generate PrepTask rows from Menu rows.

For each Menu, each linked recipe (additional_recipes + legacy menu.recipe) produces
a PrepTask with date = prep-day-before-service. Monday service preps on Friday
(no weekend prep).

Idempotent via (date, recipe) unique lookup — re-running won't duplicate.
Completed status is preserved across re-runs.
"""
from datetime import date, timedelta
from django.core.management.base import BaseCommand

from myapp.models import Menu, PrepTask


def prep_date_for(service_date: date) -> date:
    """Day before, skipping weekend. Mon service → Fri prep. Sun service → Fri prep."""
    d = service_date - timedelta(days=1)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d -= timedelta(days=1)
    return d


class Command(BaseCommand):
    help = "Generate PrepTask rows from Menus in a date range."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=str, help="YYYY-MM-DD; default: today")
        parser.add_argument("--end",   type=str, help="YYYY-MM-DD; default: today + 30 days")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        start = date.fromisoformat(opts['start']) if opts['start'] else date.today()
        end   = date.fromisoformat(opts['end'])   if opts['end']   else start + timedelta(days=30)

        menus = (Menu.objects
                 .filter(date__gte=start, date__lte=end)
                 .prefetch_related('additional_recipes'))
        self.stdout.write(f"Scanning {menus.count()} menus from {start} to {end}...")

        planned: list[tuple[date, int, str]] = []  # (prep_date, recipe_id, menu_info)
        for m in menus:
            prep_day = prep_date_for(m.date)
            recipes = list(m.additional_recipes.all())
            if m.recipe_id and m.recipe_id not in {r.id for r in recipes}:
                recipes.append(m.recipe)
            for r in recipes:
                planned.append((prep_day, r.id, f"{m.date} {m.meal_slot}: {r.name}"))

        self.stdout.write(f"Would generate {len(planned)} PrepTask rows.")

        if opts['dry_run']:
            for prep, rid, info in planned[:20]:
                self.stdout.write(f"  prep {prep}: {info}")
            if len(planned) > 20:
                self.stdout.write(f"  ... +{len(planned)-20} more")
            return

        created, kept = 0, 0
        for prep, rid, _ in planned:
            _, was_created = PrepTask.objects.get_or_create(
                date=prep, recipe_id=rid,
                defaults={'completed': False, 'notes': ''},
            )
            created += was_created
            kept += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"PrepTasks: {created} created, {kept} already existed."
        ))
