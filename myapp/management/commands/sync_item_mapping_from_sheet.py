"""Sync the Item Mapping sheet → DB ProductMapping table.

Step 1 of the sheet→DB migration roadmap (`project_sheet_to_db_migration.md`).
After this command runs, ProductMapping is a full mirror of the Item Mapping
sheet's active rows (those with non-empty col F canonical). Mapper.py
continues reading from sheet for now; Step 2 swaps it to read from DB.

Idempotent on (vendor, description) — re-running is safe, updates rows
that changed and adds new ones. Conservative on errors: rows with unknown
vendors or orphan canonicals are SKIPPED with a per-row log entry rather
than silently dropped or auto-created.

Usage:
    python manage.py sync_item_mapping_from_sheet              # dry-run (default)
    python manage.py sync_item_mapping_from_sheet --apply      # write to DB
    python manage.py sync_item_mapping_from_sheet --apply -v 2 # verbose per-row
"""
import sys
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import Vendor, Product, ProductMapping


def _import_sheet_helpers():
    """Add invoice_processor/ to sys.path so we can import sheets + config."""
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    from sheets import get_sheet_values
    from config import SPREADSHEET_ID, MAPPING_TAB
    return get_sheet_values, SPREADSHEET_ID, MAPPING_TAB


class Command(BaseCommand):
    help = 'Sync Item Mapping sheet rows → ProductMapping DB table (Step 1 of sheet→DB migration).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes to the DB. Without this flag, runs dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        verbosity = opts.get('verbosity', 1)

        get_sheet_values, SPREADSHEET_ID, MAPPING_TAB = _import_sheet_helpers()

        self.stdout.write(f"Reading {MAPPING_TAB!r} sheet...")
        rows = get_sheet_values(SPREADSHEET_ID, f"{MAPPING_TAB}!A:G")
        self.stdout.write(f"  {len(rows)} rows total (incl. header).")

        # Cache vendor + product lookups to avoid per-row queries
        vendor_by_name = {v.name: v for v in Vendor.objects.all()}
        product_by_name = {p.canonical_name: p for p in Product.objects.all()}

        created = updated = unchanged = 0
        skipped_empty = 0
        skipped_unknown_vendor = 0
        skipped_orphan_canonical = 0
        unknown_vendor_names = set()
        orphan_canonical_names = set()
        changed_pairs = []   # (vendor, desc, old_product_name, new_product_name)

        for row in rows[1:]:   # skip header
            while len(row) < 7:
                row.append('')
            vendor_name = row[0].strip()
            description = row[1].strip()
            # cols 2-4 (category/primary/secondary) live on Product, not PM
            canonical = row[5].strip()
            supc = row[6].strip()

            if not canonical:
                skipped_empty += 1
                continue

            if not description:
                # PM unique_together is (vendor, description); empty desc is meaningless
                skipped_empty += 1
                continue

            # Vendor lookup — must already exist (don't auto-create)
            vendor = None
            if vendor_name:
                vendor = vendor_by_name.get(vendor_name)
                if vendor is None:
                    skipped_unknown_vendor += 1
                    unknown_vendor_names.add(vendor_name)
                    if verbosity >= 2:
                        self.stdout.write(f"  [skip] unknown vendor {vendor_name!r}: {description[:50]!r}")
                    continue

            # Product lookup — must already exist (don't auto-create ghosts)
            product = product_by_name.get(canonical)
            if product is None:
                skipped_orphan_canonical += 1
                orphan_canonical_names.add(canonical)
                if verbosity >= 2:
                    self.stdout.write(f"  [skip] orphan canonical {canonical!r}: {description[:50]!r}")
                continue

            # Upsert on (vendor, description) — the PM unique_together
            existing = ProductMapping.objects.filter(
                vendor=vendor, description=description
            ).first()

            if existing is None:
                if apply_changes:
                    ProductMapping.objects.create(
                        vendor=vendor,
                        description=description,
                        supc=supc,
                        product=product,
                    )
                created += 1
                if verbosity >= 2:
                    self.stdout.write(f"  [create] {vendor_name} | {description[:40]!r} → {canonical!r}")
            else:
                # Check whether anything changed
                needs_update = (
                    existing.product_id != product.id
                    or (existing.supc or '') != supc
                )
                if needs_update:
                    old_canon = existing.product.canonical_name if existing.product else None
                    if existing.product_id != product.id:
                        changed_pairs.append((vendor_name, description, old_canon, canonical))
                    if apply_changes:
                        existing.product = product
                        existing.supc = supc
                        existing.save()
                    updated += 1
                    if verbosity >= 2:
                        self.stdout.write(f"  [update] {vendor_name} | {description[:40]!r} "
                                          f"product {old_canon!r} → {canonical!r}")
                else:
                    unchanged += 1

        # Report
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write('')
        self.stdout.write(f"=== {mode} report ===")
        self.stdout.write(f"  Created:                            {created}")
        self.stdout.write(f"  Updated (FK or SUPC changed):       {updated}")
        self.stdout.write(f"  Unchanged (already in sync):        {unchanged}")
        self.stdout.write(f"  Skipped — empty canonical/desc:     {skipped_empty}")
        self.stdout.write(f"  Skipped — unknown vendor in DB:     {skipped_unknown_vendor}")
        self.stdout.write(f"  Skipped — orphan canonical in DB:   {skipped_orphan_canonical}")
        self.stdout.write('')
        if unknown_vendor_names:
            self.stdout.write(f"  Unknown vendors ({len(unknown_vendor_names)}): "
                              f"{sorted(unknown_vendor_names)}")
        if orphan_canonical_names:
            self.stdout.write(f"  Orphan canonicals ({len(orphan_canonical_names)}):")
            for n in sorted(orphan_canonical_names):
                self.stdout.write(f"    {n!r}")
        if changed_pairs:
            self.stdout.write('')
            self.stdout.write(f"  FK changes (existing PM had different product):")
            for v, d, old, new in changed_pairs[:20]:
                self.stdout.write(f"    [{v}] {d[:40]!r}: {old!r} → {new!r}")
            if len(changed_pairs) > 20:
                self.stdout.write(f"    ... ({len(changed_pairs) - 20} more)")

        # Final state
        if apply_changes:
            self.stdout.write('')
            self.stdout.write(f"  Total ProductMapping rows now: {ProductMapping.objects.count()}")
        else:
            self.stdout.write('')
            self.stdout.write(f"  (Dry-run — no DB writes. Re-run with --apply to commit.)")
