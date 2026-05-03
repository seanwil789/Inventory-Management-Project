"""One-shot migration: import legacy negative_matches.json triples into
ProductMappingProposal as status='rejected' rows.

Sean 2026-05-02: when the sheet's Mapping Review tab was retired, the
code that read invoice_processor/mappings/negative_matches.json was
deleted. The file still exists with ~124 historical rejection triples
(vendor, raw_description, suggested_canonical) — Sean's accumulated
"this mapping is wrong" decisions over months.

This migration preserves that teaching signal by translating each
triple into a rejected PMP that the new audit + populate cmds respect
(both filter by status='rejected' for same-target dedup).

Wildcard entries (raw containing '*') are skipped with a warning —
they need manual review since they don't map cleanly to a single
PMP row.

Idempotent: get_or_create checks for existing PMPs before creating.

Usage:
    python manage.py migrate_negative_matches
    python manage.py migrate_negative_matches --apply
    python manage.py migrate_negative_matches --apply --reason wrong_canonical
"""
from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import (Vendor, Product, ProductMappingProposal)


class Command(BaseCommand):
    help = "Migrate legacy negative_matches.json triples into rejected PMP rows."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit writes (default is dry-run).')
        parser.add_argument('--path', default='',
                            help='Path to negative_matches.json (default: '
                                 'invoice_processor/mappings/negative_matches.json).')
        parser.add_argument('--reason', default='wrong_canonical',
                            help='reject_reason to stamp (default wrong_canonical).')

    def handle(self, *args, **opts):
        path = opts['path'] or os.path.join(
            settings.BASE_DIR, 'invoice_processor', 'mappings',
            'negative_matches.json',
        )
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f'No file at {path}.'))
            return

        with open(path) as f:
            triples = json.load(f)

        apply_writes = opts['apply']
        reason = opts['reason']
        valid_reasons = {k for k, _ in ProductMappingProposal.REJECT_REASON_CHOICES}
        if reason not in valid_reasons:
            self.stderr.write(self.style.ERROR(
                f'Invalid reason {reason!r}. Valid: {sorted(valid_reasons)}'
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== migrate_negative_matches ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'Source: {path}')
        self.stdout.write(f'Triples in file: {len(triples)}')
        self.stdout.write(f'Stamping reject_reason={reason!r}')
        self.stdout.write('')

        created = updated = wildcards = no_vendor = no_canonical = already_rejected = 0
        for triple in triples:
            if not isinstance(triple, list) or len(triple) != 3:
                continue
            vendor_name, raw_desc, suggested_canonical = triple
            if '*' in (raw_desc or ''):
                wildcards += 1
                continue

            vendor = Vendor.objects.filter(name=vendor_name).first()
            if vendor is None:
                no_vendor += 1
                self.stdout.write(self.style.WARNING(
                    f'  unknown vendor {vendor_name!r}, skipped: {raw_desc!r}'))
                continue

            target = Product.objects.filter(canonical_name=suggested_canonical).first()
            if target is None:
                no_canonical += 1
                self.stdout.write(self.style.WARNING(
                    f'  no canonical {suggested_canonical!r} '
                    f'(possibly renamed/merged), skipped: {raw_desc!r}'))
                continue

            # Use get_or_create for idempotency — find existing PMP if any
            existing = ProductMappingProposal.objects.filter(
                vendor=vendor,
                raw_description=raw_desc,
                source='discover_unmapped',
                suggested_product=target,
            ).first()

            if existing is not None:
                if existing.status == 'rejected':
                    already_rejected += 1
                    continue
                if apply_writes:
                    existing.reject(reason=reason,
                                    notes='Migrated from negative_matches.json')
                updated += 1
            else:
                if apply_writes:
                    ProductMappingProposal.objects.create(
                        vendor=vendor,
                        raw_description=raw_desc,
                        source='discover_unmapped',
                        suggested_product=target,
                        status='rejected',
                        reject_reason=reason,
                        notes='Migrated from negative_matches.json',
                    )
                created += 1

        self.stdout.write('')
        self.stdout.write(f'New rejected PMPs:           {created}')
        self.stdout.write(f'Existing PMPs marked rejected: {updated}')
        self.stdout.write(f'Already rejected (idempotent): {already_rejected}')
        self.stdout.write(f'Skipped — wildcards:           {wildcards}')
        self.stdout.write(f'Skipped — unknown vendor:      {no_vendor}')
        self.stdout.write(f'Skipped — canonical missing:   {no_canonical}')
        if not apply_writes and (created or updated):
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to commit.'
            ))
