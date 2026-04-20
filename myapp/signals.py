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
