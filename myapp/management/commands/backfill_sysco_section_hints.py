"""Backfill section_hint on Sysco ILIs that pre-date the spatial_matcher
cross-page section carry fix (commit 6fd6a89, 2026-05-17).

The parser fix prevents future leaks but doesn't touch existing ILI rows.
This command:
  1. Finds Sysco ILIs with section_hint='' that are real items
     (match_confidence in {code, vendor_exact, manual_review, ...})
  2. For each affected invoice, loads the cached OCR pages via
     IVS.cache_hashes and runs the FIXED spatial_matcher
  3. Builds a SUPC → expected_section map from the fresh spatial output
  4. Updates section_hint on existing ILIs by SUPC match
  5. Skips user_edited ILIs (don't override manual corrections)
  6. Per-invoice transaction + IVS.revalidate_from_ili() after update

Dry-run by default; --apply commits.

Usage:
    python manage.py backfill_sysco_section_hints
    python manage.py backfill_sysco_section_hints --apply
    python manage.py backfill_sysco_section_hints --invoice 775632629 --apply
"""
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import (
    InvoiceLineItem, InvoiceValidationStatus,
)


REAL_ITEM_CONFS = {
    'code', 'vendor_exact', 'exact', 'vendor_fuzzy', 'fuzzy',
    'stripped_fuzzy', 'manual_review', 'auto_repoint',
    'subset_match', 'vendor_fuzzy_pending', 'stripped_fuzzy_pending',
    'subset_match_pending', 'unmatched', 'unmatched_drift',
    'unmatched_class_mismatch',
}


class Command(BaseCommand):
    help = ("Backfill section_hint on Sysco ILIs with section_hint='' using "
            "the fixed spatial_matcher cross-page carry. Preserves all other "
            "fields + user_edited rows.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes. Without this flag, dry-run only.')
        parser.add_argument('--invoice', default='',
                            help='Limit to a single invoice_number (defaults to all)')

    def handle(self, *args, apply=False, invoice='', **kw):
        # Bootstrap invoice_processor
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)
        # Force reload — pick up the deployed fix
        for mod in ('spatial_matcher',):
            if mod in sys.modules:
                del sys.modules[mod]
        from spatial_matcher import match_sysco_spatial

        # Find affected ILIs
        orphans_qs = (InvoiceLineItem.objects
                      .filter(vendor__name='Sysco')
                      .filter(match_confidence__in=REAL_ITEM_CONFS)
                      .filter(section_hint='')
                      .exclude(invoice_number='')
                      .exclude(user_edited=True))
        if invoice:
            orphans_qs = orphans_qs.filter(invoice_number=invoice)

        # Group by invoice
        by_invoice: dict = defaultdict(list)
        for ili in orphans_qs:
            by_invoice[ili.invoice_number].append(ili)

        if not by_invoice:
            self.stdout.write("No Sysco ILIs need section_hint backfill.")
            return

        self.stdout.write(f"Found {sum(len(v) for v in by_invoice.values())} "
                          f"orphan ILIs across {len(by_invoice)} invoice(s).")
        self.stdout.write("")

        cache_dir = settings.BASE_DIR / '.ocr_cache'
        total_updated = 0
        total_skipped = 0

        for inv_num, orphan_ilis in sorted(by_invoice.items()):
            # Get IVS for cache_hashes
            try:
                ivs = InvoiceValidationStatus.objects.get(
                    vendor__name='Sysco', invoice_number=inv_num,
                )
            except InvoiceValidationStatus.DoesNotExist:
                self.stdout.write(f"INV {inv_num}: no IVS — skipped ({len(orphan_ilis)} orphans)")
                total_skipped += len(orphan_ilis)
                continue

            hashes = ivs.cache_hashes or []
            if not hashes:
                self.stdout.write(f"INV {inv_num}: no cache_hashes — skipped")
                total_skipped += len(orphan_ilis)
                continue

            # Load all cache pages
            pages = []
            for h in hashes:
                # IVS stores 16-char prefixes; cache files are full-sha named
                prefix = h[:16] if len(h) >= 16 else h
                files = list(cache_dir.glob(f'{prefix}*_docai_ocr.json'))
                if files:
                    try:
                        with open(files[0]) as f:
                            d = json.load(f)
                        pages.extend(d.get('pages', []))
                    except (json.JSONDecodeError, OSError):
                        pass

            if not pages:
                self.stdout.write(f"INV {inv_num}: cache pages not loadable — skipped")
                total_skipped += len(orphan_ilis)
                continue

            # Run FIXED spatial_matcher
            fresh_items = match_sysco_spatial(pages)

            # SUPC → section_hint map from fresh extraction
            supc_to_sec: dict = {}
            for it in fresh_items:
                supc = it.get('sysco_item_code')
                sec = it.get('section_hint') or it.get('section') or ''
                if supc and sec and supc not in supc_to_sec:
                    supc_to_sec[supc] = sec

            # Match orphans to fresh extraction by SUPC
            updates = []
            no_match = []
            for ili in orphan_ilis:
                supc = ili.sysco_item_code if hasattr(ili, 'sysco_item_code') else None
                # Sysco SUPC isn't on the model directly — encoded in match data
                # Fall back: extract from raw_description token (last numeric block)
                if not supc:
                    desc = ili.raw_description or ''
                    import re as _re
                    m = _re.search(r'\b(\d{6,8})\b', desc)
                    supc = m.group(1) if m else None
                new_sec = supc_to_sec.get(supc) if supc else None
                if new_sec:
                    updates.append((ili, new_sec))
                else:
                    no_match.append(ili)

            self.stdout.write(f"INV {inv_num}: {len(orphan_ilis)} orphans → "
                              f"{len(updates)} matched, {len(no_match)} no-match")
            for ili, new_sec in updates[:5]:
                self.stdout.write(f"  ILI #{ili.id}: '' → {new_sec!r}  "
                                  f"(${ili.extended_amount}  {(ili.raw_description or '')[:50]!r})")
            if len(updates) > 5:
                self.stdout.write(f"  ... +{len(updates)-5} more")

            if apply and updates:
                with transaction.atomic():
                    for ili, new_sec in updates:
                        ili.section_hint = new_sec
                        ili.save(update_fields=['section_hint'])
                    # Revalidate IVS to refresh section_reconciliation
                    ivs.revalidate_from_ili()
                self.stdout.write(f"  applied {len(updates)} section_hint updates; "
                                  f"revalidated IVS (status={ivs.status}, "
                                  f"gap_pct={ivs.invoice_gap_pct})")
                total_updated += len(updates)

        self.stdout.write("")
        if apply:
            self.stdout.write(f"Done. {total_updated} ILIs updated. "
                              f"{total_skipped} skipped.")
        else:
            self.stdout.write("DRY-RUN. Re-run with --apply to commit changes.")
