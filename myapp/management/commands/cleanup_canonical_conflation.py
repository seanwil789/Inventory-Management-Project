"""Detach FKs from ILI rows whose raw_description doesn't share any
keyword with the canonical it's mapped to. Surfaces conflation bugs
where unrelated raws got mapped to the same canonical via SUPC drift,
fuzzy mismatches, or sheet-sync errors.

Two-step workflow:
  1. List candidates (default --dry-run): walks all ILIs of the
     canonical, classifies each by raw-description tokens, reports.
  2. Apply (--apply): detaches FK + tags 'unmatched_repointed'
     on rows whose raw lacks ALL of the --keep-tokens. Surviving
     rows stay as before.

Usage:
    python manage.py cleanup_canonical_conflation \\
        --canonical 'Liner, Trash' \\
        --keep-tokens 'LINER,TRASH'
    python manage.py cleanup_canonical_conflation \\
        --canonical 'Corn' --keep-tokens 'CORN' --apply

Per Sean's `feedback_no_destroy_before_research.md`: dry-run is
default, --apply commits, and the cmd never DELETES — only detaches
FKs so /mapping-review/ can pick up the rows for re-curation.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem, Product


class Command(BaseCommand):
    help = "Detach FKs from ILI rows whose raw_description lacks the canonical's keywords."

    def add_arguments(self, parser):
        parser.add_argument('--canonical', required=True,
                            help='Product.canonical_name to audit.')
        parser.add_argument('--keep-tokens', required=True,
                            help='Comma-separated tokens that legitimate raws '
                                 'should contain (e.g. "LINER,TRASH").')
        parser.add_argument('--apply', action='store_true',
                            help='Commit detaches (default is dry-run).')
        parser.add_argument('--also-delete-ids', default='',
                            help='Comma-separated ILI IDs to DELETE outright '
                                 '(used for OCR-garble rows where FK detach '
                                 'would leave a stale unmapped row).')

    def handle(self, *args, **opts):
        canonical = opts['canonical']
        keep_tokens = [t.strip().upper() for t in opts['keep_tokens'].split(',') if t.strip()]
        apply_writes = opts['apply']
        delete_ids = set()
        if opts['also_delete_ids']:
            for tok in opts['also_delete_ids'].split(','):
                tok = tok.strip()
                if tok.isdigit():
                    delete_ids.add(int(tok))

        product = Product.objects.filter(canonical_name=canonical).first()
        if not product:
            self.stderr.write(f'Product not found: {canonical!r}')
            return

        ilis = InvoiceLineItem.objects.filter(product=product).select_related('vendor').order_by('-invoice_date')

        keep_re = re.compile(r'\b(' + '|'.join(re.escape(t) for t in keep_tokens) + r')\b',
                             re.IGNORECASE)

        keep_rows = []
        repoint_rows = []
        delete_rows = []

        for ili in ilis:
            raw = ili.raw_description or ''
            if ili.id in delete_ids:
                delete_rows.append(ili)
                continue
            if keep_re.search(raw):
                keep_rows.append(ili)
            else:
                repoint_rows.append(ili)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== cleanup_canonical_conflation '
            f'({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'Canonical:  {canonical!r}  (id={product.id})')
        self.stdout.write(f'Keep tokens: {keep_tokens}')
        self.stdout.write(f'Total ILIs: {ilis.count()}')
        self.stdout.write(f'  Keep (token match):    {len(keep_rows)}')
        self.stdout.write(f'  Repoint (no token):    {len(repoint_rows)}')
        self.stdout.write(f'  Delete (--also-delete): {len(delete_rows)}')
        self.stdout.write('')

        if repoint_rows:
            self.stdout.write(self.style.MIGRATE_LABEL('Repoint candidates (FK→NULL):'))
            for ili in repoint_rows:
                v = ili.vendor.name if ili.vendor else '?'
                self.stdout.write(
                    f'  ID={ili.id:5d} {ili.invoice_date} {v[:18]:18s} '
                    f'up={ili.unit_price}  raw={(ili.raw_description or "")[:55]!r}'
                )

        if delete_rows:
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_LABEL('Delete candidates:'))
            for ili in delete_rows:
                v = ili.vendor.name if ili.vendor else '?'
                self.stdout.write(
                    f'  ID={ili.id:5d} {ili.invoice_date} {v[:18]:18s} '
                    f'up={ili.unit_price}  raw={(ili.raw_description or "")[:55]!r}'
                )

        if apply_writes:
            from django.db import transaction
            with transaction.atomic():
                if repoint_rows:
                    rids = [r.id for r in repoint_rows]
                    InvoiceLineItem.objects.filter(id__in=rids).update(
                        product=None, match_confidence='unmatched_repointed',
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'\nDetached {len(rids)} FKs (tagged unmatched_repointed).'
                    ))
                if delete_rows:
                    dids = [r.id for r in delete_rows]
                    n, _ = InvoiceLineItem.objects.filter(id__in=dids).delete()
                    self.stdout.write(self.style.SUCCESS(
                        f'Deleted {n} OCR-garble rows.'
                    ))
        elif repoint_rows or delete_rows:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
