"""Record canonical-drift audit rejections via ProductMappingProposal.

Post-unification (2026-05-02): rejection is just `proposal.reject()`
on the matching PMP row. This cmd is a CLI bulk-reject helper that
finds the PMP for each `pm_id:canonical` pair and rejects it.

Equivalent to clicking Reject on each row in /mapping-review/ —
useful for batch rejections from audit output.

Usage:
    python manage.py reject_canonical_drift --pairs '112:Almonds,113:Walnuts'
    python manage.py reject_canonical_drift --pairs '...' --note 'Trail Mix is parent canonical'
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import ProductMapping, ProductMappingProposal
from myapp.management.commands.repoint_product_mappings import _parse_pairs


class Command(BaseCommand):
    help = "Bulk-reject canonical-drift proposals in ProductMappingProposal."

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

        # Validate pm_ids exist + look up vendor + description
        pm_lookup = {pm.id: pm for pm in
                     ProductMapping.objects.filter(
                         id__in=[p[0] for p in pairs]
                     ).select_related('vendor')}
        missing = [p for p in pairs if p[0] not in pm_lookup]
        if missing:
            self.stderr.write(self.style.ERROR(
                'Missing ProductMapping ids — aborting:'
            ))
            for pm_id, _ in missing:
                self.stderr.write(f'  pm_id={pm_id}')
            return

        note = opts['note']
        rejected = no_proposal = already_rejected = 0
        with transaction.atomic():
            for pm_id, canonical in pairs:
                pm = pm_lookup[pm_id]
                pmp = (ProductMappingProposal.objects
                       .filter(vendor=pm.vendor,
                               raw_description=pm.description,
                               source='drift_audit',
                               suggested_product__canonical_name=canonical)
                       .first())
                if pmp is None:
                    self.stdout.write(
                        f'  pm_id={pm_id} → {canonical!r}: no drift_audit '
                        f'proposal exists (run audit first); skipped'
                    )
                    no_proposal += 1
                    continue
                if pmp.status == 'rejected':
                    self.stdout.write(
                        f'  pm_id={pm_id} → {canonical!r}: already rejected, skipped'
                    )
                    already_rejected += 1
                    continue
                pmp.reject(notes=note)
                rejected += 1
                self.stdout.write(
                    f'  pm_id={pm_id} → {canonical!r}: rejected.'
                )

        self.stdout.write(self.style.SUCCESS(
            f'\nRejected {rejected} proposal(s).'
            + (f' Skipped {already_rejected} already-rejected.' if already_rejected else '')
            + (f' {no_proposal} had no proposal to reject.' if no_proposal else '')
        ))
