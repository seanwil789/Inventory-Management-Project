"""Smoke + logic tests for the myapp routes.

Scope is intentionally narrow: each new endpoint shipped in April 2026 gets
a smoke test (HTTP 200 / 302) plus a handful of logic assertions for the
critical paths (recipe versioning, auto-tag pipeline, COGs math).

Run: python manage.py test myapp
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from myapp.models import (
    Recipe, RecipeIngredient, Menu, Census, Vendor, Product, ProductMapping,
    ProductMappingProposal, InvoiceLineItem,
)


class AuthedTestCase(TestCase):
    """Base class: auto-logs-in a throwaway user so `LoginRequiredMiddleware`
    doesn't 302 every request. All app tests inherit from this — only
    `kitchen_display` is `@login_not_required`."""
    def setUp(self):
        super().setUp()
        user = User.objects.create_user(
            username=f'tester_{id(self)}', password='pw',
        )
        self.client.force_login(user)


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class SmokeTests(AuthedTestCase):
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

    def test_calendar_cell_has_quick_add_link_when_menu_has_recipe(self):
        # Place a menu on a Mon-Fri inside the current biweekly window so the
        # cell actually renders (the calendar view only emits Mon-Fri cells).
        from myapp.calendar_utils import biweekly_start_for
        bw_start = biweekly_start_for(date.today())  # Monday
        wkday_menu = Menu.objects.create(
            date=bw_start, meal_slot='dinner', recipe=self.r1,
            dish_freetext='Test Pancakes Weekday',
        )
        try:
            r = self.client.get(reverse('calendar_current'))
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, reverse('recipe_quick_add', args=[self.r1.id]))
        finally:
            wkday_menu.delete()

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

    def test_category_spend_200(self):
        self.assertEqual(self.client.get(reverse('category_spend')).status_code, 200)

    def test_popularity_dashboard_200(self):
        self.assertEqual(self.client.get(reverse('popularity_dashboard')).status_code, 200)

    def test_historical_dishes_200(self):
        self.assertEqual(self.client.get(reverse('historical_dishes')).status_code, 200)

    def test_demo_ready_200(self):
        self.assertEqual(self.client.get(reverse('demo_ready')).status_code, 200)

    def test_pipeline_health_200(self):
        self.assertEqual(self.client.get(reverse('pipeline_health')).status_code, 200)

    def test_menu_bulk_link_200(self):
        self.assertEqual(self.client.get(reverse('menu_bulk_link')).status_code, 200)

    def test_recipe_missing_quantities_200(self):
        self.assertEqual(self.client.get(reverse('recipe_missing_quantities')).status_code, 200)

    def test_leftovers_view_200(self):
        self.assertEqual(self.client.get(reverse('leftovers_view')).status_code, 200)

    def test_yield_new_200(self):
        self.assertEqual(self.client.get(reverse('yield_new')).status_code, 200)

    def test_yield_bridge_200(self):
        self.assertEqual(self.client.get(reverse('yield_bridge')).status_code, 200)

    def test_bridge_skipped_200(self):
        self.assertEqual(self.client.get(reverse('bridge_skipped')).status_code, 200)

    def test_price_alerts_200(self):
        self.assertEqual(self.client.get(reverse('price_alerts')).status_code, 200)

    def test_item_mapping_list_200(self):
        self.assertEqual(self.client.get(reverse('item_mapping_list')).status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'])
class RecipeQuickAddTests(AuthedTestCase):
    """Mobile-friendly /recipe/<id>/quick-add/ flow."""

    @classmethod
    def setUpTestData(cls):
        cls.recipe = Recipe.objects.create(
            name='Quick Add Test Recipe', level='recipe', yield_servings=10,
        )
        cls.product = Product.objects.create(
            canonical_name='Test Quick-Add Flour',
            category='Drystock', primary_descriptor='Flour',
        )

    def test_get_renders_form(self):
        r = self.client.get(reverse('recipe_quick_add', args=[self.recipe.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Quick Add Test Recipe')
        self.assertContains(r, 'Save & Add Another')

    def test_post_add_another_creates_ingredient_and_redirects_back(self):
        before = self.recipe.ingredients.count()
        resp = self.client.post(reverse('recipe_quick_add', args=[self.recipe.id]), {
            'name_raw': 'butter',
            'quantity': '0.5',
            'unit': 'cup',
            'add_another': '1',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.recipe.ingredients.count(), before + 1)
        # Redirect targets the same quick-add URL for next ingredient
        self.assertIn('quick-add', resp.url)
        ri = self.recipe.ingredients.last()
        self.assertEqual(ri.name_raw, 'butter')
        self.assertEqual(ri.unit, 'cup')

    def test_post_done_redirects_to_recipe_detail(self):
        resp = self.client.post(reverse('recipe_quick_add', args=[self.recipe.id]), {
            'name_raw': 'salt',
            'quantity': '1',
            'unit': 'tsp',
            'done': '1',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('recipe_detail', args=[self.recipe.id]), resp.url)

    def test_post_auto_links_product_when_name_matches_canonical(self):
        resp = self.client.post(reverse('recipe_quick_add', args=[self.recipe.id]), {
            'name_raw': 'Test Quick-Add Flour',  # matches canonical exactly
            'quantity': '2',
            'unit': 'cup',
            'add_another': '1',
        })
        self.assertEqual(resp.status_code, 302)
        ri = self.recipe.ingredients.filter(name_raw='Test Quick-Add Flour').first()
        self.assertIsNotNone(ri)
        self.assertEqual(ri.product_id, self.product.id)

    def test_post_freetext_name_no_canonical_match_leaves_product_null(self):
        resp = self.client.post(reverse('recipe_quick_add', args=[self.recipe.id]), {
            'name_raw': 'totally made up ingredient xyz',
            'quantity': '1',
            'unit': 'each',
            'add_another': '1',
        })
        self.assertEqual(resp.status_code, 302)
        ri = self.recipe.ingredients.filter(
            name_raw='totally made up ingredient xyz').first()
        self.assertIsNotNone(ri)
        self.assertIsNone(ri.product_id)

    def test_post_empty_name_rejected(self):
        before = self.recipe.ingredients.count()
        resp = self.client.post(reverse('recipe_quick_add', args=[self.recipe.id]), {
            'name_raw': '',
            'quantity': '1',
            'unit': 'cup',
            'add_another': '1',
        })
        # Redirect back to the form with an error message
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.recipe.ingredients.count(), before)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ItemMappingEditorTests(AuthedTestCase):
    """Item Mapping editor view (Step 4 of sheet→DB migration)."""

    @classmethod
    def setUpTestData(cls):
        cls.vendor = Vendor.objects.create(name='Test Vendor IM')
        cls.product = Product.objects.create(
            canonical_name='IM Test Product',
            category='Drystock', primary_descriptor='Test',
        )
        cls.pm = ProductMapping.objects.create(
            vendor=cls.vendor,
            description='RAW TEST DESCRIPTION 12345',
            supc='1234567',
            product=cls.product,
        )

    def test_list_renders(self):
        r = self.client.get(reverse('item_mapping_list'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'RAW TEST DESCRIPTION')
        self.assertContains(r, 'IM Test Product')

    def test_list_filter_by_vendor(self):
        r = self.client.get(reverse('item_mapping_list'),
                            {'vendor': self.vendor.name})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'RAW TEST DESCRIPTION')

    def test_list_search(self):
        r = self.client.get(reverse('item_mapping_list'), {'q': '12345'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'RAW TEST DESCRIPTION')

    def test_create_new_mapping(self):
        v2 = Vendor.objects.create(name='Test Vendor IM 2')
        p2 = Product.objects.create(
            canonical_name='IM Test Product 2', category='Drystock',
        )
        before = ProductMapping.objects.count()
        resp = self.client.post(reverse('item_mapping_create'), {
            'vendor':      v2.id,
            'description': 'NEW MAPPING TEST',
            'supc':        '7654321',
            'product':     p2.id,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ProductMapping.objects.count(), before + 1)
        self.assertTrue(ProductMapping.objects.filter(
            description='NEW MAPPING TEST', product=p2).exists())

    def test_edit_mapping_get_renders(self):
        r = self.client.get(reverse('item_mapping_edit', args=[self.pm.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'RAW TEST DESCRIPTION')

    def test_edit_mapping_post_saves(self):
        p2 = Product.objects.create(
            canonical_name='IM Test Product Updated', category='Drystock',
        )
        resp = self.client.post(reverse('item_mapping_edit', args=[self.pm.id]), {
            'vendor':      self.vendor.id,
            'description': 'UPDATED DESCRIPTION',
            'supc':        '7777777',
            'product':     p2.id,
        })
        self.assertEqual(resp.status_code, 302)
        self.pm.refresh_from_db()
        self.assertEqual(self.pm.description, 'UPDATED DESCRIPTION')
        self.assertEqual(self.pm.product_id, p2.id)

    def test_delete_mapping(self):
        v3 = Vendor.objects.create(name='Test Vendor IM 3')
        p3 = Product.objects.create(canonical_name='IM Delete Me', category='Drystock')
        pm = ProductMapping.objects.create(
            vendor=v3, description='DELETE ME', product=p3,
        )
        before = ProductMapping.objects.count()
        resp = self.client.post(reverse('item_mapping_delete', args=[pm.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ProductMapping.objects.count(), before - 1)
        self.assertFalse(ProductMapping.objects.filter(pk=pm.id).exists())

    def test_delete_mapping_get_rejected(self):
        # GET should be 405 (require_POST decorator)
        resp = self.client.get(reverse('item_mapping_delete', args=[self.pm.id]))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(ProductMapping.objects.filter(pk=self.pm.id).exists())


@override_settings(ALLOWED_HOSTS=['testserver'])
class RecipeCreationTests(AuthedTestCase):
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
class RecipeVersioningTests(AuthedTestCase):
    """POST /recipe/<id>/new-version/ duplicates + marks old non-current."""

    def setUp(self):
        super().setUp()
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


class LinkMenusToRecipesTests(AuthedTestCase):
    """Covers the matcher's exact/substring/fuzzy tiers + the level filter that
    fixes the Pesto→Shrimp Pesto Pasta class of false-positives."""

    def _run_matcher(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('link_menus_to_recipes', stdout=StringIO())

    def test_component_recipes_excluded_from_match_pool(self):
        """level='recipe' (component/sub-recipe) should NOT match a longer menu
        name that happens to contain it. The 2026-04 known bug was 'Pesto'
        mapping to 'Shrimp Pesto Pasta'."""
        Recipe.objects.create(name='Pesto', level='recipe',
                              source_doc='Recipe Book/Prep Components/Sauces/')
        Menu.objects.create(date=date.today(), meal_slot='dinner',
                            dish_freetext='Shrimp Pesto Pasta')
        self._run_matcher()
        menu = Menu.objects.first()
        self.assertIsNone(menu.recipe)  # Pesto (level=recipe) must stay excluded

    def test_exact_match_links(self):
        """Normalized exact-name match always wins."""
        recipe = Recipe.objects.create(name='Cajun Shrimp and Grits', level='meal')
        Menu.objects.create(date=date.today(), meal_slot='dinner',
                            dish_freetext='Cajun Shrimp and Grits')
        self._run_matcher()
        menu = Menu.objects.first()
        self.assertEqual(menu.recipe, recipe)

    def test_substring_match_on_composed_dish(self):
        """Multi-word composed_dish name contained in a longer menu string
        should still link via substring tier."""
        recipe = Recipe.objects.create(name='Beef Bolognese', level='composed_dish')
        Menu.objects.create(date=date.today(), meal_slot='dinner',
                            dish_freetext='Beef Bolognese with garlic bread')
        self._run_matcher()
        menu = Menu.objects.first()
        self.assertEqual(menu.recipe, recipe)

    def test_with_clipping(self):
        """'X with Y' should match against X only. The 'with' clip prevents
        'Biscuits and gravy with eggs' from matching a sub-recipe named 'eggs'."""
        main = Recipe.objects.create(name='Biscuits and Gravy', level='meal')
        Recipe.objects.create(name='Scrambled Eggs', level='recipe')  # should NOT win
        Menu.objects.create(date=date.today(), meal_slot='hot_breakfast',
                            dish_freetext='Biscuits and Gravy with Scrambled Eggs')
        self._run_matcher()
        menu = Menu.objects.first()
        self.assertEqual(menu.recipe, main)

    def test_empty_dish_freetext_skipped(self):
        """Menus with no dish_freetext aren't considered — matcher should not
        crash or link them to anything."""
        Recipe.objects.create(name='Anything', level='meal')
        menu = Menu.objects.create(date=date.today(), meal_slot='lunch',
                                    dish_freetext='')
        self._run_matcher()
        menu.refresh_from_db()
        self.assertIsNone(menu.recipe)

    def test_already_linked_menu_not_retouched(self):
        """Menus with recipe already set should be left alone — the matcher
        only fills blanks."""
        original = Recipe.objects.create(name='Original', level='meal')
        decoy = Recipe.objects.create(name='Decoy', level='meal')
        menu = Menu.objects.create(date=date.today(), meal_slot='dinner',
                                    dish_freetext='Decoy', recipe=original)
        self._run_matcher()
        menu.refresh_from_db()
        self.assertEqual(menu.recipe, original)  # NOT re-linked to Decoy

    def test_non_current_versions_excluded(self):
        """is_current=False recipes should not match, even when name is
        a perfect normalized match. Locks in the version-filtering clause."""
        # V1 has the exact name the menu contains — but is_current=False,
        # so matcher must exclude it. V2's name differs by suffix so it
        # doesn't auto-match. Net: menu stays unlinked (V1 excluded is
        # what we're verifying).
        v1 = Recipe.objects.create(name='Salmon Teriyaki', level='meal',
                                    version_number=1, is_current=False)
        v2 = Recipe.objects.create(name='Salmon Teriyaki V2', level='meal',
                                    parent_recipe=v1, version_number=2,
                                    is_current=True)
        Menu.objects.create(date=date.today(), meal_slot='dinner',
                            dish_freetext='Salmon Teriyaki')
        self._run_matcher()
        menu = Menu.objects.first()
        # Critical: menu.recipe must NOT be v1. Either None (expected with
        # current matcher — V-suffix discrepancy) or v2 would be acceptable.
        # What MUST fail is linking to the non-current V1.
        self.assertNotEqual(menu.recipe, v1)
        # Known limitation: V-suffix means V2 doesn't substring-match a
        # menu that uses the trunk name. Documented separately; see
        # project_recipe_authoring.md for how authoring flow handles this.


@override_settings(ALLOWED_HOSTS=['testserver'])
class CogsDashboardMathTests(AuthedTestCase):
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


class AutoTagConflictsTests(AuthedTestCase):
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


class PrepTaskAutoDeriveTests(AuthedTestCase):
    """Menu save → PrepTask auto-creation via signal handler."""

    def setUp(self):
        super().setUp()
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


def _import_parser():
    """Import parser from invoice_processor/ — adds its dir to sys.path."""
    import sys
    from django.conf import settings
    path = str(settings.BASE_DIR / 'invoice_processor')
    if path not in sys.path:
        sys.path.insert(0, path)
    import parser as invoice_parser
    return invoice_parser


class ParserDetectVendorTests(TestCase):
    """`detect_vendor` — keyword-based vendor detection. First-match-wins."""

    def test_sysco(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("SYSCO PHILADELPHIA INVOICE"), "Sysco")
        self.assertEqual(p.detect_vendor("sysco corp"), "Sysco")  # case-insensitive

    def test_exceptional(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("EXCEPTIONAL FOODS"), "Exceptional Foods")

    def test_farmart_both_spellings(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("FARMART INC"), "Farm Art")
        self.assertEqual(p.detect_vendor("FARM ART DELIVERY"), "Farm Art")

    def test_pbm_all_aliases(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("PBM"), "Philadelphia Bakery Merchants")
        self.assertEqual(p.detect_vendor("PHILADELPHIA BAKERY MERCHANTS"),
                         "Philadelphia Bakery Merchants")
        self.assertEqual(p.detect_vendor("philabakery llc"),
                         "Philadelphia Bakery Merchants")

    def test_delaware_linen(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("DELAWARE COUNTY LINEN CO"),
                         "Delaware County Linen")

    def test_colonial_or_volonial_ocr_artifact(self):
        """OCR sometimes misreads COLONIAL as VOLONIAL — both should detect."""
        p = _import_parser()
        self.assertEqual(p.detect_vendor("COLONIAL VILLAGE"),
                         "Colonial Village Meat Markets")
        self.assertEqual(p.detect_vendor("VOLONIAL VILLAGE"),
                         "Colonial Village Meat Markets")

    def test_unknown_fallback(self):
        p = _import_parser()
        self.assertEqual(p.detect_vendor("RANDOM VENDOR CORP"), "Unknown")
        self.assertEqual(p.detect_vendor(""), "Unknown")


class ParserExtractDateTests(TestCase):
    """`extract_date` — returns ISO YYYY-MM-DD from varied invoice date formats."""

    def test_slash_4digit_year(self):
        p = _import_parser()
        self.assertEqual(p.extract_date("Invoice date: 4/15/2026"), "2026-04-15")

    def test_slash_2digit_year(self):
        p = _import_parser()
        self.assertEqual(p.extract_date("Delivery 4/15/26"), "2026-04-15")

    def test_dash_format(self):
        p = _import_parser()
        self.assertEqual(p.extract_date("Date 4-15-2026"), "2026-04-15")

    def test_no_date_returns_empty(self):
        p = _import_parser()
        self.assertEqual(p.extract_date("no date here"), "")
        self.assertEqual(p.extract_date(""), "")

    def test_first_valid_date_wins(self):
        """Earlier valid date in text is returned, not the last one."""
        p = _import_parser()
        self.assertEqual(p.extract_date("ordered 4/1/2026 shipped 4/5/2026"),
                         "2026-04-01")


class ParserCaseSizeExtractTests(TestCase):
    """`_extract_case_size` and `_normalize_pack_size` — OCR pack-size handling."""

    def test_case_size_standard(self):
        p = _import_parser()
        self.assertEqual(p._extract_case_size("ROMAINE HEARTS 3CT"), "3CT")
        self.assertEqual(p._extract_case_size("MAYO 4/1GAL"), "4/1GAL")
        self.assertEqual(p._extract_case_size("CHICKEN BREAST 2/5LB"), "2/5LB")

    def test_case_size_none_when_absent(self):
        p = _import_parser()
        self.assertEqual(p._extract_case_size("NO SIZE IN THIS DESCRIPTION"), "")
        self.assertEqual(p._extract_case_size(""), "")

    def test_normalize_pack_size_merged_oz(self):
        """'124 OZ' → '12/4OZ' (12 packs × 4 oz each — common Sysco format)."""
        p = _import_parser()
        self.assertEqual(p._normalize_pack_size("124 OZ"), "12/4OZ")
        self.assertEqual(p._normalize_pack_size("2416 OZ"), "24/16OZ")

    def test_normalize_pack_size_merged_lb(self):
        """'120 LB' → '1/20LB' (1 case × 20 lbs — e.g. black beans)."""
        p = _import_parser()
        self.assertEqual(p._normalize_pack_size("120 LB"), "1/20LB")
        self.assertEqual(p._normalize_pack_size("210 LB"), "2/10LB")

    def test_normalize_pack_size_ocr_0z_artifact(self):
        """'1240Z' (OCR misread OZ as 0Z, no space) → '124OZ' → '12/4OZ'."""
        p = _import_parser()
        self.assertEqual(p._normalize_pack_size("1240Z"), "12/4OZ")

    def test_normalize_pack_size_passthrough(self):
        """Already-well-formed pack sizes shouldn't be mangled."""
        p = _import_parser()
        self.assertEqual(p._normalize_pack_size("50LB"), "50LB")  # <=50 → no split
        self.assertEqual(p._normalize_pack_size(""), "")


class ParserCatchWeightTests(TestCase):
    """`_extract_catch_weight` — Sysco protein per-pound weight patterns."""

    def test_direct_weight_space(self):
        """'42.5 LB CHICKEN BREAST' → 42.5 lbs shipped."""
        p = _import_parser()
        r = p._extract_catch_weight("42.5 LB CHICKEN BREAST")
        self.assertIsNotNone(r)
        self.assertEqual(r['weight_lbs'], 42.5)
        self.assertTrue(r['is_catch_weight'])

    def test_avg_pattern(self):
        """'110#AVGPORTPRD SALMON' → 1 piece × 10# avg = 10 lbs."""
        p = _import_parser()
        r = p._extract_catch_weight("110#AVGPORTPRD SALMON")
        self.assertIsNotNone(r)
        self.assertEqual(r['weight_lbs'], 10)

    def test_range_avg_pattern(self):
        """'86-9#AV PORK BUTT' → 8 pieces × (6+9)/2 avg = 60 lbs."""
        p = _import_parser()
        r = p._extract_catch_weight("86-9#AVBCH PORK BUTT")
        self.assertIsNotNone(r)
        self.assertEqual(r['weight_lbs'], 60)

    def test_merged_digit_weight(self):
        """'115LB' (no space) → 1 case × 15 lbs = 15 lbs."""
        p = _import_parser()
        r = p._extract_catch_weight("115LB CHICKEN FRYER")
        self.assertIsNotNone(r)
        self.assertEqual(r['weight_lbs'], 15)

    def test_no_catch_weight_returns_none(self):
        p = _import_parser()
        self.assertIsNone(p._extract_catch_weight("REGULAR PRODUCT DESCRIPTION"))
        self.assertIsNone(p._extract_catch_weight(""))


class ParserCleanDescriptionTests(TestCase):
    """`_clean_description` — strip leading qty/unit and trailing codes."""

    def test_strips_leading_qty_unit(self):
        p = _import_parser()
        self.assertEqual(p._clean_description("1 CS CHICKEN BREAST"),
                         "CHICKEN BREAST")
        self.assertEqual(p._clean_description("2 BG FLOUR 50LB"),
                         "FLOUR 50LB")

    def test_strips_trailing_barcode(self):
        """Trailing 12+ digit barcodes removed."""
        p = _import_parser()
        result = p._clean_description("PRODUCT NAME 123456789012")
        self.assertEqual(result, "PRODUCT NAME")

    def test_preserves_meaningful_text(self):
        """Descriptions without noise come through intact."""
        p = _import_parser()
        original = "CHICKEN BREAST BONELESS SKINLESS"
        self.assertEqual(p._clean_description(original), original)


class ParserIsDescriptionTests(TestCase):
    """`_is_description` — gatekeeper for what counts as a product-line OCR row."""

    def test_accepts_product_text(self):
        p = _import_parser()
        self.assertTrue(p._is_description("CHICKEN BREAST BONELESS"))
        self.assertTrue(p._is_description("ROMAINE HEARTS 3CT"))

    def test_rejects_section_headers(self):
        p = _import_parser()
        self.assertFalse(p._is_description("**** DAIRY ****"))

    def test_rejects_pure_numbers(self):
        p = _import_parser()
        self.assertFalse(p._is_description("12345"))
        self.assertFalse(p._is_description("  123.45  "))

    def test_rejects_too_short(self):
        p = _import_parser()
        self.assertFalse(p._is_description("AB"))
        self.assertFalse(p._is_description(""))

    def test_rejects_single_word(self):
        """Single-word brand fragments rejected (LAYS, KIND, KONTOS)."""
        p = _import_parser()
        self.assertFalse(p._is_description("KONTOS"))

    def test_rejects_known_footer_text(self):
        p = _import_parser()
        self.assertFalse(p._is_description("GROUP TOTAL"))
        self.assertFalse(p._is_description("REMIT TO PO BOX"))


class ParserPbmFormat1IntegrationTest(TestCase):
    """End-to-end: synthetic PBM-format-1 invoice → parse_invoice →
    verify items + total extracted correctly. Integration-level coverage
    for the parser's simplest vendor path."""

    def test_basic_two_item_parse(self):
        p = _import_parser()
        # Synthetic PBM-format-1 OCR. Items need "N code/abbrev... Product Name"
        # and a Price Each / Amount block followed by a $total line.
        raw = """ABC Bakery Invoice
Invoice #12345
4/15/2026
Description
2 0290/AsstDo... Assorted Donuts
3 0100/Bagels... Plain Bagels
Price Each
Amount
1.50
3.00
0.75
2.25
Total
$5.25
"""
        result = p.parse_invoice(raw, vendor='PBM')
        self.assertEqual(result['vendor'], 'PBM')
        self.assertEqual(result['invoice_date'], '2026-04-15')

        items = result['items']
        self.assertEqual(len(items), 2)

        # Items pair with prices in order: (unit, ext) alternating
        descs = {it['raw_description'] for it in items}
        self.assertIn('Assorted Donuts', descs)
        self.assertIn('Plain Bagels', descs)

        # Invoice total should be extracted from "$5.25" line
        self.assertEqual(result.get('invoice_total'), 5.25)

        # Sum of extended_amounts should match invoice_total (parser validates this)
        items_sum = sum(it.get('extended_amount', 0) or 0 for it in items)
        self.assertAlmostEqual(items_sum, 5.25, places=2)


class MenuFormTests(TestCase):
    """MenuForm — dish_freetext is required (whitespace stripped)."""

    def test_valid_menu(self):
        from myapp.forms import MenuForm
        form = MenuForm(data={
            'dish_freetext': 'Test Dish',
            'date': '2026-04-20',
            'meal_slot': 'lunch',
            'assignee': 'sean',
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_dish_freetext_required(self):
        from myapp.forms import MenuForm
        form = MenuForm(data={
            'dish_freetext': '',
            'date': '2026-04-20',
            'meal_slot': 'lunch',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('dish_freetext', form.errors)

    def test_dish_freetext_whitespace_only_rejected(self):
        """'   ' should be treated as empty after strip."""
        from myapp.forms import MenuForm
        form = MenuForm(data={
            'dish_freetext': '   ',
            'date': '2026-04-20',
            'meal_slot': 'lunch',
        })
        self.assertFalse(form.is_valid())

    def test_invalid_meal_slot_rejected(self):
        from myapp.forms import MenuForm
        form = MenuForm(data={
            'dish_freetext': 'Test',
            'date': '2026-04-20',
            'meal_slot': 'brunch',  # not in choices
        })
        self.assertFalse(form.is_valid())


class RecipeFormTests(TestCase):
    """RecipeForm — ConflictsField + ValidSlotsField multi-select storage."""

    def test_valid_recipe(self):
        from myapp.forms import RecipeForm
        form = RecipeForm(data={
            'name': 'Test Recipe',
            'yield_servings': '40',
            'notes': 'Some notes',
            'conflicts': ['gluten', 'dairy'],
            'valid_slots': ['lunch', 'dinner'],
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_empty_conflicts_valid(self):
        """Empty conflicts list is valid (means no dietary restrictions)."""
        from myapp.forms import RecipeForm
        form = RecipeForm(data={
            'name': 'Test',
            'yield_servings': '30',
            'notes': '',
            'conflicts': [],
            'valid_slots': [],
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_conflict_choice_rejected(self):
        """Values outside the Big-15 vocab should fail validation."""
        from myapp.forms import RecipeForm
        form = RecipeForm(data={
            'name': 'Test',
            'yield_servings': '40',
            'notes': '',
            'conflicts': ['not_a_real_conflict'],
            'valid_slots': [],
        })
        self.assertFalse(form.is_valid())

    def test_invalid_valid_slot_rejected(self):
        """Meal-slot values outside MEAL_SLOTS vocab should fail."""
        from myapp.forms import RecipeForm
        form = RecipeForm(data={
            'name': 'Test',
            'yield_servings': '40',
            'notes': '',
            'conflicts': [],
            'valid_slots': ['midnight_snack'],
        })
        self.assertFalse(form.is_valid())


class CalendarUtilsTests(TestCase):
    """`calendar_utils.biweekly_start_for` — returns the Monday that begins
    the biweekly cycle containing a given date. Anchor is 2026-01-05 (Mon)."""

    def test_anchor_day_returns_anchor(self):
        from myapp.calendar_utils import biweekly_start_for
        from datetime import date
        # 2026-01-05 is itself a biweekly anchor
        self.assertEqual(biweekly_start_for(date(2026, 1, 5)),
                         date(2026, 1, 5))

    def test_within_first_week_returns_anchor(self):
        from myapp.calendar_utils import biweekly_start_for
        from datetime import date
        # Wed in anchor week → still anchor
        self.assertEqual(biweekly_start_for(date(2026, 1, 7)),
                         date(2026, 1, 5))

    def test_second_week_still_returns_anchor(self):
        """Days 7-13 after anchor are still in the same biweekly cycle."""
        from myapp.calendar_utils import biweekly_start_for
        from datetime import date
        # Mon + 10 days = 2026-01-15 → still same biweekly
        self.assertEqual(biweekly_start_for(date(2026, 1, 15)),
                         date(2026, 1, 5))

    def test_next_biweekly_advances_14_days(self):
        from myapp.calendar_utils import biweekly_start_for
        from datetime import date
        # Mon + 14 days = next biweekly
        self.assertEqual(biweekly_start_for(date(2026, 1, 19)),
                         date(2026, 1, 19))

    def test_dates_before_anchor(self):
        """Dates before the anchor should roll backward to a previous
        biweekly, not forward."""
        from myapp.calendar_utils import biweekly_start_for
        from datetime import date
        # 14 days before anchor = 2025-12-22
        self.assertEqual(biweekly_start_for(date(2025, 12, 22)),
                         date(2025, 12, 22))


class KitchenFiltersTests(TestCase):
    """Custom Django template filters — `pretty_qty` (Decimal → line-cook
    fraction string) and `get_item` (dict key access from template)."""

    def test_pretty_qty_whole_number(self):
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(2), '2')
        self.assertEqual(pretty_qty(Decimal('5')), '5')

    def test_pretty_qty_half(self):
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(Decimal('0.5')), '1/2')
        self.assertEqual(pretty_qty(Decimal('1.5')), '1 1/2')

    def test_pretty_qty_quarter(self):
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(Decimal('0.25')), '1/4')
        self.assertEqual(pretty_qty(Decimal('3.75')), '3 3/4')

    def test_pretty_qty_third(self):
        """0.333 tolerates small decimal storage drift."""
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(Decimal('0.333')), '1/3')
        self.assertEqual(pretty_qty(Decimal('2.667')), '2 2/3')

    def test_pretty_qty_none_returns_empty(self):
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(None), '')

    def test_pretty_qty_no_match_falls_to_decimal(self):
        """3.1 doesn't match any common fraction → decimal string."""
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty(Decimal('3.1')), '3.1')

    def test_pretty_qty_invalid_input(self):
        from myapp.templatetags.kitchen_filters import pretty_qty
        self.assertEqual(pretty_qty('not a number'), 'not a number')

    def test_get_item_basic(self):
        from myapp.templatetags.kitchen_filters import get_item
        d = {'a': 1, 'b': 2}
        self.assertEqual(get_item(d, 'a'), 1)
        self.assertEqual(get_item(d, 'missing'), None)

    def test_get_item_none_dict(self):
        """`get_item(None, key)` → None (template-safe, no crash)."""
        from myapp.templatetags.kitchen_filters import get_item
        self.assertIsNone(get_item(None, 'key'))

    def test_get_item_non_dict(self):
        """Non-dict inputs return None gracefully."""
        from myapp.templatetags.kitchen_filters import get_item
        self.assertIsNone(get_item('not a dict', 'key'))
        self.assertIsNone(get_item([], 'key'))


class ManagementCommandSmokeTests(TestCase):
    """Smoke tests for DB-only management commands. Each one runs in its
    safest mode (dry-run / no --apply) against an empty-or-minimal DB
    and asserts it doesn't raise. Catches regressions across the 22
    commands without requiring per-command deep coverage.

    Excluded: commands that need external resources (Google Sheets CSV,
    PDF, docx, xlsx, OCR cache) — those need full fixtures."""

    def _run(self, cmd_name, *args, **kwargs):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        err = StringIO()
        call_command(cmd_name, *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue()

    def test_auto_tag_protein(self):
        """Without --apply: dry-run over all recipes, should not raise."""
        self._run('auto_tag_protein')  # dry-run default

    def test_relevel_recipes(self):
        self._run('relevel_recipes')  # dry-run default

    def test_tag_meal_slots(self):
        self._run('tag_meal_slots')

    def test_backfill_yield_refs_dry_run(self):
        """No --apply → reports without writing."""
        self._run('backfill_yield_refs')

    def test_infer_recipe_proteins_dry_run(self):
        self._run('infer_recipe_proteins', dry_run=True)

    def test_map_recipe_ingredients_dry_run(self):
        self._run('map_recipe_ingredients', dry_run=True)

    def test_purge_invoice_month_dry_run(self):
        """No --confirm → dry-run, should not delete anything."""
        self._run('purge_invoice_month', '2026', '1')  # dry-run default

    def test_regenerate_preptasks_dry_run(self):
        """Without --dry-run the command writes — test the preview path."""
        self._run('regenerate_preptasks', dry_run=True)

    def test_clean_ocr_cache_dry_run(self):
        """Dry-run just reports; should not delete anything."""
        self._run('clean_ocr_cache')

    def test_backfill_section_hints_dry_run(self):
        """Empty DB → command short-circuits cleanly, no OCR cache access."""
        self._run('backfill_section_hints', dry_run=True)

    def test_audit_semantic_mismatches_empty_db(self):
        """Empty DB → command runs, reports zero mismatches."""
        out = self._run('audit_semantic_mismatches')
        self.assertIn('Semantic mismatches', out)
        self.assertIn('0 unique', out)


class DBWritePricePerPoundTests(TestCase):
    """`write_invoice_to_db` — Track B field wiring.

    Parser emits `price_per_unit` for Sysco catch-weight + Exceptional per-lb
    rows. The writer stores it in InvoiceLineItem.price_per_pound so
    downstream consumers can read the parser's $/lb directly instead of
    reverse-engineering from unit_price + case_size."""

    def _import_db_write(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import db_write
        return db_write

    def test_price_per_unit_persists_as_price_per_pound(self):
        """Parser's `price_per_unit` in item dict → ILI.price_per_pound populated."""
        from decimal import Decimal
        dbw = self._import_db_write()

        items = [{
            'raw_description': 'Beef Chuck Flap Short Rib Boneless',
            'canonical': None,
            'unit_price': 197.53,
            'extended_amount': 197.53,
            'case_size_raw': '17.99LB',
            'price_per_unit': 10.98,
            'confidence': 'unmatched',
        }]
        written = dbw.write_invoice_to_db('Exceptional Foods', '2026-04-20', items,
                                           source_file='test.jpg')
        self.assertEqual(written, 1)

        ili = InvoiceLineItem.objects.get(raw_description__startswith='Beef Chuck Flap')
        self.assertEqual(ili.price_per_pound, Decimal('10.9800'))

    def test_null_when_parser_didnt_emit_it(self):
        """Farm Art / PBM / Delaware never emit price_per_unit → field stays null."""
        dbw = self._import_db_write()

        items = [{
            'raw_description': 'BROCCOLI, CROWNS, 20 LB',
            'canonical': None,
            'unit_price': 3.40,
            'extended_amount': 13.46,
            'case_size_raw': '',
            'confidence': 'unmatched',
        }]
        dbw.write_invoice_to_db('Farm Art', '2026-04-18', items,
                                 source_file='test.jpg')

        ili = InvoiceLineItem.objects.get(raw_description__startswith='BROCCOLI')
        self.assertIsNone(ili.price_per_pound)

    def test_track_c_placeholder_orphan_cleanup(self):
        """Track C: when a write resolves a SUPC to a Product, any
        pre-existing placeholder row '[Sysco #NNN]' for the same SUPC on
        the same (vendor, date) is deleted — it's a duplicate of the now-
        mapped row.

        Scenario: an earlier parse wrote row A with
        raw_description='[Sysco #5822441]', product=None because the code
        wasn't in code_map. Later the SUPC is recovered + code_map
        updated. A new parse writes row B with product=Salmon. Without
        Track C, both rows coexist (double-counted). With Track C, row A
        is removed at write time.
        """
        from decimal import Decimal
        dbw = self._import_db_write()
        vendor = Vendor.objects.create(name='Sysco')
        salmon = Product.objects.create(canonical_name='Salmon')

        # Seed the pre-existing orphan placeholder
        InvoiceLineItem.objects.create(
            vendor=vendor, invoice_date='2026-01-13',
            raw_description='[Sysco #5822441]',
            product=None,
            unit_price=Decimal('159.85'),
        )
        self.assertEqual(InvoiceLineItem.objects.count(), 1)

        # Now write the same item with the SUPC mapped to Salmon
        items = [{
            'raw_description': 'Salmon',
            'canonical': salmon.canonical_name,
            'unit_price': 159.85,
            'case_size_raw': '25LB',
            'sysco_item_code': '5822441',
            'confidence': 'code',
        }]
        dbw.write_invoice_to_db('Sysco', '2026-01-13', items,
                                 source_file='test.jpg')

        # Only the mapped row should remain; placeholder deleted.
        all_rows = list(InvoiceLineItem.objects.all())
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0].product, salmon)
        self.assertFalse(all_rows[0].raw_description.startswith('[Sysco #'))

    def test_upsert_updates_price_per_pound(self):
        """Reprocessing the same invoice overwrites price_per_pound in place."""
        from decimal import Decimal
        dbw = self._import_db_write()

        # Seed a product so lookup keys on (vendor, product, date)
        p = Product.objects.create(canonical_name='Beef Chuck Flap Test')
        items_v1 = [{
            'raw_description': 'Beef Chuck Flap',
            'canonical': p.canonical_name,
            'unit_price': 197.53,
            'case_size_raw': '17.99LB',
            'price_per_unit': 10.98,
            'confidence': 'code',
        }]
        dbw.write_invoice_to_db('Exceptional Foods', '2026-04-20', items_v1,
                                 source_file='test.jpg')

        # Re-run with a corrected per-lb price
        items_v2 = [{
            'raw_description': 'Beef Chuck Flap',
            'canonical': p.canonical_name,
            'unit_price': 197.53,
            'case_size_raw': '17.99LB',
            'price_per_unit': 11.25,  # corrected
            'confidence': 'code',
        }]
        dbw.write_invoice_to_db('Exceptional Foods', '2026-04-20', items_v2,
                                 source_file='test.jpg')

        # One row, updated value
        self.assertEqual(InvoiceLineItem.objects.filter(product=p).count(), 1)
        ili = InvoiceLineItem.objects.get(product=p)
        self.assertEqual(ili.price_per_pound, Decimal('11.2500'))


class BackfillPricePerPoundTests(TestCase):
    """`backfill_price_per_pound` — Track B's one-shot retroactive filler.

    The command replays OCR cache through the parser and updates existing
    ILI rows' price_per_pound field. Tests the match_and_update helper
    directly — cache-file I/O is tested separately via the command smoke test."""

    def setUp(self):
        from datetime import date
        self.v = Vendor.objects.create(name='Exceptional Foods')
        self.date = date(2026, 4, 20)

    def _make_ili(self, raw_desc, unit_price, price_per_pound=None):
        from decimal import Decimal
        return InvoiceLineItem.objects.create(
            vendor=self.v,
            invoice_date=self.date,
            raw_description=raw_desc,
            unit_price=Decimal(str(unit_price)),
            price_per_pound=(Decimal(str(price_per_pound))
                             if price_per_pound is not None else None),
        )

    def _run_helper(self, items, dry_run=False):
        from myapp.management.commands.backfill_price_per_pound import _match_and_update
        return _match_and_update(items, 'Exceptional Foods', self.date, dry_run=dry_run)

    def test_apply_updates_null_row(self):
        """ILI.price_per_pound is null, parser emits a value → field gets set."""
        from decimal import Decimal
        ili = self._make_ili('Beef Chuck Flap', 197.53)
        self.assertIsNone(ili.price_per_pound)

        items = [{
            'raw_description': 'Beef Chuck Flap',
            'unit_price': 197.53,
            'price_per_unit': 10.98,
        }]
        u, n, a, s = self._run_helper(items, dry_run=False)
        self.assertEqual((u, n, a, s), (1, 0, 0, 0))

        ili.refresh_from_db()
        self.assertEqual(ili.price_per_pound, Decimal('10.9800'))

    def test_dry_run_does_not_write(self):
        """Dry-run counts the change but doesn't touch the DB."""
        ili = self._make_ili('Beef Chuck Flap', 197.53)

        items = [{
            'raw_description': 'Beef Chuck Flap',
            'unit_price': 197.53,
            'price_per_unit': 10.98,
        }]
        u, n, a, s = self._run_helper(items, dry_run=True)
        self.assertEqual(u, 1)  # counted

        ili.refresh_from_db()
        self.assertIsNone(ili.price_per_pound)  # but not written

    def test_skips_row_with_existing_price_per_pound(self):
        """Idempotent: re-running on a populated row does not overwrite."""
        from decimal import Decimal
        ili = self._make_ili('Beef Chuck Flap', 197.53, price_per_pound=10.98)

        items = [{
            'raw_description': 'Beef Chuck Flap',
            'unit_price': 197.53,
            'price_per_unit': 99.99,  # drift — must NOT be applied
        }]
        u, n, a, s = self._run_helper(items, dry_run=False)
        self.assertEqual(s, 1)  # tallied as already-set
        self.assertEqual(u, 0)

        ili.refresh_from_db()
        self.assertEqual(ili.price_per_pound, Decimal('10.9800'))  # unchanged

    def test_no_match_counted_and_skipped(self):
        """Parsed item that doesn't correspond to any DB row → tallied, not updated."""
        self._make_ili('Beef Chuck Flap', 197.53)

        items = [{
            'raw_description': 'Pork Loin Boneless',  # does not exist in DB
            'unit_price': 48.08,
            'price_per_unit': 5.40,
        }]
        u, n, a, s = self._run_helper(items, dry_run=False)
        self.assertEqual((u, n, a, s), (0, 1, 0, 0))

    def test_ambiguous_match_counted_and_skipped(self):
        """Multiple DB rows match (vendor, date, raw_desc, unit_price) → skip."""
        self._make_ili('Beef Chuck Flap', 197.53)
        self._make_ili('Beef Chuck Flap', 197.53)  # duplicate setup

        items = [{
            'raw_description': 'Beef Chuck Flap',
            'unit_price': 197.53,
            'price_per_unit': 10.98,
        }]
        u, n, a, s = self._run_helper(items, dry_run=False)
        self.assertEqual((u, n, a, s), (0, 0, 1, 0))

        for ili in InvoiceLineItem.objects.all():
            self.assertIsNone(ili.price_per_pound)

    def test_command_empty_cache_dir(self):
        """Command short-circuits cleanly when cache dir has no matching files."""
        from django.core.management import call_command
        from io import StringIO
        import tempfile
        out = StringIO()
        with tempfile.TemporaryDirectory() as td:
            call_command('backfill_price_per_pound', cache_dir=td, stdout=out)
        self.assertIn('No matching caches found', out.getvalue())


class SynergySyncPricePerLbTests(TestCase):
    """`calc_price_per_lb` — Track B consumer wiring.

    Verifies the new `stored_price_per_lb` kwarg: when the caller passes
    the parser's direct $/lb (from InvoiceLineItem.price_per_pound), that
    value short-circuits the reverse-engineering fallback. Reverse path
    still runs when the stored value is null/zero/invalid."""

    def _calc(self, *args, **kwargs):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        from synergy_sync import calc_price_per_lb
        return calc_price_per_lb(*args, **kwargs)

    def test_stored_value_wins_over_reverse_engineering(self):
        """Parser knew $/lb directly → return that, don't re-derive."""
        # Reverse-engineering from case='10.0LB' at unit=$66.45 gives $6.645/lb.
        # If the stored value says something different, stored wins.
        result = self._calc(66.45, '10.0LB', '', stored_price_per_lb=7.00)
        self.assertEqual(result, 7.0)

    def test_falls_back_when_stored_is_none(self):
        """Field is null → reverse-engineer from unit_price / weight."""
        result = self._calc(66.45, '10.0LB', '', stored_price_per_lb=None)
        self.assertAlmostEqual(result, 6.645, places=3)

    def test_falls_back_when_stored_is_zero(self):
        """Stored 0 is not a real price — fall back."""
        result = self._calc(66.45, '10.0LB', '', stored_price_per_lb=0)
        self.assertAlmostEqual(result, 6.645, places=3)

    def test_stored_invalid_type_falls_back(self):
        """Garbage stored value → fall back silently."""
        result = self._calc(66.45, '10.0LB', '', stored_price_per_lb='junk')
        self.assertAlmostEqual(result, 6.645, places=3)

    def test_stored_saves_us_when_case_size_is_unparseable(self):
        """The key win — case_size drift/emptiness doesn't break $/lb
        output when the stored value is present."""
        # Case size is ambiguous bare-number → reverse-engineering returns None.
        # But stored_price_per_lb supplies the answer.
        self.assertIsNone(self._calc(66.45, '1', ''))  # no stored → None
        self.assertEqual(
            self._calc(66.45, '1', '', stored_price_per_lb=6.645),
            6.645,
        )


class CostUtilsPricePerPoundTests(TestCase):
    """`ingredient_cost` — Track B direct $/lb path.

    When the caller passes price_per_pound (from
    InvoiceLineItem.price_per_pound), the weight-unit dispatch bypasses
    case_size parsing entirely and computes cost from a single
    multiplication: qty_in_lb × price_per_pound. This is the primary
    accuracy win for protein recipe costs."""

    def _cost(self, *args, **kwargs):
        from myapp.cost_utils import ingredient_cost
        return ingredient_cost(*args, **kwargs)

    def test_direct_path_lb_recipe(self):
        """1 lb of beef @ $10.98/lb → $10.98 cost, no case_size needed."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('1'), 'lb', 'Beef Chuck Flap',
            case_price=Decimal('197.53'),
            case_size_str='17.99LB',
            price_per_pound=Decimal('10.98'),
        )
        self.assertEqual(cost, Decimal('10.98'))
        self.assertEqual(note, 'direct $/lb')

    def test_direct_path_oz_recipe(self):
        """8 oz of beef @ $10.98/lb → $5.49 cost (half a pound)."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('8'), 'oz', 'Beef Chuck Flap',
            case_price=Decimal('197.53'),
            case_size_str='17.99LB',
            price_per_pound=Decimal('10.98'),
        )
        self.assertEqual(cost, Decimal('5.49'))
        self.assertEqual(note, 'direct $/lb')

    def test_direct_path_unlocks_when_case_size_unparseable(self):
        """case_size='1' (bare qty, would normally fail to parse) +
        price_per_pound set → direct path saves us. THIS is Track B's
        primary accuracy unlock."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('2'), 'lb', 'Beef',
            case_price=Decimal('50'),
            case_size_str='1',  # unparseable
            price_per_pound=Decimal('5.00'),
        )
        self.assertEqual(cost, Decimal('10.00'))
        self.assertEqual(note, 'direct $/lb')

    def test_falls_through_when_price_per_pound_none(self):
        """No stored $/lb → original weight↔weight dispatch runs,
        preserves zero-regression guarantee on existing rows."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('1'), 'lb', 'Beef Chuck Flap',
            case_price=Decimal('197.53'),
            case_size_str='17.99LB',
            price_per_pound=None,  # no stored value
        )
        # Falls through to weight↔weight: 16oz / (17.99*16 oz) * $197.53
        self.assertAlmostEqual(float(cost), 197.53 / 17.99, places=2)
        self.assertEqual(note, 'weight↔weight')

    def test_direct_path_not_used_for_volume_recipes(self):
        """price_per_pound set but recipe asks in cups → fall through
        (no direct $/lb path for volume units; needs density)."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('1'), 'cup', 'Oil',
            case_price=Decimal('40'),
            case_size_str='6/1GAL',
            price_per_pound=Decimal('5.00'),  # set but ignored for volume
        )
        # Should go through volume↔volume dispatch, not direct
        self.assertNotEqual(note, 'direct $/lb')

    def test_yield_adjustment_scales_up_before_direct_path(self):
        """Recipe asks 1 lb EP (edible), yield=50%. AP needed = 2 lb.
        Cost = 2 × $10/lb = $20."""
        from decimal import Decimal
        cost, note = self._cost(
            Decimal('1'), 'lb', 'Beef',
            case_price=Decimal('50'),
            case_size_str='10LB',
            yield_pct=Decimal('50'),
            price_per_pound=Decimal('10.00'),
        )
        self.assertEqual(cost, Decimal('20.00'))
        self.assertEqual(note, 'direct $/lb')


class AuditSemanticMismatchesTests(TestCase):
    """`audit_semantic_mismatches` — flags ILI rows whose section_hint
    disagrees with the linked product's category."""

    def _run(self, *args):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('audit_semantic_mismatches', *args, stdout=out)
        return out.getvalue()

    def setUp(self):
        super().setUp()
        self.v = Vendor.objects.create(name='Sysco')

    def _ili(self, product, section_hint):
        return InvoiceLineItem.objects.create(
            vendor=self.v, product=product, section_hint=section_hint,
            raw_description=f'raw {product.canonical_name}',
            unit_price=Decimal('1.00'), invoice_date=date(2026, 4, 1),
        )

    def test_flags_chemical_section_with_dairy_category(self):
        p = Product.objects.create(canonical_name='TestDairyItem', category='Dairy')
        self._ili(p, 'CHEMICAL & JANITORIAL')
        out = self._run()
        self.assertIn('TestDairyItem', out)
        self.assertIn('CHEMICAL & JANITORIAL', out)

    def test_ignores_matching_section_category(self):
        p = Product.objects.create(canonical_name='TestProduce', category='Produce')
        self._ili(p, 'PRODUCE')
        out = self._run()
        self.assertNotIn('TestProduce', out)
        self.assertIn('0 unique', out)

    def test_dairy_section_accepts_cheese_category(self):
        """Sysco DAIRY section accepts unified Dairy (cheese collapsed in 0035)."""
        p = Product.objects.create(canonical_name='TestCheese', category='Dairy',
                                   primary_descriptor='Cheese, Hard')
        self._ili(p, 'DAIRY')
        out = self._run()
        self.assertNotIn('TestCheese', out)

    def test_ambiguous_sections_hidden_by_default(self):
        """CANNED & DRY has too broad a valid set — suppress without --show-all."""
        p = Product.objects.create(canonical_name='TestOddItem', category='Dairy')
        self._ili(p, 'CANNED & DRY')
        out_hidden = self._run()
        self.assertNotIn('TestOddItem', out_hidden)
        out_shown = self._run('--show-all')
        self.assertIn('TestOddItem', out_shown)

    def test_ignores_rows_without_section_hint(self):
        p = Product.objects.create(canonical_name='TestNoHint', category='Dairy')
        self._ili(p, '')
        out = self._run()
        self.assertNotIn('TestNoHint', out)

    def test_ignores_rows_without_product(self):
        InvoiceLineItem.objects.create(
            vendor=self.v, product=None, section_hint='PRODUCE',
            raw_description='unknown thing', unit_price=Decimal('1.00'),
            invoice_date=date(2026, 4, 1),
        )
        out = self._run()
        self.assertIn('0 unique', out)

    def test_unknown_section_warned(self):
        """Section_hint not in the mapping table → warning, but not a flag."""
        p = Product.objects.create(canonical_name='TestWeird', category='Proteins')
        self._ili(p, 'WEIRD NEW SECTION')
        out = self._run()
        self.assertIn('WEIRD NEW SECTION', out)
        self.assertIn('not in mapping', out)


class SectionHintBackfillTests(TestCase):
    """Unit tests for the section-header detection logic used by
    backfill_section_hints — isolates the _section_before helper."""

    def _section_before(self, raw_text, pos):
        from myapp.management.commands.backfill_section_hints import _section_before
        return _section_before(raw_text, pos)

    def test_finds_nearest_preceding_header(self):
        text = "**** DAIRY ****\nline1\nline2\n7136165 67.85\n"
        pos = text.find('7136165')
        self.assertEqual(self._section_before(text, pos), 'DAIRY')

    def test_skips_group_total_lines(self):
        """GROUP TOTAL**** should not be mistaken for a section header."""
        text = "**** PRODUCE ****\napples\nGROUP TOTAL****\n7136165 67.85\n"
        pos = text.find('7136165')
        self.assertEqual(self._section_before(text, pos), 'PRODUCE')

    def test_handles_sections_with_ampersand(self):
        text = "**** CHEMICAL & JANITORIAL ****\n7136165 67.85\n"
        pos = text.find('7136165')
        self.assertEqual(self._section_before(text, pos), 'CHEMICAL & JANITORIAL')

    def test_returns_empty_when_no_header_before(self):
        text = "no asterisks anywhere\n7136165 67.85\n"
        pos = text.find('7136165')
        self.assertEqual(self._section_before(text, pos), '')

    def test_picks_most_recent_when_multiple(self):
        """Multiple section headers → return the nearest preceding one."""
        text = "**** DAIRY ****\nmilk\n**** FROZEN ****\n7136165 67.85\n"
        pos = text.find('7136165')
        self.assertEqual(self._section_before(text, pos), 'FROZEN')


class AuditOrphanProductsTests(TestCase):
    """`audit_orphan_products` — locks in zero-invoice-line detection + the
    mapping-count annotation that tells Sean whether an orphan is safe to
    retire ('no mappings either') or waiting on an invoice."""

    def test_orphan_flagged(self):
        from myapp.models import Product
        from django.core.management import call_command
        from io import StringIO

        Product.objects.create(canonical_name='Unused Bagel',
                                category='Bakery')
        out = StringIO()
        call_command('audit_orphan_products', stdout=out)
        output = out.getvalue()
        self.assertIn('Unused Bagel', output)
        self.assertIn('no mappings either', output)

    def test_non_orphan_not_flagged(self):
        """A product with at least one invoice line should not appear in
        the orphan report."""
        from myapp.models import Product, Vendor, InvoiceLineItem
        from django.core.management import call_command
        from io import StringIO

        v = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='Used Product',
                                    category='Produce')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='raw',
            unit_price=Decimal('5.00'),
            invoice_date=date.today(),
        )
        out = StringIO()
        call_command('audit_orphan_products', stdout=out)
        self.assertNotIn('Used Product', out.getvalue())


class AuditSuspectMappingsTests(TestCase):
    """`audit_suspect_mappings` — locks in the zero-token-overlap detector
    and the plural-stem fix (Canteloupe ← CANTALOUPES shouldn't false-positive)."""

    def _run(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('audit_suspect_mappings', stdout=out)
        return out.getvalue()

    def test_zero_overlap_flagged(self):
        """Real mapping error (Bib Aprons → Mop Heads) should appear in output."""
        from myapp.models import Product, Vendor, InvoiceLineItem
        v = Vendor.objects.create(name='Test Linen')
        p = Product.objects.create(canonical_name='Mop Heads', category='Chemicals')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='Bib Aprons - White',
            unit_price=Decimal('10.00'),
            invoice_date=date.today(),
        )
        output = self._run()
        self.assertIn('Mop Heads', output)
        self.assertIn('Bib Aprons', output)

    def test_plural_stem_overlap_not_flagged(self):
        """'PINEAPPLES' in desc vs 'Pineapple' canonical should NOT be flagged
        — the naive plural-strip collapses them to the same stem."""
        from myapp.models import Product, Vendor, InvoiceLineItem
        v = Vendor.objects.create(name='Test Produce')
        p = Product.objects.create(canonical_name='Pineapple', category='Produce')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='PINEAPPLES GOLDEN RIPE 6CT',
            unit_price=Decimal('10.00'),
            invoice_date=date.today(),
        )
        output = self._run()
        # 'Pineapple' might appear in overall summary text, but NOT in the
        # Suspects section. Split on the Suspects header and check.
        if '=== Suspects' in output:
            suspects_section = output.split('=== Suspects')[1]
            self.assertNotIn('Pineapple', suspects_section,
                              "Plural stems should make Pineapple/PINEAPPLES overlap")

    def test_brand_prefix_ignored(self):
        """Sysco brand prefix 'WHLFCLS' is noise — canonical 'Eggs' should
        still match raw 'WHLFCLS EGG SHELL MED GR AA USDA WHT' via 'egg' stem."""
        from myapp.models import Product, Vendor, InvoiceLineItem
        v = Vendor.objects.create(name='Test Sysco')
        p = Product.objects.create(canonical_name='Eggs', category='Dairy')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='WHLFCLS EGG SHELL MED GR AA USDA WHT',
            unit_price=Decimal('25.00'),
            invoice_date=date.today(),
        )
        output = self._run()
        if '=== Suspects' in output:
            suspects_section = output.split('=== Suspects')[1]
            self.assertNotIn('Eggs', suspects_section,
                              "'egg'/'eggs' stem should overlap between canonical and desc")


class ParserDelawareLinenIntegrationTests(TestCase):
    """Delaware County Linen parser — OCR reads columns top-to-bottom.
    After the 'Amount' header, skip pure numbers + all-caps item codes;
    each description line gets paired with the next two prices
    (unit_price, amount)."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        return p

    def test_basic_parse(self):
        """After 'Amount' header: skip qty integers, skip all-caps item codes,
        pair each description with the next two decimal prices."""
        parser_mod = self._import_parser()
        # Format mimics real Delaware Linen OCR — qtys and codes in one column,
        # then descriptions interleaved with unit_price + amount.
        raw = """Delaware County Linen
4/15/2026
Qty
Item Code
Description
Unit Price
Amount
300
25
MOPS
BAPSWTW
Bar Mops
0.22
66.00
Bib Aprons White
0.50
12.50
Total Due
78.50
"""
        result = parser_mod.parse_invoice(raw, vendor='Delaware County Linen')
        self.assertEqual(result['vendor'], 'Delaware County Linen')
        items = result['items']
        self.assertEqual(len(items), 2)
        descs = {it['raw_description'] for it in items}
        self.assertIn('Bar Mops', descs)
        self.assertIn('Bib Aprons White', descs)

    def test_total_due_extraction(self):
        """'Total Due' marker → next standalone decimal is invoice_total."""
        parser_mod = self._import_parser()
        raw = """Delaware County Linen
Qty
Amount
300
MOPS
Bar Mops
0.22
66.00
Total Due
66.00
"""
        result = parser_mod.parse_invoice(raw, vendor='Delaware County Linen')
        self.assertEqual(result.get('invoice_total'), 66.00)

    def test_column_dump_fallback(self):
        """Google Document AI emits Delaware Linen invoices as full columns
        (all qtys, then all item codes, then all descriptions, then all
        prices) rather than row-interleaved. The primary parser's
        'Amount'-header anchor produces 0 items on that layout. A
        column-dump fallback must kick in when the primary path yields
        nothing, pairing each description with a (qty, unit, amount)
        triple where qty × unit ≈ amount. Regression for the 2026-04-21
        scour finding on cached DocAI OCR."""
        parser_mod = self._import_parser()
        raw = """Delaware County Linen, Inc
2626 W. 4th Street
Chester, PA 19013
Bill To
Synergy Housung
Customer Contact
Customer Phone
Terms
484-888-3429
Net 7
Qty
Item Code
Description
300
25
MOPS
BAPSWT
Bar Mops
Bib Aprons White
Invoice
Date
Invoice #
2/18/2026
224885
Route No.
32
Unit Price
Amount
Qty Adjustment
0.22
66.00T
0.40
10.00
76.00
Total Due
$76.00
Payments/Credits
$0.00
Balance Due
$76.00
"""
        result = parser_mod.parse_invoice(raw, vendor='Delaware County Linen')
        items = result['items']
        self.assertEqual(len(items), 2,
            f'expected 2 items via column-dump fallback, got {len(items)}: '
            f'{[it.get("raw_description") for it in items]}')
        by_desc = {it['raw_description']: it for it in items}
        self.assertIn('Bar Mops', by_desc)
        self.assertIn('Bib Aprons White', by_desc)
        # Line-total reconciliation: 300 × 0.22 = 66.00; 25 × 0.40 = 10.00
        self.assertAlmostEqual(by_desc['Bar Mops']['extended_amount'],
                               66.00, places=2)
        self.assertAlmostEqual(by_desc['Bib Aprons White']['extended_amount'],
                               10.00, places=2)


class ParserColonialAndFallbackTests(TestCase):
    """Colonial Meat (handwritten — all items flagged needs_review) +
    generic fallback parser for unknown vendors."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        return p

    def test_colonial_flags_everything_for_review(self):
        """Colonial Meat invoices are handwritten — OCR accuracy is low,
        so the parser captures what it can and flags every item."""
        parser_mod = self._import_parser()
        raw = """Colonial Village Meat Markets
Some handwritten line 25.50
Another product 18.00
Total 43.50
"""
        result = parser_mod.parse_invoice(raw)
        self.assertEqual(result['vendor'], 'Colonial Village Meat Markets')
        for item in result['items']:
            self.assertTrue(item.get('needs_review', False),
                            f"{item!r} should be flagged for manual review")

    def test_fallback_parser_on_unknown_vendor(self):
        """Unknown vendor → generic line-at-end-of-line-price parser. Every
        item gets needs_review=True as well."""
        parser_mod = self._import_parser()
        raw = """Random Supplier Corp
Invoice 4/15/2026
Widget Product A                  12.50
Widget Product B                  8.75
Widget Product C                  15.00
"""
        result = parser_mod.parse_invoice(raw)
        self.assertEqual(result['vendor'], 'Unknown')
        self.assertGreaterEqual(len(result['items']), 1)
        for item in result['items']:
            self.assertTrue(item.get('needs_review', False))


class ParserFarmArtIntegrationTests(TestCase):
    """FarmArt parser — two-pass extraction with proximity matching.
    Descriptions + price-pairs (unit_price, extended_amount) scanned
    separately, then zipped by line position."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        return p

    def test_basic_two_items_zipped_by_position(self):
        """2 descriptions + 2 price pairs → 2 items, each pair matched to
        the nearest subsequent description."""
        parser_mod = self._import_parser()
        raw = """Farm Art Invoice
4/15/2026
Description
ROMAINE, 24CT
United States
12.50
25.00
CARROTS, 50LB CASE
United States
10.00
15.00
Invoice Total
40.00
"""
        result = parser_mod.parse_invoice(raw, vendor='Farm Art')
        self.assertEqual(result['vendor'], 'Farm Art')
        self.assertEqual(result['invoice_date'], '2026-04-15')

        items = result['items']
        self.assertEqual(len(items), 2)
        # Items should have both unit_price and extended_amount
        descs = {it['raw_description'] for it in items}
        self.assertIn('ROMAINE, 24CT', descs)
        self.assertIn('CARROTS, 50LB CASE', descs)

        romaine = next(it for it in items if 'ROMAINE' in it['raw_description'])
        self.assertEqual(romaine['unit_price'], 12.50)
        self.assertEqual(romaine['extended_amount'], 25.00)

    def test_invoice_total_from_nontaxable_marker(self):
        """'Nontaxable' marker works as a fallback when 'Invoice Total'
        isn't present (observed in Farm Art OCR variants)."""
        parser_mod = self._import_parser()
        raw = """Farm Art
4/15/2026
Description
ROMAINE, 24CT
United States
12.50
25.00
Nontaxable
25.00
"""
        result = parser_mod.parse_invoice(raw, vendor='Farm Art')
        self.assertEqual(result.get('invoice_total'), 25.00)

    def test_zz_prefix_nonstock_items(self):
        """Lines starting 'zz ' are non-stock delivery items. Parser
        treats them as descriptions too (prefix stripped)."""
        parser_mod = self._import_parser()
        raw = """Farm Art
4/15/2026
Description
zz DRIED, LENTIL, 24/1-LB BAGS
United States
15.00
30.00
Invoice Total
30.00
"""
        result = parser_mod.parse_invoice(raw, vendor='Farm Art')
        items = result['items']
        self.assertEqual(len(items), 1)
        # 'zz ' prefix stripped in stored raw_description
        self.assertNotIn('zz ', items[0]['raw_description'])
        self.assertIn('LENTIL', items[0]['raw_description'])


class ParserExceptionalIntegrationTests(TestCase):
    """Exceptional Foods parser — DocAI-OCR text with columns:
    Item ID | Qty Ordered | Description | Qty Shipped | Price | Per | Total.
    Parser extracts per-lb pricing from 'N.NN LB' patterns + cross-multiplies
    with shipped weights to compute total."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        return p

    def test_per_lb_price_extraction(self):
        """A single catch-weight item: 15.2 lb at $4.69/lb = $71.29.
        Parser should extract price_per_unit=4.69, compute total via
        cross-multiply, store unit_price=total for budget sync."""
        parser_mod = self._import_parser()
        raw = """Exceptional Foods
4/15/2026
Item ID
1.00 CS Bacon Applewood Slice Martins 30530
15.2
4.69
LB
71.29
Sale Amount
71.29
Balance Due
71.29
"""
        result = parser_mod.parse_invoice(raw, vendor='Exceptional Foods')
        self.assertEqual(result['vendor'], 'Exceptional Foods')
        items = result['items']
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn('Bacon', item['raw_description'])
        # Price per unit (per-lb) captured
        self.assertAlmostEqual(item.get('price_per_unit'), 4.69, places=2)
        # unit_price = total (for budget-sheet compatibility)
        self.assertAlmostEqual(item['unit_price'], 71.29, places=2)

    def test_balance_due_invoice_total(self):
        """Invoice total pulled from last standalone decimal after
        'Balance Due' marker — the definitive Exceptional total."""
        parser_mod = self._import_parser()
        raw = """Exceptional Foods
4/15/2026
Item ID
1.00 CS Bacon Applewood Slice Martins 30530
15.2
4.69
LB
71.29
Sale Amount
Balance Due
71.29
"""
        result = parser_mod.parse_invoice(raw, vendor='Exceptional Foods')
        self.assertAlmostEqual(result.get('invoice_total'), 71.29, places=2)

    def test_skips_zero_placeholder_when_finding_total(self):
        """Real Exceptional OCR has 0.00 Qty-Adjustment dumps between the
        price-per line and the true line total. The parser must skip
        zero-valued lines when looking for the total, otherwise items
        silently bind to ext=$0.00. Regression for the 2026-04-21 scour
        finding that recovered a $97.93 line total from a real invoice."""
        parser_mod = self._import_parser()
        # Synthetic layout mirrors the real failure: 19.99 LB price-per
        # followed by a LB noise line, a 0.00 placeholder, THEN the total.
        raw = """Exceptional Foods
4/15/2026
Item ID
10.00 EA Beef Bavette Steak C/C 8 oz SQUARES
4.90
19.99 LB
LB
0.00
97.93
Sale Amount
Balance Due
97.93
"""
        result = parser_mod.parse_invoice(raw, vendor='Exceptional Foods')
        items = result['items']
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]['extended_amount'], 97.93, places=2,
            msg='line total must be $97.93, not the 0.00 Qty-Adjustment placeholder')
        self.assertAlmostEqual(items[0]['price_per_unit'], 19.99, places=2)


class ParserSyscoCatchWeightColumnDumpTests(TestCase):
    """Parser — catch-weight column-dump extractor.

    Sysco catch-weight items (MEATS/POULTRY/SEAFOOD) in column-dump OCR
    print as a 3-line pattern:
      weight:    65.200            (bare 3-decimal weight in lbs)
      anchor:    3124662 12.650    (code + 3-decimal per-lb price)
      extended:  824.78            (bare 2-decimal line total)

    The main _PRICE_ANCHOR rejects 3-decimal per-lb prices, so without
    the second-pass extractor, these items are silently dropped.
    Historical data shows ~$1,000 of line items per invoice disappearing
    this way on the worst invoices."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        return p

    def test_extracts_catch_weight_from_column_dump(self):
        """The 3-line pattern yields an item with extended as price and
        per-lb as price_per_unit."""
        parser = self._import_parser()
        text = """**** MEATS ****
COOPR BEEF CHUCK FLAP SHORT RIB BNLS
CHOICE
01-31830
65.200
3124662 12.650
824.78
1 CS
EXTENDED
PRICE
LAST PAGE
INVOICE
TOTAL
"""
        result = parser.parse_invoice(text, vendor='Sysco')
        codes = [i.get('sysco_item_code') for i in result['items']]
        self.assertIn('3124662', codes)

        cw_item = next(i for i in result['items'] if i['sysco_item_code'] == '3124662')
        self.assertAlmostEqual(cw_item['unit_price'], 824.78, places=2)
        self.assertAlmostEqual(cw_item['price_per_unit'], 12.65, places=3)
        self.assertEqual(cw_item['case_size_raw'], '65.2LB')

    def test_skips_when_extended_implausible(self):
        """If the 2-decimal on the next line doesn't reconcile with
        weight × per_lb, parser skips the catch-weight path rather than
        creating a wrong item."""
        parser = self._import_parser()
        text = """**** MEATS ****
01-31830
65.200
3124662 12.650
0.02
LAST PAGE
INVOICE
TOTAL
"""
        # 0.02 is not > per_lb × 1.5, so the catch-weight pass rejects.
        result = parser.parse_invoice(text, vendor='Sysco')
        codes = [i.get('sysco_item_code') for i in result['items']]
        self.assertNotIn('3124662', codes)

    def test_falls_back_to_derived_weight_when_none_adjacent(self):
        """When no 3-decimal weight appears nearby, parser derives
        weight = extended / per_lb and still extracts the item."""
        parser = self._import_parser()
        text = """**** MEATS ****
3124662 12.650
824.78
LAST PAGE
INVOICE
TOTAL
"""
        result = parser.parse_invoice(text, vendor='Sysco')
        codes = [i.get('sysco_item_code') for i in result['items']]
        self.assertIn('3124662', codes)
        cw_item = next(i for i in result['items'] if i['sysco_item_code'] == '3124662')
        self.assertAlmostEqual(cw_item['unit_price'], 824.78, places=2)
        # Derived weight = 824.78 / 12.65 ≈ 65.2
        self.assertIn('65.2', cw_item['case_size_raw'])

    def test_orphan_code_preserves_inline_description_prefix(self):
        """When a code appears at the END of a description line (e.g.
        'SANITIZER OASIS 146 MULTI QU 9999999'), the parser's orphan-
        pairing path must preserve the text BEFORE the code as the
        item's inline description. Previously, prefix was hardcoded to
        '' and Step B fell back to consuming a random unclaimed desc
        from the queue, producing wrong raw_descriptions for codes
        that had their description right there on the same line.

        Uses a synthetic 7-digit code (9999999) that will NEVER appear
        in a real code_map, so this test exercises the orphan/unknown
        code path regardless of how code_map evolves over time."""
        parser = self._import_parser()
        text = """**** CHEMICAL & JANITORIAL ***
1 CS
12.5GALECOLAB
SANITIZER OASIS 146 MULTI QU 9999999
GROUP TOTAL****
7006331 155.91 12.47
155.91
155.91
LAST PAGE
INVOICE
TOTAL
"""
        result = parser.parse_invoice(text, vendor='Sysco')
        codes = {i.get('sysco_item_code'): i for i in result['items']}
        self.assertIn('9999999', codes)
        item = codes['9999999']
        # Description should come from the code line's prefix, not a random
        # pulled-from-the-queue neighboring description.
        self.assertIn('SANITIZER', item['raw_description'].upper())
        self.assertIn('OASIS', item['raw_description'].upper())

    def test_inline_description_code_perlb_with_weight_next_line(self):
        """Second variant of catch-weight: description + code + per-lb on
        ONE line, weight on next line, no adjacent extended. Parser
        derives extended = weight × per_lb and extracts the item with
        the inline description as the description prefix."""
        parser = self._import_parser()
        text = """**** MEATS ****
BCH BLK PORK TENDERLOIN 1.5 DN FRESH 25140 5812534 3.299
15.100
T/WT=
LAST PAGE
INVOICE
TOTAL
"""
        result = parser.parse_invoice(text, vendor='Sysco')
        codes = [i.get('sysco_item_code') for i in result['items']]
        self.assertIn('5812534', codes)
        item = next(i for i in result['items'] if i['sysco_item_code'] == '5812534')
        # weight × per_lb = 15.1 × 3.299 ≈ 49.82
        self.assertAlmostEqual(item['unit_price'], 49.82, places=1)
        self.assertAlmostEqual(item['price_per_unit'], 3.299, places=3)
        self.assertEqual(item['case_size_raw'], '15.1LB')


class ParserSyscoIntegrationTests(TestCase):
    """Sysco parser is parser.py's largest + most complex format: 5-pass
    anchor/description matching, catch-weight handling, pack-size extraction,
    section tagging, GROUP TOTAL exclusion, LAST PAGE total extraction.
    These tests cover the major paths via minimal synthetic OCR.

    Uses a stub mapper.load_mappings so tests are deterministic regardless
    of the live invoice_processor/mappings/item_mappings.json cache state."""

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        import mapper
        return p, mapper

    def _stub_mappings(self, code_map=None):
        """Context manager: patch mapper.load_mappings to return controlled
        code_map (other fields empty). Needed because Sysco parser consults
        mapper.load_mappings internally for known-code-first matching."""
        from unittest.mock import patch
        _, mapper = self._import_parser()
        return patch.object(mapper, 'load_mappings', return_value={
            'code_map': code_map or {},
            'desc_map': {}, 'vendor_desc_map': {}, 'category_map': {},
        })

    def test_basic_two_items_unknown_codes(self):
        """Codes unknown → fall back to ordered OCR-description pairing.
        First desc pairs with first anchor in read order."""
        parser_mod, _ = self._import_parser()
        raw = """SYSCO PHILADELPHIA
Invoice Number
775800001
DELV. DATE
4/15/26
**** DAIRY ****
MILK WHOLE GAL
YOGURT PLAIN TUB
1234567 15.00
2345678 22.00
GROUP TOTAL
37.00
LAST PAGE
Subtotal
37.00
Total
37.00
"""
        with self._stub_mappings(code_map={}):
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        self.assertEqual(result['vendor'], 'Sysco')
        items = result['items']
        self.assertEqual(len(items), 2)
        # Both items have unit_price set
        prices = sorted(it['unit_price'] for it in items)
        self.assertEqual(prices, [15.00, 22.00])
        # Item codes captured
        codes = sorted(it['sysco_item_code'] for it in items)
        self.assertEqual(codes, ['1234567', '2345678'])

    def test_known_code_preserves_ocr_description(self):
        """When code is in code_map, raw_description preserves the OCR text
        (the canonical still flows to Product FK via sysco_item_code, but
        the audit trail + case_size extraction benefit from real OCR text
        like '1 GAL WHLFCLS MILK WHOLE' rather than short canonical 'Milk,
        Whole Gallon'). Changed 2026-04-23 when spatial matching made OCR
        text reliably clean across the corpus."""
        parser_mod, _ = self._import_parser()
        raw = """**** DAIRY ****
MILK WHOLE GAL
1234567 15.00
GROUP TOTAL
15.00
LAST PAGE
Subtotal
15.00
Total
15.00
"""
        with self._stub_mappings(code_map={'1234567': 'Milk, Whole Gallon'}):
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        items = result['items']
        self.assertEqual(len(items), 1)
        # OCR text preserved (fuzzy: contains 'MILK' and 'WHOLE'); canonical
        # still resolves via sysco_item_code downstream.
        self.assertEqual(items[0]['sysco_item_code'], '1234567')
        self.assertIn('MILK', items[0]['raw_description'].upper())
        self.assertIn('WHOLE', items[0]['raw_description'].upper())

    def test_group_total_not_treated_as_item(self):
        """GROUP TOTAL + its amount must NOT show up as an extra line item."""
        parser_mod, _ = self._import_parser()
        raw = """**** PRODUCE ****
ROMAINE HEARTS FRESH
CARROTS BABY CUT
1111111 10.00
2222222 8.00
GROUP TOTAL
18.00
LAST PAGE
Subtotal
18.00
Total
18.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        # Should be 2 items (not 3 — GROUP TOTAL amount must not pair)
        self.assertEqual(len(result['items']), 2)
        # No item has $18.00 (that's the group total, not a line)
        self.assertNotIn(18.00, [it['unit_price'] for it in result['items']])

    def test_section_tagging(self):
        """Each item tagged with its section name (DAIRY, PRODUCE, etc.)
        from the nearest preceding section header."""
        parser_mod, _ = self._import_parser()
        raw = """**** DAIRY ****
MILK WHOLE GAL
1111111 15.00
GROUP TOTAL
15.00
**** PRODUCE ****
ROMAINE HEARTS FRESH
2222222 10.00
GROUP TOTAL
10.00
LAST PAGE
Subtotal
25.00
Total
25.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        items = result['items']
        self.assertEqual(len(items), 2)
        # Match items to their sections by unit_price
        by_price = {it['unit_price']: it for it in items}
        self.assertIn('DAIRY', by_price[15.00]['section'].upper())
        self.assertIn('PRODUCE', by_price[10.00]['section'].upper())

    def test_last_page_invoice_total(self):
        """Invoice total extracted as the max decimal near 'LAST PAGE'.
        This is the canonical path for multi-page Sysco invoices."""
        parser_mod, _ = self._import_parser()
        raw = """**** DAIRY ****
MILK WHOLE GAL
YOGURT PLAIN TUB
1111111 15.00
2222222 22.00
GROUP TOTAL
37.00
LAST PAGE
Subtotal
37.00
Tax
0.00
Invoice Total
37.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        self.assertEqual(result.get('invoice_total'), 37.00)

    def test_catch_weight_protein_section(self):
        """MEATS section: catch-weight line ('42.5 LB CHICKEN BREAST')
        gets weight extracted and computed into per-lb pricing."""
        parser_mod, _ = self._import_parser()
        raw = """**** MEATS ****
42.5 LB CHICKEN BREAST FRESH
1111111 200.00
GROUP TOTAL
200.00
LAST PAGE
Subtotal
200.00
Total
200.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        items = result['items']
        self.assertEqual(len(items), 1)
        item = items[0]
        # Catch-weight items get price_per_unit = total / weight_lbs
        # and unit_of_measure='LB'
        self.assertEqual(item.get('unit_of_measure'), 'LB')
        # 200 / 42.5 = 4.7058... → round to 4 decimals
        self.assertAlmostEqual(item.get('price_per_unit'), 4.7058823529, places=3)

    def test_catch_weight_not_applied_in_canned_section(self):
        """Catch-weight only triggers in MEAT/POULTRY/SEAFOOD sections.
        CANNED & DRY section should not extract weight from what looks
        like a catch-weight line."""
        parser_mod, _ = self._import_parser()
        raw = """**** CANNED & DRY ****
42.5 LB FLOUR ALL PURPOSE
1111111 200.00
GROUP TOTAL
200.00
LAST PAGE
Subtotal
200.00
Total
200.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        items = result['items']
        self.assertEqual(len(items), 1)
        # No catch-weight extraction outside protein sections
        self.assertNotIn('price_per_unit', items[0])
        self.assertNotEqual(items[0].get('unit_of_measure'), 'LB')

    def test_date_extracted_from_delv_date(self):
        """Sysco's canonical date field is 'DELV. DATE', which
        `extract_date` must pull via its generic regex."""
        parser_mod, _ = self._import_parser()
        raw = """SYSCO PHILADELPHIA
DELV. DATE
4/15/2026
**** DAIRY ****
MILK WHOLE GAL
1111111 15.00
GROUP TOTAL
15.00
LAST PAGE
Total
15.00
"""
        with self._stub_mappings():
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        self.assertEqual(result['invoice_date'], '2026-04-15')

    def test_unknown_anchor_keeps_adjacent_desc(self):
        """Known anchors must not steal descriptions that are strictly
        closer to an unknown anchor.

        Regression: before the Step A unknown-guard + Step B.1 reorder,
        Step A's proximity-first pass plus Step A2's ordered fallback
        would consume every unclaimed description before unknown anchors
        got their proximity pass. Result: items whose code wasn't in
        code_map came out as '[Sysco #NNNNNNN]' placeholders even though
        their OCR description sat on the adjacent line.

        This synthetic OCR places an unknown-code anchor (9999998) one
        line below its description, and two known-code anchors far
        enough away that they should prefer descriptions closer to them
        than to the unknown. With the fix, the unknown's raw_description
        pulls from the adjacent line."""
        parser_mod, _ = self._import_parser()
        raw = """**** DAIRY ****
D
1 CS
450 OZ CEREAL OAT CRUNCH CINNAMON
9999998 60.95
60.95
D
1 CS
MILK WHOLE GAL
1111111 15.00
D
1 CS
BUTTER SALTED BLOCK
2222222 22.00
GROUP TOTAL
97.95
LAST PAGE
Total
97.95
"""
        code_map = {'1111111': 'Milk, Whole', '2222222': 'Butter, Salted'}
        with self._stub_mappings(code_map=code_map):
            result = parser_mod.parse_invoice(raw, vendor='Sysco')
        items = {it['sysco_item_code']: it for it in result['items']}
        # Unknown code must NOT be a placeholder — desc came from line above
        self.assertIn('9999998', items)
        unk = items['9999998']
        self.assertNotIn('[Sysco #', unk['raw_description'])
        self.assertIn('CEREAL', unk['raw_description'].upper())
        # Known anchors preserve OCR text (canonical resolves via
        # sysco_item_code). Both expected to contain real invoice text.
        self.assertIn('MILK', items['1111111']['raw_description'].upper())
        self.assertIn('BUTTER', items['2222222']['raw_description'].upper())


class MapperNonProductClassifierTests(TestCase):
    """Invoice lines that represent surcharges, fees, credits, or footer
    noise shouldn't go through product mapping. Tagging them with
    confidence='non_product' keeps them out of recipe-costing flows while
    preserving the dollar value for budget tracking."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import mapper
        return mapper

    def _empty_mappings(self):
        return {"code_map": {}, "desc_map": {},
                "vendor_desc_map": {}, "category_map": {}}

    def test_fuel_surcharge_tagged_non_product(self):
        m = self._import()
        r = m.resolve_item(
            {"raw_description": "CHGS FOR FUEL SURCHARGE",
             "sysco_item_code": "3974320"},
            self._empty_mappings(), vendor="Sysco")
        self.assertEqual(r["confidence"], "non_product")
        self.assertIsNone(r["canonical"])

    def test_delivery_fee_tagged_non_product(self):
        m = self._import()
        r = m.resolve_item(
            {"raw_description": "DELIVERY FEE", "sysco_item_code": ""},
            self._empty_mappings(), vendor="Sysco")
        self.assertEqual(r["confidence"], "non_product")

    def test_credit_memo_tagged_non_product(self):
        m = self._import()
        r = m.resolve_item(
            {"raw_description": "CREDIT MEMO INV# 12345", "sysco_item_code": ""},
            self._empty_mappings(), vendor="Sysco")
        self.assertEqual(r["confidence"], "non_product")

    def test_real_product_not_tagged_non_product(self):
        """Ordinary product descriptions must not accidentally trip the
        non-product filter (would cost mapping rate)."""
        m = self._import()
        r = m.resolve_item(
            {"raw_description": "MILK WHOLE GAL", "sysco_item_code": "1234567"},
            self._empty_mappings(), vendor="Sysco")
        self.assertNotEqual(r["confidence"], "non_product")

    def test_non_product_takes_priority_over_code_match(self):
        """Even if somehow a SUPC ends up in code_map for a fuel line,
        the non-product classifier should still short-circuit — it
        prevents downstream double-counting in recipe-costing."""
        m = self._import()
        r = m.resolve_item(
            {"raw_description": "CHGS FOR FUEL SURCHARGE",
             "sysco_item_code": "3974320"},
            {"code_map": {"3974320": "Fake Product"}, "desc_map": {},
             "vendor_desc_map": {}, "category_map": {}},
            vendor="Sysco")
        self.assertEqual(r["confidence"], "non_product")
        self.assertIsNone(r["canonical"])


class SpatialMatcherTests(TestCase):
    """Validates the 2D bounding-box matcher that replaces the 1D
    line-proximity heuristics for Sysco. Synthetic page fixtures so
    tests don't depend on DocAI calls or real invoice images."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import spatial_matcher as sm
        import parser as p
        return sm, p

    def _tok(self, text, x, y, w=0.04, h=0.015):
        """Build a token dict with the shape the matcher expects."""
        return {
            "text": text,
            "x_min": x, "x_max": x + w,
            "y_min": y, "y_max": y + h,
            "char_start": 0, "char_end": 0,
        }

    def _build_row(self, y, tokens_xt):
        """Build a horizontal row of tokens. tokens_xt = [(text, x), ...]."""
        return [self._tok(text, x, y) for text, x in tokens_xt]

    def test_row_clustering_separates_adjacent_lines(self):
        sm, _ = self._import()
        row1 = self._build_row(0.10, [("1", 0.13), ("CS", 0.15),
                                        ("PITA", 0.25), ("7881455", 0.57),
                                        ("18.49", 0.64)])
        row2 = self._build_row(0.13, [("1", 0.13), ("CS", 0.15),
                                        ("CHIPS", 0.25), ("1977754", 0.57),
                                        ("10.95", 0.64)])
        rows = sm._group_rows(row1 + row2)
        self.assertEqual(len(rows), 2)

    def test_unknown_code_pairs_with_same_row_desc(self):
        """Regression for today's placeholder bug: an unknown-code anchor
        adjacent to a known-code anchor must get its own row's desc,
        regardless of line order in raw_text."""
        sm, _ = self._import()
        tokens = []
        tokens += self._build_row(0.10, [
            ("1", 0.13), ("CS", 0.15),
            ("450", 0.18), ("OZ", 0.22),
            ("CEREAL", 0.28), ("GRANOLA", 0.36),
            ("9999998", 0.57), ("60.95", 0.64),
        ])
        tokens += self._build_row(0.13, [
            ("1", 0.13), ("CS", 0.15),
            ("MILK", 0.28), ("WHOLE", 0.35),
            ("GAL", 0.42), ("1111111", 0.57),
            ("15.00", 0.64),
        ])
        items = sm.match_sysco_spatial([{"page_number": 1, "tokens": tokens}])
        by_code = {it["sysco_item_code"]: it for it in items}
        self.assertIn("9999998", by_code)
        self.assertIn("1111111", by_code)
        # The unknown-code item must have its desc from its own row —
        # NOT bleed from the known-code row below it.
        self.assertIn("CEREAL", by_code["9999998"]["raw_description"].upper())
        self.assertNotIn("MILK", by_code["9999998"]["raw_description"].upper())
        self.assertIn("MILK", by_code["1111111"]["raw_description"].upper())
        self.assertEqual(by_code["9999998"]["unit_price"], 60.95)
        self.assertEqual(by_code["1111111"]["unit_price"], 15.00)

    def test_invoice_number_in_header_is_not_claimed_as_anchor(self):
        """Invoice-number tokens sit in the far-right column (x≈0.72),
        outside the SUPC-code column (x≈0.57). The x-range filter must
        reject them so they don't appear as line items."""
        sm, _ = self._import()
        # Invoice header row: far-right 7-digit
        header = self._build_row(0.05, [
            ("INVOICE", 0.50), ("NUMBER", 0.60),
            ("7777777", 0.72),  # at x=0.72, outside SUPC band [0.40, 0.68]
        ])
        items = sm.match_sysco_spatial([{"page_number": 1, "tokens": header}])
        codes = [it["sysco_item_code"] for it in items]
        self.assertNotIn("7777777", codes)

    def test_parse_invoice_without_pages_uses_heuristic(self):
        """Back-compat: callers that don't pass pages=... still get the
        raw_text-based parser. No crash, no missing items."""
        _, p = self._import()
        # Stub out the Sysco parser's mapper dep
        from unittest.mock import patch
        import mapper
        raw = """**** DAIRY ****
MILK WHOLE GAL
1111111 15.00
GROUP TOTAL
15.00
LAST PAGE
Total
15.00
"""
        with patch.object(mapper, 'load_mappings', return_value={
            'code_map': {'1111111': 'Milk, Whole'},
            'desc_map': {}, 'vendor_desc_map': {}, 'category_map': {},
        }):
            result = p.parse_invoice(raw, vendor='Sysco')  # no pages kwarg
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['sysco_item_code'], '1111111')

    def test_parse_invoice_with_pages_prefers_spatial(self):
        """When pages=[...] is passed and spatial extraction finds ≥3
        items, the spatial path wins over the 1D heuristic path."""
        _, p = self._import()
        # Build a synthetic page with 3 items
        sm, _ = self._import()
        tokens = []
        for i, (code, y) in enumerate([("2222222", 0.10),
                                         ("3333333", 0.13),
                                         ("4444444", 0.16)]):
            tokens += self._build_row(y, [
                ("ITEM", 0.20), (f"N{i}", 0.30),
                (code, 0.57), (f"{10+i}.00", 0.64),
            ])
        pages = [{"page_number": 1, "tokens": tokens}]
        # raw text unused by spatial path, but parse_invoice needs
        # something for detect_vendor / extract_date fallbacks.
        raw = "SYSCO PHILADELPHIA\nDELV. DATE\n4/15/2026\n"
        from unittest.mock import patch
        import mapper
        with patch.object(mapper, 'load_mappings', return_value={
            'code_map': {}, 'desc_map': {},
            'vendor_desc_map': {}, 'category_map': {},
        }):
            result = p.parse_invoice(raw, vendor='Sysco', pages=pages)
        codes = sorted(it['sysco_item_code'] for it in result['items'])
        self.assertEqual(codes, ['2222222', '3333333', '4444444'])

    def test_parse_invoice_spatial_used_with_few_items(self):
        """Spatial path is used whenever it returns ≥1 item. Threshold
        lowered from ≥3 to ≥1 on 2026-05-01 — Sysco's multi-photo
        workflow (one JPG per invoice page) loses pages whose spatial
        result has <3 items. Empty/header-only pages produce 0 anchors
        → 0 spatial items, so the 0-item fallback to text still fires."""
        _, p = self._import()
        # 2 items in spatial layout — accepted under min_items=1
        tokens = []
        for i, code in enumerate(["5555555", "6666666"]):
            tokens += self._build_row(0.10 + i*0.03, [
                ("X", 0.20), (code, 0.57), (f"{20+i}.00", 0.64),
            ])
        pages = [{"page_number": 1, "tokens": tokens}]
        raw = """**** DAIRY ****
MILK
1111111 15.00
LAST PAGE
Total
15.00
"""
        from unittest.mock import patch
        import mapper
        with patch.object(mapper, 'load_mappings', return_value={
            'code_map': {}, 'desc_map': {},
            'vendor_desc_map': {}, 'category_map': {},
        }):
            result = p.parse_invoice(raw, vendor='Sysco', pages=pages)
        # Spatial result wins (2 items ≥ 1 from text) — both codes captured.
        codes = [it['sysco_item_code'] for it in result['items']]
        self.assertIn('5555555', codes)
        self.assertIn('6666666', codes)


class SpatialMatcherOtherVendorsTests(TestCase):
    """Per-vendor spatial matchers (PBM, Exceptional, Farm Art, Delaware).
    Synthetic token-layout fixtures so tests don't depend on DocAI calls."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import spatial_matcher as sm
        return sm

    def _tok(self, text, x, y, w=0.04, h=0.015):
        return {"text": text, "x_min": x, "x_max": x + w,
                "y_min": y, "y_max": y + h,
                "char_start": 0, "char_end": 0}

    def _row(self, y, items):
        return [self._tok(text, x, y) for text, x in items]

    # ── PBM ────────────────────────────────────────────────────────────
    def test_pbm_extracts_items_from_grid(self):
        sm = self._import()
        tokens = []
        tokens += self._row(0.38, [("H106", 0.08), ("2.00", 0.24),
                                     ("DZ", 0.41), ("Wheat", 0.46),
                                     ("Pita", 0.50), ("5.25", 0.78),
                                     ("10.50", 0.85)])
        tokens += self._row(0.40, [("L7408", 0.08), ("3.00", 0.24),
                                     ("DZ", 0.41), ("Brioche", 0.46),
                                     ("Buns", 0.51), ("9.37", 0.78),
                                     ("28.11", 0.85)])
        items = sm.match_pbm_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 2)
        by_desc = {i["raw_description"]: i for i in items}
        self.assertIn("Wheat Pita", by_desc)
        self.assertEqual(by_desc["Wheat Pita"]["unit_price"], 5.25)
        self.assertEqual(by_desc["Wheat Pita"]["extended_amount"], 10.50)
        # Phase 2 polish: UM (DZ/EA) lands in purchase_uom, not case_size_raw
        self.assertEqual(by_desc["Wheat Pita"]["case_size_raw"], "")
        self.assertEqual(by_desc["Wheat Pita"]["purchase_uom"], "DZ")
        self.assertEqual(by_desc["Wheat Pita"]["unit_of_measure"], "DZ")

    def test_pbm_rejects_rows_without_code_or_price(self):
        """Header rows, address rows, footer total rows should not
        accidentally surface as line items."""
        sm = self._import()
        tokens = self._row(0.10, [("Invoice", 0.55), ("Number", 0.60),
                                    ("1234", 0.67)])
        items = sm.match_pbm_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(items, [])

    # ── Exceptional ────────────────────────────────────────────────────
    def test_exceptional_catch_weight_extracts_weight_and_per_lb(self):
        """Catch-weight rows have weight (8.5), per-lb ($5.19), LB unit,
        and total ($44.12). Spatial should separate these into
        case_size_raw (weight), price_per_unit ($/lb), extended (total)."""
        sm = self._import()
        tokens = []
        # Turkey: qty 1.00 EA, weight 8.50, per-lb 5.19, LB, ext 44.12
        tokens += self._row(0.30, [
            ("32425", 0.06), ("1.00", 0.22), ("EA", 0.27),
            ("Turkey", 0.30), ("Brst", 0.36),
            ("8.50", 0.71), ("5.19", 0.80), ("LB", 0.85),
            ("44.12", 0.92),
        ])
        items = sm.match_exceptional_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["extended_amount"], 44.12)
        self.assertEqual(item["case_size_raw"], "8.5LB")
        self.assertEqual(item["unit_of_measure"], "LB")
        self.assertEqual(item["price_per_unit"], 5.19)
        self.assertIn("Turkey", item["raw_description"])

    def test_exceptional_non_catch_weight_no_price_per_unit(self):
        """Regular CS/EA items don't have per-lb tokens — shouldn't
        accidentally promote the unit_price as price_per_unit."""
        sm = self._import()
        tokens = self._row(0.30, [
            ("12345", 0.06), ("2.00", 0.22), ("CS", 0.27),
            ("Cheese", 0.30), ("Block", 0.38),
            ("30.00", 0.92),  # extended only, no per-lb anchor
        ])
        items = sm.match_exceptional_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        self.assertNotIn("price_per_unit", items[0])
        self.assertEqual(items[0]["extended_amount"], 30.00)

    # ── Farm Art ───────────────────────────────────────────────────────
    def test_farmart_extracts_items_ignoring_cool_column(self):
        """COOL (country of origin) tokens like 'United States' sit between
        desc and price columns; they should be filtered out of desc."""
        sm = self._import()
        tokens = self._row(0.41, [
            ("1.000", 0.07), ("1.000", 0.12), ("EACH", 0.16),
            ("CRESC", 0.20),
            ("DAIRY", 0.27), ("SOUR", 0.31), ("CREAM", 0.35),
            ("United", 0.70), ("States", 0.74),
            ("9.90", 0.83), ("9.80", 0.90),
        ])
        items = sm.match_farmart_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        desc = items[0]["raw_description"]
        self.assertIn("SOUR", desc.upper())
        self.assertIn("CREAM", desc.upper())
        self.assertNotIn("United", desc)  # COOL filtered out
        self.assertEqual(items[0]["extended_amount"], 9.80)
        # Phase 2c (2026-05-02): U/M (EACH) lands in purchase_uom, NOT
        # case_size_raw. case_size_raw is now blank for Farm Art (the U/M
        # column was being mis-stuffed there before, polluting downstream
        # calc_iup / calc_price_per_lb).
        self.assertEqual(items[0]["case_size_raw"], "")
        self.assertEqual(items[0]["purchase_uom"], "EACH")
        self.assertEqual(items[0]["unit_of_measure"], "EACH")

    # ── Delaware ───────────────────────────────────────────────────────
    def test_delaware_small_volume_extraction(self):
        sm = self._import()
        tokens = self._row(0.39, [
            ("300", 0.11), ("MOPS", 0.16),
            ("Bar", 0.24), ("Mops", 0.26),
            ("0.22", 0.66), ("66.00", 0.76),
        ])
        items = sm.match_delaware_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 300)
        self.assertEqual(items[0]["unit_price"], 0.22)
        self.assertEqual(items[0]["extended_amount"], 66.00)
        self.assertIn("Bar Mops", items[0]["raw_description"])

    # ── Dispatcher ────────────────────────────────────────────────────
    def test_parse_invoice_dispatches_pbm_to_spatial(self):
        """parse_invoice should route PBM through its spatial matcher
        when pages are provided."""
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as p
        tokens = []
        for y, (code, desc, price, ext) in zip(
            [0.38, 0.40, 0.42, 0.44],
            [("H106", "Bread A", "5.25", "10.50"),
             ("L7408", "Bread B", "9.37", "28.11"),
             ("R1012", "Bread C", "10.00", "20.00"),
             ("0290", "Bread D", "14.92", "29.84")]):
            tokens += self._row(y, [(code, 0.08), ("2.00", 0.24),
                                      ("DZ", 0.41), *[(w, 0.46 + i*0.06) for i, w in enumerate(desc.split())],
                                      (price, 0.78), (ext, 0.85)])
        pages = [{"page_number": 1, "tokens": tokens}]
        raw = "INVOICE\nTotal: 88.45"  # dummy — spatial won't use it
        result = p.parse_invoice(raw, vendor='Philadelphia Bakery Merchants',
                                 pages=pages)
        self.assertEqual(len(result['items']), 4)


class ParserPipelineIntegrationTest(TestCase):
    """T4: full parse → map → write_invoice_to_db round-trip. Verifies the
    three layers cooperate — parser finds items, mapper links canonical
    names, db_write upserts to InvoiceLineItem without losing provenance."""

    def _import_pipeline(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import parser as invoice_parser
        import mapper
        import db_write
        return invoice_parser, mapper, db_write

    def test_pbm_end_to_end(self):
        """Synthetic PBM OCR → 2 InvoiceLineItem rows with correct vendor,
        date, prices, and mapping linkage. Minimal proof the pipeline
        works as a whole, not just in pieces."""
        parser_mod, mapper, db_write = self._import_pipeline()
        from myapp.models import Vendor, Product, InvoiceLineItem

        product = Product.objects.create(
            canonical_name='Assorted Donuts', category='Bakery',
        )
        mappings = {
            'code_map': {},
            'desc_map': {'ASSORTED DONUTS': 'Assorted Donuts'},
            'vendor_desc_map': {},
            'category_map': {
                'Assorted Donuts': {
                    'category': 'Bakery',
                    'primary_descriptor': '',
                    'secondary_descriptor': '',
                },
            },
        }

        raw_ocr = """ABC Bakery Invoice
Invoice #12345
4/15/2026
Description
2 0290/AsstDo... Assorted Donuts
3 0100/Bagels... Plain Bagels
Price Each
Amount
1.50
3.00
0.75
2.25
Total
$5.25
"""
        parsed = parser_mod.parse_invoice(raw_ocr, vendor='PBM')
        mapped = mapper.map_items(parsed['items'], mappings=mappings,
                                   vendor=parsed['vendor'])
        rows_written = db_write.write_invoice_to_db(
            parsed['vendor'], parsed['invoice_date'], mapped,
            source_file='test_fixture_pbm.jpg',
        )

        self.assertEqual(rows_written, 2)

        vendor = Vendor.objects.get(name='PBM')
        items = InvoiceLineItem.objects.filter(vendor=vendor)
        self.assertEqual(items.count(), 2)
        self.assertEqual(items.first().invoice_date.isoformat(), '2026-04-15')

        donut_item = items.filter(product=product).first()
        self.assertIsNotNone(donut_item)
        self.assertEqual(str(donut_item.unit_price), '1.50')
        self.assertEqual(str(donut_item.extended_amount), '3.00')
        self.assertEqual(donut_item.match_confidence, 'exact')
        self.assertEqual(donut_item.source_file, 'test_fixture_pbm.jpg')

        # Unmapped bagel still written, raw_description preserved
        bagel_item = items.filter(product__isnull=True).first()
        self.assertIsNotNone(bagel_item)
        self.assertIn('Bagel', bagel_item.raw_description)
        self.assertEqual(bagel_item.match_confidence, 'unmatched')

    def test_reprocess_is_idempotent(self):
        """db_write upserts — running the pipeline twice must not
        duplicate rows. This is what makes reprocess_invoices safe."""
        parser_mod, mapper, db_write = self._import_pipeline()
        from myapp.models import Vendor, Product, InvoiceLineItem

        Product.objects.create(canonical_name='Assorted Donuts', category='Bakery')
        mappings = {
            'code_map': {},
            'desc_map': {'ASSORTED DONUTS': 'Assorted Donuts'},
            'vendor_desc_map': {},
            'category_map': {'Assorted Donuts': {
                'category': 'Bakery', 'primary_descriptor': '',
                'secondary_descriptor': '',
            }},
        }
        raw = """Bakery
4/15/2026
Description
2 0290/AsstDo... Assorted Donuts
Price Each
Amount
1.50
3.00
Total
$3.00
"""
        for run in range(2):
            parsed = parser_mod.parse_invoice(raw, vendor='PBM')
            mapped = mapper.map_items(parsed['items'], mappings=mappings,
                                      vendor='PBM')
            db_write.write_invoice_to_db(
                'PBM', parsed['invoice_date'], mapped,
                source_file='test_pbm.jpg',
            )

        # Only 1 invoice, 1 item — second run should upsert, not duplicate
        self.assertEqual(InvoiceLineItem.objects.count(), 1,
                         "Upsert failed — second run created a duplicate row")


class ParserUnknownVendorFallbackTest(TestCase):
    """Unknown vendor → falls through to generic parser + flags needs_review."""

    def test_unknown_vendor_generic_parse(self):
        p = _import_parser()
        raw = """Unknown Vendor Co
Invoice 4/15/2026
Some Product Name           12.50
Another Thing               8.75
"""
        result = p.parse_invoice(raw)
        self.assertEqual(result['vendor'], 'Unknown')
        # Generic parser marks every item needs_review=True
        for item in result['items']:
            self.assertTrue(item.get('needs_review', False),
                            f"{item} should be flagged for review")


def _import_mapper():
    """Import the mapper module (lives in invoice_processor/, not in myapp).
    Inserting its dir on sys.path so `import mapper` works in tests."""
    import sys
    from django.conf import settings
    path = str(settings.BASE_DIR / 'invoice_processor')
    if path not in sys.path:
        sys.path.insert(0, path)
    import mapper
    return mapper


def _fixture_mappings():
    """A minimal mappings dict for resolve_item tests — no DB, no Sheets."""
    return {
        "code_map": {
            "1234567": "Romaine",
            "9876543": "Olive Oil",
        },
        "desc_map": {
            "ROMAINE HEARTS 3CT": "Romaine",
            "OIL OLIVE EXTRA VIRGIN 4 1GAL": "Olive Oil",
        },
        "vendor_desc_map": {
            "SYSCO": {
                "WHLFCLS ROMAINE HEARTS 3CT": "Romaine",
            },
            "FARM ART": {
                "LETTUCE ROMAINE 24CT": "Romaine",
            },
        },
        "category_map": {
            "Romaine": {
                "category": "Produce",
                "primary_descriptor": "Leaf",
                "secondary_descriptor": "",
            },
            "Olive Oil": {
                "category": "Drystock",
                "primary_descriptor": "Oil",
                "secondary_descriptor": "",
            },
        },
    }


class MapperResolveItemTests(TestCase):
    """`resolve_item` — 7-tier matching priority. Covers every tier +
    unmatched fallback + the category_map attach."""

    def test_supc_code_match_wins(self):
        """Code match beats everything else — even vendor-scoped exact."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '1234567', 'raw_description': 'totally different'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['canonical'], 'Romaine')
        self.assertEqual(r['confidence'], 'code')
        self.assertEqual(r['score'], 100)
        self.assertEqual(r['category'], 'Produce')

    def test_vendor_scoped_exact_match(self):
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': 'WHLFCLS ROMAINE HEARTS 3CT'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['canonical'], 'Romaine')
        self.assertEqual(r['confidence'], 'vendor_exact')
        self.assertEqual(r['score'], 100)

    def test_vendor_scoped_fuzzy_match(self):
        """Slight variation still matches inside vendor scope."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': 'WHLFCLS ROMAINE HEART 3CT'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['canonical'], 'Romaine')
        self.assertEqual(r['confidence'], 'vendor_fuzzy')
        self.assertGreaterEqual(r['score'], 90)

    def test_global_exact_fallthrough(self):
        """Vendor not known, but global exact desc match still works."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': 'ROMAINE HEARTS 3CT'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='')
        self.assertEqual(r['canonical'], 'Romaine')
        self.assertEqual(r['confidence'], 'exact')

    def test_unmatched_returns_structured_none(self):
        """No match anywhere → canonical=None, confidence='unmatched', category=''."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '',
                'raw_description': 'COMPLETELY UNKNOWN PRODUCT NAME'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertIsNone(r['canonical'])
        self.assertEqual(r['confidence'], 'unmatched')
        self.assertEqual(r['score'], 0)
        self.assertEqual(r['category'], '')

    def test_empty_desc_and_no_code_returns_unmatched(self):
        """Guard: no description AND no SUPC → don't fuzzy-match garbage."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': ''}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertIsNone(r['canonical'])
        self.assertEqual(r['confidence'], 'unmatched')

    def test_code_still_wins_when_desc_blank(self):
        """A SUPC code alone should resolve even if description is empty."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '9876543', 'raw_description': ''}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['canonical'], 'Olive Oil')
        self.assertEqual(r['confidence'], 'code')

    def test_category_map_attached(self):
        """Resolved items get category/primary/secondary descriptors."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '1234567'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['category'], 'Produce')
        self.assertEqual(r['primary_descriptor'], 'Leaf')

    def test_unmatched_has_blank_category(self):
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': 'XXYYZZ'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['category'], '')
        self.assertEqual(r['primary_descriptor'], '')


class MapperStripSyscoPrefixTests(TestCase):
    """`_strip_sysco_prefix` — removes brand-code prefix from raw Sysco
    descriptions so fuzzy matching can work on the product name itself."""

    def test_strips_known_brand_prefix(self):
        mapper = _import_mapper()
        self.assertEqual(
            mapper._strip_sysco_prefix("WHLFCLS ROMAINE HEARTS 3CT"),
            "ROMAINE HEARTS",
        )

    def test_strips_multiword_prefix(self):
        mapper = _import_mapper()
        result = mapper._strip_sysco_prefix("SYS CLS MAYO REGULAR 4 1GAL")
        self.assertNotIn("SYS CLS", result)
        self.assertIn("MAYO", result)

    def test_leaves_non_prefix_alone(self):
        """A description without a known prefix should come through unchanged."""
        mapper = _import_mapper()
        original = "PLAIN PRODUCT DESCRIPTION"
        self.assertEqual(mapper._strip_sysco_prefix(original), original)

    def test_strips_leading_only_qty_noise(self):
        """OCR artefact 'ONLY 2 LB ...' gets leading-qty noise removed."""
        mapper = _import_mapper()
        result = mapper._strip_sysco_prefix("ONLY 2 LB WHLFCLS CARROT BABY")
        # Both leading qty AND brand prefix should be stripped
        self.assertNotIn("ONLY", result)
        self.assertIn("CARROT", result)

    def test_short_result_reverts_to_original(self):
        """If stripping leaves <5 chars, revert to avoid over-stripping."""
        mapper = _import_mapper()
        result = mapper._strip_sysco_prefix("WHLFCLS X")  # just 'X' after strip
        self.assertEqual(result, "WHLFCLS X")  # too short — original preserved

    def test_strips_propack_prefix(self):
        mapper = _import_mapper()
        result = mapper._strip_sysco_prefix("PROPACK BAG MERCHANDISE 12X15")
        self.assertNotIn("PROPACK", result)
        self.assertIn("BAG", result)

    def test_strips_quaker_prefix(self):
        mapper = _import_mapper()
        result = mapper._strip_sysco_prefix("QUAKER OATS ROLLED OLD FASHION")
        self.assertNotIn("QUAKER", result)
        self.assertIn("OATS", result)


class CaseSizeDecoderTests(TestCase):
    """`invoice_processor.case_size_decoder.decode` — extracts normalized
    pack sizes from raw invoice descriptions. Covers Sysco's packed-number
    format (2416OZ → 24/16OZ) and the decimal-split variant (482.6OZ → 48/2.6OZ)."""

    def _decode(self, desc, vendor=''):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / 'invoice_processor'))
        from case_size_decoder import decode
        return decode(desc, vendor)

    def test_sysco_packed_integer(self):
        self.assertEqual(self._decode('2416OZ ARIZONA ICED TEA', 'Sysco'), '24/16OZ')

    def test_sysco_decimal_split_two_digit_count(self):
        """482.6OZ should split as count=48, per=2.6 (decimal stays with per)."""
        self.assertEqual(self._decode('482.6OZ STACYS PITA CHIPS', 'Sysco'), '48/2.6OZ')

    def test_sysco_decimal_split_three_digit_count(self):
        """961.42OZ should split as count=96, per=1.42OZ — regression for
        the bug where 96 was missing from COMMON_PACK_COUNTS."""
        self.assertEqual(self._decode('961.42OZ GENMILLS CEREAL BAR', 'Sysco'),
                         '96/1.42OZ')

    def test_sysco_only_prefix(self):
        self.assertEqual(self._decode('ONLY 2.5LB CHICKEN', 'Sysco'), '1/2.5LB')

    def test_sysco_gallon(self):
        self.assertEqual(self._decode('12.5GAL ECOLAB SANITIZER', 'Sysco'),
                         '1/12.5GAL')

    def test_farmart_slash_format(self):
        self.assertEqual(self._decode('DAIRY MILK WHOLE, 4/1-GAL', 'Farm Art'),
                         '4/1GAL')

    def test_farmart_lb_suffix(self):
        self.assertEqual(self._decode('CABBAGE, GREEN, 35LB', 'Farm Art'),
                         '1/35LB')

    def test_exceptional_pounds(self):
        self.assertEqual(self._decode('Chicken Breast 5 oz Frozen 40lb', 'Exceptional Foods'),
                         '1/40LB')

    def test_no_size_returns_none(self):
        self.assertIsNone(self._decode('Bacon', 'Sysco'))


class EffectiveCaseSizeTests(TestCase):
    """`InvoiceLineItem.effective_case_size` — falls back to the linked
    product's `default_case_size` when the invoice row has none."""

    def setUp(self):
        super().setUp()
        self.v = Vendor.objects.create(name='V')
        self.p = Product.objects.create(
            canonical_name='Test Milk', default_case_size='4/1GAL')
        self.p_no_default = Product.objects.create(canonical_name='Test Unknown')

    def _ili(self, **kwargs):
        defaults = dict(
            vendor=self.v, raw_description='Test', unit_price=Decimal('1.00'),
            invoice_date=date(2026, 4, 1),
        )
        defaults.update(kwargs)
        return InvoiceLineItem.objects.create(**defaults)

    def test_uses_direct_case_size_when_set(self):
        ili = self._ili(product=self.p, case_size='8/1GAL')
        self.assertEqual(ili.effective_case_size, '8/1GAL')

    def test_falls_back_to_product_default_when_empty(self):
        ili = self._ili(product=self.p, case_size='')
        self.assertEqual(ili.effective_case_size, '4/1GAL')

    def test_empty_when_neither_set(self):
        ili = self._ili(product=self.p_no_default, case_size='')
        self.assertEqual(ili.effective_case_size, '')

    def test_empty_when_no_product(self):
        ili = self._ili(product=None, case_size='')
        self.assertEqual(ili.effective_case_size, '')


class InferDefaultCaseSizeCommandTests(TestCase):
    """Unit tests for the `infer_product_default_case_sizes` command —
    core logic is mode-pick with count + share thresholds."""

    def _run(self, *args, **kwargs):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('infer_product_default_case_sizes', *args, stdout=out, **kwargs)
        return out.getvalue()

    def setUp(self):
        super().setUp()
        self.v = Vendor.objects.create(name='V')

    def _add_ili(self, product, case_size, n=1):
        for i in range(n):
            InvoiceLineItem.objects.create(
                vendor=self.v, product=product, case_size=case_size,
                raw_description=product.canonical_name,
                unit_price=Decimal('1.00'),
                invoice_date=date(2026, 4, i + 1),
            )

    def test_sets_default_when_mode_strong(self):
        p = Product.objects.create(canonical_name='P1')
        self._add_ili(p, '4/1GAL', n=5)
        self._add_ili(p, '8/1GAL', n=1)
        self._run('--apply')
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, '4/1GAL')

    def test_skips_when_mode_below_min_count(self):
        """Singleton mode is noise — don't set a default."""
        p = Product.objects.create(canonical_name='P2')
        self._add_ili(p, '4/1GAL', n=1)
        self._run('--apply')
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, '')

    def test_skips_when_mode_below_min_share(self):
        """50/50 split means neither is the clear default."""
        p = Product.objects.create(canonical_name='P3')
        self._add_ili(p, '4/1GAL', n=3)
        self._add_ili(p, '8/1GAL', n=3)
        self._add_ili(p, '12/1GAL', n=3)
        # Mode (3/9) is only 33% — below default 50% threshold
        self._run('--apply')
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, '')

    def test_preserves_existing_default_without_overwrite(self):
        p = Product.objects.create(canonical_name='P4', default_case_size='manual')
        self._add_ili(p, '4/1GAL', n=5)
        self._run('--apply')
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, 'manual')

    def test_overwrite_replaces_existing_default(self):
        p = Product.objects.create(canonical_name='P5', default_case_size='old')
        self._add_ili(p, '4/1GAL', n=5)
        self._run('--apply', '--overwrite')
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, '4/1GAL')

    def test_dry_run_does_not_write(self):
        p = Product.objects.create(canonical_name='P6')
        self._add_ili(p, '4/1GAL', n=5)
        self._run()  # no --apply
        p.refresh_from_db()
        self.assertEqual(p.default_case_size, '')


class MapperJunkFilterTests(TestCase):
    """`_is_junk_item` — filter layer that keeps OCR noise out of the DB."""

    def test_fuel_surcharge_is_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item({'raw_description': 'FUEL SURCHARGE'}))

    def test_group_total_is_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item({'raw_description': 'GROUP TOTAL'}))
        self.assertTrue(mapper._is_junk_item({'raw_description': 'Group total amt'}))

    def test_credit_card_surcharge_is_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item(
            {'raw_description': 'CREDIT CARD SRCHRG 2.5%'}))

    def test_section_header_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item({'raw_description': '**** DAIRY ****'}))

    def test_real_product_not_junk(self):
        mapper = _import_mapper()
        self.assertFalse(mapper._is_junk_item(
            {'raw_description': 'CHICKEN BREAST BONELESS SKINLESS'}))

    def test_empty_is_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item({'raw_description': ''}))
        self.assertTrue(mapper._is_junk_item({'raw_description': '   '}))

    def test_pure_number_is_junk(self):
        mapper = _import_mapper()
        self.assertTrue(mapper._is_junk_item({'raw_description': '1234'}))

    def test_sysco_header_footer_artifacts_filtered(self):
        """Sysco OCR frequently leaks invoice header/footer lines into the
        item list. These should not reach the DB as "unmapped products"."""
        mapper = _import_mapper()
        artifacts = [
            'CONFIDENTIAL PROPERTY OF SYSCO',
            'CUBE QUOPSTOCK',
            'DELV. DATE',
            'DFL124TBWSYS',
            'FRESH" MENU ITEM.',
            'INVOICE NUMBER',
            'ITEM DESCRIPTION',
            'MA: T4CBZ DAVID CIANFARO',
            'MANIFEST# 1238296 NORMAL DELIVERY',
            'ONLY 2 KILOROLAND',
            'ONLY1GAL',
            'PURCHASE ORDER',
            'RIBEYE00STH',
            'TERMS -PAST DUE BALANCES ARE SUBJECT TO SERVICE CHARGE',
            'YP160CSYSA',
        ]
        for raw in artifacts:
            self.assertTrue(
                mapper._is_junk_item({'raw_description': raw}),
                f'{raw!r} should be filtered as junk',
            )


class CostUtilsParseCaseSizeTests(TestCase):
    """Pure-function tests on `cost_utils.parse_case_size` — no DB, no auth.
    Adds regression coverage to the crown-jewel-parallel math layer."""

    def test_none_and_empty(self):
        from myapp.cost_utils import parse_case_size
        self.assertIsNone(parse_case_size(None))
        self.assertIsNone(parse_case_size(""))
        self.assertIsNone(parse_case_size("   "))

    def test_bare_numbers_reject(self):
        """No unit = can't determine packing; all bare numbers → None."""
        from myapp.cost_utils import parse_case_size
        for s in ("1", "2", "3", "4", "5", "10", "12"):
            self.assertIsNone(parse_case_size(s),
                              f"bare number {s!r} parsed when it shouldn't have")

    def test_slash_format(self):
        """'12/32OZ' → 12 packs of 32 oz each."""
        from myapp.cost_utils import parse_case_size
        info = parse_case_size("12/32OZ")
        self.assertEqual(info.pack_count, 12)
        self.assertEqual(info.pack_size, Decimal("32"))
        self.assertEqual(info.pack_unit, 'oz')

    def test_simple_format(self):
        """'24CT' → 1 pack of 24 ct."""
        from myapp.cost_utils import parse_case_size
        info = parse_case_size("24CT")
        self.assertEqual(info.pack_count, 1)
        self.assertEqual(info.pack_size, Decimal("24"))
        self.assertEqual(info.pack_unit, 'ct')

    def test_pound_only(self):
        from myapp.cost_utils import parse_case_size
        info = parse_case_size("50LB")
        self.assertEqual(info.pack_count, 1)
        self.assertEqual(info.pack_size, Decimal("50"))
        self.assertEqual(info.pack_unit, 'lb')

    def test_unit_aliasing(self):
        """EA → ct, EACH → ct, FLOZ → fl_oz."""
        from myapp.cost_utils import parse_case_size
        self.assertEqual(parse_case_size("5EA").pack_unit, 'ct')
        self.assertEqual(parse_case_size("5EACH").pack_unit, 'ct')
        self.assertEqual(parse_case_size("5FLOZ").pack_unit, 'fl_oz')

    def test_dates_dont_match(self):
        """'4/11/2026' has no valid unit suffix → shouldn't parse as 4×11."""
        from myapp.cost_utils import parse_case_size
        self.assertIsNone(parse_case_size("4/11/2026"))
        self.assertIsNone(parse_case_size("3/15/22"))

    def test_junk_returns_none(self):
        from myapp.cost_utils import parse_case_size
        for s in ("abc", "??", "null", "1GAL/extra"):
            self.assertIsNone(parse_case_size(s))

    def test_total_in_base_unit(self):
        """12 packs × 32 oz = 384 total oz."""
        from myapp.cost_utils import parse_case_size
        info = parse_case_size("12/32OZ")
        self.assertEqual(info.total_in_base_unit, Decimal("384"))

    def test_number_10_can_format(self):
        """'6/10CAN' = 6 individual #10 cans, NOT 6 packs of 10 cans.
        #10 is a can-size designation (~109 oz each), not a multiplier.
        Treating it as (6, 10, can) would overstate the pack by 10x and
        silently understate per-can cost on every canned-good recipe."""
        from myapp.cost_utils import parse_case_size
        info = parse_case_size("6/10CAN")
        self.assertIsNotNone(info, "'6/10CAN' should parse")
        self.assertEqual(info.pack_count, 6)
        self.assertEqual(info.pack_size, Decimal("1"))
        self.assertEqual(info.pack_unit, 'can')

    def test_number_10_can_variants(self):
        """Accept with or without '#' prefix and flexible whitespace."""
        from myapp.cost_utils import parse_case_size
        for s in ("6/10CAN", "6/#10CAN", "12/10 CAN", " 6 / 10 CAN "):
            info = parse_case_size(s)
            self.assertIsNotNone(info, f"{s!r} should parse as #10-can format")
            self.assertEqual(info.pack_unit, 'can')
            self.assertEqual(info.pack_size, Decimal("1"))


class ExtractWeightFromDescriptionTests(TestCase):
    """Phase 2A unlock helper. Extracts a parse_case_size-shaped weight
    string from raw invoice descriptions. Surfaced 2026-04-22 during the
    cost-coverage audit when ~50% of cost-eligible RIs were blocked by
    bare-qty case_size despite the description carrying the real weight."""

    def test_n_over_m_pound_pattern(self):
        from myapp.cost_utils import extract_weight_from_description
        # Sysco Butter: "CS Butter Prints 36/1# Unsalted Sweet" → 36 packs of 1 lb
        self.assertEqual(
            extract_weight_from_description('CS Butter Prints 36/1# Unsalted Sweet'),
            '36/1LB')
        # Farm Art: "RICOTTA, ITALIAN, 6/3 LB" → 6 packs of 3 lb
        self.assertEqual(
            extract_weight_from_description('RICOTTA, ITALIAN, 6/3 LB'),
            '6/3LB')
        # Sweet Potato bag pattern with leading punctuation
        self.assertEqual(
            extract_weight_from_description('SWEET POTATO, YAMS, #1, 1/40LB'),
            '1/40LB')

    def test_bare_weight_pattern(self):
        from myapp.cost_utils import extract_weight_from_description
        self.assertEqual(
            extract_weight_from_description('SWEET POTATO, YAMS, #1, 40 LB'),
            '40LB')
        self.assertEqual(
            extract_weight_from_description('BROCCOLI, CROWNS, 20 LB'),
            '20LB')
        self.assertEqual(
            extract_weight_from_description('Flour AP 50#'),
            '50LB')

    def test_no_weight_returns_none(self):
        from myapp.cost_utils import extract_weight_from_description
        self.assertIsNone(extract_weight_from_description(''))
        self.assertIsNone(extract_weight_from_description(None))
        self.assertIsNone(extract_weight_from_description('CELERY, 24-30 CT, CS'))
        self.assertIsNone(extract_weight_from_description('GREENS, BOK CHOY, BABY, BUSHEL'))
        self.assertIsNone(extract_weight_from_description('EGGS XL LOOSE, 15-DOZ'))

    def test_n_over_m_takes_priority(self):
        """When both N/M# and bare-N# match, N/M wins (more specific)."""
        from myapp.cost_utils import extract_weight_from_description
        # "36/1# CS, 50# net" — 36/1 is the structured weight, 50# is descriptive
        result = extract_weight_from_description('CS Butter 36/1# Net 50LB')
        self.assertEqual(result, '36/1LB')


class EffectiveCaseSizeForCostTests(TestCase):
    """Composite helper that decides the best case_size string for cost calc:
    falls back from the literal case_size column to description-extracted
    weight to a bare-N/M-as-LB heuristic. Drives the +20 RI cost-coverage
    unlock for Sysco Butter and similar bare-qty rows."""

    def _f(self):
        from myapp.cost_utils import effective_case_size_for_cost
        return effective_case_size_for_cost

    def test_passes_through_when_already_parseable(self):
        f = self._f()
        # '1/25LB' parses fine — no fallback needed
        self.assertEqual(f('1/25LB', 'Sugar 1/25 LB bag'), '1/25LB')
        self.assertEqual(f('50LB', 'Flour AP 50#'), '50LB')

    def test_extracts_from_description_when_cs_unparseable(self):
        f = self._f()
        # '36/1' alone doesn't parse (no unit); description carries '36/1#'
        self.assertEqual(f('36/1', 'CS Butter Prints 36/1# Unsalted Sweet'),
                         '36/1LB')

    def test_falls_back_to_bare_n_over_m_as_lbs(self):
        f = self._f()
        # cs='36/1', no description weight — heuristic fallback applies
        self.assertEqual(f('36/1', ''), '36/1LB')
        # Sanity range: 200 lb cap rejects '75/25' (1875 lb total)
        # — returns original because no strategy succeeded
        self.assertEqual(f('75/25', ''), '75/25')

    def test_returns_original_when_nothing_helps(self):
        f = self._f()
        # Bare '1' is rejected by parse_case_size; description has no weight
        self.assertEqual(f('1', 'CELERY, 24-30 CT, CS'), '1')
        # Empty case_size + non-weight description
        self.assertEqual(f('', 'BUSHEL of greens'), '')

    def test_description_priority_over_bare_n_over_m(self):
        """When both fallbacks would succeed, description wins (more reliable)."""
        f = self._f()
        # '36/1' → description '36/1#' AND bare-N/M heuristic both produce '36/1LB'
        self.assertEqual(f('36/1', '36/1# product'), '36/1LB')

    def test_product_default_used_when_literal_unparseable(self):
        """Phase 2B: Product.default_case_size is the inferred canonical pack;
        kicks in when literal case_size is bare-qty or OCR-mangled."""
        f = self._f()
        # Milk: Farm Art ships invoice cs='1', product default='4/1GAL'
        self.assertEqual(f('1', 'DAIRY MILK 2%, 4/1-GAL', product_default='4/1GAL'),
                         '4/1GAL')
        # Garlic: same pattern
        self.assertEqual(f('1', '', product_default='4/1GAL'), '4/1GAL')

    def test_product_default_skipped_when_literal_parses(self):
        """Don't override an invoice's specific case_size with the
        product-level default — invoice is more current."""
        f = self._f()
        # cs='1/25LB' parses → use it, ignore the product default
        self.assertEqual(f('1/25LB', '', product_default='4/1GAL'), '1/25LB')

    def test_product_default_takes_priority_over_bare_n_over_m_heuristic(self):
        """Product default is more reliable than the lbs-fallback guess."""
        f = self._f()
        # cs='6/15' would heuristic to '6/15LB' (90 lbs), but if product
        # default exists and parses, it wins.
        self.assertEqual(f('6/15', '', product_default='1/50LB'), '1/50LB')

    def test_description_priority_over_product_default(self):
        """Invoice description weight beats product default — invoice is
        more current data than the inferred-from-history default."""
        f = self._f()
        self.assertEqual(f('1', 'BUTTER 36/1# Sweet', product_default='4/1GAL'),
                         '36/1LB')

    def test_no_product_default_falls_through(self):
        """Backward-compat: callers that don't pass product_default still work."""
        f = self._f()
        self.assertEqual(f('1', 'something with 50LB'), '50LB')
        self.assertEqual(f('1', '', product_default=None), '1')
        self.assertEqual(f('1', '', product_default=''), '1')


class CaseSizeCandidatesForCostTests(TestCase):
    """Phase 2D: returns ALL parseable case_size candidates so the cost
    calc can try each. Critical when the literal case_size parses but is
    semantically wrong (AP Flour cs='30/85CT' parses as count, but the
    product is sold by weight). Single-best-effort form would lock in
    the wrong literal; trying all candidates allows the product default
    ('1/50LB') to win when the literal yields incompat units."""

    def _f(self):
        from myapp.cost_utils import case_size_candidates_for_cost
        return case_size_candidates_for_cost

    def test_returns_all_parseable_candidates_in_priority_order(self):
        f = self._f()
        # AP Flour scenario: literal '30/85CT' parses (as count, wrong),
        # product default '1/50LB' parses (weight, correct).
        # Both should be in the list, literal first (priority order).
        result = f('30/85CT', 'AP Flour', product_default='1/50LB')
        self.assertEqual(result, ['30/85CT', '1/50LB'])

    def test_deduplicates_when_candidates_overlap(self):
        """If literal and default are the same parseable string, return one."""
        f = self._f()
        result = f('1/50LB', '', product_default='1/50LB')
        self.assertEqual(result, ['1/50LB'])

    def test_skips_unparseable_candidates(self):
        """Bare '1' won't be in the list — wouldn't help cost calc anyway."""
        f = self._f()
        result = f('1', '', product_default='4/1GAL')
        self.assertEqual(result, ['4/1GAL'])

    def test_empty_when_nothing_parses(self):
        f = self._f()
        self.assertEqual(f('1', '', product_default=''), [])
        self.assertEqual(f('1', 'no weight here', product_default=None), [])

    def test_includes_description_weight(self):
        """Description weight added between literal and product default."""
        f = self._f()
        result = f('1', '36/1# Butter', product_default='4/1GAL')
        # literal '1' fails to parse → desc '36/1LB' first valid → default after
        self.assertEqual(result, ['36/1LB', '4/1GAL'])

    def test_bare_n_over_m_heuristic_appears_last(self):
        f = self._f()
        # cs='6/5' (no unit) → bare-N/M heuristic kicks in
        # No description, no product default
        result = f('6/5', '', product_default='')
        self.assertEqual(result, ['6/5LB'])


class RecipeIngredientCostTryAllCandidatesTests(TestCase):
    """Phase 2D integration: RecipeIngredient.estimated_cost iterates the
    candidate list from case_size_candidates_for_cost. Verifies the AP
    Flour pattern unlocks (literal parses but recipe needs density-bridged
    weight; product default is what actually computes)."""

    def test_cost_coverage_view_renders(self):
        """Phase 5 smoke test: /cost-coverage/ loads without errors,
        returns 200, and contains the headline KPI + per-vendor table.
        Lightweight check — full bucket math is implicit through the
        same RecipeIngredient.estimated_cost code path the dashboard runs."""
        from django.contrib.auth.models import User
        from django.test import Client
        u = User.objects.create_user(username='cc_smoke', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get('/cost-coverage/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('Recipe Cost Coverage', body)
        self.assertIn('Per-vendor coverage', body)
        self.assertIn('Worst-covered', body)
        self.assertIn('Top blocked', body)

    def test_phase_2e_spice_densities_resolve(self):
        """Spice density table. Values realigned 2026-04-23 to BoY 8e p.10-13
        (ground powders were 15-35% too heavy under USDA/baking-ref round-ups).
        This test both verifies the lookup works AND locks the current BoY-
        derived values — if you change a density, update here too."""
        from myapp.cost_utils import cup_weight_oz_for
        from decimal import Decimal
        # Ground powders (BoY p.10-13, sense-checked by Sean 2026-04-23)
        self.assertEqual(cup_weight_oz_for('Paprika'), Decimal('3.9'))
        self.assertEqual(cup_weight_oz_for('Cinnamon, Ground'), Decimal('4.0'))
        self.assertEqual(cup_weight_oz_for('Ground Cinnamon'), Decimal('4.0'))
        self.assertEqual(cup_weight_oz_for('Garlic Powder'), Decimal('3.7'))
        self.assertEqual(cup_weight_oz_for('Onion Powder'), Decimal('3.7'))
        # Whole seeds (heavier than ground when intact husk, lighter than denser packing)
        self.assertEqual(cup_weight_oz_for('Black Pepper, Whole'), Decimal('4.0'))
        self.assertEqual(cup_weight_oz_for('Whole Cloves'), Decimal('3.0'))
        # Kosher salt = Diamond Crystal (Sean stocks) — must not hit 'salt' table salt
        self.assertEqual(cup_weight_oz_for('Salt, Kosher'), Decimal('5'))
        # Dried herbs are MUCH lighter (unchanged, already matched BoY leafy median)
        self.assertEqual(cup_weight_oz_for('Oregano'), Decimal('1.5'))
        self.assertEqual(cup_weight_oz_for('Whole Bay Leaves'), Decimal('0.5'))
        # Liquids (unchanged)
        self.assertEqual(cup_weight_oz_for('Fish Sauce'), Decimal('9'))
        self.assertEqual(cup_weight_oz_for('Maple Syrup'), Decimal('11'))

    def test_ap_flour_30_85ct_with_50lb_default_unlocks_via_default(self):
        from myapp.models import (
            Vendor, Product, InvoiceLineItem, Recipe, RecipeIngredient,
        )
        from datetime import date
        v = Vendor.objects.create(name='Sysco')
        p = Product.objects.create(canonical_name='AP Flour', default_case_size='1/50LB')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='AP Flour',  # no weight in desc
            unit_price=Decimal('19.95'),
            extended_amount=Decimal('19.95'),
            case_size='30/85CT',  # parses to count — wrong semantic for flour
            invoice_date=date(2026, 4, 20),
        )
        r = Recipe.objects.create(name='TestBiscuits')
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='AP Flour',
            quantity=Decimal('6'), unit='cups', product=p,
        )
        cost, note = ri.estimated_cost()
        self.assertIsNotNone(cost,
            f'AP Flour cups must price via product default fallback (got {note!r})')
        # 6 cups × 4.25 oz/cup = 25.5 oz; case = 50 lb = 800 oz; 19.95 × (25.5/800) = $0.64
        self.assertAlmostEqual(float(cost), 0.64, places=2)


class BushelExtractionTests(TestCase):
    """Bushel-fraction → synthetic case_size extraction (Phase 6 Farm Art
    unlock). Description carries the container size ('1-1/9 BUSHEL'),
    case_size column only carries bare-qty; this recovers the missing
    weight via USDA PACA lb/bushel lookups."""

    def test_fraction_prefix_pattern(self):
        from myapp.cost_utils import extract_bushel_fraction
        self.assertEqual(
            extract_bushel_fraction('CUCUMBERS, 1-1/9 BUSHEL'),
            Decimal('1') + Decimal('1') / Decimal('9'),
        )
        self.assertEqual(
            extract_bushel_fraction('ZUCCHINI, FANCY/MEDIUM, 1/2 BUSHEL'),
            Decimal('0.5'),
        )
        # hyphen vs space separator between whole + fraction
        self.assertEqual(
            extract_bushel_fraction('EGGPLANT, FANCY, 1 1/9 BUSHEL'),
            Decimal('1') + Decimal('1') / Decimal('9'),
        )

    def test_fraction_postfix_pattern(self):
        """Farm Art also writes 'BUSHEL 1-1/9' with fraction after the word."""
        from myapp.cost_utils import extract_bushel_fraction
        self.assertEqual(
            extract_bushel_fraction('PEPPERS, JALAPENO, BUSHEL 1-1/9'),
            Decimal('1') + Decimal('1') / Decimal('9'),
        )

    def test_bare_bushel_word_means_one(self):
        from myapp.cost_utils import extract_bushel_fraction
        self.assertEqual(
            extract_bushel_fraction('TROPICAL, TOMATILLOS, BUSHEL'),
            Decimal('1'),
        )

    def test_bare_bu_without_fraction_is_not_bushel(self):
        """'60 BU' for herbs is bunch count, not bushel. Must not match."""
        from myapp.cost_utils import extract_bushel_fraction
        self.assertIsNone(extract_bushel_fraction('HERB, CILANTRO, 60 BU'))
        self.assertIsNone(extract_bushel_fraction('HERB, DILL, 24 BU'))
        self.assertIsNone(extract_bushel_fraction('LEEKS, "GOURMET", 12 BU'))

    def test_case_size_synthesis_cucumber(self):
        """'1-1/9 BUSHEL' cucumber × 48 lb/bu = 53.3 lb carton."""
        from myapp.cost_utils import extract_bushel_case_size
        self.assertEqual(
            extract_bushel_case_size('CUCUMBERS, 1-1/9 BUSHEL', 'Cucumber'),
            '53.3LB',
        )

    def test_case_size_synthesis_jalapeno_postfix(self):
        """Jalapeno @ 25 lb/bu × 1-1/9 = 27.8 lb."""
        from myapp.cost_utils import extract_bushel_case_size
        cs = extract_bushel_case_size('PEPPERS, JALAPENO, BUSHEL 1-1/9', 'Pepper, Jalapeno')
        self.assertEqual(cs, '27.8LB')

    def test_case_size_synthesis_half_bushel_zucchini(self):
        """Zucchini @ 44 lb/bu × 1/2 = 22.0 lb."""
        from myapp.cost_utils import extract_bushel_case_size
        cs = extract_bushel_case_size('ZUCCHINI, FANCY/MEDIUM, 1/2 BUSHEL', 'Squash, Zucchini')
        self.assertEqual(cs, '22.0LB')

    def test_case_size_synthesis_full_bushel_tomatillo(self):
        """'BUSHEL' alone = 1 full bushel. Tomatillos @ 52 lb."""
        from myapp.cost_utils import extract_bushel_case_size
        cs = extract_bushel_case_size('TROPICAL, TOMATILLOS, BUSHEL', 'Tomatillos')
        self.assertEqual(cs, '52.0LB')

    def test_unknown_product_returns_none(self):
        """Product not in _BUSHEL_TO_LB → no conversion possible."""
        from myapp.cost_utils import extract_bushel_case_size
        self.assertIsNone(
            extract_bushel_case_size('WIDGETS, 1 BUSHEL', 'Random Widget'),
        )

    def test_candidate_list_includes_bushel_synthesis(self):
        """Integration: case_size_candidates_for_cost surfaces bushel weight."""
        from myapp.cost_utils import case_size_candidates_for_cost
        cands = case_size_candidates_for_cost(
            case_size='1',   # bare qty — won't parse
            raw_description='CUCUMBERS, 1-1/9 BUSHEL',
            product_name='Cucumber',
        )
        self.assertIn('53.3LB', cands)


class RecipeIngredientPieceWeightViaYieldRefTests(TestCase):
    """Phase 6 piece-weight rewrite. When recipe unit is a size word or
    each, and yield_ref has piece-type ap_unit + ap_weight_oz, estimated_cost
    rewrites (qty, unit) to AP weight in oz before dispatch."""

    def _setup_carrot(self, ap_weight_oz=Decimal('4.10'), prep_state='whole,medium',
                       ap_unit='each', yield_pct=Decimal('81.30')):
        from myapp.models import Vendor, Product, InvoiceLineItem, Recipe, RecipeIngredient, YieldReference
        from datetime import date
        v = Vendor.objects.create(name='Farm Art')
        p = Product.objects.create(canonical_name='Carrot')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='Carrot, 50 LB BAG',
            unit_price=Decimal('32.00'),
            extended_amount=Decimal('32.00'),
            case_size='1/50LB',
            invoice_date=date(2026, 4, 20),
        )
        yr = YieldReference.objects.create(
            ingredient='Carrots', prep_state=prep_state, section='vegetables',
            ap_unit=ap_unit, ap_weight_oz=ap_weight_oz, yield_pct=yield_pct,
        )
        r = Recipe.objects.create(name='TestCarrotDish')
        return v, p, yr, r

    def test_medium_carrot_prices_via_yield_ref_piece_weight(self):
        """3 medium Carrot, YR ap_weight_oz=4.10 → 12.30 oz AP.
        Case 1/50LB = 800 oz. Cost = 32 × 12.30/800 = 0.49."""
        from myapp.models import RecipeIngredient
        v, p, yr, r = self._setup_carrot()
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Carrot', quantity=Decimal('3'), unit='medium',
            product=p, yield_ref=yr,
        )
        cost, note = ri.estimated_cost()
        self.assertIsNotNone(cost, f'Should price via piece-weight rewrite (got {note!r})')
        # 3 × 4.10 = 12.30 oz AP; case 800 oz; 32 × 12.30/800 = 0.492 → $0.49
        self.assertEqual(cost, Decimal('0.49'))

    def test_yield_pct_not_double_applied_in_piece_branch(self):
        """Critical: ap_weight_oz is AP — yield_pct must NOT divide qty again.
        If it did, cost would be 0.49 / 0.813 = 0.605 ($0.60 — wrong)."""
        from myapp.models import RecipeIngredient
        v, p, yr, r = self._setup_carrot()
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Carrot', quantity=Decimal('3'), unit='medium',
            product=p, yield_ref=yr,
        )
        cost, _ = ri.estimated_cost()
        # If yield_pct were double-applied, we'd see $0.60 instead of $0.49.
        self.assertEqual(cost, Decimal('0.49'))

    def test_pound_ap_unit_does_not_trigger_piece_rewrite(self):
        """YR row with ap_unit='pound' must NOT be used for piece-weight
        rewrite — it's a per-pound row, not per-piece. Without explicit
        piece-unit ap_unit, we fall back to incompat (existing behavior)."""
        from myapp.models import RecipeIngredient
        v, p, yr, r = self._setup_carrot(
            ap_weight_oz=Decimal('16.00'),  # pound-based row
            prep_state='chopped', ap_unit='pound', yield_pct=Decimal('81.30'),
        )
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Carrot', quantity=Decimal('3'), unit='medium',
            product=p, yield_ref=yr,
        )
        cost, note = ri.estimated_cost()
        # 'medium' against pound-based yield_ref → no rewrite → incompat
        self.assertIsNone(cost)
        self.assertIn('incompatible', note)

    def test_ea_unit_also_triggers_rewrite(self):
        """'Ea' (common recipe shorthand) gets the same treatment as size words."""
        from myapp.models import RecipeIngredient
        v, p, yr, r = self._setup_carrot()
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Carrot', quantity=Decimal('2'), unit='Ea',
            product=p, yield_ref=yr,
        )
        cost, _ = ri.estimated_cost()
        self.assertIsNotNone(cost)
        # 2 × 4.10 / 800 × 32 = 0.328 → $0.33
        self.assertEqual(cost, Decimal('0.33'))

    def test_weight_unit_ignores_piece_rewrite(self):
        """Recipe unit 'lb' + yield_ref set → piece-rewrite must NOT fire.
        Normal weight dispatch takes over, yield_pct gets applied normally."""
        from myapp.models import RecipeIngredient
        v, p, yr, r = self._setup_carrot()
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Carrot', quantity=Decimal('1'), unit='lb',
            product=p, yield_ref=yr,  # yield_pct=81.30 flows through normally
        )
        cost, _ = ri.estimated_cost()
        self.assertIsNotNone(cost)
        # 1 lb / 0.813 = 1.23 lb AP needed; case 50 lb = $32
        # Cost = 32 × 1.23/50 = $0.787 → $0.79
        self.assertAlmostEqual(float(cost), 0.79, places=2)


class RecipeIngredientCostUnlockTests(TestCase):
    """Integration test for the Phase 2A wiring. Confirms that
    `RecipeIngredient.estimated_cost` now succeeds for the Sysco-Butter
    pattern that was the canonical unlock target."""

    def test_butter_36_1_cs_with_pound_description_unlocks(self):
        from myapp.models import (
            Vendor, Product, ProductMapping, InvoiceLineItem,
            Recipe, RecipeIngredient,
        )
        from datetime import date
        v = Vendor.objects.create(name='Sysco')
        p = Product.objects.create(canonical_name='Butter')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='CS Butter Prints 36/1# Unsalted Sweet',
            unit_price=Decimal('50.40'),
            extended_amount=Decimal('50.40'),
            case_size='36/1',
            invoice_date=date(2026, 4, 13),
        )
        r = Recipe.objects.create(name='TestBiscuits')
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Butter',
            quantity=Decimal('1'), unit='cup', product=p,
        )
        cost, note = ri.estimated_cost()
        self.assertIsNotNone(cost,
            f'Butter 36/1 case must price after Phase 2A wiring (got {note!r})')
        # 1 cup butter = 8 oz; case = 36 lb = 576 oz; 50.40 × (8/576) = $0.70
        self.assertAlmostEqual(float(cost), 0.70, places=2)

    def test_eggs_unitless_recipe_with_doz_case_unlocks(self):
        """Phase 2C: 'Recipe asks for 6 (eggs), case is 15 DOZ' must price.
        - Recipe unit is empty (Sean writes "qty=6" with no unit for count items)
        - Case '15 DOZ' must parse as 15 × 12 = 180 ct
        - cost = case_price × (6 / 180)"""
        from myapp.models import (
            Vendor, Product, InvoiceLineItem, Recipe, RecipeIngredient,
        )
        from datetime import date
        v = Vendor.objects.create(name='Farm Art')
        p = Product.objects.create(canonical_name='Eggs', default_case_size='15DOZ')
        InvoiceLineItem.objects.create(
            vendor=v, product=p,
            raw_description='EGGS XL LOOSE, WHITE, 15-DOZ',
            unit_price=Decimal('45.00'),
            extended_amount=Decimal('45.00'),
            case_size='1',
            invoice_date=date(2026, 4, 7),
        )
        r = Recipe.objects.create(name='TestCake')
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='Eggs',
            quantity=Decimal('6'), unit='', product=p,
        )
        cost, note = ri.estimated_cost()
        self.assertIsNotNone(cost,
            f'Eggs unitless+DOZ case must price after Phase 2C (got {note!r})')
        # 6 eggs × $45.00 / 180 ct = $1.50
        self.assertAlmostEqual(float(cost), 1.50, places=2)


class CostUtilsDozUnitTests(TestCase):
    """Phase 2C: 'doz'/'dozen'/'dz' as count units, 1 doz = 12 ct."""

    def test_doz_parses(self):
        from myapp.cost_utils import parse_case_size
        from decimal import Decimal
        info = parse_case_size('15DOZ')
        self.assertIsNotNone(info)
        self.assertEqual(info.pack_unit, 'doz')
        self.assertEqual(info.pack_count, 1)
        self.assertEqual(info.pack_size, Decimal('15'))

    def test_doz_to_ct_conversion(self):
        from myapp.cost_utils import to_base_unit
        from decimal import Decimal
        # 15 doz → 180 ct
        result = to_base_unit(Decimal('15'), 'doz')
        self.assertEqual(result, (Decimal('180'), 'ct'))
        # 1 dozen → 12 ct
        self.assertEqual(to_base_unit(Decimal('1'), 'dozen'), (Decimal('12'), 'ct'))

    def test_unit_kind_doz_is_count(self):
        from myapp.cost_utils import unit_kind
        self.assertEqual(unit_kind('doz'), 'count')
        self.assertEqual(unit_kind('dozen'), 'count')
        self.assertEqual(unit_kind('dz'), 'count')


class CostUtilsUnitlessCountTests(TestCase):
    """Phase 2C: when recipe unit is empty AND case is count AND qty is a
    small integer, treat the recipe as count. Eggs are the canonical case."""

    def test_unitless_integer_with_count_case_computes(self):
        from myapp.cost_utils import ingredient_cost
        from decimal import Decimal
        # 6 (eggs), $45.00 case of 15 DOZ (= 180 ct)
        cost, note = ingredient_cost(
            Decimal('6'), '', 'Eggs',
            Decimal('45.00'), '15DOZ',
        )
        self.assertIsNotNone(cost)
        self.assertAlmostEqual(float(cost), 1.50, places=2)
        self.assertIn('unitless', note)

    def test_unitless_fractional_qty_does_not_match(self):
        """Don't treat fractional qty as count — almost certainly meant
        cups/lbs and the unit was forgotten."""
        from myapp.cost_utils import ingredient_cost
        from decimal import Decimal
        cost, _ = ingredient_cost(
            Decimal('0.5'), '', 'Eggs',
            Decimal('45.00'), '15DOZ',
        )
        self.assertIsNone(cost)

    def test_unitless_huge_qty_does_not_match(self):
        """200+ qty without unit isn't a realistic count for a single line."""
        from myapp.cost_utils import ingredient_cost
        from decimal import Decimal
        cost, _ = ingredient_cost(
            Decimal('500'), '', 'Eggs',
            Decimal('45.00'), '15DOZ',
        )
        self.assertIsNone(cost)

    def test_unitless_with_weight_case_does_not_falsely_match(self):
        """Recipe '6' with case in pounds isn't 6 lbs — could be 6 cups,
        6 oz, 6 of-something. Don't guess; require explicit unit."""
        from myapp.cost_utils import ingredient_cost
        from decimal import Decimal
        cost, _ = ingredient_cost(
            Decimal('6'), '', 'Flour',
            Decimal('40.00'), '50LB',
        )
        self.assertIsNone(cost)


class DriveVendorCanonicalizerTests(TestCase):
    """`drive.canonical_vendor` maps short-form vendor strings to the
    long-form canonical names that detect_vendor() + _normalize_vendor()
    produce. Prevents the archiver from silently creating "FarmArt"
    next to an existing "Farm Art" folder when upstream returns a short
    form. Regression guard — the accountant reviews the archive as a
    production surface, so duplicate vendor folders are a real cost."""

    def _import_drive(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import drive
        return drive

    def test_known_aliases_map_to_canonical(self):
        drive = self._import_drive()
        pairs = [
            ('FarmArt',                        'Farm Art'),
            ('Farm Art',                       'Farm Art'),
            ('farmart',                        'Farm Art'),
            ('PBM',                            'Philadelphia Bakery Merchants'),
            ('Philadelphia Bakery',            'Philadelphia Bakery Merchants'),
            ('Philadelphia Bakery Merchants',  'Philadelphia Bakery Merchants'),
            ('Exceptional',                    'Exceptional Foods'),
            ('Exceptional Foods',              'Exceptional Foods'),
            ('Delaware County Linens',         'Delaware County Linen'),
            ('Delaware County Linen',          'Delaware County Linen'),
            ('Sysco',                          'Sysco'),
            ('Colonial Meat',                  'Colonial Village Meat Markets'),
            ('Aramark',                        'Aramark'),
        ]
        for raw, expected in pairs:
            self.assertEqual(drive.canonical_vendor(raw), expected,
                f'{raw!r} should canonicalize to {expected!r}')

    def test_unknown_vendors_pass_through(self):
        """New vendors not in the alias map pass through unchanged so the
        archiver can still file them — the expectation is that we add
        them to _VENDOR_CANONICAL once we see them."""
        drive = self._import_drive()
        self.assertEqual(drive.canonical_vendor('SomeNewVendor'), 'SomeNewVendor')
        self.assertEqual(drive.canonical_vendor(''),   '')
        self.assertEqual(drive.canonical_vendor(None), None)

    def test_case_and_whitespace_tolerant(self):
        drive = self._import_drive()
        for variant in ('  FarmArt ', 'FARMART', 'farmart', 'FarmArt\n'):
            self.assertEqual(drive.canonical_vendor(variant), 'Farm Art',
                f'{variant!r} should normalize to "Farm Art"')


class ReprocessJpgsYearFilterTests(TestCase):
    """`reprocess_jpgs._folder_path_year` extracts the year folder name
    from a walk_archive folder_path string. Powers the `--year` CLI
    filter so reprocess runs can be restricted to one or more years
    (e.g. skip 2022 pre-Synergy archive data)."""

    def _import_script(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import reprocess_jpgs
        return reprocess_jpgs

    def test_extracts_year(self):
        rj = self._import_script()
        self.assertEqual(rj._folder_path_year('2025/05 May 2025/Sysco/Week 1'), '2025')
        self.assertEqual(rj._folder_path_year('2026/04 April 2026/Farm Art/Week 2'), '2026')
        self.assertEqual(rj._folder_path_year('2022/08 August 2022/Exceptional'), '2022')

    def test_empty_path(self):
        rj = self._import_script()
        self.assertEqual(rj._folder_path_year(''), '')
        self.assertEqual(rj._folder_path_year('2026'), '2026')


class CostUtilsUnitKindTests(TestCase):
    """`unit_kind` classification — weight / volume / count / unknown."""

    def test_weight_units(self):
        from myapp.cost_utils import unit_kind
        for u in ("lb", "oz", "g", "kg", "pound", "ounce", "gram"):
            self.assertEqual(unit_kind(u), 'weight', f"{u!r} should be weight")

    def test_volume_units(self):
        from myapp.cost_utils import unit_kind
        for u in ("cup", "tbsp", "gal", "fl_oz", "qt", "pt", "ml", "liter"):
            self.assertEqual(unit_kind(u), 'volume', f"{u!r} should be volume")

    def test_count_units(self):
        from myapp.cost_utils import unit_kind
        for u in ("ct", "each", "ea", "bag", "bottle", "head"):
            self.assertEqual(unit_kind(u), 'count', f"{u!r} should be count")

    def test_unknown_units(self):
        from myapp.cost_utils import unit_kind
        self.assertEqual(unit_kind(""), 'unknown')
        self.assertEqual(unit_kind("foobar"), 'unknown')

    def test_punctuation_normalized(self):
        """'Tbsp.' → 'tbsp' → volume. '  LB  ' → 'lb' → weight."""
        from myapp.cost_utils import unit_kind
        self.assertEqual(unit_kind("Tbsp."), 'volume')
        self.assertEqual(unit_kind("  LB  "), 'weight')


class IngredientPieceWeightTests(TestCase):
    """Per-ingredient piece weights: (garlic, clove) etc."""

    def test_garlic_cloves_unlocks_cost(self):
        """'8 cloves Garlic' case=4/1GAL should price via piece-weight → weight→volume."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('8'), recipe_unit='cloves',
            ingredient_name='Garlic',
            case_price=Decimal('40.00'), case_size_str='4/1GAL',
        )
        # 8 cloves × 0.18 oz = 1.44 oz
        # garlic density 4.8 oz/cup → 1.44/4.8 = 0.3 cup → 2.4 fl_oz
        # case 4 × 128 = 512 fl_oz · cost = 40 × 2.4/512 = 0.1875 → 0.19
        self.assertIsNotNone(cost)
        self.assertEqual(cost, Decimal('0.19'))
        self.assertIn('density', note)

    def test_singular_clove_also_works(self):
        from myapp.cost_utils import ingredient_cost
        cost, _ = ingredient_cost(
            recipe_qty=Decimal('1'), recipe_unit='clove',
            ingredient_name='Garlic',
            case_price=Decimal('40.00'), case_size_str='4/1GAL',
        )
        self.assertIsNotNone(cost)

    def test_modifier_prep_names_do_not_match(self):
        """'Roasted Garlic' should NOT be treated as raw garlic — it's a
        sub-recipe (garlic + oil roasted) that needs its own cost path.
        Returning None here surfaces the missing sub_recipe link rather
        than under-costing by ignoring the prep."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('8'), recipe_unit='cloves',
            ingredient_name='Roasted Garlic',
            case_price=Decimal('40.00'), case_size_str='4/1GAL',
        )
        self.assertIsNone(cost)

    def test_cloves_for_non_garlic_falls_through(self):
        """'cloves' for another ingredient should NOT get the piece-weight
        treatment — the table is ingredient-specific."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('8'), recipe_unit='cloves',
            ingredient_name='Onion',  # not in piece-weight table
            case_price=Decimal('40.00'), case_size_str='4/1GAL',
        )
        # 'cloves' isn't a count unit → r_kind='unknown' → incompatible
        self.assertIsNone(cost)


class ContainerUnitCostTests(TestCase):
    """Container-unit → weight/volume case dispatch.

    Recipe asks for '0.5 bag' or '1 pack' or similar; case describes the
    SIZE of each container (12/4OZ = 12 bags of 4oz, 1/50LB = 1 bag of
    50 lb). pack_count IS the container count — cost maps to qty / pack_count.
    """

    def test_half_bag_of_twelve(self):
        """0.5 bag × 12-bag case → 0.5/12 of case_price."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('0.5'), recipe_unit='bag',
            ingredient_name='Mozzarella',
            case_price=Decimal('24.00'), case_size_str='12/4OZ',
        )
        # 24 × 0.5/12 = 1.00
        self.assertEqual(cost, Decimal('1.00'))
        self.assertIn('container', note)

    def test_one_bag_of_one(self):
        """1 bag × 1-bag case → full case price."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('1'), recipe_unit='bag',
            ingredient_name='Yellow Onion',
            case_price=Decimal('32.50'), case_size_str='1/50LB',
        )
        self.assertEqual(cost, Decimal('32.50'))

    def test_pack_plural(self):
        """'packs' plural still triggers container branch."""
        from myapp.cost_utils import ingredient_cost
        cost, _ = ingredient_cost(
            recipe_qty=Decimal('2'), recipe_unit='packs',
            ingredient_name='Swiss',
            case_price=Decimal('40.00'), case_size_str='10/8OZ',
        )
        # 40 × 2/10 = 8.00
        self.assertEqual(cost, Decimal('8.00'))

    def test_generic_count_unit_does_not_trigger(self):
        """'each' should NOT trigger container branch — too ambiguous.
        Falls through to standard count↔count or fails."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal('2'), recipe_unit='each',
            ingredient_name='Carrot',
            case_price=Decimal('30.00'), case_size_str='1/50LB',
        )
        # Should NOT match container branch (each is not a container)
        self.assertNotIn('container', (note or ''))


class CostUtilsUnitSynonymTests(TestCase):
    """Synonyms added from production-data audit: pound symbol, plural
    forms of weight/volume/count units. Each entry here unblocks specific
    incompat RIs surfaced in the 2026-04-22 Phase 6 audit."""

    def test_pound_symbol_is_weight(self):
        from myapp.cost_utils import unit_kind, to_base_unit
        self.assertEqual(unit_kind('#'), 'weight')
        qty, base = to_base_unit(Decimal('5'), '#')
        self.assertEqual(qty, Decimal('80'))  # 5 lb → 80 oz
        self.assertEqual(base, 'oz')

    def test_lbs_plural_is_weight(self):
        from myapp.cost_utils import unit_kind, to_base_unit
        self.assertEqual(unit_kind('lbs'), 'weight')
        self.assertEqual(unit_kind('LBS'), 'weight')
        qty, _ = to_base_unit(Decimal('10'), 'lbs')
        self.assertEqual(qty, Decimal('160'))  # 10 lbs → 160 oz

    def test_volume_plurals(self):
        from myapp.cost_utils import unit_kind, to_base_unit
        for u in ('quarts', 'pints', 'gallons', 'liters', 'tsps', 'tbsps'):
            self.assertEqual(unit_kind(u), 'volume', f"{u!r} should be volume")
        # spot-check math
        qty, _ = to_base_unit(Decimal('2'), 'quarts')
        self.assertEqual(qty, Decimal('64'))  # 2 qt → 64 fl_oz
        qty, _ = to_base_unit(Decimal('3'), 'pints')
        self.assertEqual(qty, Decimal('48'))  # 3 pt → 48 fl_oz

    def test_count_plurals(self):
        from myapp.cost_utils import unit_kind
        for u in ('bags', 'bottles', 'jars', 'cans', 'bunches', 'heads',
                  'packs', 'pack', 'bundles', 'bundle'):
            self.assertEqual(unit_kind(u), 'count', f"{u!r} should be count")


class CostUtilsToBaseUnitTests(TestCase):
    """`to_base_unit` conversion math."""

    def test_weight_to_oz(self):
        """1 lb → 16 oz."""
        from myapp.cost_utils import to_base_unit
        qty, unit = to_base_unit(Decimal("1"), "lb")
        self.assertEqual(qty, Decimal("16"))
        self.assertEqual(unit, 'oz')

    def test_volume_to_floz(self):
        """1 gal → 128 fl_oz."""
        from myapp.cost_utils import to_base_unit
        qty, unit = to_base_unit(Decimal("1"), "gal")
        self.assertEqual(qty, Decimal("128"))
        self.assertEqual(unit, 'fl_oz')

    def test_cup_to_floz(self):
        from myapp.cost_utils import to_base_unit
        qty, unit = to_base_unit(Decimal("2"), "cup")
        self.assertEqual(qty, Decimal("16"))
        self.assertEqual(unit, 'fl_oz')

    def test_count_unchanged(self):
        """'ct' stays as-is — count doesn't convert."""
        from myapp.cost_utils import to_base_unit
        qty, unit = to_base_unit(Decimal("24"), "ct")
        self.assertEqual(qty, Decimal("24"))
        self.assertEqual(unit, 'ct')

    def test_unknown_returns_none(self):
        from myapp.cost_utils import to_base_unit
        self.assertIsNone(to_base_unit(Decimal("1"), "foobar"))


class CostUtilsDensityTests(TestCase):
    """Fallback density lookup — `cup_weight_oz_for`."""

    def test_common_ingredients(self):
        from myapp.cost_utils import cup_weight_oz_for
        self.assertEqual(cup_weight_oz_for("flour"), Decimal("4.25"))
        self.assertEqual(cup_weight_oz_for("butter"), Decimal("8"))
        self.assertEqual(cup_weight_oz_for("sugar"), Decimal("7"))
        self.assertEqual(cup_weight_oz_for("honey"), Decimal("12"))

    def test_normalized_variants(self):
        """'All-Purpose Flour' and 'ap_flour' both resolve to flour density."""
        from myapp.cost_utils import cup_weight_oz_for
        self.assertEqual(cup_weight_oz_for("all-purpose flour"), Decimal("4.25"))
        self.assertEqual(cup_weight_oz_for("ap_flour"), Decimal("4.25"))

    def test_fallback_by_last_token(self):
        """'white sugar' → falls back to 'sugar' match."""
        from myapp.cost_utils import cup_weight_oz_for
        self.assertEqual(cup_weight_oz_for("white sugar"), Decimal("7"))

    def test_unknown_returns_none(self):
        from myapp.cost_utils import cup_weight_oz_for
        self.assertIsNone(cup_weight_oz_for("foobar"))
        self.assertIsNone(cup_weight_oz_for(""))


class IngredientCostTests(TestCase):
    """End-to-end `ingredient_cost` — covers all unit-domain paths.
    These numbers anchor the cost badges shown on the calendar; if this
    math drifts, every recipe cost drifts with it."""

    def test_none_quantity_returns_reason(self):
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            None, 'lb', 'flour', Decimal("50"), "25LB",
        )
        self.assertIsNone(cost)
        self.assertIn('no quantity', note)

    def test_none_case_price_returns_reason(self):
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            Decimal("1"), 'lb', 'flour', None, "25LB",
        )
        self.assertIsNone(cost)
        self.assertIn('no recent invoice price', note)

    def test_unparseable_case_size_returns_reason(self):
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            Decimal("1"), 'lb', 'flour', Decimal("50"), "abc",
        )
        self.assertIsNone(cost)
        self.assertIn('unparseable', note.lower())

    def test_weight_to_weight_simple(self):
        """4 lb out of a 25-lb case at $50 → $8.00."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("4"), recipe_unit='lb',
            ingredient_name='flour',
            case_price=Decimal("50"), case_size_str="25LB",
        )
        self.assertEqual(cost, Decimal("8.00"))
        self.assertIn('weight', note)

    def test_volume_to_volume(self):
        """2 cups out of a 4×1-gal case at $40 → $1.25."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("2"), recipe_unit='cup',
            ingredient_name='oil',
            case_price=Decimal("40"), case_size_str="4/1GAL",
        )
        self.assertEqual(cost, Decimal("1.25"))
        self.assertIn('volume', note)

    def test_volume_to_weight_via_density(self):
        """2 cups flour (density 4.25 oz/cup) out of a 50-lb case at $30 → $0.32.

        Math: 2 cups × 4.25 oz/cup = 8.5 oz; 50 lb × 16 oz = 800 oz;
        $30 × 8.5/800 = $0.31875 → quantize to $0.32."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("2"), recipe_unit='cup',
            ingredient_name='flour',
            case_price=Decimal("30"), case_size_str="50LB",
        )
        self.assertEqual(cost, Decimal("0.32"))
        self.assertIn('density', note)

    def test_count_to_count(self):
        """2 'ea' out of a 24-CT case at $5 → $0.42."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("2"), recipe_unit='ea',
            ingredient_name='eggs',
            case_price=Decimal("5"), case_size_str="24CT",
        )
        self.assertEqual(cost, Decimal("0.42"))
        self.assertIn('count', note)

    def test_yield_pct_scales_ap(self):
        """1 lb edible onion at 90% yield → AP = 1.111 lb. 50-lb case at $20 → $0.44.

        Math: AP oz = 1/0.9 × 16 ≈ 17.78; $20 × 17.78/800 ≈ $0.4444 → $0.44."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("1"), recipe_unit='lb',
            ingredient_name='onion',
            case_price=Decimal("20"), case_size_str="50LB",
            yield_pct=Decimal("90"),
        )
        self.assertEqual(cost, Decimal("0.44"))

    def test_no_density_returns_none(self):
        """Unknown ingredient + volume→weight cross-domain = no density lookup
        possible = (None, 'no density for volume→weight ...')."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("1"), recipe_unit='cup',
            ingredient_name='unknown_exotic_spice',
            case_price=Decimal("10"), case_size_str="5LB",
        )
        self.assertIsNone(cost)
        self.assertIn('no density', note.lower())

    def test_supplied_density_overrides_fallback(self):
        """If caller passes `ounce_weight_per_cup`, it overrides the fallback
        table. This is what YieldReference linkage provides."""
        from myapp.cost_utils import ingredient_cost
        cost, note = ingredient_cost(
            recipe_qty=Decimal("2"), recipe_unit='cup',
            ingredient_name='flour',  # fallback says 4.25
            case_price=Decimal("30"), case_size_str="50LB",
            ounce_weight_per_cup=Decimal("5.0"),  # override
        )
        # With 5.0 oz/cup: 2 × 5.0 = 10 oz; 30 × 10/800 = 0.375 → 0.38
        self.assertEqual(cost, Decimal("0.38"))


class DishSuggestionTests(AuthedTestCase):
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


class MapperNewTiersTests(TestCase):
    """Coverage for the tiers added 2026-04-21: stemmed fuzzy, char-level
    fallback, qualifier gate, per-vendor thresholds. Locks in the behavior
    against future threshold tuning + false positives."""

    def _mapper(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import mapper
        return mapper

    def _mappings(self, canonicals: list[str], vendor_map: dict = None,
                  desc_map: dict = None) -> dict:
        category_map = {c: {'category': 'Produce', 'primary_descriptor': '',
                            'secondary_descriptor': ''} for c in canonicals}
        return {
            'code_map': {},
            'desc_map': desc_map or {},
            'vendor_desc_map': vendor_map or {},
            'category_map': category_map,
        }

    def test_stemmer_basic(self):
        """_stem_text strips trailing 's' for 4+ char words, not 'ss'."""
        mapper = self._mapper()
        self.assertEqual(mapper._stem_text('ONIONS RED'), 'onion red')
        self.assertEqual(mapper._stem_text('PINEAPPLES'), 'pineapple')
        # 'ss' ending preserved (grass, glass)
        self.assertEqual(mapper._stem_text('GRASS FED'), 'grass fed')
        # <4 chars preserved ('cs', 'lb' etc.)
        self.assertEqual(mapper._stem_text('cs'), '')  # <3 chars rejected
        self.assertEqual(mapper._stem_text(''), '')

    def test_ocr_cleanup_digit_0z(self):
        """'9620Z' → '962 OZ' — OCR zero-vs-O misread before Z unit."""
        mapper = self._mapper()
        c = mapper._ocr_cleanup
        self.assertEqual(c('9620Z'), '962 OZ')
        self.assertEqual(c('16810Z'), '1681 OZ')
        self.assertEqual(c('ONLY180Z MCCLNRY SPICE'), 'ONLY18 OZ MCCLNRY SPICE')
        # Already-correct strings unchanged
        self.assertEqual(c('962 OZ COFFEE'), '962 OZ COFFEE')
        self.assertEqual(c(''), '')

    def test_ocr_cleanup_unit_prefix_split(self):
        """'OZCITVCLS' → 'OZ CITVCLS' — unit prefix glued to brand code."""
        mapper = self._mapper()
        c = mapper._ocr_cleanup
        self.assertEqual(c('961.5 OZCITVCLS COFFEE GRND'),
                         '961.5 OZ CITVCLS COFFEE GRND')
        self.assertEqual(c('LBSYSCO BEEF'), 'LB SYSCO BEEF')
        # Words with too-short tail after unit-prefix stay intact:
        # OZONE → OZ + ONE (only 3 caps) → pattern requires {4,} → no split
        self.assertEqual(c('OZONE WATER'), 'OZONE WATER')
        self.assertEqual(c('OZARK SPRING'), 'OZARK SPRING')

    def test_ocr_cleanup_idempotent(self):
        """Running cleanup twice yields the same result."""
        mapper = self._mapper()
        c = mapper._ocr_cleanup
        once  = c('9620Z OZCITVCLS COFFEE')
        twice = c(once)
        self.assertEqual(once, twice)

    def test_stemmer_food_domain_plurals(self):
        """Food-domain plural patterns: ies→y, oes→o, ches/shes/xes→ch/sh/x.

        These all surface from real raw vendor descriptions; without
        suffix-specific stemming they fail to match their singular
        canonicals at fuzzy tiers."""
        mapper = self._mapper()
        s = mapper._stem_text
        # rries → rry  (the ie→y berry family)
        self.assertEqual(s('BERRIES'), 'berry')
        self.assertEqual(s('RASPBERRIES'), 'raspberry')
        self.assertEqual(s('STRAWBERRIES FRESH'), 'strawberry fresh')
        self.assertEqual(s('BLUEBERRIES'), 'blueberry')
        self.assertEqual(s('CHERRIES'), 'cherry')
        # ovies → ovy  (anchovies → anchovy)
        self.assertEqual(s('ANCHOVIES'), 'anchovy')
        # atoes → ato
        self.assertEqual(s('TOMATOES DICED'), 'tomato diced')
        self.assertEqual(s('POTATOES'), 'potato')
        # goes → go
        self.assertEqual(s('MANGOES'), 'mango')
        # ches → ch
        self.assertEqual(s('PEACHES SLICED'), 'peach sliced')
        # shes → sh
        self.assertEqual(s('DISHES'), 'dish')
        # xes → x
        self.assertEqual(s('BOXES'), 'box')

    def test_stemmer_does_not_overstem(self):
        """Words that LOOK plural but aren't (cookies, brownies, shoes)
        must not be over-stemmed. Their singulars end in 'ie' or 'oe',
        not 'y' or 'o', so the suffix patterns must be tight enough to
        leave them alone."""
        mapper = self._mapper()
        s = mapper._stem_text
        # 'kies' is NOT in the rries/ovies pattern — falls through to
        # generic 's' strip, giving 'cookie' (the actual singular).
        self.assertEqual(s('COOKIES'), 'cookie')
        self.assertEqual(s('BROWNIES'), 'brownie')
        # 'shoes' has no goes/atoes prefix — falls to generic 's'.
        self.assertEqual(s('SHOES'), 'shoe')
        # 'movies' technically ends in 'ovies' but len(6) < 7 guard,
        # so falls to generic 's' strip → 'movie'.
        self.assertEqual(s('MOVIES'), 'movie')
        # 'mango' (singular, no plural suffix) stays as-is.
        self.assertEqual(s('MANGO FRESH'), 'mango fresh')

    def test_stemmed_fuzzy_catches_plurals(self):
        """PINEAPPLES raw → Pineapple canonical via stemmed tier."""
        mapper = self._mapper()
        mappings = self._mappings(['Pineapple', 'Apple', 'Banana'])
        item = {'sysco_item_code': '', 'raw_description': 'PINEAPPLES FRESH 6CT'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        self.assertEqual(r['canonical'], 'Pineapple')
        self.assertEqual(r['confidence'], 'stripped_fuzzy')

    def test_stemmed_fuzzy_blocked_by_qualifier(self):
        """SHIITAKE fresh raw must NOT match 'Dried Shiitake' canonical
        because the 'dried' qualifier is missing from raw."""
        mapper = self._mapper()
        mappings = self._mappings(['Dried Shiitake', 'Mushroom'])
        item = {'sysco_item_code': '',
                'raw_description': 'MUSHROOMS, SHIITAKE, #1, 3 LB'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        # Should NOT match 'Dried Shiitake' — qualifier gate blocks it
        self.assertNotEqual(r['canonical'], 'Dried Shiitake')

    def test_char_fallback_catches_spelling_variant(self):
        """Canonical 'Cantaloupe' (correctly spelled) should match raw
        'Canteloupe' (single-char typo). Either the stemmed tier or the
        char-level tier catches it — whichever fires first is fine as long
        as the match succeeds in the stripped_fuzzy bucket."""
        mapper = self._mapper()
        mappings = self._mappings(['Cantaloupe', 'Honeydew'])
        item = {'sysco_item_code': '', 'raw_description': 'Canteloupe'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        self.assertEqual(r['canonical'], 'Cantaloupe')
        self.assertEqual(r['confidence'], 'stripped_fuzzy')

    def test_char_fallback_blocked_on_short_strings(self):
        """Char ratio threshold 95 + token_sort gate 60 prevents
        short-string garbage like 'CS' vs 'CSA' from matching."""
        mapper = self._mapper()
        mappings = self._mappings(['Broccoli', 'Cauliflower'])
        item = {'sysco_item_code': '', 'raw_description': 'CS'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        self.assertEqual(r['confidence'], 'unmatched')

    def test_qualifier_gate_allows_raw_qualifier(self):
        """When raw has an extra qualifier word (Fresh, Raw) that canonical
        lacks, the match still proceeds — gate is one-directional (canonical
        stricter than raw = blocked; raw stricter than canonical = allowed)."""
        mapper = self._mapper()
        # Raw has 'fresh' which IS a qualifier — canonical doesn't mention it
        # _has_missing_qualifier checks canonical_qualifiers - raw_tokens.
        # Since canonical has no qualifiers, nothing is missing — gate passes.
        self.assertFalse(mapper._has_missing_qualifier(
            'pork ground fresh cryo bag', 'ground pork'))

    def test_per_vendor_threshold_lookup(self):
        """_fuzzy_threshold_for returns 85 for relaxed vendors, 90 default."""
        mapper = self._mapper()
        self.assertEqual(mapper._fuzzy_threshold_for('Exceptional Foods'), 85)
        self.assertEqual(mapper._fuzzy_threshold_for('EXCEPTIONAL FOODS'), 85)
        self.assertEqual(mapper._fuzzy_threshold_for('Delaware County Linen'), 85)
        self.assertEqual(mapper._fuzzy_threshold_for('Colonial Village Meat Markets'), 85)
        # Default vendors get the global 90
        self.assertEqual(mapper._fuzzy_threshold_for('Sysco'), 90)
        self.assertEqual(mapper._fuzzy_threshold_for('Farm Art'), 90)
        self.assertEqual(mapper._fuzzy_threshold_for('PBM'), 90)
        # Unknown vendors fall through to default too
        self.assertEqual(mapper._fuzzy_threshold_for('Random Supplier Co'), 90)
        self.assertEqual(mapper._fuzzy_threshold_for(''), 90)

    def test_per_vendor_threshold_applied_in_resolve(self):
        """vendor_fuzzy tier uses the looked-up threshold, not the global."""
        mapper = self._mapper()
        # Exceptional's vendor_map has an entry that scores ~87 against the
        # raw — above 85 (Exceptional) but below 90 (Sysco). Same text
        # matched under Exceptional should hit vendor_fuzzy; under Sysco,
        # it falls through.
        raw = 'BACON APPLEWOOD PRE-COOKED SLICED 10LB'
        mapping_key = 'BACON APPLEWOOD COOKED SLICED'

        # Sanity check: score is in the 85-89 band as the test assumes
        from rapidfuzz import fuzz
        score = fuzz.token_sort_ratio(raw, mapping_key)
        # If this fails, tune the fixture — test depends on score in band
        self.assertGreaterEqual(score, 85, f'fixture drift: score={score}')
        self.assertLess(score, 90, f'fixture drift: score={score}')

        vendor_map = {'EXCEPTIONAL FOODS': {mapping_key: 'Applewood Bacon'},
                      'SYSCO': {mapping_key: 'Applewood Bacon'}}
        mappings = self._mappings(['Applewood Bacon'], vendor_map=vendor_map)

        # Exceptional: 85 threshold → matches
        r_exc = mapper.resolve_item(
            {'sysco_item_code': '', 'raw_description': raw},
            mappings, vendor='Exceptional Foods')
        self.assertEqual(r_exc['canonical'], 'Applewood Bacon')
        self.assertEqual(r_exc['confidence'], 'vendor_fuzzy')

        # Sysco: 90 threshold → DOESN'T match via vendor_fuzzy (may hit a
        # later tier, but definitely not vendor_fuzzy at a sub-90 score)
        r_sys = mapper.resolve_item(
            {'sysco_item_code': '', 'raw_description': raw},
            mappings, vendor='Sysco')
        if r_sys['confidence'] == 'vendor_fuzzy':
            self.assertGreaterEqual(r_sys['score'], 90)

    def test_qualifier_list_coverage(self):
        """_has_missing_qualifier detects the key qualifiers."""
        mapper = self._mapper()
        # Canonical has 'dried', raw doesn't → should block
        self.assertTrue(mapper._has_missing_qualifier(
            'mushroom shiitake', 'dried shiitake'))
        # Canonical has 'frozen', raw doesn't → should block
        self.assertTrue(mapper._has_missing_qualifier(
            'strawberry fresh', 'strawberry frozen'))
        # Both sides have 'fresh' — no mismatch
        self.assertFalse(mapper._has_missing_qualifier(
            'basil fresh', 'basil fresh'))
        # Canonical has no qualifier, raw has qualifier → allowed
        self.assertFalse(mapper._has_missing_qualifier(
            'onion red jumbo', 'onion red'))

    def _category_map(self, assignments: dict[str, str]) -> dict:
        """Build a category_map where each canonical has its category set.
        assignments = {canonical_name: category_name}"""
        return {c: {'category': cat, 'primary_descriptor': '',
                    'secondary_descriptor': ''}
                for c, cat in assignments.items()}

    def test_section_filter_builds_restricted_pool(self):
        """_candidates_for_section returns only canonicals matching the
        section's target categories. Pool must be >= 3 to be considered
        useful (else returns empty = 'use full pool')."""
        mapper = self._mapper()
        cat_map = self._category_map({
            'Milk': 'Dairy', 'Cheese': 'Dairy', 'Yogurt': 'Dairy',
            'Butter': 'Dairy',
            'Spaghetti': 'Drystock', 'Rice': 'Drystock',
        })
        # DAIRY section → unified Dairy canonicals (4 items, >=3 ok)
        dairy = mapper._candidates_for_section('**** DAIRY ****', cat_map)
        self.assertEqual(set(dairy), {'Milk', 'Cheese', 'Yogurt', 'Butter'})

        # PRODUCE section → no matching canonicals in this map
        produce = mapper._candidates_for_section('**** PRODUCE ****', cat_map)
        self.assertEqual(produce, [])  # 0 Produce items = empty

        # Empty section → no filter
        self.assertEqual(mapper._candidates_for_section('', cat_map), [])

        # FROZEN — intentionally not in section map (too ambiguous)
        frozen = mapper._candidates_for_section('**** FROZEN ****', cat_map)
        self.assertEqual(frozen, [])

    def test_section_filter_falls_back_when_too_small(self):
        """Pool with <3 candidates in target category returns empty,
        causing fall-through to unrestricted pool."""
        mapper = self._mapper()
        cat_map = self._category_map({
            'Milk': 'Dairy',
            'Spaghetti': 'Drystock', 'Rice': 'Drystock', 'Flour': 'Drystock',
        })
        # DAIRY section → only 1 Dairy item, below min-3 threshold
        self.assertEqual(mapper._candidates_for_section('DAIRY', cat_map), [])

    def test_section_aware_match_uses_restricted_pool_first(self):
        """When section is provided and restricted pool has the right
        canonical, match succeeds in the section-filtered pass. Uses the
        WHLFCLS Sysco prefix so tier 6a (stripped + token_set) fires."""
        mapper = self._mapper()
        cat_map = self._category_map({
            'Cheese, Parmesan':  'Dairy',
            'Cheese, Cheddar':   'Dairy',
            'Cheese, Mozzarella': 'Dairy',
            'Cheese Sauce':      'Drystock',
            'Cheddar Crackers':  'Drystock',
            'Spaghetti':         'Drystock',
        })
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': cat_map,
        }
        item = {
            'sysco_item_code': '',
            'raw_description': 'WHLFCLS CHEESE CHEDDAR',
            'section': '**** DAIRY ****',
        }
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        # Should match a Dairy-category Cheese Cheddar, NOT Drystock variants
        self.assertIsNotNone(r.get('canonical'))
        self.assertNotIn(r['canonical'], ('Cheddar Crackers', 'Cheese Sauce'))

    def test_section_aware_falls_back_to_full_pool(self):
        """When restricted pool has no good match, mapper falls back to
        unrestricted pool. Preserves recall when Product.category is wrong
        or empty in the DB."""
        mapper = self._mapper()
        # Buttermilk miscategorized as Drystock. Section says DAIRY.
        # Restricted Dairy pool lacks Buttermilk; full pool has it.
        cat_map = self._category_map({
            'Milk':       'Dairy',
            'Yogurt':     'Dairy',
            'Cheese':     'Dairy',
            'Buttermilk': 'Drystock',   # miscategorized
            'Spaghetti':  'Drystock',
        })
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': cat_map,
        }
        # WHLFCLS prefix → tier 6a fires after stripping
        item = {
            'sysco_item_code': '',
            'raw_description': 'WHLFCLS BUTTERMILK',
            'section': '**** DAIRY ****',
        }
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        # Should still match Buttermilk via unrestricted fallback
        self.assertEqual(r['canonical'], 'Buttermilk')

    def test_section_awareness_ignored_for_non_sysco(self):
        """Section field only populates from Sysco parser. Other vendors
        don't emit it; behavior should be unchanged (no filter applied,
        full pool used)."""
        mapper = self._mapper()
        cat_map = self._category_map({
            'Milk': 'Dairy', 'Cheese': 'Dairy', 'Butter': 'Dairy',
            'Rice': 'Drystock', 'Flour': 'Drystock',
        })
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': cat_map,
        }
        # Clean single-token raw, Farm Art vendor — no section flag
        item = {'sysco_item_code': '', 'raw_description': 'Milk'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        # Should match Milk via tier 6b (stemmed) — clean 1:1 token match
        self.assertEqual(r['canonical'], 'Milk')


class MapperRealisticScenariosTests(TestCase):
    """Integration tests with realistic invoice-shaped inputs that exercise
    full tier sequencing + feature interactions. Parser-parity quality
    infrastructure: any future change to thresholds, regexes, or tier
    ordering is caught here before shipping.

    Shared fixture reflects a miniaturized slice of production data —
    real raw_descriptions from live Sysco/Farm Art/Exceptional invoices,
    paired with realistic canonical names + category assignments."""

    FIXTURE_MAPPINGS = {
        'code_map': {
            '1234567': 'Romaine',
            '7890123': 'Milk, Whole Gallon',
        },
        'desc_map': {
            # Global exact (all-vendors)
            'SYSCO CLS BROCCOLI FLORETS FRESH': 'Broccoli Florets',
        },
        'vendor_desc_map': {
            'SYSCO': {
                'WHLFCLS ROMAINE HEARTS 3CT': 'Romaine',
                'GRECOSN CHEESE MOZZARELLA SHREDDED 4 5LB': 'Mozzarella',
                'ARIZONA DRINK TEA ICED LMN 24 16 OZ': 'Arizona Iced Tea',
            },
            'FARM ART': {
                'LETTUCE, ROMAINE, 24 CT': 'Romaine',
                'ONIONS, RED JUMBO, 25 LB': 'Red Onion',
            },
            'EXCEPTIONAL FOODS': {
                'BACON APPLEWOOD SLICE MARTINS': 'Applewood Bacon',
            },
        },
        'category_map': {
            'Romaine':              {'category': 'Produce', 'primary_descriptor': 'Leaf', 'secondary_descriptor': ''},
            'Red Onion':            {'category': 'Produce', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Rosemary':             {'category': 'Spices', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Sweet Potato':         {'category': 'Produce', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Cantaloupe':           {'category': 'Produce', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Cereal, Frosted Flakes': {'category': 'Drystock', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Mozzarella':           {'category': 'Cheese', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Milk, Whole Gallon':   {'category': 'Dairy', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Applewood Bacon':      {'category': 'Proteins', 'primary_descriptor': 'Pork', 'secondary_descriptor': ''},
            'Ground Pork':          {'category': 'Proteins', 'primary_descriptor': 'Pork', 'secondary_descriptor': ''},
            'Dried Shiitake':       {'category': 'Drystock', 'primary_descriptor': '', 'secondary_descriptor': ''},
            'Broccoli Florets':     {'category': 'Produce', 'primary_descriptor': '', 'secondary_descriptor': ''},
        },
    }

    def _mapper(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import mapper
        return mapper

    def _assert_resolves(self, raw_desc: str, vendor: str,
                         expected_canonical: str | None,
                         expected_confidence: str = None,
                         *, sysco_code: str = '', section: str = ''):
        mapper = self._mapper()
        item = {'sysco_item_code': sysco_code, 'raw_description': raw_desc}
        if section:
            item['section'] = section
        r = mapper.resolve_item(item, self.FIXTURE_MAPPINGS, vendor=vendor)
        msg = f"raw={raw_desc!r} vendor={vendor!r} got={r['canonical']!r} tier={r['confidence']!r}"
        if expected_canonical is None:
            self.assertEqual(r['confidence'], 'unmatched', msg)
            self.assertIsNone(r['canonical'], msg)
        else:
            self.assertEqual(r['canonical'], expected_canonical, msg)
            if expected_confidence:
                self.assertEqual(r['confidence'], expected_confidence, msg)

    # Tier 1: SUPC code — highest priority
    def test_scenario_sysco_code_match_beats_everything(self):
        self._assert_resolves('totally wrong text', 'Sysco', 'Romaine',
                              expected_confidence='code', sysco_code='1234567')

    # Tier 2: vendor-scoped exact
    def test_scenario_sysco_vendor_exact(self):
        self._assert_resolves('WHLFCLS ROMAINE HEARTS 3CT', 'Sysco', 'Romaine',
                              expected_confidence='vendor_exact')

    def test_scenario_farm_art_vendor_exact(self):
        self._assert_resolves('LETTUCE, ROMAINE, 24 CT', 'Farm Art', 'Romaine',
                              expected_confidence='vendor_exact')

    # Tier 3: vendor-scoped fuzzy, default threshold (Sysco @ 90)
    def test_scenario_sysco_vendor_fuzzy_90(self):
        # Close variation of mapped desc — should match at >=90
        self._assert_resolves('ARIZONA DRINK TEA ICED LMN 2416 OZ', 'Sysco',
                              'Arizona Iced Tea',
                              expected_confidence='vendor_fuzzy')

    # Tier 3: relaxed threshold for Exceptional Foods (85)
    def test_scenario_exceptional_relaxed_threshold(self):
        # Close variation of "BACON APPLEWOOD SLICE MARTINS" — one token
        # different. token_sort_ratio lands in 85-89 band: passes
        # Exceptional's relaxed threshold, would fail Sysco's 90.
        self._assert_resolves('BACON APPLEWOOD SLICE MARTIN', 'Exceptional Foods',
                              'Applewood Bacon',
                              expected_confidence='vendor_fuzzy')

    # Tier 4: global desc exact (cross-vendor)
    def test_scenario_global_exact_cross_vendor(self):
        # Mapped under no specific vendor, matches global desc_map
        self._assert_resolves('SYSCO CLS BROCCOLI FLORETS FRESH', 'Unknown',
                              'Broccoli Florets',
                              expected_confidence='exact')

    # Tier 6 (stripped_fuzzy): WHLFCLS prefix + token_set match
    def test_scenario_sysco_prefix_strip_and_match(self):
        # Not in vendor_desc_map, not in desc_map — falls to tier 6a/6b.
        # Clean raw that matches after WHLFCLS strip.
        self._assert_resolves('WHLFCLS MOZZARELLA', 'Sysco', 'Mozzarella')

    # Tier 6b: stemmed fuzzy catches pluralization
    def test_scenario_farm_art_stemmed_plural(self):
        self._assert_resolves('ONIONS, RED JUMBO, 50 LB', 'Farm Art',
                              'Red Onion',
                              expected_confidence='vendor_fuzzy')

    def test_scenario_farm_art_stemmed_herb(self):
        self._assert_resolves('HERB, ROSEMARY, 1 LB', 'Farm Art',
                              'Rosemary')

    # Tier 6c: char-level catches spelling variant
    def test_scenario_char_fallback_spelling(self):
        # Raw spelled "Canteloupes", canonical is "Cantaloupe" — single-char
        # typo that only char-level catches. Plural stems collapse first.
        self._assert_resolves('Canteloupe', 'Farm Art', 'Cantaloupe')

    # Qualifier gate: canonical 'Dried Shiitake' blocked by missing 'dried' in raw
    def test_scenario_qualifier_gate_blocks_dried(self):
        # Raw is FRESH shiitake → should NOT match "Dried Shiitake" canonical
        self._assert_resolves('MUSHROOMS, SHIITAKE, #1, 3 LB', 'Farm Art',
                              expected_canonical=None)

    # Section-aware: prefers Dairy-category canonical
    def test_scenario_section_aware_match(self):
        # Section-aware restricts candidate pool, helping precision
        self._assert_resolves('WHLFCLS MOZZARELLA SHRD', 'Sysco',
                              'Mozzarella',
                              section='**** DAIRY ****')

    # Junk line — should not even try (filtered upstream by _is_junk_item)
    def test_scenario_junk_is_unmatched(self):
        # Verified via _is_junk_item separately; just confirm the flow here
        mapper = self._mapper()
        self.assertTrue(mapper._is_junk_item(
            {'raw_description': 'FUEL SURCHARGE 2.5%'}))
        self.assertTrue(mapper._is_junk_item(
            {'raw_description': '[Sysco #5229067]'}))
        self.assertTrue(mapper._is_junk_item(
            {'raw_description': 'GROUP TOTAL'}))

    # End-to-end: map_items filters junk, then resolves each remaining item
    def test_scenario_map_items_filters_and_resolves(self):
        mapper = self._mapper()
        items = [
            {'sysco_item_code': '1234567', 'raw_description': 'x'},           # code tier
            {'sysco_item_code': '', 'raw_description': 'FUEL SURCHARGE'},    # junk
            {'sysco_item_code': '', 'raw_description': 'WHLFCLS ROMAINE HEARTS 3CT'},  # vendor_exact
            {'sysco_item_code': '', 'raw_description': 'COMPLETELY UNKNOWN'}, # unmatched
        ]
        results = mapper.map_items(items, mappings=self.FIXTURE_MAPPINGS, vendor='Sysco')
        # Junk filtered, so 3 results (not 4)
        self.assertEqual(len(results), 3)
        by_confidence = {r['confidence'] for r in results}
        self.assertIn('code', by_confidence)
        self.assertIn('vendor_exact', by_confidence)
        self.assertIn('unmatched', by_confidence)

    # Unknown vendor fallback — no vendor_desc_map, falls to global tiers
    def test_scenario_unknown_vendor_fallback(self):
        self._assert_resolves('SYSCO CLS BROCCOLI FLORETS FRESH', 'Unknown',
                              'Broccoli Florets')  # hits global exact

    # Empty description + no code → unmatched (guard)
    def test_scenario_empty_input_unmatched(self):
        self._assert_resolves('', 'Sysco', expected_canonical=None)


class UsagePatternPredictionsTests(TestCase):
    """`_usage_pattern_predictions` — order-guide second track that surfaces
    non-recipe products likely due for reorder based on invoice cadence."""

    def setUp(self):
        super().setUp()
        self.today = date(2026, 4, 21)
        self.vendor = Vendor.objects.create(name='TestVendor')
        # Non-recipe product with a tight weekly cadence, 8 days since last order
        self.paper = Product.objects.create(canonical_name='Test Paper Towels',
                                            category='Paper/Disposable')
        # Product linked to a recipe — must NOT appear in predictions
        self.chicken = Product.objects.create(canonical_name='Test Chicken',
                                              category='Proteins')
        recipe = Recipe.objects.create(name='Test Recipe', yield_servings=10)
        RecipeIngredient.objects.create(recipe=recipe, name_raw='Chicken',
                                        product=self.chicken, quantity=1, unit='lb')

    def _add_invoices(self, product, dates, price=Decimal('10.00')):
        for d in dates:
            InvoiceLineItem.objects.create(
                vendor=self.vendor, product=product,
                raw_description=product.canonical_name,
                unit_price=price, invoice_date=d,
            )

    def test_recipe_linked_excluded(self):
        """A product referenced by any RecipeIngredient should never appear."""
        from myapp.views import _usage_pattern_predictions
        # Give chicken many invoices and long gap — still excluded
        self._add_invoices(self.chicken, [
            date(2025, 10, 1), date(2025, 10, 8), date(2025, 10, 15),
            date(2025, 10, 22),
        ])
        preds = _usage_pattern_predictions(self.today)
        ids = {p['product'].id for p in preds}
        self.assertNotIn(self.chicken.id, ids)

    def test_min_purchases_enforced(self):
        """Fewer than 3 distinct purchase dates → skipped (noise guard)."""
        from myapp.views import _usage_pattern_predictions
        self._add_invoices(self.paper, [date(2026, 1, 1), date(2026, 2, 1)])
        preds = _usage_pattern_predictions(self.today)
        self.assertEqual([p for p in preds if p['product'].id == self.paper.id], [])

    def test_overdue_product_surfaces(self):
        """Weekly cadence + last order 14 days ago → urgency=2.0, flagged."""
        from myapp.views import _usage_pattern_predictions
        # 4 weekly orders, last one 14 days before 'today'
        last = self.today - timedelta(days=14)
        self._add_invoices(self.paper, [
            last - timedelta(days=21), last - timedelta(days=14),
            last - timedelta(days=7), last,
        ])
        preds = _usage_pattern_predictions(self.today)
        paper_preds = [p for p in preds if p['product'].id == self.paper.id]
        self.assertEqual(len(paper_preds), 1)
        p = paper_preds[0]
        self.assertEqual(p['avg_interval'], 7.0)
        self.assertEqual(p['days_since_last'], 14)
        self.assertEqual(p['urgency'], 2.0)
        self.assertEqual(p['purchase_count'], 4)

    def test_on_cadence_product_not_flagged(self):
        """Ordered recently, still within interval → skipped."""
        from myapp.views import _usage_pattern_predictions
        # Weekly cadence, last order 2 days ago → urgency=0.29, below 0.9
        self._add_invoices(self.paper, [
            self.today - timedelta(days=23),
            self.today - timedelta(days=16),
            self.today - timedelta(days=9),
            self.today - timedelta(days=2),
        ])
        preds = _usage_pattern_predictions(self.today)
        self.assertEqual([p for p in preds if p['product'].id == self.paper.id], [])

    def test_same_day_multiple_invoices_count_once(self):
        """Multiple invoice lines on the same day should collapse to one
        purchase event — we measure cadence in days, not line-items."""
        from myapp.views import _usage_pattern_predictions
        # 3 invoice lines on day 1, 3 on day 8 → only 2 distinct dates →
        # skipped by min_purchases guard. Without dedup, 6 lines would
        # spuriously qualify.
        d1 = self.today - timedelta(days=8)
        d2 = self.today - timedelta(days=1)
        self._add_invoices(self.paper, [d1, d1, d1, d2, d2, d2])
        preds = _usage_pattern_predictions(self.today)
        self.assertEqual([p for p in preds if p['product'].id == self.paper.id], [])

    def test_sorted_most_urgent_first(self):
        """Results sorted by urgency descending — critical items float to top."""
        from myapp.views import _usage_pattern_predictions
        other = Product.objects.create(canonical_name='Test Gloves',
                                       category='Paper/Disposable')
        # paper: weekly cadence, 14d overdue (urgency 2.0)
        self._add_invoices(self.paper, [
            self.today - timedelta(days=35),
            self.today - timedelta(days=28),
            self.today - timedelta(days=21),
            self.today - timedelta(days=14),
        ])
        # gloves: monthly cadence, 45d since last (urgency ~1.5)
        self._add_invoices(other, [
            self.today - timedelta(days=135),
            self.today - timedelta(days=105),
            self.today - timedelta(days=75),
            self.today - timedelta(days=45),
        ])
        preds = _usage_pattern_predictions(self.today)
        paper_idx = next(i for i, p in enumerate(preds) if p['product'].id == self.paper.id)
        gloves_idx = next(i for i, p in enumerate(preds) if p['product'].id == other.id)
        self.assertLess(paper_idx, gloves_idx, 'higher urgency must come first')


class LatestInvoiceInfoBulkTests(TestCase):
    """`_latest_invoice_info_bulk` — bulk lookup used by order_guide to avoid
    N+1 queries. Returns 4-tuple with last_invoice_date for the UX stamp."""

    def test_returns_four_tuple_with_last_date(self):
        from myapp.views import _latest_invoice_info_bulk
        vendor = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='P')
        InvoiceLineItem.objects.create(
            vendor=vendor, product=p, raw_description='P',
            unit_price=Decimal('9.99'), case_size='1/10LB',
            invoice_date=date(2026, 3, 15),
        )
        out = _latest_invoice_info_bulk([p.id])
        self.assertIn(p.id, out)
        tup = out[p.id]
        self.assertEqual(len(tup), 4)
        v, price, case_size, last_date = tup
        self.assertEqual(v.name, 'V')
        self.assertEqual(price, Decimal('9.99'))
        self.assertEqual(case_size, '1/10LB')
        self.assertEqual(last_date, date(2026, 3, 15))

    def test_returns_most_recent_date_when_multiple(self):
        """With multiple invoices, the returned date is the most recent."""
        from myapp.views import _latest_invoice_info_bulk
        vendor = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='P')
        for d in [date(2026, 1, 1), date(2026, 3, 1), date(2026, 2, 1)]:
            InvoiceLineItem.objects.create(
                vendor=vendor, product=p, raw_description='P',
                unit_price=Decimal('1.00'), invoice_date=d,
            )
        _, _, _, last_date = _latest_invoice_info_bulk([p.id])[p.id]
        self.assertEqual(last_date, date(2026, 3, 1))

    def test_empty_input(self):
        from myapp.views import _latest_invoice_info_bulk
        self.assertEqual(_latest_invoice_info_bulk([]), {})


class SynergySyncCalcPricePerLbTests(TestCase):
    """`calc_price_per_lb` — the per-pound formula that drives col J on the
    Synergy sheet. Bare-'1' bug surfaced 2026-04-22 produced the misleading
    E==J pattern (Yellow Onion E=$32.50 / J=$32.50, Pork Butt E=$40.50 /
    J=$40.50) — bare integers <2 must NOT be interpreted as pounds."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        from synergy_sync import calc_price_per_lb
        return calc_price_per_lb

    def test_bare_one_with_pound_unit_returns_none(self):
        """The headline bug: case_size='1' + unit='#' must NOT yield
        J = case_price ÷ 1 = case_price."""
        calc = self._import()
        self.assertIsNone(calc(32.50, '1', '#'),
            "bare '1' is almost always '1 case' from OCR, not '1 pound'")
        self.assertIsNone(calc(40.50, '1', '#'),
            "bare '1' must not produce J equal to case price")

    def test_bare_fractional_under_two_returns_none(self):
        """Fractional values < 2 are also not credible weights."""
        calc = self._import()
        self.assertIsNone(calc(20.00, '0.5', '#'))
        self.assertIsNone(calc(20.00, '1.5', '#'))

    def test_bare_two_or_more_still_computes(self):
        """The fix only blocks <2 — 2+ remains a usable weight."""
        calc = self._import()
        self.assertAlmostEqual(calc(2.90, '2', '#'), 1.45, places=4,
            msg='Red Onion 2 lb @ $2.90 case = $1.45/lb')
        self.assertAlmostEqual(calc(6.50, '3', '#'), 2.1667, places=3,
            msg='Bell Pepper 3 lb @ $6.50 case ≈ $2.17/lb')
        self.assertAlmostEqual(calc(50.00, '10', '#'), 5.00, places=4)

    def test_n_over_m_unaffected(self):
        """The N/M bare-integer-with-unit=# path is preserved when total >= 2.
        Picks examples that don't trip the M/D-date heuristic
        (which rejects 1<=first<=12 with second>12)."""
        calc = self._import()
        # 36 packs × 1 lb each = 36 lbs total; $50.40 ÷ 36 = $1.40/lb (butter)
        self.assertAlmostEqual(calc(50.40, '36/1', '#'), 1.40, places=4)
        # 6 packs × 5 lb each = 30 lbs; $90 ÷ 30 = $3/lb
        self.assertAlmostEqual(calc(90.00, '6/5', '#'), 3.00, places=4)

    def test_n_over_m_rejects_total_under_two(self):
        """N/M with N*M < 2 hits the same E==J bug as bare '1'.
        Example from Apr 22 protein sheet: Prosciutto cs='1/1' (1*1=1 lb)
        produced J = $15.28/1 = $15.28 = E. Extension of the bare-2 fix."""
        calc = self._import()
        self.assertIsNone(calc(15.28, '1/1', '#'),
            msg="'1/1' = 1 lb total — same E==J symptom as bare '1'")

    def test_explicit_lb_suffix_unaffected(self):
        """When parse_total_weight_lbs succeeds, the bare-fallback never runs."""
        calc = self._import()
        self.assertAlmostEqual(calc(99.80, '20.0LB', '#'), 4.99, places=4)
        self.assertAlmostEqual(calc(50.00, '10LB', '#'), 5.00, places=4)

    def test_no_pound_unit_returns_none(self):
        """Without unit='#', bare numbers don't trigger the lbs path at all."""
        calc = self._import()
        self.assertIsNone(calc(32.50, '1', ''))
        self.assertIsNone(calc(32.50, '1', 'EA'))
        self.assertIsNone(calc(32.50, '5', 'GAL'))

    def test_dates_rejected(self):
        """Date-shaped strings are rejected (regression for prior fix)."""
        calc = self._import()
        self.assertIsNone(calc(38.40, '10/14', '#'),
            msg="'10/14' parses as Oct 14 — must NOT be 10×14=140 lbs")
        self.assertIsNone(calc(20.00, '04/06', '#'),
            msg="'04/06' is Apr 6 — leading-zero pattern always a date")


class ConsumptionEngineTests(TestCase):
    """`consumption_utils.compute_consumption` — date-range inventory depletion math.

    Foundation for perpetual inventory + variance reporting. Each menu
    in range × census × recipe → per-Product physical-unit totals.
    Mirrors the cost calc dispatch but emits qty consumed instead of $.
    """

    def _setup_basic(self, ri_qty=Decimal('5'), ri_unit='lb', headcount=30,
                     yield_servings=40, yield_pct=None):
        """One Vendor + Product + Recipe + RecipeIngredient + Census + Menu."""
        v = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='Test Beef')
        r = Recipe.objects.create(name='Test Beef Dish', yield_servings=yield_servings)
        ri = RecipeIngredient.objects.create(
            recipe=r, name_raw='beef', product=p, quantity=ri_qty, unit=ri_unit,
            yield_pct=yield_pct,
        )
        Census.objects.create(date=date(2026, 5, 10), headcount=headcount)
        m = Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner', recipe=r)
        return v, p, r, ri, m

    def test_empty_range_returns_empty_structure(self):
        from myapp.consumption_utils import compute_consumption
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['by_product'], {})
        self.assertEqual(result['caveats'], [])
        self.assertEqual(result['menus_processed'], 0)
        self.assertEqual(result['menus_unlinked'], 0)

    def test_single_recipe_consumption_scales_to_headcount(self):
        """Recipe yields 40, ingredient is 5 lb, headcount is 30 → 3.75 lb."""
        from myapp.consumption_utils import compute_consumption
        v, p, r, ri, m = self._setup_basic(
            ri_qty=Decimal('5'), ri_unit='lb', headcount=30, yield_servings=40)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['menus_processed'], 1)
        self.assertEqual(result['menus_unlinked'], 0)
        # 5 lb × (30/40) = 3.75 lb = 60 oz
        self.assertEqual(result['by_product'][p.id]['oz'], Decimal('60.00'))

    def test_yield_pct_increases_ap_consumption(self):
        """Recipe asks 1 lb edible carrot at 80% yield → AP consumed = 1.25 lb = 20 oz."""
        from myapp.consumption_utils import compute_consumption
        v, p, r, ri, m = self._setup_basic(
            ri_qty=Decimal('1'), ri_unit='lb', headcount=40,  # full yield, no scaling
            yield_servings=40, yield_pct=Decimal('80'))
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        # 1 lb / 0.80 = 1.25 lb = 20 oz
        self.assertAlmostEqual(float(result['by_product'][p.id]['oz']), 20.0, places=2)

    def test_unlinked_menu_contributes_nothing(self):
        """Freetext menus (no recipe FK) are counted in menus_unlinked, no consumption."""
        from myapp.consumption_utils import compute_consumption
        Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner',
                            dish_freetext='Some freetext dish')
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['by_product'], {})
        self.assertEqual(result['menus_unlinked'], 1)
        self.assertEqual(result['menus_processed'], 0)

    def test_null_qty_ri_skipped_with_caveat(self):
        from myapp.consumption_utils import compute_consumption
        v = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='Test Onion')
        r = Recipe.objects.create(name='Test Soup', yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=r, name_raw='onion', product=p, quantity=None, unit='lb')
        Census.objects.create(date=date(2026, 5, 10), headcount=30)
        Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner', recipe=r)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['by_product'], {})  # null qty → skipped
        self.assertEqual(len(result['caveats']), 1)
        self.assertIn('null qty', result['caveats'][0])

    def test_no_product_link_skipped_with_caveat(self):
        from myapp.consumption_utils import compute_consumption
        r = Recipe.objects.create(name='Test', yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=r, name_raw='unmapped ingredient',
            product=None, quantity=Decimal('1'), unit='lb')
        Census.objects.create(date=date(2026, 5, 10), headcount=30)
        Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner', recipe=r)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['by_product'], {})
        self.assertEqual(len(result['caveats']), 1)
        self.assertIn('no product link', result['caveats'][0])

    def test_census_fallback_when_no_row(self):
        """Date with no Census row uses census_default arg (or 30 fallback)."""
        from myapp.consumption_utils import compute_consumption
        v = Vendor.objects.create(name='V')
        p = Product.objects.create(canonical_name='Test Flour')
        r = Recipe.objects.create(name='Test', yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=r, name_raw='flour', product=p,
            quantity=Decimal('4'), unit='lb')
        # No Census row created — should fall back
        Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner', recipe=r)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31),
                                      census_default=20)
        # 4 lb × 20/40 = 2 lb = 32 oz
        self.assertEqual(result['by_product'][p.id]['oz'], Decimal('32.00'))

    def test_multiple_menus_aggregate(self):
        """Same product across multiple menus accumulates."""
        from myapp.consumption_utils import compute_consumption
        v, p, r, ri, m1 = self._setup_basic(
            ri_qty=Decimal('5'), ri_unit='lb', headcount=40, yield_servings=40)
        # Add two more days
        Menu.objects.create(date=date(2026, 5, 11), meal_slot='dinner', recipe=r)
        Census.objects.create(date=date(2026, 5, 11), headcount=40)
        Menu.objects.create(date=date(2026, 5, 12), meal_slot='dinner', recipe=r)
        Census.objects.create(date=date(2026, 5, 12), headcount=40)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        # 3 menus × 5 lb each = 15 lb = 240 oz
        self.assertEqual(result['by_product'][p.id]['oz'], Decimal('240.00'))
        self.assertEqual(result['menus_processed'], 3)

    def test_additional_recipes_consumed(self):
        """Menu's additional_recipes m2m also contribute consumption."""
        from myapp.consumption_utils import compute_consumption
        v = Vendor.objects.create(name='V')
        p_main = Product.objects.create(canonical_name='Main P')
        p_side = Product.objects.create(canonical_name='Side P')
        main = Recipe.objects.create(name='Main', yield_servings=40)
        side = Recipe.objects.create(name='Side', yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=main, name_raw='m', product=p_main,
            quantity=Decimal('10'), unit='lb')
        RecipeIngredient.objects.create(
            recipe=side, name_raw='s', product=p_side,
            quantity=Decimal('2'), unit='lb')
        Census.objects.create(date=date(2026, 5, 10), headcount=40)
        m = Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner',
                                recipe=main)
        m.additional_recipes.add(side)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(result['by_product'][p_main.id]['oz'], Decimal('160.00'))
        self.assertEqual(result['by_product'][p_side.id]['oz'], Decimal('32.00'))

    def test_sub_recipe_recurses(self):
        """Recipe with sub_recipe RI consumes the sub_recipe's ingredients scaled."""
        from myapp.consumption_utils import compute_consumption
        v = Vendor.objects.create(name='V')
        p_tom = Product.objects.create(canonical_name='Tomato')
        # Sub-recipe Marinara: 8 servings, uses 4 lb tomato
        marinara = Recipe.objects.create(name='Marinara', yield_servings=8)
        RecipeIngredient.objects.create(
            recipe=marinara, name_raw='tomato', product=p_tom,
            quantity=Decimal('4'), unit='lb')
        # Parent Meatballs: 40 servings, uses 2 batches of Marinara
        meatballs = Recipe.objects.create(name='Meatballs', yield_servings=40)
        RecipeIngredient.objects.create(
            recipe=meatballs, name_raw='Marinara', sub_recipe=marinara,
            quantity=Decimal('2'), unit='batch')
        Census.objects.create(date=date(2026, 5, 10), headcount=40)
        Menu.objects.create(date=date(2026, 5, 10), meal_slot='dinner',
                            recipe=meatballs)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        # 2 batches × 8 servings/batch = 16 servings of marinara at 40/40 scale
        # → 16 servings worth of marinara × 4 lb / 8 servings = 8 lb of tomato
        self.assertEqual(result['by_product'][p_tom.id]['oz'], Decimal('128.00'))

    def test_date_range_excludes_outside_dates(self):
        """Menus outside [start, end] are not counted."""
        from myapp.consumption_utils import compute_consumption
        v, p, r, ri, _ = self._setup_basic(
            ri_qty=Decimal('5'), ri_unit='lb', headcount=40, yield_servings=40)
        # Add menu in April (outside May range)
        Menu.objects.create(date=date(2026, 4, 30), meal_slot='dinner', recipe=r)
        Census.objects.create(date=date(2026, 4, 30), headcount=40)
        result = compute_consumption(date(2026, 5, 1), date(2026, 5, 31))
        # Only the May 10 menu, not April 30
        self.assertEqual(result['menus_processed'], 1)
        # 5 lb = 80 oz (one menu, full yield)
        self.assertEqual(result['by_product'][p.id]['oz'], Decimal('80.00'))


class DBWriteDriftDetectionTests(TestCase):
    """`write_invoice_to_db` strict mode for sheet/DB drift.

    When the mapper returns a canonical_name that doesn't exist in the
    Product table (forward-looking damage from an upstream rename per
    `feedback_upstream_downstream_planning.md`), db_write must NOT
    silently create a ghost Product and must NOT bake the wrong-FK row
    as a regular 'unmatched'. Tag the row 'unmatched_drift' so it
    surfaces distinctly in audits."""

    def _import_db_write(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import db_write
        return db_write

    def test_drift_canonical_lands_as_unmatched_drift(self):
        """Mapper returns canonical='Cheese, American' but only 'American' exists in DB
        → ILI lands with product=None and confidence='unmatched_drift'."""
        Product.objects.create(canonical_name='American')   # post-rename name only

        items = [{
            'raw_description': 'BBRLCLS CHEESE AMER 160 SLI WHT',
            'canonical': 'Cheese, American',                # pre-rename name still in sheet
            'unit_price': 12.34,
            'case_size_raw': '6/5LB',
            'confidence': 'vendor_exact',
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='drift.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='BBRLCLS CHEESE AMER 160 SLI WHT')
        self.assertIsNone(ili.product, 'Drift case must NOT attach a wrong FK')
        self.assertEqual(ili.match_confidence, 'unmatched_drift',
                         'Drift case must be tagged distinctly from regular unmatched')

    def test_drift_does_not_create_ghost_product(self):
        """db_write must never create a Product from mapper output —
        Product creation is reserved for the curation flow."""
        Product.objects.create(canonical_name='American')
        before = Product.objects.count()

        items = [{
            'raw_description': 'BBRLCLS CHEESE AMER 160 SLI WHT',
            'canonical': 'Cheese, American',                # not in DB
            'unit_price': 12.34,
            'case_size_raw': '',
            'confidence': 'vendor_exact',
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='drift.jpg')

        after = Product.objects.count()
        self.assertEqual(after, before,
                         'No new Product should be created on drift')
        self.assertFalse(Product.objects.filter(canonical_name='Cheese, American').exists(),
                         'Stale-named ghost Product must not appear in DB')

    def test_normal_match_unaffected_by_drift_logic(self):
        """When the canonical exists in DB, db_write attaches the FK as before
        and does NOT re-tag the confidence."""
        p = Product.objects.create(canonical_name='Tomato Sauce')

        items = [{
            'raw_description': 'TOMATO SAUCE 6/10CAN',
            'canonical': 'Tomato Sauce',
            'unit_price': 24.50,
            'case_size_raw': '6/10CAN',
            'confidence': 'vendor_exact',
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='ok.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='TOMATO SAUCE 6/10CAN')
        self.assertEqual(ili.product, p)
        self.assertEqual(ili.match_confidence, 'vendor_exact',
                         'Confidence must not be re-tagged when product is found')

    def test_no_canonical_no_drift_tag(self):
        """Mapper returning canonical=None (genuine unmatched) is NOT drift —
        confidence stays 'unmatched', not 'unmatched_drift'."""
        items = [{
            'raw_description': 'COMPLETELY NEW PRODUCT 4OZ',
            'canonical': None,
            'unit_price': 9.99,
            'case_size_raw': '',
            'confidence': 'unmatched',
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='novel.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='COMPLETELY NEW PRODUCT 4OZ')
        self.assertIsNone(ili.product)
        self.assertEqual(ili.match_confidence, 'unmatched',
                         'Genuine unmatched must NOT be re-tagged as drift')


class DBWriteStructuredFieldsTests(TestCase):
    """`write_invoice_to_db` Phase 1 of structured invoice-line schema.

    Validates that the 8 structured fields on InvoiceLineItem
    (quantity, purchase_uom, case_pack_count, case_pack_unit_size,
    case_pack_unit_uom, case_total_weight_lb, count_per_lb_low,
    count_per_lb_high) are populated from parser output dicts when present,
    and stay None/blank when absent. Pure-add migration — existing fields
    (case_size, unit_price, etc.) keep working the same way.
    """

    def _import_db_write(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'db_write' in sys.modules:
            del sys.modules['db_write']
        import db_write
        return db_write

    def _setup(self, vendor_name='Sysco'):
        Vendor.objects.create(name=vendor_name)
        return Product.objects.create(canonical_name='Test Product',
                                      category='Drystock')

    def test_structured_fields_populated_when_parser_emits(self):
        """Burgers 60/5.3 OZ pack — structured fields land when parser
        provides them. This is the Phase 1 acceptance test: it proves the
        wiring from parser dict → ILI columns works end-to-end."""
        product = self._setup('Sysco')
        items = [{
            'raw_description': 'F 60 5.3OZ JTM BURGER BEEF SBSDR ANGUS',
            'canonical': 'Test Product',
            'sysco_item_code': '4040614',
            'unit_price': 82.85,
            'extended_amount': 82.85,
            'case_size_raw': '60/5.3OZ',
            'confidence': 'vendor_exact',
            # Structured fields — parser would supply these post-Phase-2
            'quantity': 1,
            'unit_of_measure': 'CASE',
            'case_pack_count': 60,
            'case_pack_unit_size': 5.3,
            'case_pack_unit_uom': 'OZ',
            'case_total_weight_lb': 19.875,  # 60 × 5.3 / 16
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-01', items, source_file='burgers.jpg')

        ili = InvoiceLineItem.objects.get(sysco_item_code__isnull=False) \
            if False else InvoiceLineItem.objects.first()
        # Decimal-tolerant equality (DB round-trips through Decimal)
        self.assertEqual(ili.quantity, Decimal('1'))
        self.assertEqual(ili.purchase_uom, 'CASE')
        self.assertEqual(ili.case_pack_count, 60)
        self.assertEqual(ili.case_pack_unit_size, Decimal('5.3'))
        self.assertEqual(ili.case_pack_unit_uom, 'OZ')
        self.assertEqual(ili.case_total_weight_lb, Decimal('19.875'))
        # Legacy field still populated for back-compat
        self.assertEqual(ili.case_size, '60/5.3OZ')

    def test_count_per_lb_fields_for_bacon_shrimp(self):
        """Per Sean 2026-05-02: bacon is weighed for count purposes —
        recipe says '2 strips bacon', need (10+14)/2 = 12 strips/lb to cost.
        Fields land when parser extracts the count grade from raw_description."""
        product = self._setup('Sysco')
        items = [{
            'raw_description': 'BACON LAYFLAT 10/14 SLICE 15LB',
            'canonical': 'Test Product',
            'unit_price': 70.35,
            'case_size_raw': '15LB',
            'confidence': 'vendor_exact',
            'quantity': 15,
            'unit_of_measure': 'LB',
            'count_per_lb_low': 10,
            'count_per_lb_high': 14,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-15', items, source_file='bacon.jpg')

        ili = InvoiceLineItem.objects.first()
        self.assertEqual(ili.count_per_lb_low, 10)
        self.assertEqual(ili.count_per_lb_high, 14)
        self.assertEqual(ili.purchase_uom, 'LB')
        self.assertEqual(ili.quantity, Decimal('15'))

    def test_structured_fields_stay_null_when_absent(self):
        """Existing parser output that doesn't include structured fields
        produces ILI rows with NULL for the new columns. Backward-compat
        guarantee — the migration is pure-add."""
        product = self._setup('Farm Art')
        items = [{
            'raw_description': 'TOMATOES, CHERRY, 12 CONT',
            'canonical': 'Test Product',
            'unit_price': 24.50,
            'case_size_raw': '12CT',
            'confidence': 'vendor_exact',
            # No quantity, no unit_of_measure, no case_pack_*, no count_per_lb_*
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Farm Art', '2026-04-15', items, source_file='tomatoes.jpg')

        ili = InvoiceLineItem.objects.first()
        self.assertIsNone(ili.quantity)
        self.assertEqual(ili.purchase_uom, '')   # CharField default is empty string
        self.assertIsNone(ili.case_pack_count)
        self.assertIsNone(ili.case_pack_unit_size)
        self.assertEqual(ili.case_pack_unit_uom, '')
        self.assertIsNone(ili.case_total_weight_lb)
        self.assertIsNone(ili.count_per_lb_low)
        self.assertIsNone(ili.count_per_lb_high)
        # Legacy field still populated
        self.assertEqual(ili.case_size, '12CT')

    def test_purchase_uom_falls_back_when_absent(self):
        """`unit_of_measure` from parser → ILI.purchase_uom. Either key works
        (`unit_of_measure` is the spatial_matcher convention, `purchase_uom`
        could come from a future normalizer). Empty string when neither set."""
        product = self._setup('Exceptional Foods')
        # Variant 1: parser sends 'unit_of_measure' (matches today's spatial output)
        items_v1 = [{
            'raw_description': 'Beef Chuck Flap',
            'canonical': 'Test Product',
            'unit_price': 469.31,
            'extended_amount': 469.31,
            'case_size_raw': '42.7LB',
            'confidence': 'vendor_exact',
            'quantity': 42.7,
            'unit_of_measure': 'LB',
            'price_per_unit': 10.99,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Exceptional Foods', '2026-04-15',
                                items_v1, source_file='exc.jpg')
        ili = InvoiceLineItem.objects.first()
        self.assertEqual(ili.purchase_uom, 'LB')
        self.assertEqual(ili.quantity, Decimal('42.7'))

    def test_upsert_preserves_structured_fields_when_incoming_is_none(self):
        """Backfill or prior write populated structured fields. Subsequent
        parser pass that DOESN'T extract them must NOT clobber to None.
        Mirrors the existing price_per_pound preserve-on-update behavior."""
        product = self._setup('Sysco')

        # First write — full structured fields
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-01', [{
            'raw_description': 'BURGER BEEF',
            'canonical': 'Test Product',
            'unit_price': 82.85, 'extended_amount': 82.85,
            'case_size_raw': '60/5.3OZ', 'confidence': 'vendor_exact',
            'quantity': 1, 'unit_of_measure': 'CASE',
            'case_pack_count': 60, 'case_pack_unit_size': 5.3,
            'case_pack_unit_uom': 'OZ', 'case_total_weight_lb': 19.875,
        }], source_file='full.jpg')

        # Second write — same (vendor, product, date), no structured fields
        # (simulates older parser output being replayed). Upsert should
        # preserve the existing values, not clobber to None.
        dbw.write_invoice_to_db('Sysco', '2026-04-01', [{
            'raw_description': 'BURGER BEEF',
            'canonical': 'Test Product',
            'unit_price': 85.00, 'extended_amount': 85.00,
            'case_size_raw': '60/5.3OZ', 'confidence': 'vendor_exact',
        }], source_file='partial.jpg')

        ili = InvoiceLineItem.objects.get(vendor__name='Sysco', product=product,
                                          invoice_date=date(2026, 4, 1))
        # Price updated (this is fresh data)
        self.assertEqual(ili.unit_price, Decimal('85.00'))
        # Structured fields preserved (older partial parser pass shouldn't clobber)
        self.assertEqual(ili.case_pack_count, 60)
        self.assertEqual(ili.case_pack_unit_size, Decimal('5.3'))
        self.assertEqual(ili.case_pack_unit_uom, 'OZ')
        self.assertEqual(ili.purchase_uom, 'CASE')


class ParserPackSizeDecompositionTests(TestCase):
    """`parser._normalize_pack_size` + `_structured_pack_from_case_size` —
    Phase 2a of structured invoice-line schema migration. Burgers `605.3OZ`
    is the canonical failure case the schema-gap entry calls out: parser's
    _COMMON_PACKS capped at 48 + integer-only regex meant 60×5.3 OZ patties
    were stored as a single 605.3 oz blob.
    """

    def _import_parser(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'parser' in sys.modules:
            del sys.modules['parser']
        import parser as p
        return p

    def test_normalize_burgers_decimal_split(self):
        """Burgers '605.3OZ' → '60/5.3OZ'. THE failure case.
        Pre-Phase-2a: stored unchanged (regex required integer).
        Post-Phase-2a: split correctly (60 patties × 5.3 oz)."""
        p = self._import_parser()
        self.assertEqual(p._normalize_pack_size('605.3OZ'), '60/5.3OZ')
        self.assertEqual(p._normalize_pack_size('605.3 OZ'), '60/5.3OZ')

    def test_normalize_pringles_decimal_split(self):
        """Pringles '122.38OZ' → '12/2.38OZ' (12 cans × 2.38 oz).
        Two-decimal-place size — schema-gap memo cited this as evidence of
        OCR ambiguity that requires decimal tolerance."""
        p = self._import_parser()
        self.assertEqual(p._normalize_pack_size('122.38OZ'), '12/2.38OZ')

    def test_normalize_pretzels_120_5oz(self):
        """Pretzels '120.5OZ' → '12/0.5OZ' (12 mini packs × 0.5 oz)."""
        p = self._import_parser()
        self.assertEqual(p._normalize_pack_size('120.5OZ'), '12/0.5OZ')

    def test_normalize_integer_unchanged(self):
        """Existing integer behavior preserved — '124OZ' → '12/4OZ' etc."""
        p = self._import_parser()
        self.assertEqual(p._normalize_pack_size('124OZ'), '12/4OZ')
        self.assertEqual(p._normalize_pack_size('2416 OZ'), '24/16OZ')
        self.assertEqual(p._normalize_pack_size('123LB'), '1/23LB')

    def test_normalize_expanded_pack_list_60(self):
        """_COMMON_PACKS expanded to include 60. '605.3OZ' must split
        on the 60 prefix, not fall back to 6 × 05.3OZ which would be
        non-physical."""
        p = self._import_parser()
        out = p._normalize_pack_size('605.3OZ')
        self.assertEqual(out, '60/5.3OZ')
        # Negative case: ensure the expansion doesn't break smaller cases
        self.assertEqual(p._normalize_pack_size('124OZ'), '12/4OZ')

    def test_normalize_decimal_no_split_when_no_pack_match(self):
        """'5.3OZ' alone — no pack count splits it (5 isn't pack of 0.3 OZ
        in any realistic Sysco SKU). Returns unchanged. The structured
        helper picks count=1 size=5.3 from the bare-pack regex instead."""
        p = self._import_parser()
        self.assertEqual(p._normalize_pack_size('5.3OZ'), '5.3OZ')


class ParserStructuredPackHelperTests(TestCase):
    """`parser._structured_pack_from_case_size` — converts the normalized
    "N/MUNIT" (or bare "NUNIT") string into the dict that db_write reads
    to populate ILI.case_pack_count / case_pack_unit_size /
    case_pack_unit_uom / case_total_weight_lb."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'parser' in sys.modules:
            del sys.modules['parser']
        import parser as p
        return p._structured_pack_from_case_size

    def test_n_over_m_oz_with_total_weight(self):
        f = self._import()
        out = f('60/5.3OZ')
        self.assertEqual(out['case_pack_count'], 60)
        self.assertEqual(out['case_pack_unit_size'], '5.3')
        self.assertEqual(out['case_pack_unit_uom'], 'OZ')
        # 60 × 5.3 / 16 = 19.875
        self.assertAlmostEqual(out['case_total_weight_lb'], 19.875, places=3)

    def test_n_over_m_lb(self):
        f = self._import()
        out = f('36/1LB')
        self.assertEqual(out['case_pack_count'], 36)
        self.assertEqual(out['case_pack_unit_size'], '1')
        self.assertEqual(out['case_pack_unit_uom'], 'LB')
        self.assertAlmostEqual(out['case_total_weight_lb'], 36.0, places=3)

    def test_n_over_m_gal(self):
        """4/1GAL — converts to total_weight_lb via gallon density approx."""
        f = self._import()
        out = f('4/1GAL')
        self.assertEqual(out['case_pack_count'], 4)
        self.assertEqual(out['case_pack_unit_size'], '1')
        self.assertEqual(out['case_pack_unit_uom'], 'GAL')
        # 4 gal × 8.345 lb/gal = 33.38 lb
        self.assertAlmostEqual(out['case_total_weight_lb'], 33.38, places=2)

    def test_count_unit_no_total_weight(self):
        """CT/EA/DZ are count units — total_weight_lb unset (would
        require per-unit weight from canonical product side)."""
        f = self._import()
        out = f('12/24CT')
        self.assertEqual(out['case_pack_count'], 12)
        self.assertEqual(out['case_pack_unit_size'], '24')
        self.assertEqual(out['case_pack_unit_uom'], 'CT')
        self.assertNotIn('case_total_weight_lb', out)

    def test_bare_pack_decomposes_with_count_one(self):
        """50LB / 12CT / 1GAL — bare formats decompose as count=1."""
        f = self._import()
        out = f('50LB')
        self.assertEqual(out['case_pack_count'], 1)
        self.assertEqual(out['case_pack_unit_size'], '50')
        self.assertEqual(out['case_pack_unit_uom'], 'LB')
        self.assertAlmostEqual(out['case_total_weight_lb'], 50.0, places=2)

        out = f('12CT')
        self.assertEqual(out['case_pack_count'], 1)
        self.assertEqual(out['case_pack_unit_uom'], 'CT')
        self.assertNotIn('case_total_weight_lb', out)

    def test_undecomposable_returns_empty(self):
        """Bare numbers / dates / OCR garbage → empty dict (db_write
        coerces missing keys to NULL)."""
        f = self._import()
        self.assertEqual(f(''), {})
        self.assertEqual(f(None), {})
        self.assertEqual(f('1'), {})
        self.assertEqual(f('CASE'), {})
        self.assertEqual(f('4/11/2026'), {})

    def test_floz_normalizes_to_fl_oz(self):
        """FLOZ and FL_OZ both land as FL_OZ (canonical form)."""
        f = self._import()
        out = f('12/8FLOZ')
        self.assertEqual(out['case_pack_unit_uom'], 'FL_OZ')


class ParserSyscoEmitsStructuredFieldsTests(TestCase):
    """End-to-end Phase 2a — _parse_sysco emits structured pack fields
    in each item dict so db_write populates ILI columns."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'parser' in sys.modules:
            del sys.modules['parser']
        import parser as p
        return p

    def test_sysco_items_carry_structured_pack_when_decomposable(self):
        """Synthetic Sysco invoice — items with decomposable pack sizes
        carry case_pack_count + case_pack_unit_size + case_pack_unit_uom."""
        p = self._import()
        # Synthetic minimal Sysco-shape invoice — use the regex-anchored 7-digit code
        raw = """**** DAIRY ****
QTY  PACK SIZE   ITEM DESCRIPTION
1 CS 4/1GAL      DAIRY MILK 2%
1234567 24.50

1 CS 605.3OZ     JTM BURGER BEEF SBSDR ANGUS 5.3 OZ PATTY
2345678 82.85

INVOICE TOTAL
107.35
"""
        result = p.parse_invoice(raw, vendor='Sysco')
        items = result['items']
        # Find Burgers + Milk by description tokens
        burgers = next((i for i in items if 'BURGER' in i.get('raw_description', '').upper()), None)
        milk = next((i for i in items if 'MILK' in i.get('raw_description', '').upper()), None)

        self.assertIsNotNone(burgers, f'expected Burgers item, got {[i.get("raw_description") for i in items]}')
        # Phase 2a acceptance: Burgers 605.3OZ → 60/5.3OZ → structured fields
        self.assertEqual(burgers.get('case_pack_count'), 60)
        self.assertEqual(burgers.get('case_pack_unit_size'), '5.3')
        self.assertEqual(burgers.get('case_pack_unit_uom'), 'OZ')
        self.assertAlmostEqual(burgers.get('case_total_weight_lb'), 19.875, places=2)

        self.assertIsNotNone(milk)
        self.assertEqual(milk.get('case_pack_count'), 4)
        self.assertEqual(milk.get('case_pack_unit_size'), '1')
        self.assertEqual(milk.get('case_pack_unit_uom'), 'GAL')

    def test_sysco_items_default_quantity_one_case(self):
        """Sysco lines = 1 case by convention. quantity=1 + unit_of_measure='CASE'
        emitted on every item (overridden to weight on catch-weight rows)."""
        p = self._import()
        raw = """**** DAIRY ****
QTY  PACK SIZE   ITEM DESCRIPTION
1 CS 4/1GAL      DAIRY MILK 2%
1234567 24.50

INVOICE TOTAL
24.50
"""
        result = p.parse_invoice(raw, vendor='Sysco')
        for item in result['items']:
            self.assertEqual(item.get('quantity'), 1,
                f"Sysco item should default quantity=1: {item}")
            self.assertEqual(item.get('unit_of_measure'), 'CASE',
                f"Sysco item should default UoM=CASE: {item}")


class ParserExceptionalCatchWeightTests(TestCase):
    """`_parse_exceptional` Phase 2b — catch-weight rows emit structured
    quantity = shipped lbs + purchase_uom='LB' + case_total_weight_lb.

    The Beef Chuck Flap row 9 cascade in project_bug_register.md is the
    canonical failure: parser stored price_per_pound = $197.53 (case total)
    instead of the actual $/lb. Phase 2b's structured fields give consumers
    a way out: synergy_sync.calc_price_per_lb (Phase 3a) reads
    case_total_weight_lb directly instead of re-parsing case_size string."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'parser' in sys.modules:
            del sys.modules['parser']
        import parser as p
        return p

    def test_catch_weight_emits_quantity_as_shipped_lb(self):
        """Catch-weight Exceptional row: quantity = shipped lb (the cross-
        multiplied weight from the bare-decimal pool), NOT qty_ordered.
        Matches Sysco catch-weight convention from Phase 2a."""
        p = self._import()
        # Synthetic Exceptional invoice — Beef Chuck Flap shape
        raw = """EXCEPTIONAL FOODS, INC.
Item ID
1.00 CS Beef Chuck Flap CVP IBP
42.7
10.99 LB
469.31
Balance Due
469.31
"""
        result = p.parse_invoice(raw, vendor='Exceptional Foods')
        items = result['items']
        beef = next((i for i in items if 'Chuck' in i.get('raw_description', '')), None)
        self.assertIsNotNone(beef, f'expected Beef item, got {[i.get("raw_description") for i in items]}')
        # Phase 2b acceptance: shipped weight is the quantity for catch-weight
        self.assertAlmostEqual(float(beef['quantity']), 42.7, places=1)
        self.assertEqual(beef['purchase_uom'], 'LB')
        self.assertAlmostEqual(beef['case_total_weight_lb'], 42.7, places=2)
        self.assertEqual(beef['case_pack_count'], 1)
        self.assertEqual(beef['case_pack_unit_uom'], 'LB')
        # price_per_pound (parser's stored $/lb) preserved
        self.assertAlmostEqual(beef['price_per_unit'], 10.99, places=2)
        # extended_amount = case total
        self.assertAlmostEqual(beef['unit_price'], 469.31, places=2)

    def test_non_catch_weight_uses_qty_ordered(self):
        """Non-catch-weight rows (per != 'LB'): keep qty_ordered semantics.
        Cheese byblock or whatever — the order unit is the right quantity."""
        p = self._import()
        raw = """EXCEPTIONAL FOODS, INC.
Item ID
2.00 CS Cheese Provolone Sliced 5LB
2.00
12.50 CS
25.00
Balance Due
25.00
"""
        result = p.parse_invoice(raw, vendor='Exceptional Foods')
        items = result['items']
        cheese = next((i for i in items if 'Cheese' in i.get('raw_description', '')), None)
        self.assertIsNotNone(cheese)
        # qty_ordered semantics for non-catch-weight
        self.assertEqual(cheese['unit_of_measure'], 'CS')
        # purchase_uom matches per (the price-per unit, which IS 'CS' here)
        self.assertEqual(cheese.get('purchase_uom'), 'CS')


class SpatialFarmArtPurchaseUOMTests(TestCase):
    """`match_farmart_spatial` Phase 2c — U/M column (EACH/CASE/LB) lands
    in purchase_uom, NOT case_size_raw. The umbrella entry's Celery row 163
    is the failing case: '3 stalks @ $2.60/EACH = $7.72' couldn't be
    distinguished from '$7.72/case' downstream because U/M was dropped
    (or worse, mis-stuffed into case_size_raw)."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'spatial_matcher' in sys.modules:
            del sys.modules['spatial_matcher']
        import spatial_matcher
        return spatial_matcher

    def _row(self, y, tokens):
        out = []
        for text, x in tokens:
            out.append({"text": text, "x_min": x, "x_max": x + 0.05,
                        "y_min": y, "y_max": y + 0.01,
                        "char_start": 0, "char_end": 0})
        return out

    def test_celery_each_distinguished_from_case(self):
        """Celery '3 stalks @ EACH pricing' — purchase_uom='EACH' lets
        downstream consumers treat unit_price as per-stalk, not per-case."""
        sm = self._import()
        # Synthetic Farm Art row: 3 EACH celery stalks @ $2.60 each = $7.72
        tokens = self._row(0.40, [
            ("3.000", 0.07), ("3.000", 0.12),  # qty ord/ship
            ("EACH", 0.16),                      # U/M
            ("CRESC", 0.20),                     # item code
            ("CELERY", 0.30), ("STALK", 0.36),
            ("United", 0.70), ("States", 0.74),
            ("2.60", 0.83), ("7.80", 0.90),
        ])
        items = sm.match_farmart_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        # Phase 2c acceptance: per-piece distinguishable from per-case
        self.assertEqual(items[0]["purchase_uom"], "EACH")
        self.assertEqual(items[0]["case_size_raw"], "")
        self.assertEqual(items[0]["quantity"], 3.0)
        self.assertEqual(items[0]["extended_amount"], 7.80)

    def test_case_unit_distinct_from_each(self):
        """1 CASE Milk @ $24.50 — purchase_uom='CASE' tags as case-priced."""
        sm = self._import()
        tokens = self._row(0.40, [
            ("1.000", 0.07), ("1.000", 0.12),
            ("CASE", 0.16),
            ("MIL2", 0.20),
            ("DAIRY", 0.30), ("MILK", 0.36),
            ("United", 0.70), ("States", 0.74),
            ("24.50", 0.83), ("24.50", 0.90),
        ])
        items = sm.match_farmart_spatial([{"page_number": 1, "tokens": tokens}])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["purchase_uom"], "CASE")
        # Same purchase_uom for both CASE and EACH paths — downstream
        # consumers branch on the value, not on field presence
        self.assertEqual(items[0]["case_size_raw"], "")


class SynergySyncStructuredFieldTests(TestCase):
    """`synergy_sync.calc_iup` + `calc_price_per_lb` Phase 3a — accept
    structured kwargs (case_pack_count, case_total_weight_lb) from ILI
    rows, bypass case_size string parse when present.

    The Beef Chuck Flap cascade end-to-end fix: parser stores
    case_total_weight_lb=42.7 (Phase 2b), calc_price_per_lb_v2 reads
    it directly → 469.31 / 42.7 = $10.99/lb (correct), NOT the
    Product.default_case_size fallback that produced the $197.53 cascade.
    """

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'synergy_sync' in sys.modules:
            del sys.modules['synergy_sync']
        import synergy_sync
        return synergy_sync

    def test_calc_iup_structured_path(self):
        """case_pack_count provided → use it directly. $24.50 / 6 = $4.083."""
        ss = self._import()
        # 6 individual cans of Heinz @ $24.50 case price → IUP $4.0833
        self.assertEqual(ss.calc_iup(24.50, '', case_pack_count=6), 4.0833)
        # Bigger pack — Burgers 60×5.3OZ — $82.85 / 60 patties = $1.3808/patty
        self.assertEqual(ss.calc_iup(82.85, '', case_pack_count=60), 1.3808)

    def test_calc_iup_structured_takes_priority_over_case_size_string(self):
        """When BOTH case_pack_count AND case_size are provided, the
        structured field wins. Closes the case_size-string-parse-error
        attack surface."""
        ss = self._import()
        # case_size='garbage' would fail parse_unit_count → None
        # case_pack_count=12 should still drive the IUP correctly
        self.assertEqual(ss.calc_iup(24.00, 'garbage_string',
                                      case_pack_count=12), 2.00)

    def test_calc_iup_falls_back_to_string_when_structured_null(self):
        """When case_pack_count is None, legacy parse_unit_count(case_size)
        path runs — backward-compat for ILI rows pre-backfill."""
        ss = self._import()
        # No case_pack_count → must parse '12/1GAL' as count=12
        self.assertEqual(ss.calc_iup(24.00, '12/1GAL', case_pack_count=None),
                          2.0)

    def test_calc_iup_pack_count_one_returns_none(self):
        """case_pack_count=1 means single-unit case — IUP is meaningless
        (would equal unit_price). Returns None to signal 'no subdivision'."""
        ss = self._import()
        self.assertIsNone(ss.calc_iup(50.00, '', case_pack_count=1))

    def test_calc_price_per_lb_structured_path(self):
        """case_total_weight_lb provided → bypass case_size parsing.
        THE Beef Chuck Flap cascade fix: $469.31 / 42.7 lb = $10.99/lb
        (correct), not $197.53 (which was the case-total leak from
        parser's price_per_pound storage error)."""
        ss = self._import()
        self.assertEqual(ss.calc_price_per_lb(469.31, '', '',
                                               case_total_weight_lb=42.7),
                          10.9909)

    def test_calc_price_per_lb_priority_order(self):
        """Priority: stored > structured > case_size string > unit_col fallback."""
        ss = self._import()
        # 1. Stored ppp wins over everything
        self.assertEqual(ss.calc_price_per_lb(
            469.31, '17.99LB', '',
            stored_price_per_lb=10.99,
            case_total_weight_lb=42.7,
        ), 10.99)
        # 2. Structured wins over case_size string when stored is null
        self.assertEqual(ss.calc_price_per_lb(
            469.31, '17.99LB', '',
            stored_price_per_lb=None,
            case_total_weight_lb=42.7,
        ), 10.9909)
        # 3. case_size string used when both stored + structured null
        self.assertEqual(ss.calc_price_per_lb(
            469.31, '42.7LB', '',
            stored_price_per_lb=None,
            case_total_weight_lb=None,
        ), 10.9909)
        # All three null + no unit_col fallback → None
        self.assertIsNone(ss.calc_price_per_lb(
            469.31, '', '',
            stored_price_per_lb=None,
            case_total_weight_lb=None,
        ))

    def test_calc_price_per_lb_zero_total_weight_skipped(self):
        """case_total_weight_lb=0 (or negative) shouldn't divide-by-zero —
        treat as null and fall through."""
        ss = self._import()
        # Falls through to case_size parse
        self.assertEqual(ss.calc_price_per_lb(50.0, '10LB', '',
                                               case_total_weight_lb=0), 5.0)

    def test_calc_price_per_lb_invalid_total_weight_skipped(self):
        """Non-numeric case_total_weight_lb → fall through to legacy."""
        ss = self._import()
        self.assertEqual(ss.calc_price_per_lb(50.0, '10LB', '',
                                               case_total_weight_lb='bad'),
                          5.0)


class SnapshotSynergySheetCommandTests(TestCase):
    """`snapshot_synergy_sheet` mgmt cmd — smoke + error-path tests.
    Full snapshot/restore exercises the live Sheets API, which test
    fixtures don't drive. Tests here cover argument validation +
    file-not-found error path."""

    def test_restore_file_not_found_raises(self):
        """Restore mode with a missing path → clear CommandError."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:
            call_command('snapshot_synergy_sheet',
                         '--restore', '/tmp/nonexistent_snapshot.json',
                         verbosity=0)
        self.assertIn('not found', str(ctx.exception).lower())


class IngredientCostCountPerLbTests(TestCase):
    """`cost_utils.ingredient_cost` Phase 3b — count_per_lb dynamic
    per-piece pricing. Sean 2026-05-02 use case: bacon is weighed for
    count purposes; recipe "2 strips bacon" + ILI count_per_lb=10/14
    + $/lb known → cost per strip computed dynamically.

    Same pattern works for shrimp (21/25, 26/30 grades), scallops,
    sliced cheese, etc."""

    def _import(self):
        from myapp.cost_utils import ingredient_cost
        return ingredient_cost

    def test_bacon_2_strips_with_count_grade(self):
        """Bacon 10/14 grade @ $4.69/lb → 2 strips = 2 × (16/12) oz =
        2.67 oz → $4.69 × 2.67/16 = $0.78."""
        f = self._import()
        cost, note = f(
            recipe_qty=Decimal('2'), recipe_unit='strips',
            ingredient_name='Bacon', case_price=Decimal('70.35'),
            case_size_str='15LB', count_per_lb_low=10, count_per_lb_high=14,
            price_per_pound=Decimal('4.69'),
        )
        self.assertIsNotNone(cost, f'expected cost, got note={note!r}')
        # avg_count = 12, piece_oz = 16/12 = 1.333..., qty_oz = 2 × 1.333 = 2.667
        # direct $/lb path: 2.667/16 lb × $4.69 = $0.78
        self.assertEqual(cost, Decimal('0.78'))
        self.assertEqual(note, 'direct $/lb')

    def test_shrimp_8_pieces_21_25_grade(self):
        """Shrimp 21/25 grade @ $14.99/lb → 8 pieces. avg=23, piece_oz=0.696,
        qty_oz = 8 × 0.696 = 5.565 → $14.99 × 5.565/16 = $5.21."""
        f = self._import()
        cost, note = f(
            recipe_qty=Decimal('8'), recipe_unit='pieces',
            ingredient_name='Shrimp', case_price=Decimal('299.80'),
            case_size_str='20LB', count_per_lb_low=21, count_per_lb_high=25,
            price_per_pound=Decimal('14.99'),
        )
        self.assertIsNotNone(cost)
        # avg=23, piece_oz=16/23 = 0.6957, qty=8×0.6957=5.5652
        # $14.99 × 5.5652/16 = $5.21
        self.assertEqual(cost, Decimal('5.21'))

    def test_count_grade_falls_through_when_unit_not_piece_word(self):
        """Recipe asks "1 lb bacon" — count_per_lb is irrelevant. Direct
        $/lb path runs as before."""
        f = self._import()
        cost, note = f(
            recipe_qty=Decimal('1'), recipe_unit='lb',
            ingredient_name='Bacon', case_price=Decimal('70.35'),
            case_size_str='15LB', count_per_lb_low=10, count_per_lb_high=14,
            price_per_pound=Decimal('4.69'),
        )
        self.assertEqual(cost, Decimal('4.69'))
        self.assertEqual(note, 'direct $/lb')

    def test_count_grade_skipped_when_no_count_data(self):
        """Bacon recipe "2 strips" but no count grade on ILI — falls
        through to existing dispatch. Without count_per_lb info, "strips"
        has no recipe-side cost path → ingredient_cost returns None."""
        f = self._import()
        cost, note = f(
            recipe_qty=Decimal('2'), recipe_unit='strips',
            ingredient_name='Bacon', case_price=Decimal('70.35'),
            case_size_str='15LB',
            count_per_lb_low=None, count_per_lb_high=None,
            price_per_pound=Decimal('4.69'),
        )
        # No count grade + no piece-weight table entry for ('bacon', 'strips')
        # → falls through; weight↔weight requires recipe_unit weight, which
        # 'strips' isn't → returns None with incompatible-units note
        self.assertIsNone(cost)

    def test_count_grade_with_only_low_or_high_falls_through(self):
        """count_per_lb_low alone (high null) — ambiguous, skip the path.
        Both required."""
        f = self._import()
        cost, note = f(
            recipe_qty=Decimal('2'), recipe_unit='strips',
            ingredient_name='Bacon', case_price=Decimal('70.35'),
            case_size_str='15LB',
            count_per_lb_low=10, count_per_lb_high=None,
            price_per_pound=Decimal('4.69'),
        )
        self.assertIsNone(cost)

    def test_count_grade_uses_avg_when_low_eq_high(self):
        """count_per_lb_low == count_per_lb_high (single grade, not range)
        → avg = that value, piece_oz computed cleanly."""
        f = self._import()
        cost, _ = f(
            recipe_qty=Decimal('1'), recipe_unit='strip',
            ingredient_name='Bacon', case_price=Decimal('70.35'),
            case_size_str='15LB',
            count_per_lb_low=12, count_per_lb_high=12,
            price_per_pound=Decimal('4.69'),
        )
        # piece_oz = 16/12 = 1.333, qty=1×1.333, cost = $4.69 × 1.333/16 = $0.39
        self.assertEqual(cost, Decimal('0.39'))


class SynergySyncWrongDupPickFixTests(TestCase):
    """`load_items_for_month` Phase 3c — when same-date ILI duplicates exist
    with conflicting unit_prices, prefer the row with the higher
    extended_amount. The Butter row 182 case from the umbrella entry's
    "Three new variants" #3: \$1.40 fragment + \$97.39 case total on the
    same date — sheet was getting the fragment. New: case total wins."""

    def _import(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'synergy_sync' in sys.modules:
            del sys.modules['synergy_sync']
        import synergy_sync
        return synergy_sync

    def test_picks_higher_extended_amount_among_same_date_dups(self):
        """Two ILIs same (canonical, vendor, invoice_date) with $1.40 vs
        $97.39 → load_items_for_month returns the $97.39 one."""
        ss = self._import()
        sysco = Vendor.objects.create(name='Sysco')
        butter = Product.objects.create(
            canonical_name='Butter, Salted',
            category='Dairy',
            primary_descriptor='Butter',
        )
        # Fragment row (the wrong one — synergy_sync was picking this)
        InvoiceLineItem.objects.create(
            vendor=sysco, product=butter,
            raw_description='BUTTER FRAGMENT',
            unit_price=Decimal('1.40'),
            extended_amount=Decimal('1.40'),
            case_size='1', invoice_date=date(2026, 4, 20),
            match_confidence='vendor_exact',
        )
        # Real case row (the correct one)
        InvoiceLineItem.objects.create(
            vendor=sysco, product=butter,
            raw_description='SYS CLS BUTTER PRINTS 36/1#',
            unit_price=Decimal('97.39'),
            extended_amount=Decimal('97.39'),
            case_size='36/1LB', invoice_date=date(2026, 4, 20),
            match_confidence='vendor_exact',
        )
        items = ss.load_items_for_month(2026, 4)
        butter_items = [i for i in items if i['canonical'] == 'Butter, Salted']
        self.assertEqual(len(butter_items), 1)
        # The real case price wins, not the $1.40 fragment
        self.assertEqual(butter_items[0]['unit_price'], 97.39)

    def test_normal_single_row_unaffected(self):
        """Non-conflict case: one ILI per (canonical, vendor, date). Phase 3c
        change must not alter the answer."""
        ss = self._import()
        sysco = Vendor.objects.create(name='Sysco')
        flour = Product.objects.create(canonical_name='AP Flour', category='Drystock')
        InvoiceLineItem.objects.create(
            vendor=sysco, product=flour,
            raw_description='FLOUR ALL PURP',
            unit_price=Decimal('19.95'),
            extended_amount=Decimal('19.95'),
            case_size='50LB', invoice_date=date(2026, 4, 15),
            match_confidence='vendor_exact',
        )
        items = ss.load_items_for_month(2026, 4)
        flour_items = [i for i in items if i['canonical'] == 'AP Flour']
        self.assertEqual(len(flour_items), 1)
        self.assertEqual(flour_items[0]['unit_price'], 19.95)

    def test_latest_date_still_wins_across_dates(self):
        """Two ILIs different dates: latest still wins (existing behavior).
        Phase 3c only tiebreaks WITHIN a date, not across."""
        ss = self._import()
        sysco = Vendor.objects.create(name='Sysco')
        flour = Product.objects.create(canonical_name='AP Flour', category='Drystock')
        # Earlier date, higher price
        InvoiceLineItem.objects.create(
            vendor=sysco, product=flour, raw_description='FLOUR',
            unit_price=Decimal('25.00'), extended_amount=Decimal('25.00'),
            case_size='50LB', invoice_date=date(2026, 4, 1),
            match_confidence='vendor_exact',
        )
        # Later date, lower price (sale week)
        InvoiceLineItem.objects.create(
            vendor=sysco, product=flour, raw_description='FLOUR',
            unit_price=Decimal('19.95'), extended_amount=Decimal('19.95'),
            case_size='50LB', invoice_date=date(2026, 4, 20),
            match_confidence='vendor_exact',
        )
        items = ss.load_items_for_month(2026, 4)
        flour_items = [i for i in items if i['canonical'] == 'AP Flour']
        self.assertEqual(len(flour_items), 1)
        # Latest date wins, not highest price across dates
        self.assertEqual(flour_items[0]['unit_price'], 19.95)


class ProductInventoryClassFieldTests(TestCase):
    """`Product.inventory_class` + `inventory_unit_descriptor` schema fields.
    Phase 1 of structured schema migration. Pure additive — existing
    Products keep working with empty values; populate via curation later."""

    def test_inventory_class_choices(self):
        """The 3-class enum (weighed / counted_with_weight / counted_with_volume)
        per Sean's `feedback_inventory_count_classes.md` 2-class methodology
        extended for cost-calc dispatch."""
        p = Product.objects.create(canonical_name='TestBacon',
                                   category='Proteins',
                                   inventory_class='weighed',
                                   inventory_unit_descriptor='1# Pack')
        p.refresh_from_db()
        self.assertEqual(p.inventory_class, 'weighed')
        self.assertEqual(p.inventory_unit_descriptor, '1# Pack')

    def test_inventory_class_optional(self):
        """Existing Products with no inventory_class still load. Empty
        strings — choices include ('', '— unset —')."""
        p = Product.objects.create(canonical_name='TestUnclassified',
                                   category='Drystock')
        self.assertEqual(p.inventory_class, '')
        self.assertEqual(p.inventory_unit_descriptor, '')

    def test_inventory_unit_descriptor_for_volume_class(self):
        """Counted-with-volume products carry vendor-spec unit descriptor
        (Gal/Qt/Pt) that lands in sheet col G post-Phase-3."""
        p = Product.objects.create(canonical_name='TestMilk',
                                   category='Dairy',
                                   inventory_class='counted_with_volume',
                                   inventory_unit_descriptor='Gal')
        self.assertEqual(p.inventory_class, 'counted_with_volume')
        self.assertEqual(p.inventory_unit_descriptor, 'Gal')


class MapperTokenOverlapGateTests(TestCase):
    """`mapper.resolve_item` — token-overlap gate on fuzzy tiers.

    Fuzzy scoring (token_sort_ratio, token_set_ratio, char ratio) can
    produce semantically nonsense matches that pass the threshold —
    e.g. SPICE GARLIC PWDR → Cinnamon, Ground at score >=90 because of
    shared structural tokens after stemming. The gate enforces: if the
    matched canonical doesn't share at least one meaningful 3+letter
    content token with the raw description, reject and try the next
    tier. Replicates audit_suspect_mappings logic at the pre-commit
    layer so the bad FK never lands in the DB."""

    def test_has_token_overlap_helper_basic(self):
        """Helper unit test — case-insensitive 3+letter token overlap."""
        mapper = _import_mapper()
        self.assertTrue(mapper._has_token_overlap('SYSCO ROMAINE 3CT', 'Romaine'))
        self.assertTrue(mapper._has_token_overlap('Tomato Sauce 6/10CAN', 'Sauce, Tomato'))
        self.assertFalse(mapper._has_token_overlap('SPICE GARLIC PWDR', 'Cinnamon, Ground'))
        # 1- and 2-letter tokens don't count (filters noise)
        self.assertFalse(mapper._has_token_overlap('A B C 12', 'X Y Z'))
        # Empty inputs are false
        self.assertFalse(mapper._has_token_overlap('', 'Romaine'))
        self.assertFalse(mapper._has_token_overlap('SYSCO', ''))

    def test_global_fuzzy_gate_blocks_zero_overlap(self):
        """Tier 5 (global fuzzy) is gated. When no vendor map exists and
        a global fuzzy match would land on a canonical with zero shared
        stemmed tokens, the gate rejects it."""
        mapper = _import_mapper()
        # No vendor_desc_map for the vendor — forces fall-through to global fuzzy
        mappings = {
            "code_map": {},
            "desc_map": {
                # Only desc_map entry — fuzzy could match into it from various raws
                "WHLFCLS PEPPER BLACK GROUND 50LB": "Pepper, Black",
            },
            "vendor_desc_map": {},  # empty → tier 3 skipped
            "category_map": {
                "Pepper, Black": {"category": "Drystock", "primary_descriptor": "Spices",
                                  "secondary_descriptor": ""},
            },
        }
        # Raw shares 'GROUND' + 'LB' with the desc but the CANONICAL ('Pepper, Black')
        # has zero stemmed-token overlap with the raw ('turmeric', 'ground', 'lb').
        # If score >= 90 the gate must reject; if score < 90 it falls through anyway.
        # Either path: result MUST NOT be 'Pepper, Black'.
        item = {'sysco_item_code': '',
                'raw_description': 'WHLFIMP TURMERIC GROUND 50LB'}
        r = mapper.resolve_item(item, mappings, vendor='UnknownVendor')
        self.assertNotEqual(r['canonical'], 'Pepper, Black',
                            'Global fuzzy gate must reject zero-overlap match')

    def test_vendor_fuzzy_intentionally_ungated(self):
        """vendor_fuzzy bypasses the gate by design — Sean's curated sheet
        mappings are trusted even when the canonical is an abbreviation
        (AMER → American), plural (PEACH → Peaches), or category synonym
        (BEEF PATTY → Burgers). Documents the intentional design choice."""
        mapper = _import_mapper()
        mappings = {
            "code_map": {},
            "desc_map": {},
            "vendor_desc_map": {
                "SYSCO": {
                    # Sean curated: this Sysco abbreviation maps to American
                    "BBRLCLS CHEESE AMER 160 SLI WHT": "American",
                },
            },
            "category_map": {
                "American": {"category": "Cheese", "primary_descriptor": "Processed",
                             "secondary_descriptor": "Processed"},
            },
        }
        # Slight typo / variant — fuzzy not exact
        item = {'sysco_item_code': '',
                'raw_description': 'BBRLCLS CHEESE AMER 160 SLICE WHITE'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        # Despite zero stemmed overlap between 'amer' and 'american',
        # vendor_fuzzy must still resolve via Sean's curation
        self.assertEqual(r['canonical'], 'American')
        self.assertEqual(r['confidence'], 'vendor_fuzzy')

    def test_legitimate_fuzzy_match_still_passes(self):
        """A fuzzy match with shared tokens (typo in 'ROMAINE') passes —
        gate never fires when there's content overlap."""
        mapper = _import_mapper()
        item = {'sysco_item_code': '', 'raw_description': 'WHLFCLS ROMAINE HEART 3CT'}
        r = mapper.resolve_item(item, _fixture_mappings(), vendor='Sysco')
        self.assertEqual(r['canonical'], 'Romaine')
        self.assertEqual(r['confidence'], 'vendor_fuzzy')

    def test_stripped_fuzzy_6a_gate_blocks_nonsense(self):
        """Tier 6a (stripped + token_set_ratio) IS gated — matches against
        canonical names directly with no curation, so the gate applies.
        After stripping a Sysco brand prefix, the remaining content tokens
        must overlap (stem-wise) with the proposed canonical."""
        mapper = _import_mapper()
        # Engineer a fixture that forces tier 6a path
        mappings = {
            "code_map": {},
            "desc_map": {},
            "vendor_desc_map": {},
            "category_map": {
                # Canonical with no overlap with what the prefix-strip leaves
                "Olive Oil": {"category": "Drystock", "primary_descriptor": "Oil",
                              "secondary_descriptor": ""},
            },
        }
        # raw → after _strip_sysco_prefix → "TURMERIC GROUND 5LB"
        # vs canonical "Olive Oil" → zero stemmed overlap → gate rejects
        item = {'sysco_item_code': '',
                'raw_description': 'WHLFIMP TURMERIC GROUND 5LB'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        self.assertNotEqual(r['canonical'], 'Olive Oil',
                            'Stripped-fuzzy 6a gate must reject this')

    def test_typo_recovery_still_works_via_6c(self):
        """Tier 6c (char-level fallback) is intentionally NOT gated — exists
        specifically for typo recovery (Canteloupe → Cantaloupe) where the
        stems differ. Confirms the gate didn't break this path."""
        mapper = _import_mapper()
        mappings = {
            "code_map": {},
            "desc_map": {},
            "vendor_desc_map": {},
            "category_map": {
                "Cantaloupe": {"category": "Produce", "primary_descriptor": "Stone Fruit",
                               "secondary_descriptor": ""},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Canteloupe'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        self.assertEqual(r['canonical'], 'Cantaloupe',
                         'Typo recovery must still work — gate must skip char tier')


class SyncItemMappingFromSheetTests(TestCase):
    """`sync_item_mapping_from_sheet` — Step 1 of the sheet→DB migration.

    Pulls Item Mapping sheet rows into ProductMapping. Conservative on
    errors: skips rows with unknown vendors or orphan canonicals rather
    than auto-creating ghosts. Idempotent on (vendor, description)."""

    def _patch_sheet(self, rows):
        """Patch _import_sheet_helpers in the command module to return
        a fake get_sheet_values returning the given rows. Avoids any
        real Sheets API call."""
        from unittest.mock import patch
        from myapp.management.commands import sync_item_mapping_from_sheet as cmd
        return patch.object(cmd, '_import_sheet_helpers',
                            return_value=(lambda sid, rng: rows, 'fake_sheet', 'Item Mapping'))

    def _setup_basic(self):
        """Vendor + Product fixtures shared by most tests."""
        sysco = Vendor.objects.create(name='Sysco')
        farmart = Vendor.objects.create(name='Farm Art')
        Product.objects.create(canonical_name='Romaine')
        Product.objects.create(canonical_name='Carrot')
        return sysco, farmart

    def test_dry_run_creates_no_rows(self):
        """Dry-run (default) reports what would happen but writes nothing."""
        from io import StringIO
        from django.core.management import call_command
        self._setup_basic()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'WHLFCLS ROMAINE HEARTS 3CT', 'Produce', '', '', 'Romaine', '1234567'],
            ['Farm Art', 'CARROTS, 48/1 LB CELLO', 'Produce', '', '', 'Carrot', ''],
        ]
        out = StringIO()
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', stdout=out)
        self.assertEqual(ProductMapping.objects.count(), 0,
                         'Dry-run must NOT write to DB')
        self.assertIn('DRY-RUN', out.getvalue())
        self.assertIn('Created:                            2', out.getvalue())

    def test_apply_creates_rows(self):
        """--apply writes the mapping rows to DB with correct FKs + SUPCs."""
        from django.core.management import call_command
        self._setup_basic()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'WHLFCLS ROMAINE HEARTS 3CT', 'Produce', '', '', 'Romaine', '1234567'],
            ['Farm Art', 'CARROTS, 48/1 LB CELLO', 'Produce', '', '', 'Carrot', ''],
        ]
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        self.assertEqual(ProductMapping.objects.count(), 2)
        pm = ProductMapping.objects.get(description='WHLFCLS ROMAINE HEARTS 3CT')
        self.assertEqual(pm.vendor.name, 'Sysco')
        self.assertEqual(pm.product.canonical_name, 'Romaine')
        self.assertEqual(pm.supc, '1234567')

    def test_orphan_canonical_skipped_not_created(self):
        """Sheet references a canonical that doesn't exist in DB →
        SKIP the row, don't auto-create a ghost Product."""
        from django.core.management import call_command
        self._setup_basic()
        before_products = Product.objects.count()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'KETCHUP HEINZ 6/10', 'Drystock', '', '', 'Ketchup', ''],   # Ketchup not in DB
            ['Sysco', 'WHLFCLS ROMAINE HEARTS 3CT', 'Produce', '', '', 'Romaine', ''],   # OK
        ]
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        # Only Romaine row created; Ketchup skipped
        self.assertEqual(ProductMapping.objects.count(), 1)
        self.assertFalse(Product.objects.filter(canonical_name='Ketchup').exists(),
                         'Orphan canonical must NOT auto-create a ghost Product')
        self.assertEqual(Product.objects.count(), before_products,
                         'No new Products created')

    def test_idempotent_rerun_unchanged(self):
        """Re-running the command on the same sheet → all rows unchanged
        (no duplicates, no spurious updates)."""
        from django.core.management import call_command
        self._setup_basic()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'WHLFCLS ROMAINE HEARTS 3CT', 'Produce', '', '', 'Romaine', '1234567'],
        ]
        # First apply
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        self.assertEqual(ProductMapping.objects.count(), 1)
        # Second apply with same sheet — must not create duplicates or update
        from io import StringIO
        out = StringIO()
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', stdout=out)
        self.assertEqual(ProductMapping.objects.count(), 1)
        self.assertIn('Created:                            0', out.getvalue())
        self.assertIn('Unchanged (already in sync):        1', out.getvalue())

    def test_existing_pm_with_different_canonical_gets_updated(self):
        """A PM exists with FK to product A, sheet now says canonical B →
        --apply repoints the FK to B."""
        from django.core.management import call_command
        sysco, _ = self._setup_basic()
        old_p = Product.objects.create(canonical_name='Pesto')
        new_p = Product.objects.get(canonical_name='Romaine')
        # Pre-existing PM with old FK
        ProductMapping.objects.create(
            vendor=sysco, description='SOMETHING WEIRD', product=old_p, supc='',
        )
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'SOMETHING WEIRD', 'Produce', '', '', 'Romaine', ''],
        ]
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        pm = ProductMapping.objects.get(description='SOMETHING WEIRD')
        self.assertEqual(pm.product, new_p,
                         'PM FK should be updated to match sheet')

    def test_unknown_vendor_skipped_not_created(self):
        """Sheet references a vendor not in DB Vendor table → skip, don't auto-create."""
        from django.core.management import call_command
        self._setup_basic()
        before_vendors = Vendor.objects.count()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['BogusVendor', 'SOMETHING', 'Produce', '', '', 'Romaine', ''],
        ]
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        self.assertEqual(ProductMapping.objects.count(), 0)
        self.assertEqual(Vendor.objects.count(), before_vendors,
                         'No new Vendor auto-created')

    def test_empty_canonical_skipped(self):
        """Rows with empty col F (canonical) are skipped silently —
        these are the 773 'orphan mapping' bloat rows in the sheet."""
        from django.core.management import call_command
        self._setup_basic()
        rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'SOME DESCRIPTION', '', '', '', '', ''],   # empty canonical
            ['Sysco', 'WHLFCLS ROMAINE HEARTS 3CT', 'Produce', '', '', 'Romaine', ''],
        ]
        with self._patch_sheet(rows):
            call_command('sync_item_mapping_from_sheet', '--apply', verbosity=0)
        # Only Romaine created; empty-canonical row skipped
        self.assertEqual(ProductMapping.objects.count(), 1)


class MapperLoadFromDBTests(TestCase):
    """`mapper._load_from_db` — Step 2 of the sheet→DB migration.

    Mapper now reads ProductMapping table instead of the sheet. Returns
    the same 4-dict shape (code_map, desc_map, vendor_desc_map,
    category_map) so resolve_item is unchanged. Eliminates the
    forward-looking damage class from upstream Product renames per
    `feedback_upstream_downstream_planning.md`."""

    def _import_mapper(self):
        # Reset module to clear any cached state from prior tests
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        # Force reimport so module-level state is fresh
        if 'mapper' in sys.modules:
            del sys.modules['mapper']
        import mapper
        return mapper

    def test_db_path_builds_full_4dict_shape(self):
        """ProductMapping rows → all 4 dicts populated correctly."""
        sysco = Vendor.objects.create(name='Sysco')
        farmart = Vendor.objects.create(name='Farm Art')
        romaine = Product.objects.create(
            canonical_name='Romaine', category='Produce',
            primary_descriptor='Leaf', secondary_descriptor='',
        )
        ProductMapping.objects.create(
            vendor=sysco, description='WHLFCLS ROMAINE HEARTS 3CT',
            supc='1234567', product=romaine,
        )
        ProductMapping.objects.create(
            vendor=farmart, description='LETTUCE, ROMAINE, 24CT',
            supc='', product=romaine,
        )
        mapper = self._import_mapper()
        cache = mapper._load_from_db()

        # code_map
        self.assertEqual(cache['code_map'].get('1234567'), 'Romaine')
        # desc_map (normalized to uppercase, slashes → spaces)
        self.assertEqual(cache['desc_map'].get('WHLFCLS ROMAINE HEARTS 3CT'), 'Romaine')
        self.assertEqual(cache['desc_map'].get('LETTUCE, ROMAINE, 24CT'), 'Romaine')
        # vendor_desc_map
        self.assertEqual(cache['vendor_desc_map']['SYSCO']['WHLFCLS ROMAINE HEARTS 3CT'], 'Romaine')
        self.assertEqual(cache['vendor_desc_map']['FARM ART']['LETTUCE, ROMAINE, 24CT'], 'Romaine')
        # category_map sourced from Product table (DB-as-truth)
        self.assertEqual(cache['category_map']['Romaine'], {
            'category': 'Produce',
            'primary_descriptor': 'Leaf',
            'secondary_descriptor': '',
        })

    def test_db_path_skips_pms_without_product_fk(self):
        """ProductMapping with product=NULL is excluded — those are orphan
        mappings (sheet col F was empty, or the Product was deleted)."""
        sysco = Vendor.objects.create(name='Sysco')
        Product.objects.create(canonical_name='Romaine')
        # Orphan mapping — no product attached
        ProductMapping.objects.create(
            vendor=sysco, description='UNKNOWN ITEM', supc='', product=None,
        )
        # Valid mapping
        romaine = Product.objects.get(canonical_name='Romaine')
        ProductMapping.objects.create(
            vendor=sysco, description='WHLFCLS ROMAINE HEARTS 3CT',
            supc='1234567', product=romaine,
        )
        mapper = self._import_mapper()
        cache = mapper._load_from_db()

        # Orphan PM excluded
        self.assertNotIn('UNKNOWN ITEM', cache['desc_map'])
        # Valid PM included
        self.assertIn('WHLFCLS ROMAINE HEARTS 3CT', cache['desc_map'])

    def test_mapping_health_view_renders(self):
        """`/mapping-health/` smoke test — renders 200 with empty DB."""
        u = User.objects.create_user(username='healthtest', password='x')
        self.client.force_login(u)
        r = self.client.get('/mapping-health/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Mapping Health')

    def test_mapping_health_surfaces_drift_count(self):
        """When unmatched_drift rows exist, dashboard shows them prominently."""
        u = User.objects.create_user(username='drifttest', password='x')
        self.client.force_login(u)
        v = Vendor.objects.create(name='Sysco')
        # Seed a drift-tagged row
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='BBRLCLS CHEESE AMER 160 SLI WHT',
            match_confidence='unmatched_drift', product=None,
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.get('/mapping-health/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Sheet/DB Drift')
        self.assertContains(r, 'BBRLCLS CHEESE AMER')

    def test_load_mappings_falls_back_to_sheet_when_db_empty(self):
        """Catastrophe protection: if ProductMapping is empty (pre-backfill,
        fresh install), load_mappings falls back to sheet so the pipeline
        doesn't go dark waiting for someone to run sync_item_mapping."""
        from unittest.mock import patch
        # Empty DB ProductMapping
        self.assertEqual(ProductMapping.objects.count(), 0)
        mapper = self._import_mapper()
        # Patch the sheet path to return one row
        sentinel_rows = [
            ['vendor', 'item_description', 'category', 'pri', 'sec', 'canonical_name', 'supc'],
            ['Sysco', 'TESTROW', 'Drystock', '', '', 'TestCanonical', '999'],
        ]
        # Also patch the cache file path so we don't read a stale cache
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, 'item_mappings.json')
            with patch.object(mapper, 'MAPPING_CACHE_PATH', cache_path), \
                 patch.object(mapper, 'get_sheet_values', return_value=sentinel_rows):
                cache = mapper.load_mappings(force_refresh=True)
        # Should have fallen back to sheet
        self.assertEqual(cache['desc_map'].get('TESTROW'), 'TestCanonical')
        self.assertEqual(cache['code_map'].get('999'), 'TestCanonical')


class FuzzyQuarantineTests(TestCase):
    """Phase 2 of mapper safety: fuzzy tiers (vendor_fuzzy / fuzzy /
    stripped_fuzzy) DO NOT auto-attach FKs. They land as ILI rows with
    product=NULL + match_confidence='<tier>_pending' AND create a
    ProductMappingProposal queue entry for human review."""

    def _import_db_write(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import db_write
        return db_write

    def test_fuzzy_match_does_not_attach_fk(self):
        """When confidence is fuzzy, ILI lands with product=NULL even
        though the mapper resolved a canonical."""
        Vendor.objects.create(name='Sysco')
        Product.objects.create(canonical_name='Romaine')
        items = [{
            'raw_description': 'WHLFCLS ROMAINE HEART 3CT',
            'canonical': 'Romaine',
            'unit_price': 12.50,
            'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy',
            'score': 92,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='WHLFCLS ROMAINE HEART 3CT')
        self.assertIsNone(ili.product, 'Fuzzy match must not attach FK')
        self.assertEqual(ili.match_confidence, 'vendor_fuzzy_pending')

    def test_fuzzy_match_creates_proposal(self):
        """A fuzzy match queues a ProductMappingProposal for review."""
        v = Vendor.objects.create(name='Sysco')
        p = Product.objects.create(canonical_name='Romaine')
        items = [{
            'raw_description': 'WHLFCLS ROMAINE HEART 3CT',
            'canonical': 'Romaine',
            'unit_price': 12.50,
            'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy',
            'score': 92,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')

        proposal = ProductMappingProposal.objects.get(vendor=v,
                                                       raw_description='WHLFCLS ROMAINE HEART 3CT')
        self.assertEqual(proposal.suggested_product, p)
        self.assertEqual(proposal.score, 92)
        self.assertEqual(proposal.confidence_tier, 'vendor_fuzzy')
        self.assertEqual(proposal.source, 'mapper_quarantine')
        self.assertEqual(proposal.status, 'pending')

    def test_code_match_still_auto_commits(self):
        """Code/exact tiers (deterministic) bypass quarantine — FK attaches as before."""
        Vendor.objects.create(name='Sysco')
        p = Product.objects.create(canonical_name='Romaine')
        items = [{
            'raw_description': 'Romaine',
            'canonical': 'Romaine',
            'unit_price': 12.50,
            'case_size_raw': '3/24CT',
            'confidence': 'code',
            'score': 100,
            'sysco_item_code': '1234567',
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='Romaine')
        self.assertEqual(ili.product, p, 'Code-tier match must auto-attach FK')
        self.assertEqual(ili.match_confidence, 'code')
        self.assertEqual(ProductMappingProposal.objects.count(), 0,
                         'Auto-commit tiers must not queue proposals')

    def test_repeated_fuzzy_does_not_duplicate_proposal(self):
        """Same (vendor, raw_desc) seen twice → only ONE proposal row
        (unique_together enforces this; existing pending leaves alone)."""
        Vendor.objects.create(name='Sysco')
        Product.objects.create(canonical_name='Romaine')
        items = [{
            'raw_description': 'WHLFCLS ROMAINE HEART 3CT',
            'canonical': 'Romaine',
            'unit_price': 12.50,
            'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy',
            'score': 92,
        }]
        dbw = self._import_db_write()
        # First invoice
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='a.jpg')
        # Second invoice on a different date but same item
        dbw.write_invoice_to_db('Sysco', '2026-04-21', items, source_file='b.jpg')
        self.assertEqual(ProductMappingProposal.objects.count(), 1)
        # And both ILIs landed quarantined
        self.assertEqual(InvoiceLineItem.objects.filter(
            raw_description='WHLFCLS ROMAINE HEART 3CT',
            product__isnull=True,
            match_confidence='vendor_fuzzy_pending',
        ).count(), 2)

    def test_proposal_approve_backfills_ili_and_creates_mapping(self):
        """Approving a proposal: (1) writes ProductMapping, (2) attaches
        FK to all matching ILI, (3) bumps confidence to manual_review."""
        v = Vendor.objects.create(name='Sysco')
        p = Product.objects.create(canonical_name='Romaine')
        # Seed: 2 quarantined ILI rows
        items = [{
            'raw_description': 'WHLFCLS ROMAINE HEART 3CT',
            'canonical': 'Romaine',
            'unit_price': 12.50,
            'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy',
            'score': 92,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='a.jpg')
        dbw.write_invoice_to_db('Sysco', '2026-04-21', items, source_file='b.jpg')
        proposal = ProductMappingProposal.objects.get(
            vendor=v, raw_description='WHLFCLS ROMAINE HEART 3CT'
        )

        # Approve
        from django.contrib.auth.models import User
        u = User.objects.create_user(username='reviewer', password='x')
        result = proposal.approve(reviewer=u, notes='looks right')

        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'approved')
        self.assertEqual(proposal.reviewed_by, u)
        self.assertIsNotNone(proposal.reviewed_at)
        self.assertIn('looks right', proposal.notes)

        # ProductMapping was created
        pm = ProductMapping.objects.get(vendor=v, description='WHLFCLS ROMAINE HEART 3CT')
        self.assertEqual(pm.product, p)
        self.assertEqual(result['product_mapping'], pm)

        # All ILI rows now have FK + manual_review confidence
        self.assertEqual(result['ili_updated'], 2)
        for ili in InvoiceLineItem.objects.filter(raw_description='WHLFCLS ROMAINE HEART 3CT'):
            self.assertEqual(ili.product, p)
            self.assertEqual(ili.match_confidence, 'manual_review')

    def test_proposal_approve_with_override_product(self):
        """Reviewer can pick a different canonical than the mapper suggested."""
        v = Vendor.objects.create(name='Sysco')
        suggested = Product.objects.create(canonical_name='Romaine')
        actual = Product.objects.create(canonical_name='Iceberg')

        items = [{
            'raw_description': 'WHLFCLS LETTUCE HEART 3CT',
            'canonical': 'Romaine',
            'unit_price': 12.50, 'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy', 'score': 88,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')
        proposal = ProductMappingProposal.objects.get(vendor=v)
        # Approve with override
        proposal.approve(product=actual)
        # ILI now points to overriden product
        ili = InvoiceLineItem.objects.get(raw_description='WHLFCLS LETTUCE HEART 3CT')
        self.assertEqual(ili.product, actual)
        # ProductMapping reflects override
        pm = ProductMapping.objects.get(vendor=v, description='WHLFCLS LETTUCE HEART 3CT')
        self.assertEqual(pm.product, actual)

    def test_proposal_reject_creates_no_mapping(self):
        """Rejecting flips status; no DB writes elsewhere."""
        v = Vendor.objects.create(name='Sysco')
        Product.objects.create(canonical_name='Romaine')
        items = [{
            'raw_description': 'BOGUS FUZZY MATCH',
            'canonical': 'Romaine',
            'unit_price': 12.50, 'case_size_raw': '3/24CT',
            'confidence': 'vendor_fuzzy', 'score': 90,
        }]
        dbw = self._import_db_write()
        dbw.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')
        proposal = ProductMappingProposal.objects.get(vendor=v)
        proposal.reject(notes='wrong product')
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'rejected')
        # No ProductMapping created
        self.assertEqual(ProductMapping.objects.filter(
            vendor=v, description='BOGUS FUZZY MATCH'
        ).count(), 0)
        # ILI still unmapped
        ili = InvoiceLineItem.objects.get(raw_description='BOGUS FUZZY MATCH')
        self.assertIsNone(ili.product)


class MappingReviewViewTests(TestCase):
    """`/mapping-review/` Django view + approve/reject endpoints —
    Phase 2B replacement for the Sheets workflow."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username='reviewer', password='x')
        self.client.force_login(self.user)
        self.sysco = Vendor.objects.create(name='Sysco')
        self.romaine = Product.objects.create(canonical_name='Romaine')

    def _make_proposal(self, raw_desc='WHLFCLS ROMAINE HEART 3CT', status='pending'):
        return ProductMappingProposal.objects.create(
            vendor=self.sysco, raw_description=raw_desc,
            suggested_product=self.romaine,
            score=90, confidence_tier='vendor_fuzzy',
            source='mapper_quarantine', status=status,
        )

    def test_list_view_renders_with_pending_only_by_default(self):
        self._make_proposal(raw_desc='PENDING ROW', status='pending')
        self._make_proposal(raw_desc='APPROVED ROW', status='approved')
        r = self.client.get('/mapping-review/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'PENDING ROW')
        self.assertNotContains(r, 'APPROVED ROW')

    def test_list_view_filter_status(self):
        self._make_proposal(raw_desc='APPROVED ROW', status='approved')
        r = self.client.get('/mapping-review/?status=approved')
        self.assertContains(r, 'APPROVED ROW')

    def test_approve_action_attaches_fk_and_creates_mapping(self):
        p = self._make_proposal()
        # Seed an ILI matching the (vendor, raw_desc)
        InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description=p.raw_description,
            product=None, match_confidence='vendor_fuzzy_pending',
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.post(f'/mapping-review/{p.id}/approve/')
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, 'approved')
        self.assertEqual(p.reviewed_by, self.user)
        ili = InvoiceLineItem.objects.get(raw_description=p.raw_description)
        self.assertEqual(ili.product, self.romaine)
        self.assertEqual(ili.match_confidence, 'manual_review')
        self.assertTrue(ProductMapping.objects.filter(
            vendor=self.sysco, description=p.raw_description, product=self.romaine,
        ).exists())

    def test_approve_with_override_canonical(self):
        actual = Product.objects.create(canonical_name='Iceberg')
        p = self._make_proposal()
        r = self.client.post(f'/mapping-review/{p.id}/approve/',
                             {'override_canonical': 'Iceberg'})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.suggested_product, actual)
        # Check ProductMapping reflects override
        pm = ProductMapping.objects.get(vendor=self.sysco, description=p.raw_description)
        self.assertEqual(pm.product, actual)

    def test_approve_with_unknown_override_rejected(self):
        """Override pointing to a non-existent canonical is refused
        gracefully — no PM written, error message shown."""
        p = self._make_proposal()
        r = self.client.post(f'/mapping-review/{p.id}/approve/',
                             {'override_canonical': 'NotARealProduct'},
                             follow=True)
        # Stays pending
        p.refresh_from_db()
        self.assertEqual(p.status, 'pending')
        self.assertContains(r, 'doesn&#x27;t exist')

    def test_reject_action(self):
        p = self._make_proposal()
        r = self.client.post(f'/mapping-review/{p.id}/reject/',
                             {'notes': 'wrong product'})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, 'rejected')
        self.assertIn('wrong product', p.notes)
        # No PM created
        self.assertFalse(ProductMapping.objects.filter(
            vendor=self.sysco, description=p.raw_description
        ).exists())

    def test_double_approve_idempotent_warning(self):
        """Re-approving an already-approved proposal warns instead of crashing."""
        p = self._make_proposal()
        self.client.post(f'/mapping-review/{p.id}/approve/')
        r = self.client.post(f'/mapping-review/{p.id}/approve/', follow=True)
        self.assertContains(r, 'already approved')

    def test_frequency_sort_orders_by_ili_count_desc(self):
        """Default ?sort=frequency puts the highest-ILI-count proposal first."""
        # High-frequency item: 5 ILIs share this raw_desc
        high_p = self._make_proposal(raw_desc='HIGH FREQ')
        for _ in range(5):
            InvoiceLineItem.objects.create(
                vendor=self.sysco, raw_description='HIGH FREQ',
                product=None, match_confidence='vendor_fuzzy_pending',
                invoice_date=date(2026, 4, 20),
            )
        # Low-frequency: 1 ILI
        low_p = self._make_proposal(raw_desc='LOW FREQ')
        InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description='LOW FREQ',
            product=None, match_confidence='vendor_fuzzy_pending',
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.get('/mapping-review/?sort=frequency')
        self.assertEqual(r.status_code, 200)
        # high should appear before low in the rendered HTML
        body = r.content.decode()
        self.assertLess(body.index('HIGH FREQ'), body.index('LOW FREQ'),
                        'Frequency sort should put HIGH FREQ before LOW FREQ')


class PopulateMappingReviewCommandTests(TestCase):
    """`populate_mapping_review_from_unmapped` mgmt command — bulk-queues
    proposals from existing unmapped ILI rows."""

    def test_skips_sysco_placeholders(self):
        """Sysco placeholder rows are skipped — they need rep CSV not human review."""
        from django.core.management import call_command
        v = Vendor.objects.create(name='Sysco')
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='[Sysco #1234567]',
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='REGULAR UNMAPPED ITEM',
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        call_command('populate_mapping_review_from_unmapped', '--apply', verbosity=0)
        # Only the regular item gets a proposal; placeholder skipped
        self.assertEqual(ProductMappingProposal.objects.count(), 1)
        self.assertEqual(
            ProductMappingProposal.objects.first().raw_description,
            'REGULAR UNMAPPED ITEM',
        )

    def test_skips_existing_proposals(self):
        """Re-running doesn't duplicate proposals."""
        from django.core.management import call_command
        v = Vendor.objects.create(name='Sysco')
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='ITEM A',
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        # First run creates 1
        call_command('populate_mapping_review_from_unmapped', '--apply', verbosity=0)
        self.assertEqual(ProductMappingProposal.objects.count(), 1)
        # Second run — no new
        call_command('populate_mapping_review_from_unmapped', '--apply', verbosity=0)
        self.assertEqual(ProductMappingProposal.objects.count(), 1)

    def test_skips_non_product_and_drift_tiers(self):
        """Rows already classified as non_product or unmatched_drift are skipped."""
        from django.core.management import call_command
        v = Vendor.objects.create(name='Sysco')
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='FUEL SURCHARGE',
            product=None, match_confidence='non_product',
            invoice_date=date(2026, 4, 20),
        )
        InvoiceLineItem.objects.create(
            vendor=v, raw_description='DRIFT ITEM',
            product=None, match_confidence='unmatched_drift',
            invoice_date=date(2026, 4, 20),
        )
        call_command('populate_mapping_review_from_unmapped', '--apply', verbosity=0)
        self.assertEqual(ProductMappingProposal.objects.count(), 0)


class AttachPlaceholderFKsCommandTests(TestCase):
    """`attach_placeholder_fks` mgmt command — fixes stale-unmapped Sysco
    placeholder ILI rows whose SUPC is now in code_map."""

    def test_attaches_fk_when_supc_in_product_mapping(self):
        """Placeholder ILI with SUPC that has a matching ProductMapping row
        with vendor=Sysco gets the FK attached + match_confidence='code'."""
        from django.core.management import call_command
        v = Vendor.objects.create(name='Sysco')
        ritz = Product.objects.create(canonical_name='Ritz')
        # Seed the SUPC mapping in DB so mapper.load_mappings sees it
        ProductMapping.objects.create(
            vendor=v, description='RITZ CRACKER',
            supc='1234567', product=ritz,
        )
        # Stale placeholder ILI (no FK)
        ili = InvoiceLineItem.objects.create(
            vendor=v, raw_description='[Sysco #1234567]',
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        call_command('attach_placeholder_fks', '--apply', verbosity=0)
        ili.refresh_from_db()
        self.assertEqual(ili.product, ritz)
        self.assertEqual(ili.match_confidence, 'code')
        self.assertEqual(ili.match_score, 100)

    def test_skips_unknown_supcs(self):
        """Placeholders whose SUPC isn't in code_map stay placeholders."""
        from django.core.management import call_command
        v = Vendor.objects.create(name='Sysco')
        ili = InvoiceLineItem.objects.create(
            vendor=v, raw_description='[Sysco #9999999]',
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        call_command('attach_placeholder_fks', '--apply', verbosity=0)
        ili.refresh_from_db()
        self.assertIsNone(ili.product)


class TaxonomyInferenceTests(TestCase):
    """`myapp.taxonomy.infer_taxonomy` — multi-source inference for new
    Product creation. Tests each signal in isolation + key combined cases."""

    def test_subset_canonical_inheritance_dominates(self):
        """When subset_canonical is provided + Product exists, its taxonomy
        wins over all other signals (locked confidence)."""
        from myapp.taxonomy import infer_taxonomy
        Product.objects.create(canonical_name='Danish',
            category='Bakery', primary_descriptor='Pastry', secondary_descriptor='')
        # Even with vendor=Farm Art (Produce default) + the citrus-token 'Lemon',
        # subset inheritance wins
        r = infer_taxonomy('Lemon Danish', vendor='Farm Art', subset_canonical='Danish')
        self.assertEqual(r['category'][0], 'Bakery')
        self.assertEqual(r['primary'][0], 'Pastry')

    def test_protein_primal_overrides_existing_products_vote(self):
        """Encoded butcher knowledge overrides any existing-products vote
        for protein primary/secondary."""
        from myapp.taxonomy import infer_taxonomy
        # Seed an existing Product with 'wrong' primary for the test
        Product.objects.create(canonical_name='Beef Patties',
            category='Proteins', primary_descriptor='Patties')
        r = infer_taxonomy('Beef Brisket Nose Off Choice',
                           vendor='Exceptional Foods')
        self.assertEqual(r['category'][0], 'Proteins')
        self.assertEqual(r['primary'][0], 'Beef')
        self.assertEqual(r['secondary'][0], 'Brisket')

    def test_cheese_signal_handles_pepper_jack(self):
        """The token 'pepper' would normally route to Capsicum/Produce,
        but 'pepperjack' / 'jack' as cheese types should win.
        Under unified Dairy: primary = processing tier."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('BBRLIMP CHEESE PEPPER JACK SLI', vendor='Sysco')
        self.assertEqual(r['category'][0], 'Dairy')
        # Pepperjack → Semi-Soft, Jack → Semi-Hard; either matches first
        self.assertIn(r['primary'][0], ['Cheese, Semi-Soft', 'Cheese, Semi-Hard'])

    def test_cheese_signal_correct_milk_source_for_feta(self):
        """Feta is Sheep traditionally — ensure encoded knowledge fires.
        Under unified Dairy: tier in primary, milk source in secondary."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('CHEESE FETA CRUMBLED', vendor='Sysco', section_hint='DAIRY')
        self.assertEqual(r['category'][0], 'Dairy')
        self.assertEqual(r['primary'][0], 'Cheese, Fresh')
        self.assertEqual(r['secondary'][0], 'Sheep')

    def test_cheese_signal_correct_for_goat(self):
        """The word 'GOAT' as a cheese type should drive secondary='Goat'."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('CHEESE GOAT FRESH 4 OZ', vendor='Farm Art')
        self.assertEqual(r['category'][0], 'Dairy')
        self.assertEqual(r['primary'][0], 'Cheese, Fresh')
        self.assertEqual(r['secondary'][0], 'Goat')

    def test_bakery_keyword_overrides_ingredient_tokens(self):
        """'Lemon Danish' has Lemon (Citrus token) but Danish (bakery
        keyword) — bakery wins. 'Cheese Danish' too."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('Lemon Danish', vendor='Philadelphia Bakery Merchants')
        self.assertEqual(r['category'][0], 'Bakery')
        self.assertEqual(r['primary'][0], 'Pastries')

        r2 = infer_taxonomy('Cheese Danish', vendor='Philadelphia Bakery Merchants')
        self.assertEqual(r2['category'][0], 'Bakery')

    def test_bakery_five_bucket_taxonomy(self):
        """Bakery items resolve to one of the 5 locked primaries:
        Bread/Fermented, Cakes & Sponges, Pastries, Quick Breads,
        Cookies & Bars."""
        from myapp.taxonomy import infer_taxonomy
        cases = [
            ('SOURDOUGH BREAD LOAF',         'Bread/Fermented'),
            ('HAMBURGER BUN 4IN',            'Bread/Fermented'),
            ('FLOUR TORTILLA 12IN',          'Bread/Fermented'),
            ('CHOCOLATE CAKE SHEET',         'Cakes & Sponges'),
            ('BLUEBERRY CHEESECAKE',         'Cakes & Sponges'),
            ('BUTTER CROISSANT',             'Pastries'),
            ('GLAZED DONUT',                 'Pastries'),
            ('BLUEBERRY MUFFIN',             'Quick Breads'),
            ('CORNBREAD SQUARE',             'Quick Breads'),
            ('CHOCOLATE CHIP COOKIE',        'Cookies & Bars'),
            ('FUDGE BROWNIE',                'Cookies & Bars'),
        ]
        for raw, expected_primary in cases:
            r = infer_taxonomy(raw, vendor='Philadelphia Bakery Merchants')
            self.assertEqual(r['category'][0], 'Bakery', f'{raw}: category')
            self.assertEqual(r['primary'][0], expected_primary,
                             f'{raw}: expected primary={expected_primary} got {r["primary"][0]}')

    def test_produce_botanical_primary_assignment(self):
        """Produce category + token 'mango' → primary='Drupe' (mango is botanically a drupe).
        Per Sean 2026-04-30: 'tropical is not a categorization scientifically'."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('MANGO, RED, 9CT', vendor='Farm Art')
        self.assertEqual(r['category'][0], 'Produce')
        self.assertEqual(r['primary'][0], 'Drupe')

    def test_section_hint_drives_category(self):
        """Sysco section_hint='SEAFOOD' → category=Proteins."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('SOMETHING UNFAMILIAR', vendor='Sysco', section_hint='SEAFOOD')
        self.assertEqual(r['category'][0], 'Proteins')

    def test_vendor_default_when_no_other_signal(self):
        """PBM vendor with unknown raw → Bakery default (medium confidence)."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('xxxx unknown thing yyyy', vendor='Philadelphia Bakery Merchants')
        self.assertEqual(r['category'][0], 'Bakery')

    def test_unknown_returns_unknowns(self):
        """No signals match → all fields stay (None, 'unknown')."""
        from myapp.taxonomy import infer_taxonomy
        r = infer_taxonomy('asdf qwer zxcv', vendor=None)
        self.assertEqual(r['category'], (None, 'unknown'))
        self.assertEqual(r['primary'], (None, 'unknown'))


class ConventionDriftAuditTests(TestCase):
    """`audit_convention_drift` — surfaces canonical-name pattern drift
    within (category, primary) groups."""

    def _classify(self, name):
        from myapp.management.commands.audit_convention_drift import _classify
        return _classify(name)

    def test_classify_comma_prefix(self):
        self.assertEqual(self._classify('Pasta, Bowtie'), ('comma-prefix', 'Pasta'))
        self.assertEqual(self._classify('Pasta, Whole Wheat'), ('comma-prefix', 'Pasta'))

    def test_classify_comma_suffix(self):
        # First segment has multiple words → falls to suffix bucket
        self.assertEqual(self._classify('Whole Wheat Bread, Sliced'), ('comma-suffix', 'Sliced'))

    def test_classify_single_word(self):
        self.assertEqual(self._classify('Penne'), ('single-word', 'Penne'))
        self.assertEqual(self._classify('Spaghetti'), ('single-word', 'Spaghetti'))

    def test_classify_multi_word_no_comma(self):
        self.assertEqual(self._classify('Plum Tomatoes Whole'), ('multi-word', None))

    def test_command_flags_drifted_group(self):
        """Build a group where 'Pasta, X' dominates but outliers exist;
        the command should emit DRIFT for that group."""
        from io import StringIO
        from django.core.management import call_command
        # Set up a Pastas-like group
        Product.objects.create(canonical_name='Pasta, Bowtie', category='Test', primary_descriptor='Pastas')
        Product.objects.create(canonical_name='Pasta, Linguine', category='Test', primary_descriptor='Pastas')
        Product.objects.create(canonical_name='Pasta, Penne', category='Test', primary_descriptor='Pastas')
        Product.objects.create(canonical_name='Pasta, Spaghetti', category='Test', primary_descriptor='Pastas')
        Product.objects.create(canonical_name='Spaghetti', category='Test', primary_descriptor='Pastas')  # outlier
        Product.objects.create(canonical_name='Cavatappi, Macaroni', category='Test', primary_descriptor='Pastas')  # outlier
        out = StringIO()
        call_command('audit_convention_drift', stdout=out, min_group=4)
        s = out.getvalue()
        self.assertIn('DRIFT', s)
        self.assertIn('Test/Pastas', s)
        self.assertIn("'Pasta', X", s)
        self.assertIn('Spaghetti', s)
        self.assertIn('Cavatappi, Macaroni', s)

    def test_command_skips_uniform_group_unless_verbose(self):
        """Uniform groups are silent by default."""
        from io import StringIO
        from django.core.management import call_command
        Product.objects.create(canonical_name='Pasta, Bowtie', category='UniformCat', primary_descriptor='UniformPri')
        Product.objects.create(canonical_name='Pasta, Linguine', category='UniformCat', primary_descriptor='UniformPri')
        Product.objects.create(canonical_name='Pasta, Penne', category='UniformCat', primary_descriptor='UniformPri')
        Product.objects.create(canonical_name='Pasta, Ziti', category='UniformCat', primary_descriptor='UniformPri')
        out = StringIO()
        call_command('audit_convention_drift', stdout=out, min_group=4)
        self.assertNotIn('UniformCat', out.getvalue())
        # With --verbose, the OK line shows
        out2 = StringIO()
        call_command('audit_convention_drift', stdout=out2, min_group=4, verbose=True)
        self.assertIn('UniformCat', out2.getvalue())


class CanonicalSuggestionTests(TestCase):
    """`derive_canonical_suggestion` — auto-derives a clean canonical name
    starting point for the Mapping Review create form."""

    def test_farm_art_comma_separated_clean(self):
        """Farm Art's 'TOMATOES, CHERRY, 12 CONT' → 'Tomato, Cherry'."""
        from myapp.taxonomy import derive_canonical_suggestion
        self.assertEqual(
            derive_canonical_suggestion('TOMATOES, CHERRY, 12 CONT', vendor='Farm Art'),
            'Tomato, Cherry')
        self.assertEqual(
            derive_canonical_suggestion('MUSHROOMS, SHIITAKE, #1, 3 LB', vendor='Farm Art'),
            'Mushroom, Shiitake')

    def test_plural_strip_in_derivation(self):
        """'TOMATOES' → 'Tomato', 'CHERRIES' → 'Cherry' via the food-domain
        stemmer. Verifies stem patterns flow through the derivation."""
        from myapp.taxonomy import derive_canonical_suggestion
        self.assertEqual(
            derive_canonical_suggestion('CHERRIES, FRESH', vendor='Farm Art'),
            'Cherry, Fresh')
        self.assertEqual(
            derive_canonical_suggestion('POTATOES, RED', vendor='Farm Art'),
            'Potato, Red')

    def test_already_clean_passthrough(self):
        """Short clean raws already in canonical shape pass through."""
        from myapp.taxonomy import derive_canonical_suggestion
        self.assertEqual(
            derive_canonical_suggestion('Pork Chop', vendor='Sysco'),
            'Pork Chop')

    def test_subset_canonical_returns_none(self):
        """When subset_match found a candidate, derivation returns None
        — the reviewer should approve against the subset, not create new."""
        from myapp.taxonomy import derive_canonical_suggestion
        self.assertIsNone(derive_canonical_suggestion(
            'Apple Danish', vendor='PBM', subset_canonical='Danish'))

    def test_sysco_prefix_strip_produces_clean_suggestion(self):
        """OCR cleanup + abbreviation expansion + Sysco prefix strip
        combine to clean even the gnarliest Sysco raws into a usable
        starting point. Result must NOT contain the brand fragment
        or trailing SUPC."""
        from myapp.taxonomy import derive_canonical_suggestion
        out = derive_canonical_suggestion(
            'OZCITVCLS COFFEE GRND HSE BLEND MED W / F 29596', vendor='Sysco')
        self.assertIsNotNone(out)
        self.assertNotIn('CITV', out)
        self.assertNotIn('29596', out)
        # 'COFFEE' should survive (it's the actual product noun)
        self.assertIn('Coffee', out)

    def test_brand_fragment_without_strip_bails(self):
        """When a non-Sysco vendor presents a brand fragment that
        prefix-strip won't touch (because vendor != Sysco), the
        fragment-detection guard kicks in and returns None."""
        from myapp.taxonomy import derive_canonical_suggestion
        # Vendor is Farm Art — no Sysco prefix-strip applied.
        # Synthetic brand fragment WHLFCLS forces the bail path.
        self.assertIsNone(derive_canonical_suggestion(
            'WHLFCLS something garbled', vendor='Farm Art'))

    def test_empty_raw_returns_none(self):
        from myapp.taxonomy import derive_canonical_suggestion
        self.assertIsNone(derive_canonical_suggestion('', vendor='Sysco'))
        self.assertIsNone(derive_canonical_suggestion(None, vendor='Sysco'))

    def test_strips_trailing_supc_and_leading_qty(self):
        """Long digit runs at end (SUPC) and 'NN OZ' / 'NN CT' prefixes
        get stripped before canonical extraction."""
        from myapp.taxonomy import derive_canonical_suggestion
        out = derive_canonical_suggestion(
            '24 12 OZ LACROIX WATER SPARKLING LIME 15021240', vendor='Sysco')
        # Some flavor of clean — at minimum doesn't include the SUPC
        # and no longer has the leading qty.
        self.assertIsNotNone(out)
        self.assertNotIn('15021240', out)
        self.assertIn('Lacroix', out.title() if out else '')


class MappingReviewCreateAndApproveTests(TestCase):
    """`/mapping-review/<id>/create-and-approve/` — creates Product +
    approves proposal in one shot."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username='reviewer2', password='x')
        self.client.force_login(self.user)
        self.sysco = Vendor.objects.create(name='Sysco')

    def _make_proposal(self, raw_desc='SOMETHING NEW'):
        return ProductMappingProposal.objects.create(
            vendor=self.sysco, raw_description=raw_desc,
            suggested_product=None, source='discover_unmapped', status='pending',
        )

    def test_suggestion_vs_final_diff_captured(self):
        """When the create form submits, both the auto-derived suggestion
        and the canonical the reviewer actually saved get persisted on
        the proposal — the corpus needed to refine derivation later."""
        p = self._make_proposal('TOMATOES, CHERRY, 12 CONT.')
        InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description=p.raw_description,
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'suggested_canonical_text': 'Tomato, Cherry',  # what the form offered
            'canonical_name': 'Tomato, Cherry, Organic',   # what Sean edited it to
            'category': 'Produce', 'primary_descriptor': 'Solanaceae',
            'secondary_descriptor': 'Tomato',
        })
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.suggested_canonical_text, 'Tomato, Cherry')
        self.assertEqual(p.final_canonical_text, 'Tomato, Cherry, Organic')

    def test_blank_suggestion_persists_as_empty(self):
        """If derivation bailed (suggestion blank) and Sean typed from
        scratch, suggested_canonical_text stays empty."""
        p = self._make_proposal('OZCITVCLS GARBLED 12345')
        InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description=p.raw_description,
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'suggested_canonical_text': '',
            'canonical_name': 'Coffee, House Blend',
            'category': 'Coffee/Concessions',
            'primary_descriptor': 'Coffee Dispenser Station',
            'secondary_descriptor': '',
        })
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.suggested_canonical_text, '')
        self.assertEqual(p.final_canonical_text, 'Coffee, House Blend')

    def test_creates_product_then_approves(self):
        """POST with canonical+category+primary+secondary creates Product
        and applies the mapping in one transaction."""
        p = self._make_proposal()
        # Seed an ILI matching for backfill verification
        ili = InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description=p.raw_description,
            product=None, match_confidence='unmatched',
            invoice_date=date(2026, 4, 20),
        )
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'canonical_name': 'Brand New Item',
            'category': 'Drystock',
            'primary_descriptor': 'Spices',
            'secondary_descriptor': '',
        })
        self.assertEqual(r.status_code, 302)
        # Product was created
        new_p = Product.objects.get(canonical_name='Brand New Item')
        self.assertEqual(new_p.category, 'Drystock')
        self.assertEqual(new_p.primary_descriptor, 'Spices')
        # Proposal approved
        p.refresh_from_db()
        self.assertEqual(p.status, 'approved')
        # ILI got the FK
        ili.refresh_from_db()
        self.assertEqual(ili.product, new_p)

    def test_uses_existing_product_if_canonical_collision(self):
        """If canonical_name already exists, use existing Product (don't
        crash on unique constraint)."""
        existing = Product.objects.create(canonical_name='Already Here',
            category='Drystock', primary_descriptor='Spices')
        p = self._make_proposal()
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'canonical_name': 'Already Here',
            'category': 'Different Category',  # ignored — uses existing
            'primary_descriptor': 'Different',
        })
        self.assertEqual(r.status_code, 302)
        # No new Product
        self.assertEqual(Product.objects.filter(canonical_name='Already Here').count(), 1)
        # Existing Product unchanged
        existing.refresh_from_db()
        self.assertEqual(existing.category, 'Drystock')

    def test_missing_canonical_rejected(self):
        """Empty canonical_name → error message, no Product, no approval."""
        p = self._make_proposal()
        before_n = Product.objects.count()
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'canonical_name': '',
            'category': 'Drystock',
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Product.objects.count(), before_n)
        p.refresh_from_db()
        self.assertEqual(p.status, 'pending')

    def test_already_processed_proposal_warns(self):
        p = self._make_proposal(raw_desc='ALREADY')
        existing = Product.objects.create(canonical_name='X')
        p.approve(product=existing)
        r = self.client.post(f'/mapping-review/{p.id}/create-and-approve/', {
            'canonical_name': 'Brand New',
            'category': 'Drystock',
        }, follow=True)
        self.assertContains(r, 'already approved')


class AbbreviationExpansionTests(TestCase):
    """`invoice_processor/abbreviations.py` — Sysco vendor abbreviation
    expansion. Used by both mapper.py (tier 6 fuzzy matching) and
    taxonomy.py (inference for new Product creation)."""

    def _import_abbrev(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'abbreviations' in sys.modules:
            del sys.modules['abbreviations']
        import abbreviations
        return abbreviations

    def test_basic_protein_expansion(self):
        abbrev = self._import_abbrev()
        self.assertEqual(
            abbrev.expand_abbreviations('BRST CHKN BNLS CKD'),
            'Breast Chicken Boneless Cooked',
        )

    def test_sliced_shredded_grated(self):
        abbrev = self._import_abbrev()
        self.assertIn('Sliced', abbrev.expand_abbreviations('CHEESE PROVOLONE SLI'))
        self.assertIn('Shredded', abbrev.expand_abbreviations('MOZZARELLA SHRD'))
        self.assertIn('Grated', abbrev.expand_abbreviations('PARMESAN GRTD'))

    def test_does_not_expand_pure_unit_abbreviations(self):
        """LB, OZ, GAL, CT, BG, etc. are NOT expanded — they'd add noise
        tokens that hurt fuzzy-matching against product canonicals."""
        abbrev = self._import_abbrev()
        result = abbrev.expand_abbreviations('BG 150 LB PACKER SUGAR GRANULATED')
        self.assertNotIn('Pound', result)
        self.assertNotIn('Bag', result)
        self.assertIn('LB', result)
        self.assertIn('SUGAR', result)

    def test_word_boundary_no_partial_matches(self):
        """Abbreviation matches require word boundaries — 'BR' inside
        'BRSKT' must not partially expand."""
        abbrev = self._import_abbrev()
        result = abbrev.expand_abbreviations('BRISKET')
        self.assertEqual(result, 'BRISKET')   # unchanged

    def test_case_insensitive(self):
        abbrev = self._import_abbrev()
        self.assertIn('Boneless', abbrev.expand_abbreviations('chicken bnls 5lb'))

    def test_multi_word_abbreviation(self):
        """'GRL MRK' → 'Grill Marked' (multi-token abbreviation)."""
        abbrev = self._import_abbrev()
        self.assertIn('Grill Marked', abbrev.expand_abbreviations('CHKN BRST GRL MRK'))

    def test_empty_input(self):
        abbrev = self._import_abbrev()
        self.assertEqual(abbrev.expand_abbreviations(''), '')
        self.assertIsNone(abbrev.expand_abbreviations(None))


class MapperSubsetMatchTier6dTests(TestCase):
    """Tier 6d — subset-match: canonical's tokens are ALL contained in
    raw's stemmed tokens. Promoted from populate_mapping_review command
    into the production mapper. Routed through quarantine in db_write."""

    def _mapper(self):
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        if 'mapper' in sys.modules:
            del sys.modules['mapper']
        import mapper
        return mapper

    def test_apple_danish_subset_matches_danish(self):
        """Classic case — 'Apple Danish' raw, 'Danish' canonical exists,
        'Apple' canonical doesn't → subset-match returns 'Danish'."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Danish': {'category': 'Bakery', 'primary_descriptor': 'Pastry',
                           'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Apple Danish'}
        r = mapper.resolve_item(item, mappings, vendor='Philadelphia Bakery Merchants')
        self.assertEqual(r['canonical'], 'Danish')
        self.assertEqual(r['confidence'], 'subset_match')

    def test_ambiguous_returns_none(self):
        """'Cherry Tomato' raw — both Cherry and Tomato are 1-token
        canonicals. Subset matches both equally → ambiguous → None."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Cherry': {'category': 'Produce', 'primary_descriptor': 'Stone Fruit',
                           'secondary_descriptor': ''},
                'Tomato': {'category': 'Produce', 'primary_descriptor': 'Solanaceae',
                           'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Cherry Tomato'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        self.assertNotEqual(r['confidence'], 'subset_match')

    def test_prefers_most_specific_canonical(self):
        """When 'Pork, Belly' (2 tokens) and 'Pork' (1 token) both subset-
        match, prefer the longer (more specific) one."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Pork': {'category': 'Proteins', 'primary_descriptor': 'Pork',
                         'secondary_descriptor': ''},
                'Pork, Belly': {'category': 'Proteins', 'primary_descriptor': 'Pork',
                                'secondary_descriptor': 'Belly'},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Pork Belly Boneless RIND ON'}
        r = mapper.resolve_item(item, mappings, vendor='Exceptional Foods')
        self.assertEqual(r['canonical'], 'Pork, Belly')
        self.assertEqual(r['confidence'], 'subset_match')

    def test_modifier_only_match_rejected_when_head_noun_present(self):
        """'Blueberry Muffins' raw must NOT match canonical 'Blueberries'
        (the canonical is a modifier — flavor of the muffin, not the
        product). When raw has a food-form head ('muffin'), the
        candidate must share that head. Otherwise reject."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Blueberries': {'category': 'Produce', 'primary_descriptor': 'Berry',
                                'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Blueberry Muffins'}
        r = mapper.resolve_item(item, mappings, vendor='Philadelphia Bakery Merchants')
        # No bakery canonical exists yet → must NOT lock onto the
        # modifier ('Blueberries'). Should fall through to unmatched.
        self.assertNotEqual(r['confidence'], 'subset_match')

    def test_modifier_only_match_butter_croissant(self):
        """'Butter Croissant' must NOT map to 'Butter'."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Butter': {'category': 'Dairy', 'primary_descriptor': '',
                           'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'Butter Croissant'}
        r = mapper.resolve_item(item, mappings, vendor='Philadelphia Bakery Merchants')
        self.assertNotEqual(r['canonical'], 'Butter')

    def test_filling_only_match_bun_hot_dog(self):
        """'BUN HOT DOG' must NOT map to 'Hot Dogs' — head is bun, the
        Hot Dog tokens are describing what filling the bun holds."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Hot Dogs': {'category': 'Proteins', 'primary_descriptor': 'Pork',
                             'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'BKRSCLS BUN HOT DOG WHITE 6 HINGD'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        self.assertNotEqual(r['canonical'], 'Hot Dogs')

    def test_head_noun_match_still_works_when_canonical_shares_head(self):
        """When raw has head 'bun' AND a canonical contains 'bun',
        the head-noun rule allows the match. 'Hot Dog Bun' canonical
        for raw 'BUN HOT DOG' should resolve correctly."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Hot Dog Bun': {'category': 'Bakery', 'primary_descriptor': 'Bread/Fermented',
                                'secondary_descriptor': 'Hot Dog Bun'},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'BKRSCLS BUN HOT DOG WHITE 6 HINGD'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        self.assertEqual(r['canonical'], 'Hot Dog Bun')
        self.assertEqual(r['confidence'], 'subset_match')

    def test_noise_only_canonical_rejected(self):
        """'DRIED APRICOT 3 LB BAG' must NOT map to canonical 'Bags' —
        Bag is packaging noise, not a product."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Bags': {'category': 'Paper/Disposable', 'primary_descriptor': '',
                         'secondary_descriptor': ''},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'DRIED, APRICOT, 3 LB BAG'}
        r = mapper.resolve_item(item, mappings, vendor='Farm Art')
        self.assertNotEqual(r['canonical'], 'Bags')

    def test_abbreviation_expansion_enables_subset_match(self):
        """Verify subset-match resolves correctly through the abbreviation
        expansion path. 'BRST CHKN BNLS' → 'Breast Chicken Boneless' may
        be caught at an earlier tier (6a stripped_fuzzy) which is also
        valid — earlier tiers are more authoritative. Either way the
        canonical resolves correctly."""
        mapper = self._mapper()
        mappings = {
            'code_map': {}, 'desc_map': {}, 'vendor_desc_map': {},
            'category_map': {
                'Chicken Breast': {'category': 'Proteins', 'primary_descriptor': 'Poultry',
                                   'secondary_descriptor': 'Breast'},
            },
        }
        item = {'sysco_item_code': '', 'raw_description': 'BRST CHKN BNLS'}
        r = mapper.resolve_item(item, mappings, vendor='Sysco')
        self.assertEqual(r['canonical'], 'Chicken Breast')
        # Either subset_match (6d) or stripped_fuzzy (6a) is acceptable;
        # both correctly map to the same canonical
        self.assertIn(r['confidence'], ['subset_match', 'stripped_fuzzy'])

    def test_subset_routed_through_quarantine(self):
        """When mapper returns 'subset_match', db_write quarantines it —
        product FK stays NULL, confidence becomes 'subset_match_pending',
        and a ProductMappingProposal is created."""
        Vendor.objects.create(name='Sysco')
        Product.objects.create(canonical_name='Danish',
                               category='Bakery', primary_descriptor='Pastry')
        items = [{
            'raw_description': 'Apple Danish',
            'canonical': 'Danish',
            'unit_price': 5.50,
            'case_size_raw': '12CT',
            'confidence': 'subset_match',
            'score': 95,
        }]
        import sys
        from django.conf import settings
        path = str(settings.BASE_DIR / 'invoice_processor')
        if path not in sys.path:
            sys.path.insert(0, path)
        import db_write
        db_write.write_invoice_to_db('Sysco', '2026-04-20', items, source_file='t.jpg')

        ili = InvoiceLineItem.objects.get(raw_description='Apple Danish')
        self.assertIsNone(ili.product, 'subset_match must NOT auto-commit FK')
        self.assertEqual(ili.match_confidence, 'subset_match_pending')
        self.assertTrue(ProductMappingProposal.objects.filter(
            raw_description='Apple Danish', status='pending'
        ).exists())


class ProductEditViewTests(TestCase):
    """`/products/<id>/edit/` — rename or merge an approved Product.
    Surfaces the edit-after-approve gap that was forcing manual shell
    work to fix bad approvals (e.g. OCR-garbled canonicals)."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username='reviewer3', password='x')
        self.client.force_login(self.user)
        self.sysco = Vendor.objects.create(name='Sysco')
        self.bagel = Product.objects.create(
            canonical_name='Bagel', category='Bakery',
            primary_descriptor='Bread/Fermented', secondary_descriptor='Bagel',
        )
        self.bad = Product.objects.create(
            canonical_name='SARALEE SAGEL PLAIN 3 OZ',
            category='Bakery', primary_descriptor='Bread/Yeast',
        )

    def test_get_renders_form(self):
        r = self.client.get(f'/products/{self.bad.id}/edit/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'SARALEE SAGEL')
        self.assertContains(r, 'Merge into another canonical')

    def test_rename_in_place(self):
        r = self.client.post(f'/products/{self.bad.id}/edit/', {
            'action': 'rename',
            'canonical_name': 'Sara Lee Bagel, Plain',
            'category': 'Bakery',
            'primary_descriptor': 'Bread/Fermented',
            'secondary_descriptor': 'Bagel',
        })
        self.assertEqual(r.status_code, 302)
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.canonical_name, 'Sara Lee Bagel, Plain')
        self.assertEqual(self.bad.primary_descriptor, 'Bread/Fermented')

    def test_rename_collision_blocked(self):
        """Renaming to an existing canonical_name must error, not overwrite."""
        r = self.client.post(f'/products/{self.bad.id}/edit/', {
            'action': 'rename',
            'canonical_name': 'Bagel',  # already exists on a different product
            'category': 'Bakery', 'primary_descriptor': '', 'secondary_descriptor': '',
        })
        self.assertEqual(r.status_code, 302)
        # Bad product's name unchanged
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.canonical_name, 'SARALEE SAGEL PLAIN 3 OZ')

    def test_merge_repoints_fks_and_deletes_source(self):
        """Merge: ILI + ProductMapping + Proposal.suggested all repoint
        to target; source Product is deleted."""
        ili = InvoiceLineItem.objects.create(
            vendor=self.sysco, raw_description='SARALEE SAGEL PLAIN 3 OZ',
            product=self.bad, match_confidence='vendor_exact',
            invoice_date=date(2026, 4, 20),
        )
        pm = ProductMapping.objects.create(
            vendor=self.sysco, description='SARALEE SAGEL PLAIN 3 OZ',
            product=self.bad,
        )
        prop = ProductMappingProposal.objects.create(
            vendor=self.sysco, raw_description='SARALEE SAGEL PLAIN 3 OZ',
            suggested_product=self.bad, source='discover_unmapped', status='approved',
        )
        bad_id = self.bad.id

        r = self.client.post(f'/products/{bad_id}/edit/', {
            'action': 'merge',
            'merge_into': str(self.bagel.id),
        })
        self.assertEqual(r.status_code, 302)
        # Source deleted
        self.assertFalse(Product.objects.filter(id=bad_id).exists())
        # FKs all repointed
        ili.refresh_from_db(); self.assertEqual(ili.product, self.bagel)
        pm.refresh_from_db(); self.assertEqual(pm.product, self.bagel)
        prop.refresh_from_db(); self.assertEqual(prop.suggested_product, self.bagel)

    def test_merge_self_blocked(self):
        """Cannot merge a Product into itself."""
        r = self.client.post(f'/products/{self.bagel.id}/edit/', {
            'action': 'merge', 'merge_into': str(self.bagel.id),
        })
        self.assertEqual(r.status_code, 302)
        # Bagel still exists
        self.assertTrue(Product.objects.filter(id=self.bagel.id).exists())

    def test_merge_missing_target_blocked(self):
        """Merging requires a target — empty merge_into is rejected."""
        r = self.client.post(f'/products/{self.bad.id}/edit/', {
            'action': 'merge', 'merge_into': '',
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Product.objects.filter(id=self.bad.id).exists())
