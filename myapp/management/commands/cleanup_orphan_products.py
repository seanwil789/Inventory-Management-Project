"""Identify Product rows with no ILI history, no RecipeIngredient links,
no ProductMapping rows, and no ProductMappingProposal entries.

These "pure orphans" are typically aspirational products added during
catalog planning that no invoices or recipes ever pointed to. Safe to
delete (FK-attached writes will recreate them) but Sean reviews first
because some look like real kitchen items (Tabasco, Molasses, Sumac).

Usage:
    python manage.py cleanup_orphan_products                # list only
    python manage.py cleanup_orphan_products --delete-ids 489,487 --apply
    python manage.py cleanup_orphan_products --by-category Bakery

Re-checks orphan status of every ID at delete time so a concurrent
write doesn't get clobbered.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand

from myapp.models import (
    Product, InvoiceLineItem, RecipeIngredient,
    ProductMapping, ProductMappingProposal,
)


def _is_pure_orphan(p: Product) -> bool:
    """True when Product has no ILI / RI / PM / PMP references."""
    if InvoiceLineItem.objects.filter(product=p).exists():
        return False
    if RecipeIngredient.objects.filter(product=p).exists():
        return False
    if ProductMapping.objects.filter(product=p).exists():
        return False
    if ProductMappingProposal.objects.filter(suggested_product=p).exists():
        return False
    return True


class Command(BaseCommand):
    help = 'List pure orphan products + delete by ID list.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit deletes (requires --delete-ids).')
        parser.add_argument('--delete-ids', default='',
                            help='Comma-separated Product IDs to delete '
                                 '(re-verified as orphans before delete).')
        parser.add_argument('--by-category', default='',
                            help='Filter listing to one category.')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        delete_ids_raw = opts['delete_ids']
        cat_filter = opts['by_category']

        if apply_writes and not delete_ids_raw:
            self.stderr.write(self.style.ERROR(
                '--apply requires --delete-ids "X,Y,Z"'
            ))
            return

        delete_ids = set()
        if delete_ids_raw:
            for tok in delete_ids_raw.split(','):
                tok = tok.strip()
                if tok.isdigit():
                    delete_ids.add(int(tok))

        # Find pure orphans
        orphans = []
        qs = Product.objects.all().order_by('category', 'canonical_name')
        if cat_filter:
            qs = qs.filter(category=cat_filter)
        for p in qs:
            if _is_pure_orphan(p):
                orphans.append(p)

        # Listing
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== cleanup_orphan_products '
            f'({"APPLY" if apply_writes else "LIST"}) ===\n'
        ))
        self.stdout.write(f'Pure orphans: {len(orphans)}'
                          + (f' (category={cat_filter})' if cat_filter else ''))
        self.stdout.write('')

        by_cat: dict[str, list[Product]] = defaultdict(list)
        for p in orphans:
            by_cat[p.category or '(blank)'].append(p)

        for cat in sorted(by_cat):
            self.stdout.write(self.style.MIGRATE_LABEL(f'{cat}:'))
            for p in by_cat[cat]:
                marker = '★ DELETE' if p.id in delete_ids else '       '
                self.stdout.write(
                    f'  {marker}  ID={p.id:4d}  pd={p.primary_descriptor[:18]:18s}'
                    f'  {p.canonical_name}'
                )

        # Apply phase
        if apply_writes:
            self.stdout.write('')
            deleted = 0
            for pid in sorted(delete_ids):
                p = Product.objects.filter(id=pid).first()
                if p is None:
                    self.stdout.write(self.style.WARNING(
                        f'  ID={pid}: not found, skipping'))
                    continue
                # RE-VERIFY orphan status — concurrent writes may have
                # attached references since the listing pass.
                if not _is_pure_orphan(p):
                    self.stdout.write(self.style.WARNING(
                        f'  ID={pid} {p.canonical_name!r}: NO LONGER ORPHAN '
                        f'(FK attached since list); skipping'))
                    continue
                self.stdout.write(f'  Deleted: ID={pid} {p.canonical_name!r}')
                p.delete()
                deleted += 1
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {deleted} of {len(delete_ids)} requested IDs.'
            ))
        elif delete_ids:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'Dry-run — {len(delete_ids)} IDs marked. Re-run with --apply to commit.'
            ))
