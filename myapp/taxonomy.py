"""Taxonomy inference for new Product creation.

Pre-fills (category, primary_descriptor, secondary_descriptor) when the
mapping-review reviewer creates a new Product from a raw_description
that doesn't match any existing canonical. Reduces the typing burden
from "fill 3 fields per new Product" to "confirm 3 pre-filled fields."

Multi-source inference, in priority order (high → low confidence):

  1. subset_canonical's existing taxonomy   — inherit if provided
  2. Existing Products sharing tokens       — your locked-convention truth
  3. BoY YieldReference ingredient match    — industry-standard food taxonomy
  4. Sysco section_hint mapping             — _SYSCO_SECTION_TO_CATEGORIES
  5. Vendor heuristic                       — PBM=Bakery, Farm Art=Produce, etc.
  6. Hardcoded convention rules             — botanical primaries for Produce,
                                              primal cuts for Proteins, etc.

Signal contributions are combined into one (category, primary, secondary)
output with confidence labels and a reasoning trail so the reviewer can
see WHY each field was suggested.

The convention encoded here is the locked version from
`project_naming_taxonomy_convention.md` (locked 2026-04-25).
"""
from __future__ import annotations

import re
from collections import Counter

# ---- Hardcoded convention encoding ----

# BoY section → (category, default_primary) mapping per locked convention.
# Section names from yield_parsing/__init__.py PARSER_FOR_SECTION keys.
_BOY_SECTION_TO_TAXONOMY = {
    'meats':         ('Proteins', None),     # primary inferred from ingredient tokens
    'poultry':       ('Proteins', 'Poultry'),
    'seafood':       ('Proteins', 'Seafood'),
    'dairy':         ('Dairy',    None),     # could be Milk/Cream/Butter/Yogurt
    'fruit':         ('Produce',  None),     # specific primary inferred from token
    'vegetables':    ('Produce',  None),     # specific primary inferred from token
    'herbs_spices':  ('Drystock', 'Spices'),
    'fresh_herbs':   ('Produce',  'Herbs'),
    'flour':         ('Drystock', 'Flours and Starches'),
    'pasta':         ('Drystock', 'Pasta'),
    'grains':        ('Drystock', 'Grains'),
    'sweeteners':    ('Drystock', 'Sugars'),
    'baking':        ('Drystock', 'Baking'),
    'fats_oils':     ('Drystock', 'Oils'),
    'condiments':    ('Condiments/Sauces', None),
    'liquids':       ('Drystock', None),
    'beverages':     ('Coffee/Concessions', 'Beverage'),
    'canned':        ('Drystock', 'Canned'),
    'dry_legumes':   ('Drystock', 'Legumes'),
    'nuts_seeds':    ('Drystock', 'Baking'),
}

# Vendor → (default_category, default_primary). Used as fallback when other
# signals are weak. PBM ships almost exclusively bakery; Farm Art produce.
_VENDOR_DEFAULTS = {
    'PHILADELPHIA BAKERY MERCHANTS': ('Bakery',   'Pastry'),
    'FARM ART':                      ('Produce',  None),
    'COLONIAL VILLAGE MEAT MARKETS': ('Proteins', None),
    'EXCEPTIONAL FOODS':             ('Proteins', None),
    'DELAWARE COUNTY LINEN':         ('Paper/Disposable', None),
    'ARAMARK':                       ('Coffee/Concessions', 'Coffee'),
    # Sysco intentionally omitted — too multi-category for a useful default
}

# Per-convention botanical primaries — explicit token lookups for Produce.
# Wins over BoY's generic 'Produce' assignment because the convention
# locked specific botanical groupings.
_PRODUCE_TOKEN_TO_PRIMARY = {
    # Allium
    'onion': 'Allium', 'onions': 'Allium', 'garlic': 'Allium',
    'leek': 'Allium', 'leeks': 'Allium', 'shallot': 'Allium',
    'shallots': 'Allium', 'chive': 'Allium', 'chives': 'Allium',
    'scallion': 'Allium', 'scallions': 'Allium',
    # Brassica Oleracea
    'broccoli': 'Brassica Oleracea', 'cauliflower': 'Brassica Oleracea',
    'cabbage': 'Brassica Oleracea', 'kale': 'Brassica Oleracea',
    'brussels': 'Brassica Oleracea', 'collard': 'Brassica Oleracea',
    'collards': 'Brassica Oleracea',
    # Capsicum
    'jalapeno': 'Capsicum', 'jalapeño': 'Capsicum', 'poblano': 'Capsicum',
    'serrano': 'Capsicum', 'habanero': 'Capsicum', 'capsicum': 'Capsicum',
    # Cucurbit
    'cucumber': 'Cucurbit', 'cucumbers': 'Cucurbit',
    'squash': 'Cucurbit', 'zucchini': 'Cucurbit',
    'honeydew': 'Cucurbit', 'cantaloupe': 'Cucurbit',
    'watermelon': 'Cucurbit', 'pumpkin': 'Cucurbit',
    'melon': 'Cucurbit', 'melons': 'Cucurbit',
    # Stone Fruit
    'mango': 'Stone Fruit', 'mangoes': 'Stone Fruit',
    'peach': 'Stone Fruit', 'peaches': 'Stone Fruit',
    'plum': 'Stone Fruit', 'plums': 'Stone Fruit',
    'cherry': 'Stone Fruit', 'cherries': 'Stone Fruit',
    'apricot': 'Stone Fruit', 'apricots': 'Stone Fruit',
    'olive': 'Stone Fruit', 'olives': 'Stone Fruit',
    'avocado': 'Stone Fruit', 'avocados': 'Stone Fruit',
    # Citrus
    'lemon': 'Citrus', 'lemons': 'Citrus',
    'lime': 'Citrus', 'limes': 'Citrus',
    'orange': 'Citrus', 'oranges': 'Citrus',
    'grapefruit': 'Citrus', 'tangerine': 'Citrus',
    # Berry
    'strawberry': 'Berry', 'strawberries': 'Berry',
    'blueberry': 'Berry', 'blueberries': 'Berry',
    'raspberry': 'Berry', 'raspberries': 'Berry',
    'blackberry': 'Berry', 'blackberries': 'Berry',
    # Pome
    'apple': 'Pome', 'apples': 'Pome',
    'pear': 'Pome', 'pears': 'Pome',
    # Solanaceae
    'tomato': 'Solanaceae', 'tomatoes': 'Solanaceae',
    'tomatillo': 'Solanaceae', 'tomatillos': 'Solanaceae',
    'eggplant': 'Solanaceae',
    # Leaf/Greens
    'romaine': 'Leaf/Greens', 'spinach': 'Leaf/Greens',
    'lettuce': 'Leaf/Greens', 'arugula': 'Leaf/Greens',
    'iceberg': 'Leaf/Greens',
    # Tuber / root
    'potato': 'Tuber', 'potatoes': 'Tuber',
    'yam': 'Tuber', 'sweet': 'Tuber',  # 'sweet potato'
    'carrot': 'Root', 'carrots': 'Root',
    'beet': 'Root', 'beets': 'Root',
    'turnip': 'Root', 'turnips': 'Root',
    'radish': 'Root', 'radishes': 'Root',
    # Fungi
    'mushroom': 'Fungi', 'mushrooms': 'Fungi',
    'shiitake': 'Fungi', 'cremini': 'Fungi',
    'portobello': 'Fungi',
    # Herbs (fresh) — overlap with herbs_spices section
    'basil': 'Herbs', 'rosemary': 'Herbs', 'thyme': 'Herbs',
    'oregano': 'Herbs', 'parsley': 'Herbs', 'cilantro': 'Herbs',
    'sage': 'Herbs', 'mint': 'Herbs', 'dill': 'Herbs',
    # Bell pepper falls under Capsicum
    'bell': 'Capsicum',  # 'bell pepper'
    'pepper': 'Capsicum',  # ambiguous; needs Produce category context
    'peppers': 'Capsicum',
    # Misc
    'asparagus': 'Vegetable',
    'celery': 'Vegetable',
    'corn': 'Vegetable',
    'pea': 'Legume', 'peas': 'Legume',
    'bean': 'Legume', 'beans': 'Legume',
}

# Protein primal cut keywords. Token → (primary, secondary).
# Order matters — first match wins. Multi-token cuts checked first.
_PROTEIN_PRIMAL_RULES = [
    # Beef primals (your butcher knowledge — domain-locked)
    ('chuck',       ('Beef', 'Chuck')),
    ('brisket',     ('Beef', 'Brisket')),
    ('ribeye',      ('Beef', 'Rib')),
    ('striploin',   ('Beef', 'Loin')),
    ('strip loin',  ('Beef', 'Loin')),
    ('tenderloin',  ('Beef', 'Loin')),
    ('sirloin',     ('Beef', 'Sirloin')),
    ('tri tip',     ('Beef', 'Sirloin')),
    ('top round',   ('Beef', 'Round')),
    ('eye of round', ('Beef', 'Round')),
    ('bottom round', ('Beef', 'Round')),
    ('round',       ('Beef', 'Round')),
    ('skirt',       ('Beef', 'Plate')),
    ('flank',       ('Beef', 'Flank')),
    ('shank',       ('Beef', 'Shank')),
    ('ground beef', ('Beef', 'Processed')),
    ('beef',        ('Beef', None)),
    # Pork primals
    ('pork belly',  ('Pork', 'Belly')),
    ('pork butt',   ('Pork', 'Shoulder')),
    ('boston butt', ('Pork', 'Shoulder')),
    ('pork shoulder', ('Pork', 'Shoulder')),
    ('pork loin',   ('Pork', 'Loin')),
    ('pork rib',    ('Pork', 'Rib')),
    ('st louis',    ('Pork', 'Rib')),    # St. Louis-style ribs
    ('baby back',   ('Pork', 'Rib')),
    ('ham',         ('Pork', 'Leg')),
    ('bacon',       ('Pork', 'Belly')),  # bacon is cured pork belly
    ('sausage',     ('Pork', 'Processed')),
    ('kielbasa',    ('Pork', 'Processed')),
    ('chorizo',     ('Pork', 'Processed')),
    ('pepperoni',   ('Pork', 'Processed')),
    ('prosciutto',  ('Pork', 'Cured')),
    ('salami',      ('Pork', 'Cured')),
    ('pork',        ('Pork', None)),
    # Poultry
    ('chicken breast', ('Poultry', 'Breast')),
    ('chicken thigh',  ('Poultry', 'Thigh')),
    ('chicken wing',   ('Poultry', 'Wing')),
    ('chicken tender', ('Poultry', 'Breast')),
    ('chicken fryer',  ('Poultry', 'Whole')),
    ('chicken whole',  ('Poultry', 'Whole')),
    ('drumstick',      ('Poultry', 'Leg')),
    ('chicken',        ('Poultry', None)),
    ('turkey breast',  ('Poultry', 'Breast')),
    ('turkey',         ('Poultry', None)),
    ('duck',           ('Poultry', None)),
    # Seafood — usually fish name itself is the canonical
    ('tilapia',     ('Seafood', 'Fish')),
    ('salmon',      ('Seafood', 'Fish')),
    ('cod',         ('Seafood', 'Fish')),
    ('tuna',        ('Seafood', 'Fish')),
    ('halibut',     ('Seafood', 'Fish')),
    ('mahi',        ('Seafood', 'Fish')),
    ('shrimp',      ('Seafood', 'Shellfish')),
    ('lobster',     ('Seafood', 'Shellfish')),
    ('crab',        ('Seafood', 'Shellfish')),
    ('clam',        ('Seafood', 'Shellfish')),
    ('mussel',      ('Seafood', 'Shellfish')),
    # Lamb / Game
    ('lamb',        ('Lamb', None)),
    # Eggs (often classified under Poultry per your convention)
    ('egg',         ('Poultry', 'Eggs')),
    ('eggs',        ('Poultry', 'Eggs')),
    # Bases
    ('beef base',   ('Bases', None)),
    ('chicken base',('Bases', None)),
    ('crab base',   ('Bases', None)),
]

# Cheese knowledge per the locked convention (Cheese category requires
# milk source as primary and texture/age as secondary). The CHEESE_TYPES
# table encodes industry-standard cheese classifications. When raw
# contains "cheese" + a recognizable cheese type, this signal fires.
#
# Milk source: Cow (default for unspecified blends/most US cheeses),
# Goat, Sheep, Processed.
# Texture: Fresh, Soft-Ripened, Semi-Soft, Semi-Hard, Hard, Blue, Processed.
# Sources: standard dairy/cheese references; updatable as Sean refines.
_CHEESE_TYPES = {
    # token (lower)        : (primary_milk_source, secondary_texture_or_age)
    # Fresh cow cheeses
    'mozzarella':  ('Cow',       'Fresh'),
    'ricotta':     ('Cow',       'Fresh'),
    'cottage':     ('Cow',       'Fresh'),
    'burrata':     ('Cow',       'Fresh'),
    'cream':       ('Cow',       'Fresh'),  # cream cheese
    'mascarpone':  ('Cow',       'Fresh'),
    # Soft-ripened cow
    'brie':        ('Cow',       'Soft-Ripened'),
    'camembert':   ('Cow',       'Soft-Ripened'),
    # Semi-soft cow
    'provolone':   ('Cow',       'Semi-Soft'),
    'muenster':    ('Cow',       'Semi-Soft'),
    'monterey':    ('Cow',       'Semi-Soft'),
    'havarti':     ('Cow',       'Semi-Soft'),
    'pepperjack':  ('Cow',       'Semi-Soft'),
    'colby':       ('Cow',       'Semi-Soft'),
    # Semi-hard cow
    'cheddar':     ('Cow',       'Semi-Hard'),
    'swiss':       ('Cow',       'Semi-Hard'),
    'gouda':       ('Cow',       'Semi-Hard'),
    'edam':        ('Cow',       'Semi-Hard'),
    'gruyere':     ('Cow',       'Semi-Hard'),
    'emmental':    ('Cow',       'Semi-Hard'),
    'fontina':     ('Cow',       'Semi-Hard'),
    'taleggio':    ('Cow',       'Semi-Hard'),
    'jack':        ('Cow',       'Semi-Hard'),  # monterey jack et al
    # Hard cow
    'parmesan':    ('Cow',       'Hard'),
    'parm':        ('Cow',       'Hard'),
    'asiago':      ('Cow',       'Hard'),
    # Goat
    'goat':        ('Goat',      'Fresh'),
    'chevre':      ('Goat',      'Fresh'),
    'chèvre':      ('Goat',      'Fresh'),
    # Sheep
    'feta':        ('Sheep',     'Fresh'),
    'manchego':    ('Sheep',     'Hard'),
    'pecorino':    ('Sheep',     'Hard'),
    'romano':      ('Sheep',     'Hard'),
    'roquefort':   ('Sheep',     'Blue'),
    # Blue (mostly cow)
    'gorgonzola':  ('Cow',       'Blue'),
    'stilton':     ('Cow',       'Blue'),
    'blue':        ('Cow',       'Blue'),  # context-dependent — Sean overrides if Sheep/Goat
    # Processed
    'american':    ('Processed', 'Processed'),
    'velveeta':    ('Processed', 'Processed'),
    'singles':     ('Processed', 'Processed'),
    # Generic fallback patterns
    'queso':       ('Cow',       None),  # too many sub-types
}

# Bakery item keywords — when these appear in raw, it's almost certainly
# a bakery product regardless of other ingredient-name tokens.
# Maps to (primary_descriptor) within Bakery category.
#
# Five-bucket bakery-science taxonomy (locked 2026-04-25):
#   Bread/Fermented   — yeast-leavened structures (incl. flatbreads)
#   Cakes & Sponges   — foam/chemically-leavened sweet goods
#   Pastries          — laminated dough, shortcrust, choux
#   Quick Breads      — chemically-leavened, non-laminated
#   Cookies & Bars    — cookies, brownies, blondies, bar cookies
#
# Edge calls:
#   - donuts → Pastries (yeast vs cake split exists, but treating as
#     pastry by default; donut sub-type can split if needed)
#   - flatbreads (tortilla, pita, wrap) → Bread/Fermented (industry
#     convention; pita is yeasted, tortilla often baking-powder leavened)
#   - cheesecake → Cakes & Sponges (structurally a custard, but kitchen
#     usage groups with cakes)
_BAKERY_KEYWORDS = {
    # Pastries — laminated/shortcrust/choux
    'danish':       'Pastries',
    'croissant':    'Pastries',
    'pie':          'Pastries',
    'cobbler':      'Pastries',
    'donut':        'Pastries',
    'donuts':       'Pastries',
    'doughnut':     'Pastries',
    'doughnuts':    'Pastries',
    'eclair':       'Pastries',
    'turnover':     'Pastries',
    'turnovers':    'Pastries',
    'strudel':      'Pastries',
    # Cakes & Sponges
    'cake':         'Cakes & Sponges',
    'cupcake':      'Cakes & Sponges',
    'cupcakes':     'Cakes & Sponges',
    'cheesecake':   'Cakes & Sponges',
    # Quick Breads
    'muffin':       'Quick Breads',
    'muffins':      'Quick Breads',
    'scone':        'Quick Breads',
    'scones':       'Quick Breads',
    'biscuit':      'Quick Breads',
    'biscuits':     'Quick Breads',
    'cornbread':    'Quick Breads',
    # Cookies & Bars
    'cookie':       'Cookies & Bars',
    'cookies':      'Cookies & Bars',
    'brownie':      'Cookies & Bars',
    'brownies':     'Cookies & Bars',
    'blondie':      'Cookies & Bars',
    'blondies':     'Cookies & Bars',
    # Bread/Fermented (yeasted structures + flatbreads)
    'bread':        'Bread/Fermented',
    'loaf':         'Bread/Fermented',
    'baguette':     'Bread/Fermented',
    'bagel':        'Bread/Fermented',
    'bagels':       'Bread/Fermented',
    'roll':         'Bread/Fermented',
    'rolls':        'Bread/Fermented',
    'bun':          'Bread/Fermented',
    'buns':         'Bread/Fermented',
    'pita':         'Bread/Fermented',
    'tortilla':     'Bread/Fermented',
    'tortillas':    'Bread/Fermented',
    'wrap':         'Bread/Fermented',
    'wraps':        'Bread/Fermented',
    'naan':         'Bread/Fermented',
    'focaccia':     'Bread/Fermented',
    'ciabatta':     'Bread/Fermented',
}


# Sysco section_hint → category candidates.
# Mirror of mapper.py's _SYSCO_SECTION_TO_CATEGORIES with single-best-guess.
_SYSCO_SECTION_TO_CATEGORY = {
    'DAIRY':          'Dairy',
    'PRODUCE':        'Produce',
    'MEATS':          'Proteins',
    'MEAT':           'Proteins',
    'POULTRY':        'Proteins',
    'SEAFOOD':        'Proteins',
    'CANNED':         'Drystock',
    'DRY':            'Drystock',
    'PAPER':          'Paper/Disposable',
    'DISPOSABLE':     'Paper/Disposable',
    'JANITORIAL':     'Chemicals',
    'CHEMICAL':       'Chemicals',
    'BEVERAGE':       'Coffee/Concessions',
    'BAKERY':         'Bakery',
    'DELI':           'Proteins',
    'SPICES':         'Drystock',
    'GROCERY':        'Drystock',
}

# Token tokenizer matching the mapper's pattern (3+ letters, lowercase)
_TOKEN_RE = re.compile(r'[A-Za-z]{3,}')


def _expand_text(s: str) -> str:
    """Expand vendor abbreviations (BRST→Breast, CHKN→Chicken, SHRD→Shredded)
    so English-token matching against canonicals + BoY actually finds
    overlap. Single source-of-truth dict lives in
    invoice_processor/abbreviations.py."""
    if not s:
        return s
    import sys
    from django.conf import settings
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    from abbreviations import expand_abbreviations
    return expand_abbreviations(s)


def _tokenize(s: str) -> list[str]:
    """Lowercase 3+letter tokens — vendor abbreviations expanded first."""
    return [t.lower() for t in _TOKEN_RE.findall(_expand_text(s) or '')]


def _stem(token: str) -> str:
    """Plural strip — mirrors mapper._stem_text food-domain patterns.

    Handles 'rries'→'rry', 'ovies'→'ovy', 'atoes'→'ato', 'goes'→'go',
    'ches'→'ch', 'shes'→'sh', 'xes'→'x', and trailing 's' fallback.
    Suffix-anchored so cookies/brownies/shoes don't over-stem."""
    t = token.lower()
    if t.endswith('rries') and len(t) >= 6:
        return t[:-3] + 'y'
    if t.endswith('ovies') and len(t) >= 7:
        return t[:-3] + 'y'
    if t.endswith('atoes') and len(t) >= 6:
        return t[:-2]
    if t.endswith('goes') and len(t) >= 5:
        return t[:-2]
    if t.endswith('ches') and len(t) >= 5:
        return t[:-2]
    if t.endswith('shes') and len(t) >= 5:
        return t[:-2]
    if t.endswith('xes') and len(t) >= 4:
        return t[:-2]
    if len(t) >= 4 and t.endswith('s') and not t.endswith('ss'):
        return t[:-1]
    return t


def _stems(s: str) -> set[str]:
    return {_stem(t) for t in _tokenize(s)}


# ---- Inference signal functions ----

def _signal_subset_canonical(subset_canonical):
    """If a subset-match canonical is provided, return its taxonomy as
    the highest-confidence signal (we're inheriting from existing data
    Sean already classified)."""
    if not subset_canonical:
        return None
    from myapp.models import Product
    p = Product.objects.filter(canonical_name=subset_canonical).first()
    if not p:
        return None
    return {
        'category': (p.category, 'high'),
        'primary': (p.primary_descriptor, 'high') if p.primary_descriptor else (None, 'low'),
        'secondary': (p.secondary_descriptor, 'high') if p.secondary_descriptor else (None, 'low'),
        'reasoning': [f"inherited from subset suggestion {subset_canonical!r}"],
    }


def _signal_existing_products(raw_tokens, index=None):
    """Find existing Products that share tokens with the raw description.
    Vote on (category, primary) — if multiple Products agree, that's signal.

    When `index` is supplied (a list of pre-stemmed Product tuples from
    `build_inference_index`), the per-call DB query + stemming is skipped.
    Used by the mapping-review page to avoid an N×M blow-up across the
    visible 100 rows."""
    if not raw_tokens:
        return None
    cat_votes = Counter()
    pri_votes = Counter()
    sec_votes = Counter()
    voters = []
    if index is not None:
        rows = index
    else:
        from myapp.models import Product
        rows = [(p.canonical_name, _stems(p.canonical_name), p.category,
                 p.primary_descriptor, p.secondary_descriptor)
                for p in Product.objects.all().only(
                    'canonical_name', 'category',
                    'primary_descriptor', 'secondary_descriptor')]
    for canonical, ptokens, cat, pri, sec in rows:
        if raw_tokens & ptokens:
            cat_votes[cat] += 1
            if pri:
                pri_votes[pri] += 1
            if sec:
                sec_votes[sec] += 1
            voters.append(canonical)
    if not cat_votes:
        return None
    best_cat, cat_n = cat_votes.most_common(1)[0]
    out = {'reasoning': [f"{len(voters)} existing Product(s) share tokens "
                         f"(e.g., {voters[:3]!r})"]}
    if cat_n >= 2 and cat_n / sum(cat_votes.values()) >= 0.6:
        out['category'] = (best_cat, 'high')
    else:
        out['category'] = (best_cat, 'medium')
    if pri_votes:
        best_pri, pri_n = pri_votes.most_common(1)[0]
        out['primary'] = (best_pri,
                          'high' if pri_n >= 2 and pri_n / sum(pri_votes.values()) >= 0.6 else 'medium')
    return out


def _signal_boy_yieldreference(raw_tokens, index=None):
    """Find BoY YieldReference rows whose ingredient shares tokens with raw.
    Returns the section + derived (category, primary) from
    _BOY_SECTION_TO_TAXONOMY.

    Accepts pre-stemmed `index` for batch use (see _signal_existing_products)."""
    if not raw_tokens:
        return None
    section_votes = Counter()
    voters = []
    if index is not None:
        rows = index
    else:
        from myapp.models import YieldReference
        rows = [(yr.ingredient, _stems(yr.ingredient), yr.section)
                for yr in YieldReference.objects.all().only('ingredient', 'section')]
    for ingredient, ytokens, section in rows:
        if raw_tokens & ytokens:
            section_votes[section] += 1
            voters.append(f"{ingredient} ({section})")
    if not section_votes:
        return None
    best_section, _ = section_votes.most_common(1)[0]
    cat, default_pri = _BOY_SECTION_TO_TAXONOMY.get(best_section, (None, None))
    if not cat:
        return None
    out = {
        'category': (cat, 'high'),
        'reasoning': [f"BoY ingredient match → section {best_section!r} "
                      f"(e.g., {voters[:2]!r})"],
    }
    if default_pri:
        out['primary'] = (default_pri, 'medium')
    return out


def _signal_section_hint(section_hint):
    """Sysco parser captures section headers — strong category signal."""
    if not section_hint:
        return None
    upper = section_hint.upper()
    for key, cat in _SYSCO_SECTION_TO_CATEGORY.items():
        if key in upper:
            return {
                'category': (cat, 'high'),
                'reasoning': [f"Sysco section_hint {section_hint!r} → {cat!r}"],
            }
    return None


def _signal_vendor(vendor):
    """Vendor → default category. Lowest confidence signal (overridden
    by other signals when present)."""
    if not vendor:
        return None
    cat_pri = _VENDOR_DEFAULTS.get(vendor.upper())
    if not cat_pri:
        return None
    cat, pri = cat_pri
    out = {
        'category': (cat, 'medium'),
        'reasoning': [f"vendor {vendor!r} → typical category {cat!r}"],
    }
    if pri:
        out['primary'] = (pri, 'low')
    return out


def _signal_produce_botanical(raw_tokens, current_category):
    """If category is Produce, look up botanical primary in
    _PRODUCE_TOKEN_TO_PRIMARY. Strong primary signal for Produce only."""
    if current_category != 'Produce':
        return None
    matches = []
    for tok in raw_tokens:
        if tok in _PRODUCE_TOKEN_TO_PRIMARY:
            matches.append((tok, _PRODUCE_TOKEN_TO_PRIMARY[tok]))
    if not matches:
        return None
    # Prefer multi-vote primaries
    pri_counter = Counter(m[1] for m in matches)
    best_pri, _ = pri_counter.most_common(1)[0]
    return {
        'primary': (best_pri, 'high'),
        'reasoning': [f"botanical primary from token(s) "
                      f"{[m[0] for m in matches if m[1] == best_pri]!r} → {best_pri!r}"],
    }


def _signal_cheese(raw_lower, raw_tokens):
    """Detect cheese products. Matching strategy:
      1. Raw contains the word 'cheese' OR
      2. Raw contains a recognized cheese-type token from _CHEESE_TYPES
    Returns category=Cheese with the convention's milk-source primary
    + texture/age secondary derived from the matched type.

    High confidence — overrides generic ingredient matches for tokens
    like 'pepper' (Pepper Jack), 'cream' (Cream Cheese), 'goat' (Goat
    Cheese) etc. that would otherwise route to wrong categories."""
    has_cheese_word = 'cheese' in raw_lower
    matched_types = [(tok, _CHEESE_TYPES[tok])
                     for tok in raw_tokens if tok in _CHEESE_TYPES]
    if not has_cheese_word and not matched_types:
        return None

    out = {
        'category': ('Cheese', 'high'),
        'reasoning': [],
    }
    if has_cheese_word:
        out['reasoning'].append("token 'cheese' present → category=Cheese")
    if matched_types:
        # Pick the FIRST recognized cheese type (raw token order roughly preserved)
        chosen_token, (milk, texture) = matched_types[0]
        out['primary'] = (milk, 'high')
        out['reasoning'].append(
            f"cheese type {chosen_token!r} → milk source {milk!r}"
            + (f" / texture {texture!r}" if texture else ""))
        if texture:
            out['secondary'] = (texture, 'high')
    elif has_cheese_word:
        # Cheese category set but type unrecognized — leave primary as default Cow
        out['primary'] = ('Cow', 'medium')
        out['reasoning'].append("cheese type unrecognized → defaulting to Cow")
    return out


def _signal_bakery_keyword(raw_lower):
    """Detect bakery items by dish-type keywords (croissant, danish,
    muffin, bagel, etc.). When raw contains one of these, force
    category=Bakery regardless of ingredient-name tokens (Lemon Danish
    is bakery, not produce, even though 'lemon' is a citrus token)."""
    matched = [(kw, pri) for kw, pri in _BAKERY_KEYWORDS.items()
               if kw in raw_lower]
    if not matched:
        return None
    # Most-specific keyword (longest) wins for the primary
    matched.sort(key=lambda x: -len(x[0]))
    kw, primary = matched[0]
    return {
        'category': ('Bakery', 'high'),
        'primary': (primary, 'high'),
        'reasoning': [f"bakery keyword {kw!r} → category=Bakery / primary={primary!r}"],
    }


def _signal_protein_primal(raw_lower, current_category):
    """If category is Proteins, scan _PROTEIN_PRIMAL_RULES for primal cuts.
    Returns (primary, secondary) pair from butcher-domain encoding."""
    if current_category != 'Proteins':
        return None
    for keyword, (primary, secondary) in _PROTEIN_PRIMAL_RULES:
        if keyword in raw_lower:
            out = {
                'primary': (primary, 'high'),
                'reasoning': [f"protein primal keyword {keyword!r} → {primary}"
                              + (f" / {secondary}" if secondary else "")],
            }
            if secondary:
                out['secondary'] = (secondary, 'high')
            return out
    return None


# ---- Main inference entry point ----

def build_inference_index():
    """Pre-stem all Products + YieldReferences for batch inference calls.

    Returns:
      {'products': [(canonical, stems_set, category, primary, secondary), ...],
       'boy':      [(ingredient, stems_set, section), ...]}

    Pass the result as the `index` kwarg to `infer_taxonomy` to amortize
    the stemming + DB load across many calls (e.g. rendering 100 rows on
    the Mapping Review page). Without this, each call re-queries and
    re-stems all 544+ Products and 1119 YieldReferences.
    """
    from myapp.models import Product, YieldReference
    products = [
        (p.canonical_name, _stems(p.canonical_name), p.category,
         p.primary_descriptor, p.secondary_descriptor)
        for p in Product.objects.all().only(
            'canonical_name', 'category',
            'primary_descriptor', 'secondary_descriptor')
    ]
    boy = [
        (yr.ingredient, _stems(yr.ingredient), yr.section)
        for yr in YieldReference.objects.all().only('ingredient', 'section')
    ]
    return {'products': products, 'boy': boy}


def infer_taxonomy(raw_description, vendor=None, section_hint=None,
                   subset_canonical=None, index=None):
    """Infer (category, primary, secondary) for a new Product.

    Returns dict shape:
      {
        'category':  (value_or_None, confidence_label),
        'primary':   (value_or_None, confidence_label),
        'secondary': (value_or_None, confidence_label),
        'reasoning': [list of human-readable signal explanations],
      }

    confidence_label is 'high' | 'medium' | 'low' | 'unknown'.
    """
    raw_tokens = _stems(raw_description)
    # Use expanded text everywhere so substring + token signals see English
    # words rather than vendor abbreviations.
    raw_lower = _expand_text(raw_description or '').lower()

    # Initialize with unknowns
    out = {
        'category':  (None, 'unknown'),
        'primary':   (None, 'unknown'),
        'secondary': (None, 'unknown'),
        'reasoning': [],
    }

    # Signals applied in priority order from least-authoritative to most.
    # Same-or-higher confidence WINS (later signal replaces earlier when
    # confidence is at least equal). This makes the call order = priority
    # order: each subsequent signal can REFINE the previous one.
    confidence_rank = {'unknown': 0, 'low': 1, 'medium': 2, 'high': 3, 'locked': 4}

    def _absorb(signal):
        if not signal:
            return
        for field in ('category', 'primary', 'secondary'):
            if field not in signal:
                continue
            new_val, new_conf = signal[field]
            if new_val is None:
                continue
            cur_val, cur_conf = out[field]
            # Same-or-higher confidence overrides earlier value
            if confidence_rank[new_conf] >= confidence_rank[cur_conf]:
                out[field] = (new_val, new_conf)
        if 'reasoning' in signal:
            out['reasoning'].extend(signal['reasoning'])

    # Order: lowest-priority (broad heuristics) → highest-priority (sean-validated).
    # Vendor is broadest. section_hint is broad. BoY is authoritative on category
    # but generic on primary. existing_products refines based on Sean's catalog.
    # Convention rules (produce_botanical, protein_primal) encode locked-convention
    # truth. subset_canonical inheriting from already-classified Product is gold.
    _absorb(_signal_vendor(vendor))
    _absorb(_signal_section_hint(section_hint))
    _absorb(_signal_boy_yieldreference(raw_tokens,
                                        index=index['boy'] if index else None))
    _absorb(_signal_existing_products(raw_tokens,
                                       index=index['products'] if index else None))
    # High-confidence keyword detectors fire BEFORE produce_botanical /
    # protein_primal so they set category authoritatively. Stops things
    # like 'Lemon Danish' (Lemon→Citrus→Produce) and 'Pepper Jack'
    # (Pepper→Capsicum→Produce) from going to wrong categories.
    #
    # Cheese fires first, then bakery — so 'Cheese Danish' (filling +
    # bakery dish) correctly resolves to Bakery (the head noun pattern:
    # last word wins for compound names). 'Cheddar Cheese' has no
    # bakery keyword so cheese signal stands.
    _absorb(_signal_cheese(raw_lower, raw_tokens))
    _absorb(_signal_bakery_keyword(raw_lower))
    # Convention rules use the CURRENT best-guess category to decide whether to fire
    cat = out['category'][0]
    _absorb(_signal_produce_botanical(raw_tokens, cat))
    _absorb(_signal_protein_primal(raw_lower, cat))
    # subset_canonical is most-authoritative — Sean's validated existing classification.
    # Mark its values as 'locked' so nothing overrides them downstream.
    subset_signal = _signal_subset_canonical(subset_canonical)
    if subset_signal:
        # Promote confidence levels to 'locked' so the absorb gives them top priority
        for field in ('category', 'primary', 'secondary'):
            if field in subset_signal:
                val, _ = subset_signal[field]
                if val is not None:
                    subset_signal[field] = (val, 'locked')
    _absorb(subset_signal)

    return out
