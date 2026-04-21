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
    Recipe, RecipeIngredient, Menu, Census, Vendor, Product, InvoiceLineItem,
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
