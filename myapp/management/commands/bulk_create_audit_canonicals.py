"""One-shot bulk creation of canonical Products + approval of pending
proposals identified during the 2026-04-25/26 mapping-review queue audit.

The audit surfaced ~86 cold-start proposals (no auto-suggestion). After
deduplication against existing taxonomy, ~33 new canonicals + ~10
existing-canonical reuses absorb ~76 of the cold-start raws. Edge cases
(PBM parser glitches, unknown-vendor rows, ambiguous OCR) stay pending
for manual review or parser fixes.

Idempotent: checks Product existence by canonical_name before creating;
skips proposals already approved/rejected.

Usage:
    python manage.py bulk_create_audit_canonicals          # dry-run
    python manage.py bulk_create_audit_canonicals --apply
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import Product, ProductMappingProposal


# ---- Phase 1: NEW canonicals to create ----
# (canonical_name, category, primary_descriptor, secondary_descriptor)
NEW_CANONICALS = [
    # Proteins — anatomical primal cut as primary
    ('Beef, Flat Iron',           'Proteins', 'Chuck',           'Fresh'),
    ('Beef, Top Sirloin Butt',    'Proteins', 'Sirloin',         'Fresh'),
    ('Beef, Tri Tip',             'Proteins', 'Sirloin',         'Fresh'),
    ('Beef Frank',                'Proteins', 'Shoulder',        'Processed'),
    ('Beef Strip Steak',          'Proteins', 'Loin',            'Fresh'),
    ('Gyro Meat, Beef',           'Proteins', 'Shoulder',        'Processed'),
    ('Pork Chop',                 'Proteins', 'Loin',            'Fresh'),
    ('Pork Shoulder, Picnic',     'Proteins', 'Shoulder',        'Fresh'),
    ('Crab Meat, Claw',           'Proteins', 'Shellfish',       'Processed'),

    # Bakery — locked 5-bucket convention
    ('Croissant, Butter',         'Bakery',   'Pastries',        'Croissant'),
    ('Corn Muffin',               'Bakery',   'Quick Breads',    'Muffin'),
    ('Potato Hamburger Bun',      'Bakery',   'Bread/Fermented', 'Hamburger Bun'),

    # Cheese — milk source as primary
    ('American Cheese, Sliced',   'Cheese',   'Cow',             'Processed'),

    # Produce — botanical groupings
    ('Apples, Fuji',              'Produce',  'Pome',            ''),
    ('Mushroom, Shiitake',        'Produce',  'Fungus',          ''),
    ('Potato, Red A-Size',        'Produce',  'Tuber',           'Potato'),
    ('Rhubarb',                   'Produce',  'Polygonaceae',    ''),

    # Drystock
    ('Parsley, Dried',            'Drystock', 'Spices',          ''),
    ('Garam Masala',              'Drystock', 'Spices',          'Ground'),
    ('Pepper, Ancho, Dried',      'Drystock', 'Spices',          ''),
    ("Pepper, D'Arbol, Dried",    'Drystock', 'Spices',          ''),
    ('Apricot, Dried',            'Drystock', 'Baking',          ''),
    ('Pasta, Bowtie',             'Drystock', 'Pastas',          ''),
    ('Macaroni Salad',            'Drystock', 'PreFabs',         ''),

    # Coffee/Concessions
    ("Cap'n Crunch",              'Coffee/Concessions', 'Cereals',                   ''),
    ('Cocoa Mix, Dutch',          'Coffee/Concessions', 'Coffee Dispenser Station',  ''),
    ('Coffee, House Blend, Ground','Coffee/Concessions','Coffee Dispenser Station',  ''),
    ('Protein Drink, Chocolate',  'Coffee/Concessions', 'Beverages',                 ''),

    # Dairy
    ('Whipped Topping',           'Dairy',    'Cream',           ''),

    # Paper/Disposable
    ('Bags, Plastic Slide, Gallon','Paper/Disposable','Plastic Facility', ''),
    ('Lid, Plastic',              'Paper/Disposable', 'Plastic Facility', ''),
    ('Container, Paper #3',       'Paper/Disposable', 'Paper',            ''),
    ('Pan Liner, Quilon',         'Paper/Disposable', 'Paper',            ''),
    # Metal utensils — temporarily under 'Plastic Facility' until convention
    # backfill renames the primary or introduces 'Smallwares'.
    ('Forks, Metal',              'Paper/Disposable', 'Plastic Facility', ''),
    ('Spoons, Metal',             'Paper/Disposable', 'Plastic Facility', ''),
]


# ---- Phase 2: raw_description → canonical_name approval mapping ----
# Each entry = (proposal raw_description, target canonical_name).
# Target may be a NEW canonical (above) or an existing one (looked up
# by canonical_name; if not found, the entry is skipped with a warning).
APPROVALS = [
    # Beef
    ('Beef Flat Iron Ch Steak 4oz',                                            'Beef, Flat Iron'),
    ('Beef Top Butt Boneless Choice',                                          'Beef, Top Sirloin Butt'),
    ('Beef Tri Tip Fat On Choice',                                             'Beef, Tri Tip'),
    ('Franks Beef 6/1 Berks',                                                  'Beef Frank'),
    ('BBRLCLS FRANK ALL-MEAT 8×1 6',                                           'Beef Frank'),
    ('E 25 LB BBRLCLS FRANK ALL - MEAT 8 × 1 6 00074865067959',                'Beef Frank'),
    ('248 OZ BHB / NPM STEAK STRIP VEIN FRZN 21-30808',                        'Beef Strip Steak'),
    ('F 248 OZ BHB / NPM STEAK STRIP VEIN FRZN T / WT = 21-30808',             'Beef Strip Steak'),
    ('CS 110 LB KONTOS GYRO BEEF SLICES THICK IQF 21065',                      'Gyro Meat, Beef'),

    # Pork
    ('Pork Chop',                                                              'Pork Chop'),
    ('C 42 CT SWIFT PORK SHOULDER PICNIC 405 T / WT B = / I 30340',            'Pork Shoulder, Picnic'),
    ('PORK SHOULDER PICNIC 405 B/I',                                           'Pork Shoulder, Picnic'),

    # Seafood
    ('Crab Meat',                                                              'Crab Meat, Claw'),
    ('O 61 LB PHILFDS CRAB MEAT CLAW PSTRZD ASIA 15402',                       'Crab Meat, Claw'),
    ('PHILFDS CRAB MEAT CLAW PSTRZD ASIA',                                     'Crab Meat, Claw'),

    # Poultry
    ('Wings',                                                                  'Chicken Wings'),
    ('-3.5 SYS CLS CHICKEN CVP WHL WOG FZ',                                    'Chicken, Whole Fryer'),
    ('1 CS 143-3.5 SYS CLS CHICKEN CVP WHL WOG FZ',                            'Chicken, Whole Fryer'),

    # Plant-based
    ('MORNSTR PATTY VEG BLACK BEAN SPIC 2898997765',                           'Black Bean Burger'),
    ('F 1 cs 723 OZ MINH EGGROLL VEGETABLE 69039',                             'Veggie Egg Roll'),

    # Bakery
    ('Butter Croissant',                                                       'Croissant, Butter'),
    ('Corn Muffins',                                                           'Corn Muffin'),
    ('Potato Hamburger/8 PK/Sliced/3 1/2"',                                    'Potato Hamburger Bun'),
    ('F 126 CT BKRSCLS BUN HOAGIE WHITE HNGD 6IN 31873',                       'Hoagie Rolls'),
    ('21 18 egHoag... Regular Hoagie Wrapped',                                 'Hoagie Rolls'),
    ('BKRSCLS BUN HOT DOG WHITE 6 HINGD',                                      'Hot Dog Rolls'),
    ('3 LB. Whole Wheat Sandwich Sliced/Sorry this',                           'Whole Wheat Bread'),

    # Cheese / Dairy
    ('C 1scs 45 LB BBRLCLS CHEESE AMER 160 DELI SLI WH STK03334',              'American Cheese, Sliced'),
    ('C 16810Z AREZIMP CHEESE MOZZ STRING',                                    'Mozzarella, String'),
    ('115 DZ WHLFCLS EGG SHELL MED GR AA USDA WHT 2112SW',                     'Eggs'),

    # Produce
    ('APPLES, FUJI, XF, 100/88 CT. 38 LBS.',                                   'Apples, Fuji'),
    ('C 188 CT SYS IMP APPLE RED DEL X - FANCY FRESH',                         'Apples, Red Delicious'),
    ('MELONS, HONEYDEWS, JUMBO 5CT. *NO HALF CASES',                           'Honey Dew'),
    ('MUSHROOMS, SHIITAKE, #1, 3 LB',                                          'Mushroom, Shiitake'),
    ('150 LB IMPFRSH POTATO RED A SZ CRTN',                                    'Potato, Red A-Size'),
    ('IMPFRSH POTATO RED A SZ CRTN',                                           'Potato, Red A-Size'),
    ('RHUBARB, 20LB CS OREGON STATE',                                          'Rhubarb'),

    # Spices / Drystock
    ('Black Pepper',                                                           'Black Pepper, Ground, Fine'),
    ('ONLY5LB SYS CLS SPICE PEPPER BLK GRND 974516',                           'Black Pepper, Ground, Fine'),
    ('Parsley, Dried',                                                         'Parsley, Dried'),
    ('ONLY2OZ IMP / MCC SPICE PARSLEY FLAKE 974321',                           'Parsley, Dried'),
    ('Garam Masala',                                                           'Garam Masala'),
    ("OUT 114 OZ D'ALLAS SPICE GARAM MASALA",                                  'Garam Masala'),
    ('ONLY180Z MCCLNRY SPICE SESAME SEED BLK 935705',                          'Sesame Seeds, Black'),
    ('ONLY320Z SYS CLS EXTRACT VANILLA IMIT 974398',                           'Vanilla, Imitations'),
    ('DRIED, PEPPERS, ANCHOES, 5 LB box',                                      'Pepper, Ancho, Dried'),
    ("DRIED, PEPPERS, D'ARBOL, 5 LB box",                                      "Pepper, D'Arbol, Dried"),
    ('DRIED, APRICOT, 3 LB BAG',                                               'Apricot, Dried'),
    ('25 LB LABELLA PASTA BOWTIE 1s8141Q0660',                                 'Pasta, Bowtie'),
    ("C LBS DON'S SALAD MACARONI 10998",                                       'Macaroni Salad'),

    # Coffee/Concessions
    ('QUAKER CEREAL CAP N CRUNCH',                                             "Cap'n Crunch"),
    ('122 CITVCLS COCOA MIX DISPENSER DUTCH 29612',                            'Cocoa Mix, Dutch'),
    ('961.5 OZCITVCLS COFFEE GRND HSE BLEND MED W / F 29596',                  'Coffee, House Blend, Ground'),
    ('9620Z CITVCLS COFFEE GRND HSE BLEND MED W / F 29594',                    'Coffee, House Blend, Ground'),
    ('OUT 1211 OZ PREM PR DRINK PROTEIN CHOC 71420',                           'Protein Drink, Chocolate'),

    # Dairy
    ('1214 OZ SYS REL TOPPING WHPD ARSL',                                      'Whipped Topping'),
    ('SYS REL TOPPING WHPD ARSL',                                              'Whipped Topping'),
    ('F 1216 OZ SYS REL TOPPING WHPD IN BAG 52960',                            'Whipped Topping'),

    # Pantry / PreFabs
    ('CS 64.5 LESYS REL POTATO FRY 1/4 SS SYR00965',                           'Fries, Frozen'),

    # Paper / Disposable
    ('1 CS 1250CT SYS CLS BAG PLAS RECLOSE SLIDE GAL 304985473',               'Bags, Plastic Slide, Gallon'),
    ('10100CT SYS CLS LID PLAS WHT TEAR BACK DFL124TBWSYS',                    'Lid, Plastic'),
    ('265CT ERTHPLS CONTAINER PAPER # 3 TK O 192738969SYS',                    'Container, Paper #3'),
    ('ERTHPLS CONTAINER PAPER #3 TK O 192738969SYS',                           'Container, Paper #3'),
    ('CS 11000CTSYS CLS LINER PAN QUILLION TRTD 16 × 24 019785',               'Pan Liner, Quilon'),
    ('3084 CT TORKUNV TOWEL ROLL KTCHN 9 × 11 HB1990A',                        'Towel, Paper'),
    ('3084 CT TORKUNV TOWEL ROLL KTCHN 9X11 HB1990A',                          'Towel, Paper'),
    ('SYS REL LINER REPRO 38x58 1.5ML BLK KPPL4500',                           'Trash Liner'),
    ('SYS REL LINER REPRO 38×56 1.2ML X7656SKSROV',                            'Trash Liner'),
    ('136CT SUPPCLS SPOON TEA WINDSOR MEDWEIGHT 651-001s',                     'Plastic Spoons'),
    ('SUPPCLS SPOON TEA WINDSOR MEDWEIGHT 651-001s',                           'Plastic Spoons'),
    ('Forks, Metal',                                                           'Forks, Metal'),
    ('Spoons, Metal',                                                          'Spoons, Metal'),

    # Chemicals
    ('KEYSTON CLEANER DEGRSR GREASELIFT RT',                                   'Degreaser'),
    ('KEYSTON CLEANER DEGRSR GREASELIFT RT 6100285',                           'Degreaser'),
]


class Command(BaseCommand):
    help = 'Bulk-create audit canonicals + approve matching pending proposals.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'=== {mode} mode ===\n')

        with transaction.atomic():
            # Phase 1: ensure all NEW_CANONICALS exist
            self.stdout.write('--- Phase 1: Product creation ---')
            created = skipped = 0
            for canonical, cat, pri, sec in NEW_CANONICALS:
                if Product.objects.filter(canonical_name=canonical).exists():
                    self.stdout.write(f'  ⊙ skip (exists): {canonical}')
                    skipped += 1
                    continue
                if apply_changes:
                    Product.objects.create(
                        canonical_name=canonical, category=cat,
                        primary_descriptor=pri, secondary_descriptor=sec,
                    )
                self.stdout.write(f'  ✓ create: {canonical}  ({cat}/{pri}/{sec})')
                created += 1
            self.stdout.write(f'  → {created} created, {skipped} already existed\n')

            # Phase 2: approve matching proposals
            self.stdout.write('--- Phase 2: Proposal approvals ---')
            approved = canon_missing = prop_missing = already = 0
            ili_total = 0
            tier_counts = Counter()
            for raw_desc, target_canonical in APPROVALS:
                product = Product.objects.filter(canonical_name=target_canonical).first()
                if product is None:
                    self.stdout.write(f'  ✗ canonical not found: {target_canonical!r} '
                                      f'(for raw {raw_desc!r})')
                    canon_missing += 1
                    continue
                proposals = ProductMappingProposal.objects.filter(
                    raw_description=raw_desc, status='pending')
                if not proposals.exists():
                    # Check whether it was already approved
                    handled = ProductMappingProposal.objects.filter(
                        raw_description=raw_desc).exclude(status='pending').first()
                    if handled:
                        already += 1
                    else:
                        prop_missing += 1
                        self.stdout.write(f'  ⊘ no pending proposal: {raw_desc[:60]}')
                    continue
                for prop in proposals:
                    if apply_changes:
                        result = prop.approve(product=product, reviewer=None,
                                              notes='Bulk audit cleanup 2026-04-26')
                        ili_total += result['ili_updated']
                    approved += 1
                    tier_counts[prop.confidence_tier or '(none)'] += 1
            self.stdout.write(f'  → {approved} approved, '
                              f'{ili_total} ILI rows backfilled')
            self.stdout.write(f'  → {already} already approved/rejected, '
                              f'{prop_missing} no matching pending proposal, '
                              f'{canon_missing} target canonical missing')

            if not apply_changes:
                self.stdout.write('\n(Dry-run — re-run with --apply to commit.)')
                # Roll back the implicit transaction by raising — atomic
                # block aborts cleanly without writing anything. Actually
                # since we never called .create() or .approve() in dry-run
                # path, nothing was written; just exit cleanly.
                return

        # Final stats
        self.stdout.write('\n--- Post-apply stats ---')
        self.stdout.write(f'  Total Products:                {Product.objects.count()}')
        self.stdout.write(f'  Pending proposals:             '
                          f'{ProductMappingProposal.objects.filter(status="pending").count()}')
        self.stdout.write(f'  Approved proposals:            '
                          f'{ProductMappingProposal.objects.filter(status="approved").count()}')
