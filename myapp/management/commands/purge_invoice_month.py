"""
Management command to delete InvoiceLineItem records for a given month
that were imported via the old path (source_file is blank, unit_price is NULL).

Safe to run before re-processing invoices through batch.py — removes the
priceless placeholder rows so re-processing creates clean records.

Usage:
  python manage.py purge_invoice_month 2026 3            # dry run (default)
  python manage.py purge_invoice_month 2026 3 --confirm  # actually delete
"""
from django.core.management.base import BaseCommand
from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = "Delete priceless/sourceless InvoiceLineItem records for a given month"

    def add_arguments(self, parser):
        parser.add_argument("year",  type=int, help="Four-digit year (e.g. 2026)")
        parser.add_argument("month", type=int, help="Month number 1–12")
        parser.add_argument(
            "--confirm", action="store_true",
            help="Actually delete — omit this flag for a dry run"
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Include records that have a source_file (normally excluded for safety)"
        )

    def handle(self, *args, **options):
        year    = options["year"]
        month   = options["month"]
        confirm = options["confirm"]
        all_    = options["all"]

        qs = InvoiceLineItem.objects.filter(
            invoice_date__year=year,
            invoice_date__month=month,
        )

        if not all_:
            qs = qs.filter(source_file="", unit_price__isnull=True)

        count = qs.count()

        if count == 0:
            self.stdout.write(self.style.WARNING(
                f"No records found for {year}-{month:02d}"
                + ("" if all_ else " with blank source_file and NULL unit_price")
            ))
            return

        self.stdout.write(
            f"Found {count} record(s) for {year}-{month:02d}"
            + ("" if all_ else " with blank source_file / NULL unit_price")
        )

        if not confirm:
            self.stdout.write(self.style.WARNING(
                "Dry run — add --confirm to delete"
            ))
            return

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} record(s) for {year}-{month:02d}"
        ))
