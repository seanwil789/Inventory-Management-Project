"""One-shot: merge ILIs that share (vendor, invoice_number, vendor_item_code).

Reason: 2026-05-17 corpus reprocess created duplicate ILIs because Phase
4f's primary dedup key originally included `invoice_date`, but multi-
photo Sysco invoices have OCR date drift across caches. Result: same
SUPC on same invoice with different parsed dates → no Phase 4f match
→ new row created. The Phase 4f date constraint was removed afterward
(it's still scoped by invoice_number which is the stable id), but
existing duplicates remain in the DB and must be merged.

Strategy:
  Group ILIs by (vendor, invoice_number, vendor_item_code), only
  considering rows where vendor_item_code is populated. Within each
  group with >1 row:
    - Keep the LOWEST id (oldest, most likely to have human edits or
      stable history).
    - Delete the rest. user_edited rows skipped from deletion (Trust
      LAW); if a user_edited row exists in the group, keep IT instead.
"""
from __future__ import annotations
from collections import defaultdict

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = 'Merge duplicate ILIs sharing (vendor, invoice_number, vendor_item_code).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Limit to one vendor')
        parser.add_argument('--mode', choices=('supc', 'price-ext'), default='supc',
                            help='supc: group by (invoice, supc). '
                                 'price-ext: group by (invoice, unit_price, ext) — '
                                 'catches cross-SUPC duplicates where bad backfill '
                                 'assigned wrong SUPC.')

    def handle(self, *args, **opts):
        if not opts['dry_run'] and not opts['apply']:
            self.stdout.write('Pass --dry-run or --apply')
            return

        mode = opts['mode']
        qs = InvoiceLineItem.objects.exclude(invoice_number='').select_related('vendor')
        if mode == 'supc':
            qs = qs.exclude(vendor_item_code='')
        if opts['vendor']:
            qs = qs.filter(vendor__name__iexact=opts['vendor'])

        groups: dict[tuple, list] = defaultdict(list)
        for ili in qs:
            if mode == 'supc':
                key = (ili.vendor_id, ili.invoice_number, ili.vendor_item_code)
            else:  # price-ext
                key = (ili.vendor_id, ili.invoice_number,
                       float(ili.unit_price or 0),
                       float(ili.extended_amount or 0))
            groups[key].append(ili)

        to_delete_ids: list[int] = []
        keep_ids: list[int] = []
        per_invoice: dict[str, int] = {}
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            user_edited = [r for r in rows if r.user_edited]
            if user_edited:
                # Survivor: lowest-id user_edited
                survivor = sorted(user_edited, key=lambda r: r.id)[0]
            elif mode == 'supc':
                # Survivor: lowest id (oldest)
                survivor = sorted(rows, key=lambda r: r.id)[0]
            else:  # price-ext: survivor is the NEWEST (latest reprocess output;
                   # bad-backfill OLDs likely have older imported_at)
                survivor = sorted(rows, key=lambda r: r.imported_at or 0, reverse=True)[0]
            keep_ids.append(survivor.id)
            for r in rows:
                if r.id != survivor.id and not r.user_edited:
                    to_delete_ids.append(r.id)
                    inv = r.invoice_number
                    per_invoice[inv] = per_invoice.get(inv, 0) + 1

        self.stdout.write(f"Mode: {mode}")
        self.stdout.write(f"Groups with >1 row: {sum(1 for v in groups.values() if len(v) > 1)}")
        self.stdout.write(f"Rows to delete: {len(to_delete_ids)}")
        self.stdout.write(f"Survivors kept: {len(keep_ids)}")
        if per_invoice:
            self.stdout.write("Top affected invoices:")
            for inv, n in sorted(per_invoice.items(), key=lambda x: -x[1])[:15]:
                self.stdout.write(f"  {inv}: {n} deletes")

        if opts['apply'] and to_delete_ids:
            InvoiceLineItem.objects.filter(id__in=to_delete_ids).delete()
            self.stdout.write(f"DELETED {len(to_delete_ids)} duplicate rows.")
        elif opts['dry_run']:
            self.stdout.write("(dry-run, no deletes)")
