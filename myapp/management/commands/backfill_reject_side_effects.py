"""Propagate ProductMappingProposal reject_reason → ILI match_confidence
for rejections that predate the side-effect logic in mapping_review_reject.

The view at views.mapping_review_reject (added ~2026-05-02) tags matching
ILIs when a reviewer rejects with reason='typo_or_garble' (→ ILI
match_confidence='unmatched_garbled') or reason='not_a_product' (→
'non_product'). This drops the raw from the mapping-review "unresolved"
filter so Sean doesn't keep seeing rejections that have already been
adjudicated.

Rejections written before the view-side hook landed leave matching
ILIs at match_confidence='unmatched', which re-surfaces the rejected
proposal forever. This command catches up: any rejected PMP whose ILIs
are still 'unmatched' gets retagged to match its reject_reason class.

Idempotent — re-running after a drift-free corpus is a no-op.

Usage:
    python manage.py backfill_reject_side_effects             # dry-run
    python manage.py backfill_reject_side_effects --apply
"""
from django.core.management.base import BaseCommand

from myapp.models import ProductMappingProposal, InvoiceLineItem


# Mirrors the side-effect map in views.mapping_review_reject
_REASON_TO_MC = {
    'typo_or_garble': 'unmatched_garbled',
    'not_a_product':  'non_product',
}


class Command(BaseCommand):
    help = ("Retag 'unmatched' ILIs whose rejected PMP's reject_reason "
            "indicates a permanent non-product / garbled state.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes. Default is dry-run.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']

        total_pmps_with_drift = 0
        total_ilis_retagged = 0
        sample = []

        for reason, target_mc in _REASON_TO_MC.items():
            pmps = (ProductMappingProposal.objects
                    .filter(status='rejected', reject_reason=reason)
                    .select_related('vendor'))

            for p in pmps:
                ilis_qs = InvoiceLineItem.objects.filter(
                    vendor=p.vendor,
                    raw_description=p.raw_description,
                    product__isnull=True,
                    match_confidence='unmatched',
                )
                n = ilis_qs.count()
                if n == 0:
                    continue
                total_pmps_with_drift += 1
                total_ilis_retagged += n
                if len(sample) < 10:
                    sample.append((p.id, reason, target_mc, n,
                                   p.vendor.name if p.vendor else '-',
                                   p.raw_description))
                if apply_changes:
                    ilis_qs.update(match_confidence=target_mc)

        self.stdout.write(
            f"Rejected PMPs with drifted ILIs: {total_pmps_with_drift}")
        self.stdout.write(
            f"ILIs to retag: {total_ilis_retagged}")
        if sample:
            self.stdout.write("")
            self.stdout.write("Sample (first 10):")
            for pid, reason, target_mc, n, v, raw in sample:
                self.stdout.write(
                    f"  PMP id={pid} v={v} reason={reason} "
                    f"→ {n} ILI(s) tagged {target_mc}")
                self.stdout.write(f"    raw={raw[:80]}")

        if not apply_changes and total_ilis_retagged:
            self.stdout.write("")
            self.stdout.write("(Dry-run — re-run with --apply to commit.)")
        elif apply_changes and total_ilis_retagged:
            self.stdout.write("")
            self.stdout.write(
                f"Applied: {total_ilis_retagged} ILI row(s) retagged.")
