from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


class Product(models.Model):
    """Canonical product — mirrors Item Mapping col F + category columns."""
    canonical_name       = models.CharField(max_length=200, unique=True)
    category             = models.CharField(max_length=100, blank=True)
    primary_descriptor   = models.CharField(max_length=100, blank=True)
    secondary_descriptor = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.canonical_name


class ProductMapping(models.Model):
    """
    Maps a raw vendor description (or SUPC code) to a canonical Product.
    Mirrors Item Mapping rows — one row per vendor+description variant.
    """
    vendor      = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    description = models.CharField(max_length=500)           # raw OCR / CSV description
    supc        = models.CharField(max_length=20, blank=True, db_index=True)
    product     = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = [('vendor', 'description')]

    def __str__(self):
        return f"{self.description} → {self.product}"


class InvoiceLineItem(models.Model):
    """
    Single line item from a processed invoice.
    Replaces the Data Sheets transaction log.
    """
    CONFIDENCE_CHOICES = [
        ('code', 'SUPC Code Match'),
        ('exact', 'Exact Description Match'),
        ('vendor_exact', 'Vendor-Scoped Exact'),
        ('vendor_fuzzy', 'Vendor-Scoped Fuzzy'),
        ('fuzzy', 'Fuzzy Match'),
        ('stripped_fuzzy', 'Stripped Prefix Fuzzy'),
        ('keyword_batch', 'Keyword Batch (Human)'),
        ('manual_review', 'Manual Review (Human)'),
        ('unmatched', 'Unmatched'),
    ]

    vendor          = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    product         = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    raw_description = models.CharField(max_length=500, blank=True)  # fallback if unmatched
    unit_price      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    case_size       = models.CharField(max_length=100, blank=True)
    invoice_date    = models.DateField(null=True, blank=True)
    source_file     = models.CharField(max_length=255, blank=True)  # original filename
    match_confidence = models.CharField(max_length=20, blank=True, choices=CONFIDENCE_CHOICES)
    match_score     = models.IntegerField(null=True, blank=True)  # 0-100 fuzzy score
    price_flagged   = models.BooleanField(default=False)  # True if price anomaly detected
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['vendor', 'invoice_date']),
            models.Index(fields=['product', 'invoice_date']),
        ]

    def __str__(self):
        name = self.product.canonical_name if self.product else self.raw_description
        return f"{self.invoice_date} | {self.vendor} | {name} | ${self.unit_price}"


ASSIGNEE_CHOICES = [
    ('sean', 'Sean'),
    ('albert', 'Albert'),
]


PROTEIN_CHOICES = [
    ('beef',    'Beef'),
    ('chicken', 'Chicken'),
    ('pork',    'Pork'),
    ('turkey',  'Turkey'),
    ('seafood', 'Seafood'),
    ('veg',     'Vegetarian'),
    ('eggs',    'Eggs / Breakfast'),
    ('other',   'Other'),
]

FAT_HEALTH_CHOICES = [
    ('F', 'Fatty'),
    ('H', 'Healthy'),
]

POPULARITY_CHOICES = [
    ('high',   'High'),
    ('medium', 'Medium'),
    ('low',    'Low'),
]


# "Big 15" dietary conflict vocabulary — Sean-approved 2026-04-19 per
# project_recipe_authoring.md. Applied as NEGATIVE descriptors: Recipe.conflicts
# lists what the recipe CONTAINS / conflicts with. Downstream (future Client
# model): match when set(client.avoid) & set(recipe.conflicts) == empty.
CONFLICT_CHOICES = [
    ('gluten',          'Gluten'),
    ('dairy',           'Dairy'),
    ('egg',             'Egg'),
    ('peanut',          'Peanut'),
    ('tree_nut',        'Tree nuts'),
    ('fish',            'Fish'),
    ('shellfish',       'Shellfish'),
    ('soy',             'Soy'),
    ('sesame',          'Sesame'),
    ('meat',            'Contains meat'),
    ('animal_products', 'Contains animal products'),
    ('high_sugar',      'High sugar'),
    ('high_sodium',     'High sodium'),
    ('not_kosher',      'Not kosher'),
    ('not_halal',       'Not halal'),
    ('other',           'Other'),
]
CONFLICT_LABELS = dict(CONFLICT_CHOICES)
# Short emoji icons for compact rendering on calendar cells
CONFLICT_ICONS = {
    'gluten': '🌾', 'dairy': '🥛', 'egg': '🥚', 'peanut': '🥜',
    'tree_nut': '🌰', 'fish': '🐟', 'shellfish': '🦐', 'soy': '🫘',
    'sesame': '·', 'meat': '🍖', 'animal_products': '🐄',
    'high_sugar': 'S+', 'high_sodium': 'Na+', 'not_kosher': 'K✗',
    'not_halal': 'H✗', 'other': '?',
}


class Recipe(models.Model):
    name           = models.CharField(max_length=200, unique=True)
    yield_servings = models.IntegerField(default=40)
    source_doc     = models.CharField(max_length=500, blank=True)
    notes          = models.TextField(blank=True)
    protein        = models.CharField(max_length=20, blank=True, choices=PROTEIN_CHOICES,
                                      help_text="Primary protein; used for menu arrangement rules.")
    fat_health     = models.CharField(max_length=1, blank=True, choices=FAT_HEALTH_CHOICES,
                                      help_text="Sean's (F)=Fatty / (H)=Healthy oscillation tag.")
    popularity     = models.CharField(max_length=10, blank=True, choices=POPULARITY_CHOICES,
                                      help_text="Popularity among residents (from Menu Guide highlight color).")
    conflicts      = models.JSONField(default=list, blank=True,
                                      help_text="Dietary conflicts this recipe CONTAINS (Big 15 vocab). "
                                                "Used for client-dietary-safety matching.")

    def __str__(self):
        return self.name

    def conflict_labels(self):
        """Return list of (key, label, icon) tuples for rendering badges."""
        return [(k, CONFLICT_LABELS.get(k, k), CONFLICT_ICONS.get(k, '?'))
                for k in (self.conflicts or [])]

    def estimated_cost_breakdown(self):
        """Return dict: total Decimal, per_serving Decimal, priced count, total count, lines list."""
        from decimal import Decimal
        lines = []
        total = Decimal('0')
        priced = 0
        total_count = 0
        for ing in self.ingredients.all():
            if ing.sub_recipe:
                sub = ing.sub_recipe.estimated_cost_breakdown()
                # scale sub-recipe cost by quantity / sub yield_servings
                if ing.quantity and sub['total'] and ing.sub_recipe.yield_servings:
                    scaled = (sub['total'] * Decimal(ing.quantity)
                              / Decimal(ing.sub_recipe.yield_servings))
                    total += scaled
                    priced += 1
                    lines.append({'ingredient': ing, 'cost': scaled, 'note': 'sub-recipe'})
                else:
                    lines.append({'ingredient': ing, 'cost': None, 'note': 'sub-recipe incomplete'})
                total_count += 1
                continue
            cost, note = ing.estimated_cost()
            total_count += 1
            if cost is not None:
                total += cost
                priced += 1
            lines.append({'ingredient': ing, 'cost': cost, 'note': note})
        per_serving = (total / Decimal(self.yield_servings)) if self.yield_servings else None
        return {
            'total': total,
            'per_serving': per_serving,
            'priced': priced,
            'total_count': total_count,
            'coverage': (priced / total_count) if total_count else 0,
            'lines': lines,
        }

    def cost_for_headcount(self, headcount):
        """Scale the recipe's cost to a given headcount (useful for a menu serving N residents)."""
        from decimal import Decimal
        bd = self.estimated_cost_breakdown()
        if not bd['per_serving']:
            return None
        return (bd['per_serving'] * Decimal(headcount)).quantize(Decimal('0.01'))


class YieldReference(models.Model):
    """Canonical yield / conversion data — sourced from Book of Yields (8e) for internal use."""
    SECTION_CHOICES = [
        ('herbs_spices',  'Dry Herbs and Spices'),
        ('fresh_herbs',   'Fresh Herbs'),
        ('vegetables',    'Vegetables'),
        ('fruit',         'Fruit'),
        ('canned',        'Canned Foods'),
        ('dry_legumes',   'Dry Legumes'),
        ('grains',        'Rice/Grains/Cereals'),
        ('pasta',         'Pasta'),
        ('nuts_seeds',    'Nuts and Seeds'),
        ('flour',         'Flour/Meal/Bran/Crumbs'),
        ('sweeteners',    'Sweeteners'),
        ('baking',        'Special Baking Items'),
        ('fats_oils',     'Fats and Oils'),
        ('condiments',    'Condiments'),
        ('liquids',       'Liquids'),
        ('dairy',         'Dairy Products'),
        ('beverages',     'Coffee/Tea/Cocoa'),
        ('meats',         'Meats'),
        ('seafood',       'Seafood'),
        ('poultry',       'Poultry'),
    ]

    ingredient           = models.CharField(max_length=200)
    prep_state           = models.CharField(max_length=200, blank=True,
                                            help_text="e.g., 'peeled', 'grated', 'sliced about 1/4 in.'")
    section              = models.CharField(max_length=30, choices=SECTION_CHOICES, db_index=True)
    yield_pct            = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    ap_unit              = models.CharField(max_length=40, blank=True, help_text="As-purchased unit, e.g. 'pound', 'bunch'.")
    ap_weight_oz         = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    trimmed_unit         = models.CharField(max_length=40, blank=True)
    trimmed_weight_oz    = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    trimmed_count        = models.IntegerField(null=True, blank=True)
    measures_per_ap      = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                               help_text="e.g., cups per AP unit")
    ounce_weight_per_cup = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)

    extras               = models.JSONField(null=True, blank=True,
                                            help_text="Section-specific fields (cooked yield, raw→cooked ratios, etc.)")

    source               = models.CharField(max_length=50, default='Book of Yields 8e')
    source_ref           = models.CharField(max_length=50, blank=True, help_text="e.g., 'p.50'")
    notes                = models.TextField(blank=True)
    product              = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL,
                                             related_name='yield_refs')

    class Meta:
        unique_together = [('ingredient', 'prep_state', 'section')]
        indexes = [models.Index(fields=['section', 'ingredient'])]

    def __str__(self):
        bits = [self.ingredient]
        if self.prep_state:
            bits.append(self.prep_state)
        return ", ".join(bits)


class RecipeIngredient(models.Model):
    recipe     = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    name_raw   = models.CharField(max_length=300)
    quantity   = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                     help_text="Edible-portion quantity the recipe calls for.")
    unit       = models.CharField(max_length=30, blank=True)
    yield_pct  = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Edible yield % — only used if yield_ref is not set. "
                  "E.g., 81 for peeled carrots. Null = no adjustment.",
    )
    yield_ref  = models.ForeignKey(
        YieldReference, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='used_by_ingredients',
        help_text="If set, effective yield comes from this reference entry.",
    )
    product    = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    sub_recipe = models.ForeignKey(
        Recipe, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='used_in', help_text="If set, this ingredient is another recipe (composed meal component).",
    )

    def __str__(self):
        return f"{self.quantity} {self.unit} {self.name_raw}".strip()

    @property
    def effective_yield_pct(self):
        if self.yield_ref and self.yield_ref.yield_pct is not None:
            return self.yield_ref.yield_pct
        return self.yield_pct

    @property
    def ap_quantity_needed(self):
        eff = self.effective_yield_pct
        if self.quantity is None or not eff:
            return self.quantity
        return self.quantity / (eff / 100)

    def estimated_cost(self):
        """Latest-price $ cost for this ingredient line. Returns (cost_or_None, note)."""
        from .cost_utils import ingredient_cost
        from .models import InvoiceLineItem   # self-import ok — called at runtime
        if not self.product or self.quantity is None:
            return None, 'missing product or quantity'
        latest = (InvoiceLineItem.objects
                  .filter(product=self.product)
                  .order_by('-invoice_date')
                  .first())
        if not latest:
            return None, 'no invoice history'
        density = (self.yield_ref.ounce_weight_per_cup
                   if self.yield_ref and self.yield_ref.ounce_weight_per_cup
                   else None)
        return ingredient_cost(
            self.quantity, self.unit, self.name_raw,
            latest.unit_price, latest.case_size,
            yield_pct=self.effective_yield_pct,
            ounce_weight_per_cup=density,
        )


class Menu(models.Model):
    MEAL_SLOTS = [
        ('cold_breakfast', 'Cold Breakfast'),
        ('hot_breakfast', 'Hot Breakfast'),
        ('lunch', 'Lunch'),
        ('dinner', 'Dinner'),
    ]

    date                = models.DateField()
    meal_slot           = models.CharField(max_length=20, choices=MEAL_SLOTS)
    recipe              = models.ForeignKey(Recipe, null=True, blank=True, on_delete=models.SET_NULL)
    additional_recipes  = models.ManyToManyField(Recipe, blank=True, related_name='menus_as_side')
    dish_freetext       = models.CharField(max_length=200, blank=True)
    ingredients_raw     = models.TextField(blank=True)
    assignee            = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES, blank=True)

    class Meta:
        unique_together = [('date', 'meal_slot')]
        indexes = [models.Index(fields=['date'])]

    def __str__(self):
        dish = self.recipe.name if self.recipe else self.dish_freetext
        return f"{self.date} {self.meal_slot}: {dish}"


class PrepTask(models.Model):
    date      = models.DateField()
    recipe    = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='prep_tasks')
    assignee  = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES, blank=True)
    completed = models.BooleanField(default=False)
    notes     = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=['date'])]

    def __str__(self):
        return f"{self.date} prep: {self.recipe.name}"


class IngredientSkipNote(models.Model):
    """A breadcrumb left by Sean during bridge review so tomorrow's product-catalog
    pass knows which ingredient name was skipped and why.
    Stored per unique name_raw, not per RecipeIngredient.
    """
    name_raw   = models.CharField(max_length=200, unique=True)
    reason     = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name_raw}: {self.reason or '(no reason)'}"


class MenuFreetextComponent(models.Model):
    """A component on a Menu that isn't a linked Recipe yet — just a name + quantity.
    Promoted to a RecipeIngredient (name_raw + qty + unit) when the menu is saved as a meal.
    """
    menu     = models.ForeignKey('Menu', on_delete=models.CASCADE, related_name='freetext_components')
    name     = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    unit     = models.CharField(max_length=30, blank=True)
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        q = f"{self.quantity} {self.unit}".strip() if self.quantity else ''
        return f"{self.name} {q}".strip()


class Census(models.Model):
    date      = models.DateField(unique=True)
    headcount = models.IntegerField()
    notes     = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name_plural = 'Census'

    def __str__(self):
        return f"{self.date}: {self.headcount}"


class StandardPortionReference(models.Model):
    """Canonical per-portion sizes from Book of Yields 8e Chapter 15.

    Values stored as strings to preserve source fidelity ('1/4th whole 3 lb
    fryer', '3 strips', '5 fl. oz.' etc.). App-layer parses when needed.
    """
    CATEGORY_CHOICES = [
        ('soup_salad_bread', 'Soup, Salad, Bread'),
        ('beef_entrees', 'Beef Entrees'),
        ('chicken_entrees', 'Chicken Entrees'),
        ('seafood_entrees', 'Seafood Entrees'),
        ('pork_entrees', 'Pork Entrees'),
        ('veal', 'Veal'),
        ('pasta_entree', 'Pasta Entree'),
        ('potatoes', 'Potatoes'),
        ('desserts', 'Desserts'),
        ('beverages', 'Beverages'),
        ('breakfast_items', 'Breakfast Items'),
        ('lunch_items', 'Lunch Items'),
        ('hors_doeuvre', "Hors d'Oeuvre"),
    ]

    menu_item        = models.CharField(max_length=120)
    category         = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    average_measure  = models.CharField(max_length=40, blank=True)
    low_range        = models.CharField(max_length=40, blank=True)
    high_range       = models.CharField(max_length=40, blank=True)
    source           = models.CharField(max_length=60, default='Book of Yields 8e Ch 15')
    source_ref       = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ['category', 'menu_item']
        indexes = [models.Index(fields=['category', 'menu_item'])]
        constraints = [
            models.UniqueConstraint(fields=['category', 'menu_item'], name='uniq_portion_by_category_item'),
        ]

    def __str__(self):
        return f"{self.menu_item} ({self.average_measure})"
