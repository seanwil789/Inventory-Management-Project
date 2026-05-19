"""Retag 'unmatched' ILIs whose raw_description matches a known parser/OCR
junk pattern to 'unmatched_garbled'.

The skip patterns in populate_mapping_review_from_unmapped._is_skippable
catch parser/OCR noise (.P. O. Number, "204 QTY PACK SIZE", "Printed:"
footers, "T", "3.00 EACH CL2", etc.) and exclude them from PMP creation.
But the underlying ILI's match_confidence stays 'unmatched', so they
appear forever in the "actionable unmapped" pool — same Trust LAW
divergence we hit with the fee pathway (mapper doesn't tag → discover
filters → ILI never updates).

This command brings ILIs in line: anything _is_skippable catches gets
tagged 'unmatched_garbled', which exits the unmatched pool and surfaces
in the audit_parser_garbles audit instead.

Idempotent — re-running after a drift-free corpus is a no-op.

Usage:
    python manage.py backfill_garbled_unmatched              # dry-run
    python manage.py backfill_garbled_unmatched --apply
"""
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem
from myapp.management.commands.populate_mapping_review_from_unmapped import _is_skippable


class Command(BaseCommand):
    help = ("Retag 'unmatched' ILIs whose raw_description matches a junk "
            "pattern to 'unmatched_garbled' so they exit the actionable pool.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']

        candidates = (InvoiceLineItem.objects
                      .filter(product__isnull=True,
                              match_confidence='unmatched')
                      .only('id', 'raw_description', 'invoice_number', 'vendor'))

        to_retag = []
        for ili in candidates.iterator():
            if _is_skippable(ili.raw_description):
                to_retag.append(ili)

        self.stdout.write(
            f"'unmatched' ILIs scanned: {candidates.count()}")
        self.stdout.write(
            f"  matching junk patterns: {len(to_retag)}")

        if not to_retag:
            self.stdout.write("Nothing to backfill — already aligned.")
            return

        self.stdout.write("")
        self.stdout.write("Sample (first 20):")
        for ili in to_retag[:20]:
            v = ili.vendor.name if ili.vendor else '-'
            self.stdout.write(
                f"  id={ili.id} v={v} inv={ili.invoice_number} "
                f"| {ili.raw_description[:60]}")

        if apply_changes:
            ids = [i.id for i in to_retag]
            n = InvoiceLineItem.objects.filter(id__in=ids).update(
                match_confidence='unmatched_garbled')
            self.stdout.write("")
            self.stdout.write(
                f"Updated {n} rows: match_confidence 'unmatched' -> 'unmatched_garbled'.")
        else:
            self.stdout.write("")
            self.stdout.write("(Dry-run — re-run with --apply to commit.)")
