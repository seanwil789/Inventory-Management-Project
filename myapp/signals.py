"""Signal handlers for myapp.

PrepTask auto-derivation: when a Menu is saved (or its additional_recipes
m2m changes), synthesize PrepTask rows for each linked recipe on the
prep-day-before-service. Idempotent via get_or_create on (date, recipe).

Wired in apps.MyappConfig.ready().
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver


def _prep_date_for(service_date: date) -> date:
    """Mon/Sun service → Fri prep. Skip weekend prep days."""
    d = service_date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _sync_preptasks_for_menu(menu) -> int:
    """Create PrepTask rows for each linked recipe on the menu's prep date.
    Returns count of newly-created rows. Idempotent — re-running doesn't
    duplicate existing tasks; completion state is preserved."""
    from .models import PrepTask

    prep_day = _prep_date_for(menu.date)
    recipes = list(menu.additional_recipes.all())
    if menu.recipe_id and menu.recipe_id not in {r.id for r in recipes}:
        recipes.append(menu.recipe)

    created = 0
    for r in recipes:
        _, was_created = PrepTask.objects.get_or_create(
            date=prep_day, recipe=r,
            defaults={'completed': False, 'notes': ''},
        )
        if was_created:
            created += 1
    return created


@receiver(post_save, sender='myapp.Menu')
def menu_post_save_derive_prep(sender, instance, created, **kwargs):
    """On Menu save, derive PrepTasks. Signal is dispatched for both create
    and update; idempotent so no harm in running on updates."""
    # Skip tests that don't want this side effect — check for a flag on the
    # instance (caller can set instance._skip_prep_derivation = True).
    if getattr(instance, '_skip_prep_derivation', False):
        return
    try:
        _sync_preptasks_for_menu(instance)
    except Exception:
        # Signal failure shouldn't block save. Log via Django's default channel.
        import logging
        logging.getLogger('myapp.signals').exception(
            'PrepTask auto-derivation failed for menu %s', instance.pk)


ROLLING_SAMPLE_SIZE = 10
MIN_SAMPLES_FOR_LEARNED = 3


def _recompute_learned_popularity(recipe) -> bool:
    """Recompute Recipe.learned_consumption_rate from this recipe's last N
    MealService records. Returns True if Recipe changed."""
    from .models import MealService
    from decimal import Decimal

    # All services where this recipe is linked (direct or via additional_recipes)
    services = (MealService.objects
                .filter(menu__recipe=recipe)
                .order_by('-created_at'))
    via_m2m = (MealService.objects
               .filter(menu__additional_recipes=recipe)
               .order_by('-created_at'))
    combined = list(services) + [s for s in via_m2m if s.pk not in {x.pk for x in services}]
    combined.sort(key=lambda s: s.created_at or s.menu.date, reverse=True)
    combined = combined[:ROLLING_SAMPLE_SIZE]

    rates = [s.total_consumption_rate for s in combined if s.total_consumption_rate is not None]

    old_rate = recipe.learned_consumption_rate
    old_count = recipe.learned_sample_count

    if len(rates) >= MIN_SAMPLES_FOR_LEARNED:
        avg = sum(rates) / len(rates)
        new_rate = avg.quantize(Decimal('0.001'))
        new_count = len(rates)
    else:
        new_rate = None  # falls back to 0.80 baseline at callers
        new_count = len(rates)

    if new_rate != old_rate or new_count != old_count:
        recipe.learned_consumption_rate = new_rate
        recipe.learned_sample_count = new_count
        recipe.save(update_fields=['learned_consumption_rate', 'learned_sample_count'])
        return True
    return False


@receiver(post_save, sender='myapp.MealService')
def mealservice_post_save_update_popularity(sender, instance, **kwargs):
    """Recompute learned_consumption_rate for each recipe tied to this
    MealService's menu. Runs on every MealService save (cleanup + disposal)."""
    try:
        menu = instance.menu
        recipes_to_update = set()
        if menu.recipe_id:
            recipes_to_update.add(menu.recipe)
        for r in menu.additional_recipes.all():
            recipes_to_update.add(r)
        for recipe in recipes_to_update:
            _recompute_learned_popularity(recipe)
    except Exception:
        import logging
        logging.getLogger('myapp.signals').exception(
            'Learned-popularity recompute failed for MealService %s', instance.pk)


@receiver(m2m_changed, sender='myapp.Menu_additional_recipes')
def menu_additional_recipes_changed(sender, instance, action, pk_set, **kwargs):
    """Handle additional_recipes m2m add/remove. post_save on Menu doesn't
    fire when only the m2m changes, so this catches those cases."""
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if getattr(instance, '_skip_prep_derivation', False):
        return
    try:
        _sync_preptasks_for_menu(instance)
    except Exception:
        import logging
        logging.getLogger('myapp.signals').exception(
            'PrepTask sync from m2m_changed failed for menu %s', instance.pk)
