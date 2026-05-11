"""Backfill ILI.section_hint to clean up corrupt section labels stored
pre-B-CorruptSection-fix (commits 94d1813 + 0dc3d01).

Some extractor paths historically emitted non-canonical section labels
("CANNED & DRY GROUP", "HAZARD", "DISPENSER BEVERAGE", "TOTAL", etc.)
because spatial_matcher's `_find_sections` is more permissive than
canonicalize_sysco_section. Those labels reached db_write and got
stored on ILI.section_hint, polluting downstream section reconciliation.

The forward-looking fix at db_write (commit 94d1813) prevents NEW pollution
but doesn't touch existing rows. This cmd cleans the historical DB state.

Strategy:
  - Walk all ILI rows with non-empty section_hint
  - For each: run section_hint through canonicalize_sysco_section
  - If result IS in _CANONICAL_SYSCO_SECTIONS → set to canonical
  - Otherwise → set to empty (falls through to orphan handling)
  - Don't touch already-canonical rows

Usage:
  python manage.py backfill_section_hint                # dry-run
  python manage.py backfill_section_hint --apply        # commit
  python manage.py backfill_section_hint --vendor Sysco --apply
"""
import sys
import os
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = ('Normalize ILI.section_hint values through canonicalize_sysco_section. '
            'Drop non-canonical / junk labels to empty.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', help='Filter to a single vendor name')
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Without this flag, dry-run only.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N candidates (for testing)')

    def handle(self, *args, **opts):
        # Bootstrap invoice_processor path
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)
        from spatial_matcher import (canonicalize_sysco_section,
                                      _CANONICAL_SYSCO_SECTIONS)

        def normalize(label):
            if not label:
                return ''
            canon = canonicalize_sysco_section(label)
            if canon in _CANONICAL_SYSCO_SECTIONS:
                return canon
            upper = label.upper()
            if 'GROUP TOTAL' in upper or upper.startswith('TOTAL'):
                return ''
            return ''

        vendor_name = opts.get('vendor')
        apply_writes = opts.get('apply')
        limit = opts.get('limit')

        qs = InvoiceLineItem.objects.exclude(section_hint='')
        if vendor_name:
            qs = qs.filter(vendor__name=vendor_name)

        self.stdout.write(
            f'Scanning {qs.count()} ILI rows with non-empty section_hint...')

        canonical_unchanged = 0
        normalized_to_canonical = 0
        cleared_to_empty = 0
        already_canonical_str = 0
        change_pairs: Counter = Counter()
        candidates: list = []  # (ili, new_value)

        for ili in qs.iterator():
            current = ili.section_hint or ''
            new_val = normalize(current)
            if current == new_val:
                if current in _CANONICAL_SYSCO_SECTIONS:
                    already_canonical_str += 1
                else:
                    canonical_unchanged += 1
                continue
            # Change required
            if new_val == '':
                cleared_to_empty += 1
            else:
                normalized_to_canonical += 1
            change_pairs[(current, new_val)] += 1
            candidates.append((ili, new_val))
            if limit and len(candidates) >= limit:
                break

        self.stdout.write('')
        self.stdout.write(f'Already canonical (no change):     {already_canonical_str}')
        self.stdout.write(f'Empty/unchanged (no canonical):    {canonical_unchanged}')
        self.stdout.write(f'Will normalize to canonical:       {normalized_to_canonical}')
        self.stdout.write(f'Will clear to empty (junk):        {cleared_to_empty}')
        self.stdout.write(f'Total changes:                      {len(candidates)}')

        if change_pairs:
            self.stdout.write('')
            self.stdout.write('Top changes (label → new_value [count]):')
            for (old, new), n in change_pairs.most_common(20):
                self.stdout.write(
                    f"  {old[:40]!r:42s} → {new[:25]!r:27s} [{n}]")

        if not apply_writes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to commit.'))
            return

        self.stdout.write('')
        self.stdout.write(f'Applying {len(candidates)} updates...')
        applied = 0
        for ili, new_val in candidates:
            ili.section_hint = new_val
            ili.save(update_fields=['section_hint'])
            applied += 1

        self.stdout.write(self.style.SUCCESS(f'Done. Updated {applied} rows.'))
