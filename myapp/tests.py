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

    def test_known_code_uses_canonical_as_description(self):
        """When code is in code_map, raw_description becomes the canonical
        name (more reliable than OCR text). Sysco parser design choice."""
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
        # Known canonical preferred over OCR desc
        self.assertEqual(items[0]['raw_description'], 'Milk, Whole Gallon')

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
