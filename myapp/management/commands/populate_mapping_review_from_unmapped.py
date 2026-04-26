"""Scan existing unmapped ILI rows and queue ProductMappingProposal entries
for human review.

Companion to the db_write quarantine path (Phase 2A) — that path catches
NEW fuzzy matches at write time. This command catches the EXISTING backlog
of unmapped rows that accumulated before quarantine deployed.

Usage:
    python manage.py populate_mapping_review_from_unmapped              # dry-run
    python manage.py populate_mapping_review_from_unmapped --apply
"""
import sys
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceLineItem, Vendor, Product, ProductMappingProposal


def _import_mapper():
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    import mapper
    return mapper


# Skip patterns — rows that shouldn't be queued for normal review.
def _is_skippable(raw_desc: str) -> bool:
    raw = (raw_desc or '').strip()
    if not raw:
        return True
    if raw.startswith('[Sysco #'):
        # SUPC placeholder — needs Sysco rep CSV, not human canonical guessing
        return True
    return False


class Command(BaseCommand):
    help = 'Queue ProductMappingProposal entries for existing unmapped ILI rows.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write proposals to DB. Default is dry-run.')
        parser.add_argument('--min-occurrences', type=int, default=1,
                            help='Only queue items seen N+ times (default 1).')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        min_occ = opts['min_occurrences']

        # Aggregate unmapped ILIs by (vendor_id, raw_description)
        # Counts occurrence per pair so we can prioritize / threshold.
        groups: dict[tuple, dict] = defaultdict(lambda: {
            'count': 0, 'sample_ili_id': None, 'vendor': None,
        })
        unmapped_qs = (InvoiceLineItem.objects
                       .filter(product__isnull=True)
                       .exclude(match_confidence__in=['non_product', 'unmatched_drift'])
                       .select_related('vendor')
                       .only('id', 'vendor', 'raw_description'))
        skipped_kind = Counter()
        for ili in unmapped_qs.iterator():
            if _is_skippable(ili.raw_description):
                skipped_kind['skip_pattern'] += 1
                continue
            if ili.vendor is None:
                skipped_kind['no_vendor'] += 1
                continue
            key = (ili.vendor_id, ili.raw_description)
            g = groups[key]
            g['count'] += 1
            g['vendor'] = ili.vendor
            if g['sample_ili_id'] is None:
                g['sample_ili_id'] = ili.id

        self.stdout.write(f"Found {len(groups)} unique (vendor, description) pairs "
                          f"in {sum(g['count'] for g in groups.values())} unmapped ILI rows.")
        self.stdout.write(f"Skipped — junk/placeholder pattern: {skipped_kind['skip_pattern']}")
        self.stdout.write(f"Skipped — no vendor:                {skipped_kind['no_vendor']}")

        # Apply min-occurrences filter
        eligible = {k: g for k, g in groups.items() if g['count'] >= min_occ}
        self.stdout.write(f"Eligible (≥{min_occ} occurrences):       {len(eligible)}")

        # Run mapper against each eligible group to see if there's a suggestion
        mapper = _import_mapper()
        mappings = mapper.load_mappings(force_refresh=True)

        created = updated = unchanged = 0
        skipped_existing_proposal = 0
        with_suggestion = 0
        without_suggestion = 0

        for (vendor_id, raw_desc), g in eligible.items():
            vendor = g['vendor']
            # Skip if a proposal already exists for this (vendor, desc)
            existing = ProductMappingProposal.objects.filter(
                vendor=vendor, raw_description=raw_desc,
            ).first()
            if existing is not None:
                skipped_existing_proposal += 1
                continue

            # Try the mapper
            item = {'sysco_item_code': '', 'raw_description': raw_desc}
            result = mapper.resolve_item(item, mappings, vendor=vendor.name)
            canonical = result.get('canonical')
            confidence = result.get('confidence', '')
            score = result.get('score')

            suggested = None
            tier = ''
            if canonical:
                suggested = Product.objects.filter(canonical_name=canonical).first()
                tier = confidence

            if suggested is not None:
                with_suggestion += 1
            else:
                without_suggestion += 1

            if apply_changes:
                ProductMappingProposal.objects.create(
                    vendor=vendor,
                    raw_description=raw_desc,
                    suggested_product=suggested,
                    score=int(score) if score else None,
                    confidence_tier=tier,
                    source='discover_unmapped',
                    status='pending',
                )
            created += 1

        # Report
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write('')
        self.stdout.write(f"=== {mode} report ===")
        self.stdout.write(f"  Proposals created:          {created}")
        self.stdout.write(f"    With mapper suggestion:   {with_suggestion}")
        self.stdout.write(f"    Without (human invents):  {without_suggestion}")
        self.stdout.write(f"  Skipped — existing proposal: {skipped_existing_proposal}")
        if not apply_changes:
            self.stdout.write('')
            self.stdout.write('  (Dry-run — re-run with --apply to commit.)')
        else:
            total_pending = ProductMappingProposal.objects.filter(status='pending').count()
            self.stdout.write('')
            self.stdout.write(f"  Total pending proposals now: {total_pending}")
