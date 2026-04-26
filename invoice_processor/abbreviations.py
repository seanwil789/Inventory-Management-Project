"""Sysco-flavored vendor abbreviation expansion.

Used by both `mapper.py` (during fuzzy matching) and `myapp/taxonomy.py`
(during inference for new Product creation). Expanding abbreviations to
full English words BEFORE token matching dramatically improves recall —
'BRST CHKN BNLS' now matches 'Chicken Breast' canonical, not just stays
unmapped.

Origin of entries:
  - feedback_abbreviations.md (Sean's confirmed list)
  - Direct observation from unmapped queue sampling
  - Standard Sysco / food-service abbreviations

This is a living dictionary — add entries as new patterns surface in
the Mapping Review queue.

NOT abbreviations (these are brand prefixes — handled separately by
mapper._strip_sysco_prefix):
  WHLFCLS, WHLFIMP, BBRLCLS, BBRLIMP, AREZIMP, AREZRSVS, CITVIMP,
  IMPFRSH, IMP/MCC, KAPSZUB, etc.
"""
import re

SYSCO_ABBREVIATIONS = {
    # Body parts / cuts (drives protein secondary descriptor)
    'BRST':    'Breast',
    'BNLS':    'Boneless',
    'B/L':     'Boneless',
    'SKLS':    'Skinless',
    'BNINSKON': 'Bone-In Skin-On',
    'BLSL':    'Boneless Skinless',     # alt form seen in PORTCLS TILAPIA FILET BLSL
    'THGH':    'Thigh',
    'WNG':     'Wing',
    'DRMS':    'Drumsticks',
    'TNDR':    'Tender',
    'TNDRLN':  'Tenderloin',
    'STRP':    'Strip',
    'LOIN':    'Loin',
    'SHK':     'Shank',
    'SHLDR':   'Shoulder',
    'BRSKT':   'Brisket',
    'BUTT':    'Butt',
    'HAL':     'Halves',
    'HLF':     'Half',
    'QTR':     'Quarter',
    'WHL':     'Whole',
    # Animal types
    'CHKN':    'Chicken',
    'CHIK':    'Chicken',
    'TRKY':    'Turkey',
    'BF':      'Beef',
    'PRK':     'Pork',
    'BCN':     'Bacon',
    # Processing / preparation state
    'CKD':     'Cooked',
    'RAW':     'Raw',
    'FZN':     'Frozen',
    'FRS':     'Fresh',
    'IQF':     'Frozen',                # Individually Quick Frozen
    'GRD':     'Ground',
    'GRND':    'Ground',
    'BRD':     'Breaded',
    'BRDED':   'Breaded',
    'GRL':     'Grilled',
    'GRLD':    'Grilled',
    'GRL MRK': 'Grill Marked',
    'SMK':     'Smoked',
    'SMKD':    'Smoked',
    'CURED':   'Cured',
    'STMD':    'Steamed',
    'CVP':     'Cryovac Packed',
    'NAE':     'No Antibiotics',
    'SLI':     'Sliced',
    'SLCD':    'Sliced',
    'SHRD':    'Shredded',
    'SHRDD':   'Shredded',
    'CHOPPED': 'Chopped',
    'DICED':   'Diced',
    'CUBED':   'Cubed',
    'PULLED':  'Pulled',
    'CHOPPD':  'Chopped',
    'CRMBL':   'Crumbled',
    'CRMBLS':  'Crumbles',
    'GRTD':    'Grated',
    # Container / packaging — material descriptors only
    'PLAS':    'Plastic',
    'PLA':     'Plastic',
    'PPR':     'Paper',
    'POUC':    'Pouch',
    # NOTE: pure unit/qty abbreviations (LB, OZ, GAL, CT, DZ, BG, BX, CS,
    # PKG, PCS, BTL, CAN, JR, BCH, etc.) are intentionally NOT expanded —
    # they appear in MOST product descriptions and add noise tokens that
    # dilute fuzzy match scores against product canonicals (e.g. expanding
    # 'BG 150 LB PACKER SUGAR GRANULATED' → 'Bag 150 Pound PACKER...'
    # makes 'Sugar, Granulated' lose its tier-6b match). Units stay as-is
    # for both mapper + taxonomy.
    # Sizes / qualifiers
    'SM':      'Small',
    'MED':     'Medium',
    'LRG':     'Large',
    'JBO':     'Jumbo',
    'JMBO':    'Jumbo',
    'XL':      'Extra Large',
    'XXL':     'Extra Extra Large',
    'AVG':     'Average',
    'PRM':     'Premium',
    'PRT':     'Portion',
    'WGT':     'Weight',
    # Categories / common food types
    'VEG':     'Vegetable',
    'VEGGIE':  'Vegetable',
    'CHOC':    'Chocolate',
    'VAN':     'Vanilla',
    'STRAW':   'Strawberry',
    'BLU':     'Blueberry',
    'TOM':     'Tomato',
    'SPIC':    'Spicy',
    'TRT':     'Tart',
    'SWT':     'Sweet',
    'MILK':    'Milk',
    'MK':      'Milk',
    'WHL M':   'Whole Milk',
    'CRM':     'Cream',
    'BTR':     'Butter',
    'YGRT':    'Yogurt',
    'CHS':     'Cheese',
    'EGG':     'Egg',
    # Misc
    'ESL':     'Extended Shelf Life',
    'ORIG':    'Original',
    'ASST':    'Assorted',
    'REG':     'Regular',
    'FRTR':    'Fritter',
    'EGGRL':   'Egg Roll',
    'EGGROLL': 'Egg Roll',
    'NTRL':    'Natural',
    'PKD':     'Packed',
    # Specific commonly-seen Sysco product words
    'TILAPIA': 'Tilapia',                 # already a word, but reinforce
    'SALMON':  'Salmon',
    'FILET':   'Filet',
    'FIL':     'Filet',                   # SALMON ATL FIL
}


# Sort keys by length descending so longer multi-word abbrevs match first
# ('GRL MRK' before 'GRL', 'WHL M' before 'WHL').
_ABBREV_PATTERN = None


def _build_pattern():
    """Build a single regex that matches any abbreviation as a whole word.
    Lazy-built so module import is fast."""
    global _ABBREV_PATTERN
    if _ABBREV_PATTERN is None:
        keys = sorted(SYSCO_ABBREVIATIONS.keys(), key=lambda k: -len(k))
        # Word boundaries for plain alpha; allow trailing slash for B/L etc.
        # Escape special chars in keys.
        escaped = [re.escape(k) for k in keys]
        _ABBREV_PATTERN = re.compile(
            r'(?<![A-Za-z])(?:' + '|'.join(escaped) + r')(?![A-Za-z])',
            re.IGNORECASE,
        )
    return _ABBREV_PATTERN


def expand_abbreviations(text: str) -> str:
    """Replace known vendor abbreviations with their full forms.

    Word-boundary matching avoids partial-word collisions (e.g., 'BR' inside
    'BRSKT' won't expand). Case-insensitive. Returns the expanded string.

    Example:
      'WHLFCLS BRST CHKN BNLS CKD NAE 5LB' →
      'WHLFCLS Breast Chicken Boneless Cooked No Antibiotics 5Pound'
      (brand prefix WHLFCLS is left for mapper._strip_sysco_prefix to handle)
    """
    if not text:
        return text
    pat = _build_pattern()

    def _replace(m):
        # Look up the matched text case-insensitively
        matched = m.group(0).upper()
        return SYSCO_ABBREVIATIONS.get(matched, m.group(0))

    return pat.sub(_replace, text)
