"""Import Book of Yields 8e Chapter 15 (Standard Portion Sizes) into
StandardPortionReference table.

Data is embedded here rather than re-parsed from PDF because Chapter 15 is
a one-time reference import of ~120 rows. Page references per book p.154-157.

Run:
    python manage.py import_standard_portions --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import StandardPortionReference


# (category, item, average, low, high, page)
ROWS = [
    # SOUP, SALAD, BREAD — p.154
    ('soup_salad_bread', 'Soup', '6 fl. oz.', '4 fl. oz.', '8 fl. oz.', 'p.154'),
    ('soup_salad_bread', 'Soup, entrée', '10 fl. oz.', '8 fl. oz.', '12 fl. oz.', 'p.154'),
    ('soup_salad_bread', 'Salad Greens', '3 oz.', '2 oz.', '5 oz.', 'p.154'),
    ('soup_salad_bread', 'Salad Dressing', '1.5 fl. oz.', '1 fl. oz.', '3 fl. oz.', 'p.154'),
    ('soup_salad_bread', 'Bread', '4 oz.', '3 oz.', '6 oz.', 'p.154'),
    ('soup_salad_bread', 'Bread Roll, 1.5 oz each', '2 each', '1 each', '3 each', 'p.154'),
    ('soup_salad_bread', 'Butter', '1.5 oz.', '1 oz.', '2 oz.', 'p.154'),

    # BEEF ENTREES — p.154
    ('beef_entrees', 'Filet Mignon', '6 oz.', '4 oz.', '7 oz.', 'p.154'),
    ('beef_entrees', 'New York Steak', '10 oz.', '8 oz.', '12 oz.', 'p.154'),
    ('beef_entrees', 'Prime Rib, boneless', '10 oz.', '8 oz.', '12 oz.', 'p.154'),
    ('beef_entrees', 'Prime Rib, Large, boneless', '16 oz.', '14 oz.', '18 oz.', 'p.154'),
    ('beef_entrees', 'Sirloin Steak', '8 oz.', '6 oz.', '10 oz.', 'p.154'),
    ('beef_entrees', 'Flat Iron Steak', '6 oz.', '5 oz.', '8 oz.', 'p.154'),
    ('beef_entrees', 'Culotte Steak', '7 oz.', '6 oz.', '8 oz.', 'p.154'),
    ('beef_entrees', 'Tri-Tip Steak', '7 oz.', '6 oz.', '8 oz.', 'p.154'),
    ('beef_entrees', 'Tri-Tip, Roasted, sliced', '5 oz.', '3 oz.', '6 oz.', 'p.154'),

    # CHICKEN ENTREES — p.154
    ('chicken_entrees', 'Breast, Boneless, Skinless', '6 oz.', '4 oz.', '8 oz.', 'p.154'),
    ('chicken_entrees', 'Breast, Skin-on', '7 oz.', '5 oz.', '9 oz.', 'p.154'),
    ('chicken_entrees', 'Wings', '6 each', '4 each', '7 each', 'p.154'),
    ('chicken_entrees', 'Quarter', '1/4th whole 3 lb fryer', '1/4th', '1/4th', 'p.154'),
    ('chicken_entrees', 'Half', '1/2 whole 3 lb fryer', 'half', 'half', 'p.154'),

    # SEAFOOD ENTREES — p.154-155
    ('seafood_entrees', 'Fish Filets', '7 oz.', '4 oz.', '8 oz.', 'p.154'),
    ('seafood_entrees', 'Fish Steaks', '8 oz.', '6 oz.', '9 oz.', 'p.154'),
    ('seafood_entrees', 'Shrimp, 16-20', '5 each', '5 each', '7 each', 'p.154'),
    ('seafood_entrees', 'Scallops, Sea', '5 oz.', '4 oz.', '6 oz.', 'p.155'),
    ('seafood_entrees', 'Trout, whole, 8 oz.', '1 each', '1 each', '1 each', 'p.155'),
    ('seafood_entrees', 'Shellfish in shells', '12 oz.', '10 oz.', '16 oz.', 'p.155'),

    # PORK ENTREES — p.155
    ('pork_entrees', 'Chop, bone-on', '7 oz.', '5 oz.', '9 oz.', 'p.155'),
    ('pork_entrees', 'Tenderloin Medallions', '4 oz.', '3 oz.', '5 oz.', 'p.155'),
    ('pork_entrees', 'Roast Loin, sliced', '5 oz.', '4 oz.', '7 oz.', 'p.155'),
    ('pork_entrees', 'Pulled Pork', '6 oz.', '5 oz.', '7 oz.', 'p.155'),
    ('pork_entrees', 'Baby Back Ribs', '10 oz.', '8 oz.', '12 oz.', 'p.155'),
    ('pork_entrees', 'St. Louis Style Ribs', '12 oz.', '10 oz.', '16 oz.', 'p.155'),
    ('pork_entrees', 'Ham Steak', '8 oz.', '6 oz.', '9 oz.', 'p.155'),

    # VEAL — p.155
    ('veal', 'Loin Chops', '8 oz.', '7 oz.', '9 oz.', 'p.155'),
    ('veal', 'Rib Chops, bone in', '10 oz.', '9 oz.', '12 oz.', 'p.155'),
    ('veal', 'Scallopini (leg)', '6 oz.', '4 oz.', '7 oz.', 'p.155'),
    ('veal', 'Entrée Sauces', '2.5 fl. oz.', '2 fl. oz.', '3 fl. oz.', 'p.155'),

    # PASTA ENTRÉE — p.155
    ('pasta_entree', 'Cooked Pasta', '10 oz.', '8 oz.', '12 oz.', 'p.155'),
    ('pasta_entree', 'Pasta Sauce', '5 fl. oz.', '4 fl. oz.', '6 fl. oz.', 'p.155'),
    ('pasta_entree', 'Vegetables', '4 oz.', '3 oz.', '5 oz.', 'p.155'),

    # POTATOES — p.155
    ('potatoes', 'Baker, 70 or 80 Count', '1 each', '1 each', '1 each', 'p.155'),
    ('potatoes', 'Red Rose, Creamers', '4 oz.', '3 oz.', '5 oz.', 'p.155'),
    ('potatoes', 'Mashed', '6 oz.', '4 oz.', '8 oz.', 'p.155'),
    ('potatoes', 'Scalloped', '6 oz.', '4 oz.', '8 oz.', 'p.155'),
    ('potatoes', 'Rices & Grains', '5 oz.', '3 oz.', '7 oz.', 'p.155'),

    # DESSERTS — p.155-156
    ('desserts', 'Pie, 9 inch', '1/8th pie, slice', '1/10th pie', '1/6th pie', 'p.155'),
    ('desserts', 'Cake, Sheet', '2x3" piece', '2x2" piece', '3x3" piece', 'p.156'),
    ('desserts', 'Cake, 8 inch, round', '1/10th cake, slice', '1/12th slice', '1/8th slice', 'p.156'),
    ('desserts', 'Ice Cream', '6 fl. oz.', '4 fl. oz.', '8 fl. oz.', 'p.156'),
    ('desserts', 'Pudding, Custard', '6 fl. oz.', '4 fl. oz.', '8 fl. oz.', 'p.156'),
    ('desserts', 'Pastries', '1 each, 4 oz.', '1 each, 2.5 oz.', '1 each 6 oz.', 'p.156'),
    ('desserts', 'Cookies, 1 ounce', '3 each', '2 each', '4 each', 'p.156'),

    # BEVERAGES — p.156
    ('beverages', 'Water', '20 fl. oz.', '12 fl. oz.', '32 fl. oz.', 'p.156'),
    ('beverages', 'Iced tea', '20 fl. oz.', '12 fl. oz.', '32 fl. oz.', 'p.156'),
    ('beverages', 'Coffee', '16 oz.', '12 fl. oz.', '24 fl. oz.', 'p.156'),
    ('beverages', 'Hot Tea', '1 bag, 2 C water', '1 bag, 1 C water', '1.5 bags, 3 C water', 'p.156'),
    ('beverages', 'Wine', '5 fl. Oz', '4.5 fl. oz.', '6 fl. oz.', 'p.156'),
    ('beverages', 'Beer', '12 fl. oz.', '10 fl. oz.', '16 fl. oz.', 'p.156'),
    ('beverages', 'Cocktail', '1.5 fl. oz. Alcohol + mix', '1.25 fl. oz. Alc.', '1.75 fl. oz. Alc.', 'p.156'),
    ('beverages', 'Soft Drink', '20 fl. oz.', '12 fl. oz.', '32 fl. oz.', 'p.156'),

    # BREAKFAST ITEMS — p.156-157
    ('breakfast_items', 'Bacon Strips', '3 each', '2 each', '4 each', 'p.156'),
    ('breakfast_items', 'Sausage Links', '3 each', '2 each', '4 each', 'p.156'),
    ('breakfast_items', 'Sausage Patties, 3 oz.', '1 each', '1 each', '1 each', 'p.156'),
    ('breakfast_items', 'Ham Steak', '5 oz.', '3 oz.', '6 oz.', 'p.156'),
    ('breakfast_items', 'Eggs, Large', '2 each', '1 each', '3 each', 'p.156'),
    ('breakfast_items', 'Hash Browns', '4 oz.', '2 oz.', '6 oz.', 'p.156'),
    ('breakfast_items', 'Country Fried Potatoes', '5 oz.', '4 oz.', '7 oz.', 'p.156'),
    ('breakfast_items', 'Country Gravy', '4 fl. oz.', '3 fl. oz.', '6 fl. oz.', 'p.156'),
    ('breakfast_items', 'Biscuits', '2 each', '2 each', '3 each', 'p.156'),
    ('breakfast_items', 'Toast', '2 slices', '2 slices', '3 slices', 'p.156'),
    ('breakfast_items', 'English Muffin', '1 each, split', '1 each', '1 each', 'p.156'),
    ('breakfast_items', 'Butter pats, .5 oz.', '2 each', '1 each', '4 each', 'p.156'),
    ('breakfast_items', 'Jam/Jelly P.C.', '2 each', '1 each', '4 each', 'p.157'),
    ('breakfast_items', 'Juice', '5 fl. oz.', '4 fl. oz.', '8 fl. oz.', 'p.157'),
    ('breakfast_items', 'Pancake Batter', '6 fl. oz.', '4 fl. oz.', '10 fl. oz.', 'p.157'),

    # LUNCH ITEMS — p.157
    ('lunch_items', 'Sandwich Meats, fresh', '4 oz.', '3 oz.', '6 oz.', 'p.157'),
    ('lunch_items', 'Sandwich Meats, cured', '2 oz.', '1.5 oz.', '3 oz.', 'p.157'),
    ('lunch_items', 'Hamburger patty', '4 oz.', '3 oz.', '8 oz.', 'p.157'),
    ('lunch_items', 'Cheese, sliced', '1 oz.', '1/2 oz.', '2 oz.', 'p.157'),
    ('lunch_items', 'French Fries, 1/4"', '4 oz.', '3 oz.', '6 oz.', 'p.157'),
    ('lunch_items', 'French Fries, shoestring', '4 oz.', '3 oz.', '6 oz.', 'p.157'),
    ('lunch_items', 'Steak Fries', '5 oz.', '4 oz.', '8 oz.', 'p.157'),
    ('lunch_items', 'Potato Salad', '3.5 oz.', '3 oz.', '6 oz.', 'p.157'),
    ('lunch_items', 'Cole Slaw', '3 oz.', '2.5 oz.', '5 oz.', 'p.157'),
    ('lunch_items', 'Garden Salad', '4 oz.', '3.5 oz.', '5 oz.', 'p.157'),

    # HORS D'OEUVRE ITEMS — p.157
    ('hors_doeuvre', 'Crudites (raw veggies)', '2 oz.', '1 oz.', '3 oz.', 'p.157'),
    ('hors_doeuvre', 'Olives', '1.5 oz.', '1 oz.', '2 oz.', 'p.157'),
    ('hors_doeuvre', 'Cheeses', '2 oz.', '1 oz.', '2.5 oz.', 'p.157'),
    ('hors_doeuvre', 'Cured Meats', '2 oz.', '1.5 oz.', '3 oz.', 'p.157'),
    ('hors_doeuvre', 'Shrimp, 16-20', '3 each', '2 each', '4 each', 'p.157'),
    ('hors_doeuvre', 'Fresh Fruit pieces', '3 oz.', '2 oz.', '5 oz.', 'p.157'),
    ('hors_doeuvre', 'Dips and Spreads', '2.5 fl. oz.', '2 fl. oz.', '3 fl. oz.', 'p.157'),
    ('hors_doeuvre', 'Salsa', '3 fl. oz.', '2 fl. oz.', '4 fl. oz.', 'p.157'),
    ('hors_doeuvre', 'Chips, tortilla', '1 oz.', '1/2 oz.', '1.5 oz.', 'p.157'),
    ('hors_doeuvre', 'Baguette Slices', '3 slices', '2 slices', '5 slices', 'p.157'),
]


class Command(BaseCommand):
    help = 'Import Book of Yields 8e Chapter 15 (Standard Portion Sizes).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB (default is dry-run)')

    @transaction.atomic
    def handle(self, *args, **opts):
        self.stdout.write(f'Rows to import: {len(ROWS)}')

        if not opts['apply']:
            # Dry-run: summarize by category
            from collections import Counter
            cats = Counter(r[0] for r in ROWS)
            for c, n in sorted(cats.items()):
                self.stdout.write(f'  {c:<22s} {n} rows')
            self.stdout.write(self.style.WARNING(
                'Dry run — no DB writes. Re-run with --apply to save.'
            ))
            return

        created, updated = 0, 0
        for category, item, avg, low, high, page in ROWS:
            obj, was_created = StandardPortionReference.objects.update_or_create(
                category=category,
                menu_item=item,
                defaults=dict(
                    average_measure=avg,
                    low_range=low,
                    high_range=high,
                    source_ref=page,
                ),
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done. Created: {created}, Updated: {updated}'
        ))
