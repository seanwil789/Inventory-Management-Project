from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from myapp.models import (
    Menu, Recipe, RecipeIngredient, Product, MenuFreetextComponent,
)


class RecipeCoverageReportTests(TestCase):
    def _menu(self, d, slot, recipe=None, freetext=False):
        m = Menu(date=d, meal_slot=slot, recipe=recipe)
        m._skip_prep_derivation = True
        m.save()
        if freetext:
            MenuFreetextComponent.objects.create(menu=m, name='x', quantity=1, unit='lb')
        return m

    def test_stock_breakdown(self):
        prod = Product.objects.create(canonical_name='CovProd')
        full = Recipe.objects.create(name='FullCov', level='meal')
        RecipeIngredient.objects.create(recipe=full, name_raw='a', quantity=2, unit='lb', product=prod)
        nullq = Recipe.objects.create(name='NullQty', level='meal')
        RecipeIngredient.objects.create(recipe=nullq, name_raw='b', quantity=None, unit='lb', product=prod)

        d = date.today()
        self._menu(d, 'cold_breakfast', recipe=full)   # covered
        self._menu(d, 'hot_breakfast', recipe=nullq)    # linked-incomplete (null qty)
        self._menu(d, 'lunch', freetext=True)           # freetext-only
        self._menu(d, 'dinner')                          # unlinked

        out = StringIO()
        call_command('recipe_coverage_report', '--start', d.isoformat(),
                     '--end', d.isoformat(), stdout=out)
        s = out.getvalue()
        self.assertIn('STOCK = 25.0%', s)
        self.assertIn('covered 1', s)
        self.assertIn('linked-incomplete 1', s)
        self.assertIn('freetext-only 1', s)
        self.assertIn('unlinked 1', s)

    def test_unlinked_ingredient_blocks_coverage(self):
        # recipe has qty but NO product FK -> ingredient drops to stragglers -> not covered
        r = Recipe.objects.create(name='NoFK', level='meal')
        RecipeIngredient.objects.create(recipe=r, name_raw='c', quantity=3, unit='lb', product=None)
        d = date.today()
        self._menu(d, 'lunch', recipe=r)
        out = StringIO()
        call_command('recipe_coverage_report', '--start', d.isoformat(),
                     '--end', d.isoformat(), stdout=out)
        s = out.getvalue()
        self.assertIn('STOCK = 0.0%', s)
        self.assertIn('linked-incomplete 1', s)

    def test_flow(self):
        full = Recipe.objects.create(name='FlowR', level='meal')
        d = date.today() + timedelta(days=40)  # outside default stock window; flow keys on created_at
        self._menu(d, 'lunch', recipe=full)   # authored now, has recipe
        self._menu(d, 'dinner')               # authored now, no recipe
        out = StringIO()
        call_command('recipe_coverage_report', stdout=out)
        s = out.getvalue()
        self.assertIn('FLOW', s)
        self.assertIn('50.0%', s)  # 1 of 2 freshly-authored cells has a recipe
