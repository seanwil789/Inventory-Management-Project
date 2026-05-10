"""Backfill `invoice_number` on existing InvoiceLineItem rows.

Phase 4c (Sean 2026-05-10): the new dedup primary key is `(vendor,
canonical_FK, invoice_number, invoice_date)`. Existing ILI rows have
`invoice_number=''` because the field was just added (migration 0072).
Without backfill, the new primary key never finds existing rows on
re-photo/reprocess — defeating the purpose of the schema change.

Backfill logic:
  Pass 1 (cache-hash match): for each IVS row, walk its `cache_hashes`
  list. For each hash, find ILIs whose source_file starts with that
  16-char prefix (or equals it). Set their invoice_number to the IVS's.
  Most reliable; works for ILIs from cache-keyed ingest paths.

  Pass 2 (vendor + date unique match): for ILIs still empty, find IVS
  rows matching their (vendor, invoice_date). When exactly ONE IVS row
  matches, use its invoice_number. Skip when 0 or N>1 (ambiguous).

  Pass 3 (fallback report): list ILIs that remain empty after both
  passes — these have no IVS coverage (PBM/Delaware/Exceptional partial,
  Colonial entirely). They'll continue using the legacy source_file
  dedup path until invoice_number extraction lands for those vendors.

Read-only by default. --apply commits writes inside a transaction.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from myapp.models import InvoiceLineItem, InvoiceValidationStatus


class Command(BaseCommand):
    help = 'Backfill invoice_number on ILI rows from InvoiceValidationStatus.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Default is dry-run.')
        parser.add_argument('--vendor', default=None,
                            help='Restrict to one vendor name (icontains).')

    def handle(self, *args, **opts):
        apply = opts['apply']
        vendor_filter = opts['vendor']
        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(self.style.HTTP_INFO(
            f'=== invoice_number backfill [{mode}] ==='))

        ili_qs = InvoiceLineItem.objects.filter(invoice_number='')
        if vendor_filter:
            ili_qs = ili_qs.filter(vendor__name__icontains=vendor_filter)
        total_empty = ili_qs.count()
        self.stdout.write(f'ILIs needing backfill: {total_empty}')

        ivs_qs = InvoiceValidationStatus.objects.exclude(invoice_number='')
        if vendor_filter:
            ivs_qs = ivs_qs.filter(vendor__name__icontains=vendor_filter)
        self.stdout.write(f'IVS rows available as source: {ivs_qs.count()}')

        # Pass 1: cache-hash match
        self.stdout.write('\n--- Pass 1: cache_hashes match ---')
        hash_to_invnum: dict[tuple, str] = {}  # (vendor_id, hash16) -> invoice_number
        for ivs in ivs_qs:
            for h in (ivs.cache_hashes or []):
                if not h:
                    continue
                key = (ivs.vendor_id, h[:16])
                # If two IVS rows share a hash (shouldn't happen), prefer
                # the one matching the ILI's date — handled per-ILI below.
                hash_to_invnum.setdefault(key, ivs.invoice_number)

        pass1_updates: list[tuple[int, str]] = []  # (ili_id, invoice_number)
        ambiguous_pass1 = 0
        for ili in ili_qs.iterator():
            if not ili.source_file or not ili.vendor_id:
                continue
            # Try first 16 chars of source_file as cache hash prefix
            sf = ili.source_file
            # source_file can be 'HASH', 'HASH+1', 'HASH+2', or 'filename.jpg'
            candidate = sf.split('+', 1)[0][:16]
            if not all(c in '0123456789abcdef' for c in candidate.lower()):
                continue  # not a hash — skip Pass 1 for this row
            key = (ili.vendor_id, candidate)
            inv = hash_to_invnum.get(key)
            if inv:
                pass1_updates.append((ili.id, inv))

        self.stdout.write(f'  Pass 1 candidates: {len(pass1_updates)}')

        # Apply Pass 1 if --apply
        if apply and pass1_updates:
            updated_p1 = 0
            with transaction.atomic():
                for ili_id, inv in pass1_updates:
                    InvoiceLineItem.objects.filter(id=ili_id).update(
                        invoice_number=inv)
                    updated_p1 += 1
            self.stdout.write(self.style.SUCCESS(
                f'  Pass 1 applied: {updated_p1} ILIs'))
        elif pass1_updates:
            self.stdout.write(
                f'  (would update {len(pass1_updates)} ILIs in --apply mode)')

        # Pass 2: vendor+date unique match
        self.stdout.write('\n--- Pass 2: (vendor, invoice_date) unique match ---')
        # Build index: (vendor_id, invoice_date) -> list of (invoice_number)
        date_index: dict[tuple, list[str]] = defaultdict(list)
        for ivs in ivs_qs:
            if ivs.invoice_date:
                date_index[(ivs.vendor_id, ivs.invoice_date)].append(
                    ivs.invoice_number)

        # Re-query empty ILIs (Pass 1 may have just filled some)
        empty_after_p1 = InvoiceLineItem.objects.filter(invoice_number='')
        if vendor_filter:
            empty_after_p1 = empty_after_p1.filter(
                vendor__name__icontains=vendor_filter)

        pass2_updates: list[tuple[int, str]] = []
        ambiguous_p2 = 0
        no_match_p2 = 0
        for ili in empty_after_p1.iterator():
            if not ili.invoice_date or not ili.vendor_id:
                no_match_p2 += 1
                continue
            inv_nums = date_index.get((ili.vendor_id, ili.invoice_date), [])
            if len(inv_nums) == 1:
                pass2_updates.append((ili.id, inv_nums[0]))
            elif len(inv_nums) > 1:
                ambiguous_p2 += 1
            else:
                no_match_p2 += 1

        self.stdout.write(f'  Pass 2 candidates: {len(pass2_updates)}')
        self.stdout.write(f'  Ambiguous (multi-IVS same date): {ambiguous_p2}')
        self.stdout.write(f'  No IVS coverage: {no_match_p2}')

        if apply and pass2_updates:
            updated_p2 = 0
            with transaction.atomic():
                for ili_id, inv in pass2_updates:
                    InvoiceLineItem.objects.filter(id=ili_id).update(
                        invoice_number=inv)
                    updated_p2 += 1
            self.stdout.write(self.style.SUCCESS(
                f'  Pass 2 applied: {updated_p2} ILIs'))
        elif pass2_updates:
            self.stdout.write(
                f'  (would update {len(pass2_updates)} ILIs in --apply mode)')

        # Pass 3: report remaining empties by vendor
        self.stdout.write('\n--- Pass 3: remaining empties by vendor ---')
        remaining = InvoiceLineItem.objects.filter(invoice_number='')
        if vendor_filter:
            remaining = remaining.filter(vendor__name__icontains=vendor_filter)
        # Always show what would remain (using projected post-apply count)
        if not apply:
            # Subtract pass1+pass2 candidates from the "remaining" list
            pass1_ids = {p[0] for p in pass1_updates}
            pass2_ids = {p[0] for p in pass2_updates}
            remaining = remaining.exclude(id__in=pass1_ids | pass2_ids)
        by_vendor = remaining.values('vendor__name').annotate(c=Count('id')).order_by('-c')
        for r in by_vendor:
            self.stdout.write(
                f'  {r["vendor__name"] or "(null)":30s}  {r["c"]:5d} rows still empty')

        total = len(pass1_updates) + len(pass2_updates)
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            f'=== Total backfill {"applied" if apply else "candidates"}: {total} '
            f'/ {total_empty} ILI rows ==='))
        if not apply:
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to commit writes.'))
