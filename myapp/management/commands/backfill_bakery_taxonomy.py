"""Migrate existing Bakery products from the old free-form primary
descriptors (Pastry, Loaf Bread, Rolls) to the locked 5-bucket
convention (Pastries, Bread/Fermented, Quick Breads, Cakes & Sponges,
Cookies & Bars), with consistent secondary descriptors.

Idempotent: re-runs are safe because each entry overwrites with the
target values. Skips IDs that don't exist (already deleted).

Usage:
    python manage.py backfill_bakery_taxonomy          # dry-run
    python manage.py backfill_bakery_taxonomy --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import Product


# (product_id, new_primary, new_secondary)
UPDATES = [
    # Pastry → Pastries
    (569, 'Pastries',         'Croissant'),
    ( 61, 'Pastries',         'Danish'),
    (  4, 'Pastries',         'Donut'),
    # Mini Muffins was misfiled — muffins are Quick Breads, not Pastries
    (  5, 'Quick Breads',     'Muffin'),
    # Loaf Bread → Bread/Fermented + Sandwich Loaf
    (487, 'Bread/Fermented',  'Sandwich Loaf'),
    (485, 'Bread/Fermented',  'Sandwich Loaf'),
    (197, 'Bread/Fermented',  'Sandwich Loaf'),
    (486, 'Bread/Fermented',  'Sandwich Loaf'),
    (430, 'Bread/Fermented',  'Sandwich Loaf'),
    # Rolls → Bread/Fermented + specific secondary
    (415, 'Bread/Fermented',  'Bagel'),
    (489, 'Bread/Fermented',  'Bagel'),
    (  7, 'Bread/Fermented',  'Bagel'),
    ( 11, 'Bread/Fermented',  'Brioche Bun'),
    (565, 'Bread/Fermented',  'Brioche Bun'),
    ( 10, 'Bread/Fermented',  'Hamburger Bun'),
    ( 89, 'Bread/Fermented',  'Hoagie Roll'),
    (488, 'Bread/Fermented',  'Hoagie Roll'),
    (207, 'Bread/Fermented',  'Hot Dog Bun'),
    (384, 'Bread/Fermented',  'Dinner Roll'),
    (555, 'Bread/Fermented',  'Kaiser Roll'),
    (317, 'Bread/Fermented',  'Breadstick'),
    ( 37, 'Bread/Fermented',  'Pita'),
    (241, 'Bread/Fermented',  'Tortilla'),
    (239, 'Bread/Fermented',  'Tortilla'),
    (102, 'Bread/Fermented',  'Wrap'),
    # Quick Breads — fill in missing secondary
    (572, 'Quick Breads',     'Muffin'),
    (571, 'Quick Breads',     'Muffin'),
]


class Command(BaseCommand):
    help = 'Backfill Bakery products to the locked 5-bucket primary convention.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'=== {mode} mode ===\n')

        updated = unchanged = missing = 0
        with transaction.atomic():
            for pid, new_pri, new_sec in UPDATES:
                p = Product.objects.filter(id=pid).first()
                if p is None:
                    self.stdout.write(f'  ⊘ id={pid} not found (skipped)')
                    missing += 1
                    continue
                if (p.primary_descriptor == new_pri and
                        p.secondary_descriptor == new_sec):
                    unchanged += 1
                    continue
                old_pri, old_sec = p.primary_descriptor, p.secondary_descriptor
                if apply_changes:
                    p.primary_descriptor = new_pri
                    p.secondary_descriptor = new_sec
                    p.save()
                self.stdout.write(
                    f'  ✓ id={pid:>4} {p.canonical_name!r:<32}  '
                    f'{old_pri!r}/{old_sec!r} → {new_pri!r}/{new_sec!r}'
                )
                updated += 1

        self.stdout.write('')
        self.stdout.write(f'  Updated:    {updated}')
        self.stdout.write(f'  Unchanged:  {unchanged}')
        self.stdout.write(f'  Missing ID: {missing}')
        if not apply_changes:
            self.stdout.write('\n(Dry-run — re-run with --apply to commit.)')
