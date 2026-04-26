"""Attach product FK to Sysco placeholder ILI rows whose SUPC is now known
in code_map but never got the FK at write time.

Pattern: when a Sysco invoice line is parsed with a known SUPC but no
description (column-dump OCR), parser emits raw_description='[Sysco #NNN]'
and db_write resolves the SUPC via mapper code_map → attaches FK.
But if the SUPC was added to col G AFTER the original write, the
existing ILI row has product=None and stays placeholder-text. The
mapper resolve_item path runs again only on next reprocess.

This command finds all placeholder ILIs whose SUPC IS now in code_map
but FK is None, and attaches the FK directly. Idempotent — safe to
re-run any time SUPCs are added to col G.

Usage:
    python manage.py attach_placeholder_fks              # dry-run
    python manage.py attach_placeholder_fks --apply
"""
import re
import sys
from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceLineItem, Product


def _import_mapper():
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    import mapper
    return mapper


SUPC_RE = re.compile(r'^\[Sysco #(\d+)\]$')


class Command(BaseCommand):
    help = 'Attach FK to Sysco placeholder ILI rows whose SUPC is now in code_map.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write FK updates. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        mapper = _import_mapper()
        mappings = mapper.load_mappings(force_refresh=True)
        code_map = mappings['code_map']

        product_by_name = {p.canonical_name: p for p in Product.objects.all()}

        candidates = (InvoiceLineItem.objects
                      .filter(raw_description__startswith='[Sysco #', product__isnull=True)
                      .order_by('id'))

        fixed = 0
        skipped_unknown_supc = 0
        skipped_orphan_canonical = 0
        for ili in candidates:
            m = SUPC_RE.match(ili.raw_description)
            if not m:
                continue
            supc = m.group(1)
            canonical = code_map.get(supc)
            if not canonical:
                skipped_unknown_supc += 1
                continue
            product = product_by_name.get(canonical)
            if not product:
                skipped_orphan_canonical += 1
                self.stdout.write(
                    f"  [skip] ILI #{ili.id}: SUPC {supc} → "
                    f"{canonical!r} (orphan canonical, no DB Product)")
                continue

            if apply_changes:
                # Bump confidence to 'code' since SUPC is the match basis
                ili.product = product
                ili.match_confidence = 'code'
                ili.match_score = 100
                ili.save()
            self.stdout.write(
                f"  {'[fix]' if apply_changes else '[would-fix]'} "
                f"ILI #{ili.id}: SUPC {supc} → {canonical!r}")
            fixed += 1

        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write('')
        self.stdout.write(f"=== {mode} report ===")
        self.stdout.write(f"  Fixed (FK attached):                {fixed}")
        self.stdout.write(f"  Skipped — SUPC still unknown:       {skipped_unknown_supc}")
        self.stdout.write(f"  Skipped — orphan canonical:         {skipped_orphan_canonical}")
        if not apply_changes:
            self.stdout.write('  (Dry-run — re-run with --apply to commit.)')
