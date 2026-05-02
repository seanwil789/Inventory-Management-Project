"""Record that a canonical-drift audit proposal should NOT be applied.

Stores `(ProductMapping, rejected_canonical_name)` in CanonicalDriftRejection.
The next audit run reads this table and excludes the pair from proposals,
so Sean never sees the same wrong proposal twice.

First pass = reject = teaches the system. Permanent — the unique constraint
prevents duplicate rejections, so subsequent runs are idempotent.

Usage:
    python manage.py reject_canonical_drift --pairs '112:Almonds,113:Walnuts'
    python manage.py reject_canonical_drift --pairs '...' --note 'Trail Mix is the parent canonical'

Pairs format mirrors repoint_product_mappings: comma-separated `pm_id:canonical_name`,
splitting on `,N:` lookahead so canonical names with commas work.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import ProductMapping, CanonicalDriftRejection
from myapp.management.commands.repoint_product_mappings import _parse_pairs


class Command(BaseCommand):
    help = "Record canonical-drift audit proposals as rejected (permanent skip)."

    def add_arguments(self, parser):
        parser.add_argument('--pairs', default='',
                            help='Comma-separated pm_id:rejected_canonical pairs.')
        parser.add_argument('--from-file', default='',
                            help='Read pairs from a file, one per line.')
        parser.add_argument('--note', default='',
                            help='Optional rejection note (applies to all pairs).')

    def handle(self, *args, **opts):
        pairs_raw = opts['pairs']
        if opts['from_file']:
            with open(opts['from_file']) as f:
                lines = [l.strip() for l in f
                         if l.strip() and not l.startswith('#')]
            pairs_raw = ','.join(lines)

        pairs = _parse_pairs(pairs_raw)
        if not pairs:
            self.stderr.write('No pairs provided. Use --pairs or --from-file.')
            return

        # Validate every pm_id exists.
        existing_ids = set(ProductMapping.objects.filter(
            id__in=[p[0] for p in pairs]
        ).values_list('id', flat=True))
        missing = [p for p in pairs if p[0] not in existing_ids]
        if missing:
            self.stderr.write(self.style.ERROR(
                'Missing ProductMapping ids — aborting:'
            ))
            for pm_id, _ in missing:
                self.stderr.write(f'  pm_id={pm_id}')
            return

        note = opts['note']
        recorded = duplicate = 0
        with transaction.atomic():
            for pm_id, canonical in pairs:
                _, created = CanonicalDriftRejection.objects.get_or_create(
                    product_mapping_id=pm_id,
                    rejected_canonical=canonical,
                    defaults={'note': note},
                )
                if created:
                    recorded += 1
                else:
                    duplicate += 1
                    self.stdout.write(
                        f'  pm_id={pm_id} → {canonical!r}: already rejected, skipped'
                    )

        self.stdout.write(self.style.SUCCESS(
            f'\nRecorded {recorded} new rejection(s).'
            + (f' Skipped {duplicate} duplicate(s).' if duplicate else '')
        ))
