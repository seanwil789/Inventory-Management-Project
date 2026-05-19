"""Backfill ILIs whose raw_description matches a known fee pattern but
whose match_confidence is 'unmatched' rather than 'non_product'.

Origin: 2026-05-19 mapping-review cleanup surfaced 8 Delaware County Linen
rows (7 Delivery Charge + 1 PA Sales Tax) that landed at 'unmatched'
because their raw_descriptions didn't match the mapper's pre-2026-05-19
_NON_PRODUCT_PATTERNS. The 'DELIVERY CHARGE' pattern (alongside existing
DELIVERY CHG / DELIVERY FEE) was added the same day. This command brings
historical rows into line with the post-patch design so they stop
surfacing in discover_unmapped and pollute the mapping-review queue.

Idempotent — re-running after the patch is a no-op.

Usage:
    python manage.py backfill_fee_non_product            # dry-run
    python manage.py backfill_fee_non_product --apply
"""
import sys
from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceLineItem


def _import_mapper():
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    import mapper
    return mapper


class Command(BaseCommand):
    help = ("Re-classify 'unmatched' ILIs that should be 'non_product' "
            "per the current mapper non-product patterns.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        mapper = _import_mapper()

        candidates = InvoiceLineItem.objects.filter(
            match_confidence='unmatched',
        ).only('id', 'raw_description', 'invoice_number', 'vendor')

        to_update = []
        for ili in candidates.iterator():
            if mapper._is_non_product(ili.raw_description):
                to_update.append(ili)

        self.stdout.write(
            f"'unmatched' ILIs scanned: {candidates.count()}")
        self.stdout.write(
            f"  matching current non-product patterns: {len(to_update)}")

        if not to_update:
            self.stdout.write("Nothing to backfill — already aligned.")
            return

        self.stdout.write("")
        self.stdout.write("Sample (first 20):")
        for ili in to_update[:20]:
            v = ili.vendor.name if ili.vendor else '-'
            self.stdout.write(
                f"  id={ili.id} v={v} inv={ili.invoice_number} "
                f"| {ili.raw_description}")

        if apply_changes:
            ids = [i.id for i in to_update]
            n = InvoiceLineItem.objects.filter(id__in=ids).update(
                match_confidence='non_product')
            self.stdout.write("")
            self.stdout.write(f"Updated {n} rows: match_confidence "
                              "'unmatched' -> 'non_product'.")
        else:
            self.stdout.write("")
            self.stdout.write("(Dry-run — re-run with --apply to commit.)")
