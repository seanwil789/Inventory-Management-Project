"""Scan existing unmapped ILI rows and queue ProductMappingProposal entries
for human review.

Companion to the db_write quarantine path (Phase 2A) — that path catches
NEW fuzzy matches at write time. This command catches the EXISTING backlog
of unmapped rows that accumulated before quarantine deployed.

Usage:
    python manage.py populate_mapping_review_from_unmapped              # dry-run
    python manage.py populate_mapping_review_from_unmapped --apply
"""
import re
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


# Skip patterns — rows that shouldn't be queued for human review because
# they're parser/OCR artifacts rather than real product lines.
# Surfaced from 2026-04-25 mapping-review queue audit; each pattern was
# evidenced by a concrete proposal that should never have queued.
_SKIP_PATTERNS = [
    re.compile(r'^\s*\*{2,}'),                          # ** HAZARD ** lines
    re.compile(r'^\s*zz\s+', re.IGNORECASE),            # Farm Art zz placeholder ($0 special-order)
    re.compile(r'^\s*\.?P\.?\s*O\.?\s*Num', re.IGNORECASE),   # P.O. Number header
    re.compile(r'^\s*Delivery\s+Cha', re.IGNORECASE),   # Delivery Charge truncated
    re.compile(r'Printed\s*:', re.IGNORECASE),          # 's Printed: 03-18-2026' footer
    re.compile(r'^\s*\d+\s+QTY\s+PACK\s+SIZE\s*$', re.IGNORECASE),
    re.compile(r'^\s*[\d.]+\s+EACH\s+\w{1,4}\s*$', re.IGNORECASE),  # 2.00 EACH CL2
    re.compile(r'^\s*[A-Z]{0,5}\d{1,5}\s*$'),           # KSX04, CL2 — short alphanumeric
    re.compile(r'^\s*[A-Za-z]{1,3}\s*$'),               # 'T', 'OK' — too short to be product
]


def _is_skippable(raw_desc: str) -> bool:
    """Return True for rows that are parser/OCR artifacts, not real
    product lines. Order matters — cheap checks first."""
    raw = (raw_desc or '').strip()
    if not raw:
        return True
    if raw.startswith('[Sysco #'):
        # SUPC placeholder — needs Sysco rep CSV, not human canonical guessing
        return True
    # Pure non-alpha content (no word ≥3 letters) → junk
    if not re.search(r'[A-Za-z]{3,}', raw):
        return True
    for pat in _SKIP_PATTERNS:
        if pat.search(raw):
            return True
    return False


# NOTE: subset-match logic now lives in the production mapper
# (`invoice_processor/mapper._find_subset_canonical_in_pool`). This command
# delegates to it so the noise-token + head-noun filters apply uniformly
# to both proposal-suggestion generation and live mapper resolution.


class Command(BaseCommand):
    help = 'Queue ProductMappingProposal entries for existing unmapped ILI rows.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write proposals to DB. Default is dry-run.')
        parser.add_argument('--min-occurrences', type=int, default=1,
                            help='Only queue items seen N+ times (default 1).')
        parser.add_argument('--prune-junk', action='store_true',
                            help='Also delete existing pending proposals whose '
                                 'raw_description now matches a skip pattern '
                                 '(parser/OCR junk). Requires --apply to fire.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        min_occ = opts['min_occurrences']
        prune_junk = opts['prune_junk']

        # Optional first pass: clean up pending proposals whose raw is now
        # recognized as junk. Runs in dry-count mode unless --apply is set.
        if prune_junk:
            junk_qs = ProductMappingProposal.objects.filter(status='pending')
            to_delete = [p for p in junk_qs if _is_skippable(p.raw_description)]
            self.stdout.write(f"Pending proposals matching junk patterns: {len(to_delete)}")
            if to_delete and apply_changes:
                ids = [p.id for p in to_delete]
                ProductMappingProposal.objects.filter(id__in=ids).delete()
                self.stdout.write(f"  Deleted {len(ids)} junk proposals.")
            elif to_delete:
                self.stdout.write(f"  (Dry-run — re-run with --apply to delete.)")
            self.stdout.write('')

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

        # Pre-compute canonical pool for the subset-match fallback. The
        # production mapper does its own stemming inside the function, so
        # we pass canonical names directly.
        all_canonicals = list(Product.objects.values_list('canonical_name', flat=True))

        created = pending_updated = unchanged = 0
        with_mapper_suggestion = 0
        with_subset_suggestion = 0
        without_suggestion = 0

        for (vendor_id, raw_desc), g in eligible.items():
            vendor = g['vendor']

            # 1. Run the production mapper
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
                with_mapper_suggestion += 1

            # 2. Fallback: subset-match tier (catches Apple Danish → Danish
            # class that token-based scorers miss). Delegates to the
            # production mapper so noise-token + head-noun filters apply.
            if suggested is None:
                subset_canon = mapper._find_subset_canonical_in_pool(
                    raw_desc, all_canonicals)
                if subset_canon:
                    suggested = Product.objects.filter(canonical_name=subset_canon).first()
                    if suggested:
                        tier = 'subset_match'
                        score = 95
                        with_subset_suggestion += 1

            if suggested is None:
                without_suggestion += 1

            # 3. Upsert into proposal queue. Sean unification (2026-05-02):
            # if raw still has no canonical AND existing proposal was
            # rejected, AND the new mapper suggestion differs from the
            # rejected target, CREATE a new proposal (fresh review chance).
            # Same target as previously rejected → no new proposal (don't
            # spam Sean with the suggestion he already declined).
            existing = ProductMappingProposal.objects.filter(
                vendor=vendor, raw_description=raw_desc,
                source='discover_unmapped',
            ).first()

            if existing is None:
                if apply_changes:
                    # Cross-source dedup: if some other source already
                    # proposed this exact target for this raw, reuse
                    # rather than create a duplicate (multi-source
                    # convergence is logged via notes marker).
                    _, was_created, _ = ProductMappingProposal.get_or_create_dedup(
                        vendor=vendor,
                        raw_description=raw_desc,
                        suggested_product=suggested,
                        source='discover_unmapped',
                        defaults=dict(
                            score=int(score) if score else None,
                            confidence_tier=tier,
                            status='pending',
                        ),
                    )
                created += 1
            elif existing.status == 'pending':
                # Refresh suggestion when the new run produces a different
                # outcome — including clearing a now-rejected stale one.
                # Three cases: (a) old had nothing, new has match → set it,
                # (b) old had X, new has Y (different match) → swap,
                # (c) old had X, new has nothing (mapper rejects via new
                # filters like noise/head-noun) → clear it.
                if existing.suggested_product != suggested:
                    if apply_changes:
                        existing.suggested_product = suggested
                        existing.score = int(score) if score else None
                        existing.confidence_tier = tier
                        existing.save()
                    pending_updated += 1
                else:
                    unchanged += 1
            elif existing.status == 'rejected' and suggested is not None:
                # Sean's rule: items without canonicals resurface until one
                # is given. If the previously-rejected suggestion has been
                # replaced by a new mapper output, give Sean a fresh review.
                # Skip if same target (avoid re-suggesting what was declined).
                if existing.suggested_product != suggested:
                    if apply_changes:
                        ProductMappingProposal.get_or_create_dedup(
                            vendor=vendor,
                            raw_description=raw_desc,
                            suggested_product=suggested,
                            source='discover_unmapped',
                            defaults=dict(
                                score=int(score) if score else None,
                                confidence_tier=tier,
                                status='pending',
                            ),
                        )
                    created += 1
            # else: approved → leave alone (raw is canonicalized)

        # Report
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write('')
        self.stdout.write(f"=== {mode} report ===")
        self.stdout.write(f"  Proposals created:                  {created}")
        self.stdout.write(f"  Pending proposals refreshed:        {pending_updated}")
        self.stdout.write(f"  Pending unchanged (no new sugg):    {unchanged}")
        self.stdout.write('')
        self.stdout.write(f"  Suggestion source breakdown:")
        self.stdout.write(f"    Production mapper hit:            {with_mapper_suggestion}")
        self.stdout.write(f"    Subset-match tier hit:            {with_subset_suggestion}")
        self.stdout.write(f"    No suggestion (human invents):    {without_suggestion}")
        if not apply_changes:
            self.stdout.write('')
            self.stdout.write('  (Dry-run — re-run with --apply to commit.)')
        else:
            total_pending = ProductMappingProposal.objects.filter(status='pending').count()
            with_sugg = ProductMappingProposal.objects.filter(
                status='pending', suggested_product__isnull=False
            ).count()
            self.stdout.write('')
            self.stdout.write(f"  Total pending proposals: {total_pending} ({with_sugg} with suggested canonical)")
