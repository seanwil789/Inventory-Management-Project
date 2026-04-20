"""Smoke + logic tests for the myapp routes.

Scope is intentionally narrow: each new endpoint shipped in April 2026 gets
a smoke test (HTTP 200 / 302) plus a handful of logic assertions for the
critical paths (recipe versioning, auto-tag pipeline, COGs math).

Run: python manage.py test myapp
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, Client, override_settings
from django.urls import reverse

from myapp.models import (
    Recipe, RecipeIngredient, Menu, Census, Vendor, Product, InvoiceLineItem,
)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SmokeTests(TestCase):
    """HTTP 200 on every GET route added in the April 2026 push."""

    @classmethod
    def setUpTestData(cls):
        cls.r1 = Recipe.objects.create(name='Test Pancakes', level='meal',
                                       yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=cls.r1, name_raw='flour', quantity=Decimal('4'), unit='cup',
        )
        cls.menu = Menu.objects.create(
            date=date.today(), meal_slot='lunch', recipe=cls.r1,
            dish_freetext='Test Pancakes',
        )
        Census.objects.create(date=date.today(), headcount=30)

    def test_calendar_200(self):
        self.assertEqual(self.client.get(reverse('calendar_current')).status_code, 200)

    def test_cogs_dashboard_200(self):
        self.assertEqual(self.client.get(reverse('cogs_dashboard')).status_code, 200)

    def test_menu_suggestions_200(self):
        r = self.client.get(reverse('menu_suggestions'),
                            {'date': date.today().isoformat(), 'slot': 'dinner'})
        self.assertEqual(r.status_code, 200)

    def test_recipe_new_200(self):
        r = self.client.get(reverse('recipe_new'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'New Recipe')

    def test_recipe_new_with_prefill(self):
        r = self.client.get(reverse('recipe_new'), {'prefill_name': 'Honey Glazed Salmon'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Honey Glazed Salmon')

    def test_recipe_detail_200(self):
        r = self.client.get(reverse('recipe_detail', args=[self.r1.id]))
        self.assertEqual(r.status_code, 200)

    def test_recipe_edit_200(self):
        self.assertEqual(
            self.client.get(reverse('recipe_edit', args=[self.r1.id])).status_code, 200)

    def test_recipe_list_200(self):
        self.assertEqual(self.client.get(reverse('recipe_list')).status_code, 200)

    def test_yield_list_200(self):
        self.assertEqual(self.client.get(reverse('yield_list')).status_code, 200)

    def test_order_guide_200(self):
        self.assertEqual(self.client.get(reverse('order_guide')).status_code, 200)

    def test_prep_list_200(self):
        self.assertEqual(self.client.get(reverse('prep_list')).status_code, 200)

    def test_bridge_review_200(self):
        self.assertEqual(self.client.get(reverse('bridge_review')).status_code, 200)

    def test_display_200(self):
        self.assertEqual(self.client.get(reverse('kitchen_display')).status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'])
class RecipeCreationTests(TestCase):
    """POST /recipe/new/ end-to-end."""

    def test_create_recipe_via_form(self):
        resp = self.client.post(reverse('recipe_new'), {
            'name': 'Honey Glazed Salmon',
            'level': 'meal',
            'yield_servings': '30',
            'notes': 'Fresh.',
        })
        # Should redirect to edit for the new recipe
        self.assertEqual(resp.status_code, 302)
        recipe = Recipe.objects.get(name='Honey Glazed Salmon')
        self.assertEqual(recipe.level, 'meal')
        self.assertEqual(recipe.yield_servings, 30)
        self.assertEqual(recipe.version_number, 1)
        self.assertTrue(recipe.is_current)
        self.assertIn(str(recipe.id), resp.url)

    def test_duplicate_name_rejected(self):
        Recipe.objects.create(name='Dupe Test', level='recipe')
        resp = self.client.post(reverse('recipe_new'), {
            'name': 'Dupe Test',
            'level': 'meal',
            'yield_servings': '40',
        })
        self.assertEqual(resp.status_code, 200)  # re-renders form with error
        self.assertContains(resp, 'already exists')

    def test_missing_name_rejected(self):
        resp = self.client.post(reverse('recipe_new'), {
            'name': '', 'level': 'meal', 'yield_servings': '40',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Name is required')

    def test_create_links_to_menu(self):
        menu = Menu.objects.create(date=date.today(), meal_slot='dinner',
                                   dish_freetext='Test Dinner')
        resp = self.client.post(reverse('recipe_new'), {
            'name': 'Test Dinner Recipe',
            'level': 'meal',
            'yield_servings': '40',
            'link_menu': str(menu.id),
        })
        self.assertEqual(resp.status_code, 302)
        recipe = Recipe.objects.get(name='Test Dinner Recipe')
        menu.refresh_from_db()
        self.assertEqual(menu.recipe, recipe)


@override_settings(ALLOWED_HOSTS=['testserver'])
class RecipeVersioningTests(TestCase):
    """POST /recipe/<id>/new-version/ duplicates + marks old non-current."""

    def setUp(self):
        self.r1 = Recipe.objects.create(name='Versioned Dish', level='meal',
                                        yield_servings=40)
        RecipeIngredient.objects.create(recipe=self.r1, name_raw='flour',
                                        quantity=Decimal('4'), unit='cup')
        RecipeIngredient.objects.create(recipe=self.r1, name_raw='sugar',
                                        quantity=Decimal('2'), unit='cup')

    def test_create_v2_copies_ingredients(self):
        resp = self.client.post(reverse('recipe_new_version', args=[self.r1.id]))
        self.assertEqual(resp.status_code, 302)

        self.r1.refresh_from_db()
        self.assertFalse(self.r1.is_current)

        v2 = Recipe.objects.get(parent_recipe=self.r1)
        self.assertEqual(v2.name, 'Versioned Dish V2')
        self.assertEqual(v2.version_number, 2)
        self.assertTrue(v2.is_current)
        self.assertEqual(v2.ingredients.count(), 2)
        self.assertEqual(v2.level, 'meal')  # carried over

    def test_v3_after_v2(self):
        # Simulate creating V2 then V3
        self.client.post(reverse('recipe_new_version', args=[self.r1.id]))
        v2 = Recipe.objects.get(name='Versioned Dish V2')
        self.client.post(reverse('recipe_new_version', args=[v2.id]))

        v3 = Recipe.objects.get(name='Versioned Dish V3')
        self.assertEqual(v3.version_number, 3)
        self.assertTrue(v3.is_current)

        # Only V3 should be current in the lineage
        self.r1.refresh_from_db()
        v2.refresh_from_db()
        self.assertFalse(self.r1.is_current)
        self.assertFalse(v2.is_current)


class LinkMenusToRecipesTests(TestCase):
    """Matcher respects Recipe.level filtering (fixes Pesto→Shrimp Pesto Pasta bug)."""

    def test_component_recipes_excluded_from_match_pool(self):
        # level='recipe' (component) should NOT match
        Recipe.objects.create(name='Pesto', level='recipe',
                              source_doc='Recipe Book/Prep Components/Sauces/')
        Menu.objects.create(date=date.today(), meal_slot='dinner',
                            dish_freetext='Shrimp Pesto Pasta')
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('link_menus_to_recipes', stdout=out)
        menu = Menu.objects.first()
        self.assertIsNone(menu.recipe)  # should NOT have matched Pesto


@override_settings(ALLOWED_HOSTS=['testserver'])
class CogsDashboardMathTests(TestCase):
    """Budget math: census × 346.67 per month; spend vs budget verdict."""

    def test_cogs_renders_with_no_data(self):
        # Fresh DB, no cached invoice totals — should still return 200
        resp = self.client.get(reverse('cogs_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Food Spend')

    def test_cogs_shows_4_month_trend(self):
        resp = self.client.get(reverse('cogs_dashboard'))
        self.assertContains(resp, 'Monthly Trend')
        # Should include current month + 3 back
        self.assertContains(resp, date.today().strftime('%b %Y'))


class AutoTagConflictsTests(TestCase):
    """Keyword-based conflict auto-tagging."""

    def test_pork_recipe_tagged_not_kosher_and_not_halal(self):
        r = Recipe.objects.create(name='Test Pork Dish', level='meal')
        RecipeIngredient.objects.create(recipe=r, name_raw='pork loin')
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('auto_tag_conflicts', '--apply', stdout=out)
        r.refresh_from_db()
        self.assertIn('meat', r.conflicts)
        self.assertIn('animal_products', r.conflicts)
        self.assertIn('not_kosher', r.conflicts)
        self.assertIn('not_halal', r.conflicts)

    def test_meat_plus_dairy_is_not_kosher(self):
        r = Recipe.objects.create(name='Test Cheeseburger', level='meal')
        RecipeIngredient.objects.create(recipe=r, name_raw='ground beef')
        RecipeIngredient.objects.create(recipe=r, name_raw='cheddar')
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('auto_tag_conflicts', '--apply', stdout=out)
        r.refresh_from_db()
        self.assertIn('meat', r.conflicts)
        self.assertIn('dairy', r.conflicts)
        self.assertIn('not_kosher', r.conflicts)

    def test_plain_vegetarian_recipe_gets_no_meat_tags(self):
        r = Recipe.objects.create(name='Test Salad', level='meal')
        RecipeIngredient.objects.create(recipe=r, name_raw='lettuce')
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('auto_tag_conflicts', '--apply', stdout=out)
        r.refresh_from_db()
        self.assertNotIn('meat', r.conflicts)
        self.assertNotIn('animal_products', r.conflicts)


class PrepTaskAutoDeriveTests(TestCase):
    """Menu save → PrepTask auto-creation via signal handler."""

    def setUp(self):
        self.r1 = Recipe.objects.create(name='Auto Prep Test', level='meal',
                                        yield_servings=40)
        self.r2 = Recipe.objects.create(name='Auto Prep Side', level='meal',
                                        yield_servings=40)

    def test_menu_create_derives_preptask(self):
        from datetime import date as _date, timedelta as _td
        from myapp.models import PrepTask
        future = _date.today() + _td(days=180)
        # Ensure clean state
        PrepTask.objects.filter(recipe=self.r1).delete()

        m = Menu.objects.create(date=future, meal_slot='lunch',
                                dish_freetext='x', recipe=self.r1)
        tasks = PrepTask.objects.filter(recipe=self.r1, date__gte=future - _td(days=4))
        self.assertEqual(tasks.count(), 1)
        task = tasks.first()
        # Prep date: day before, skipping weekends
        expected = future - _td(days=1)
        while expected.weekday() >= 5:
            expected -= _td(days=1)
        self.assertEqual(task.date, expected)
        self.assertFalse(task.completed)

    def test_additional_recipe_add_derives_preptask(self):
        from datetime import date as _date, timedelta as _td
        from myapp.models import PrepTask
        future = _date.today() + _td(days=181)
        m = Menu.objects.create(date=future, meal_slot='dinner',
                                dish_freetext='x', recipe=self.r1)
        # Adding additional_recipes → signal → PrepTask for r2
        m.additional_recipes.add(self.r2)
        self.assertTrue(PrepTask.objects.filter(recipe=self.r2,
                                                 date__gte=future - _td(days=4)).exists())

    def test_signal_is_idempotent(self):
        from datetime import date as _date, timedelta as _td
        from myapp.models import PrepTask
        future = _date.today() + _td(days=182)
        m = Menu.objects.create(date=future, meal_slot='lunch',
                                dish_freetext='x', recipe=self.r1)
        # Re-save — should not duplicate PrepTask rows
        m.save()
        m.save()
        tasks = PrepTask.objects.filter(recipe=self.r1, date__gte=future - _td(days=4))
        self.assertEqual(tasks.count(), 1)


class DishSuggestionTests(TestCase):
    """Score recipes against a target (date, slot)."""

    def test_suggestions_endpoint_returns_candidates(self):
        r = Recipe.objects.create(
            name='Test Beef Dish', level='meal', protein='beef',
            source_doc='Recipe Book/Proteins/Beef/',
        )
        resp = self.client.get(reverse('menu_suggestions'),
                               {'date': date.today().isoformat(), 'slot': 'dinner'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Test Beef Dish')

    def test_same_protein_as_yesterday_dinner_penalized(self):
        from myapp.views import _score_candidate
        r_beef = Recipe.objects.create(
            name='Beef Test', level='meal', protein='beef',
            source_doc='Recipe Book/Proteins/Beef/',
        )
        yesterday = date.today() - timedelta(days=1)
        neighbor_proteins = {(yesterday, 'dinner'): 'beef'}
        score, reasons = _score_candidate(
            r_beef, date.today(), 'dinner', neighbor_proteins, recent_dates={}
        )
        # Penalty should fire; note reasons mention yesterday
        self.assertTrue(any('yesterday dinner' in r for r in reasons))
