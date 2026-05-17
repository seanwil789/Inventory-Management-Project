"""Cleanup mgmt command: zero out residual OUT-inline-qty phantom rows
in existing ILI table that pre-date the rank_pair Pattern C-2 extension
fix (commit 20a987c, 2026-05-17).

The parser fix prevents FUTURE extraction of `OUT <digit/#>...` rows
but doesn't touch existing ILI rows in production. This command:
  1. Finds Sysco ILIs whose raw_description matches the leak pattern
  2. For each, writes an InvoiceLineEdit audit row (reason=manual_correction,
     note explaining the cleanup) — preserves trust-bar measurement integrity
  3. Zeros qty + ext, sets user_edited=True
  4. Calls IVS.revalidate_from_ili() on each affected invoice so items_sum
     + section reconciliation + status reflect the post-cleanup state
  5. Reports per-invoice impact

Per-row dry-run output before --apply. Wrapped in transaction-per-invoice
so a mid-run failure doesn't leave partial state.

Usage:
    python manage.py cleanup_out_inline_rows           # dry-run
    python manage.py cleanup_out_inline_rows --apply
"""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import (
    InvoiceLineEdit, InvoiceLineItem, InvoiceValidationStatus,
)


# Same regex as rank_pair._NON_ITEM_DESC_PATTERNS[2] (the Pattern C-2 extension).
# Defined locally rather than imported to make the cleanup's intent explicit
# and decoupled from future filter additions.
OUT_INLINE_PATTERN = re.compile(r'^\s*OUT\s+[\d#]', re.IGNORECASE)

CLEANUP_NOTE = (
    'Pattern C-2 extension cleanup — phantom OUT-inline-qty row removed '
    'by audit. Parser now filters this pattern (commit 20a987c, 2026-05-17). '
    'Original ILI was extracted before the filter landed.'
)


class Command(BaseCommand):
    help = ("Zero out residual OUT-inline-qty phantom ILI rows for Sysco "
            "invoices. Preserves audit trail via InvoiceLineEdit. Dry-run "
            "by default; pass --apply to commit.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes. Without this flag, dry-run only.')

    def handle(self, *args, apply=False, **kw):
        # Find candidates — skip rows already cleaned (qty=0 OR ext=0 OR
        # already user_edited). Idempotent: re-running the command is a no-op
        # on rows already handled by a prior run or by Sean's manual UI audit.
        candidates = []
        skipped_already_clean = 0
        for ili in (InvoiceLineItem.objects
                    .filter(vendor__name='Sysco')
                    .exclude(raw_description='')
                    .select_related('vendor')):
            if not OUT_INLINE_PATTERN.match(ili.raw_description or ''):
                continue
            already_zero = (ili.extended_amount == 0 or ili.quantity == 0)
            already_edited = ili.user_edited
            if already_zero or already_edited:
                skipped_already_clean += 1
                continue
            candidates.append(ili)

        if skipped_already_clean:
            self.stdout.write(f"Skipped {skipped_already_clean} OUT-inline row(s) "
                              f"already cleaned (qty/ext=0 or user_edited=True).")
            self.stdout.write("")

        if not candidates:
            self.stdout.write("No OUT-inline phantom rows to clean up.")
            return

        self.stdout.write(f"Found {len(candidates)} OUT-inline phantom row(s):")
        self.stdout.write("")

        # Group by invoice for transactional update + IVS revalidation
        from collections import defaultdict
        by_invoice: dict = defaultdict(list)
        for ili in candidates:
            by_invoice[(ili.vendor_id, ili.invoice_number)].append(ili)

        for (vendor_id, invoice_number), rows in by_invoice.items():
            v_name = rows[0].vendor.name
            total_ext = sum((Decimal(str(r.extended_amount)) if r.extended_amount else Decimal('0'))
                            for r in rows)
            self.stdout.write(f"  {v_name} INV {invoice_number} — {len(rows)} row(s), ${total_ext} to be zeroed:")
            for ili in rows:
                self.stdout.write(
                    f"    ILI #{ili.id} qty={ili.quantity} unit=${ili.unit_price} "
                    f"ext=${ili.extended_amount} match={ili.match_confidence}"
                )
                self.stdout.write(f"        desc: {ili.raw_description!r}")

        self.stdout.write("")

        if not apply:
            self.stdout.write("DRY-RUN. Re-run with --apply to commit changes.")
            return

        # Apply changes per invoice in a transaction
        self.stdout.write("APPLYING...")
        self.stdout.write("")

        # Pick the system user for edited_by — fallback to None if none configured
        system_user = (User.objects.filter(username='sean').first()
                       or User.objects.filter(is_superuser=True).first())

        total_edits = 0
        ivs_revalidated = 0

        for (vendor_id, invoice_number), rows in by_invoice.items():
            with transaction.atomic():
                for ili in rows:
                    before = {
                        'quantity': str(ili.quantity) if ili.quantity is not None else None,
                        'unit_price': str(ili.unit_price) if ili.unit_price is not None else None,
                        'extended_amount': str(ili.extended_amount) if ili.extended_amount is not None else None,
                        'case_size': ili.case_size or '',
                        'raw_description': ili.raw_description or '',
                    }
                    # Zero out the ILI
                    ili.quantity = Decimal('0')
                    ili.extended_amount = Decimal('0')
                    ili.user_edited = True
                    # Preserve raw_description + unit_price + case_size so the
                    # row still carries diagnostic info; only quantity + ext
                    # change to reflect "not delivered."
                    ili.save(update_fields=['quantity', 'extended_amount', 'user_edited'])

                    after = {
                        'quantity': '0',
                        'unit_price': str(ili.unit_price) if ili.unit_price is not None else None,
                        'extended_amount': '0',
                        'case_size': ili.case_size or '',
                        'raw_description': ili.raw_description or '',
                    }
                    InvoiceLineEdit.objects.create(
                        ili=ili,
                        edited_by=system_user,
                        before=before,
                        after=after,
                        reason='manual_correction',
                        note=CLEANUP_NOTE,
                    )
                    total_edits += 1

                # Revalidate the IVS so items_sum/section_reconciliation/status
                # reflect post-cleanup state immediately
                try:
                    ivs = InvoiceValidationStatus.objects.get(
                        vendor_id=vendor_id, invoice_number=invoice_number,
                    )
                    pre_sum = ivs.items_sum
                    pre_status = ivs.status
                    ivs.revalidate_from_ili()
                    ivs.refresh_from_db()
                    self.stdout.write(
                        f"  INV {invoice_number}: items_sum ${pre_sum} -> ${ivs.items_sum} "
                        f"({pre_status} -> {ivs.status}, gap_pct={ivs.invoice_gap_pct})"
                    )
                    ivs_revalidated += 1
                except InvoiceValidationStatus.DoesNotExist:
                    self.stdout.write(
                        f"  INV {invoice_number}: no IVS row, skipped revalidation"
                    )

        self.stdout.write("")
        self.stdout.write(f"Done. {total_edits} InvoiceLineEdit row(s) created. "
                          f"{ivs_revalidated} IVS row(s) revalidated.")
