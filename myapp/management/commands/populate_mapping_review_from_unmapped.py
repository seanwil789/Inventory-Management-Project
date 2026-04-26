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


def _find_subset_canonical(raw: str, canonicals_with_stems):
    """Subset-match tier: find a canonical whose ALL stemmed tokens appear
    in the raw description's stemmed token set. Surfaces matches that
    fuzzy/token scorers miss because the raw has many extra modifier
    tokens pulling the ratio down — e.g. 'Apple Danish' → 'Danish' fails
    token_sort_ratio (score 11) but trivially passes a subset check.

    Prefers the most-specific match (longest canonical by token count).
    Returns None if no match or if the top tier is ambiguous (multiple
    canonicals tied for most-specific).

    Args:
      raw: raw_description string
      canonicals_with_stems: list of (canonical_name, stemmed_token_set)
                              precomputed once for speed
    Returns:
      best canonical name, or None
    """
    import re
    word_re = re.compile(r'[A-Za-z]{3,}')
    def stems(s):
        out = set()
        for t in word_re.findall(s or ''):
            low = t.lower()
            if len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
                low = low[:-1]
            out.add(low)
        return out

    raw_tokens = stems(raw)
    if not raw_tokens:
        return None

    matches = []
    for canon, ctokens in canonicals_with_stems:
        if not ctokens:
            continue
        if ctokens.issubset(raw_tokens):
            matches.append((canon, len(ctokens)))

    if not matches:
        return None

    # Prefer most-specific (most tokens)
    matches.sort(key=lambda x: -x[1])
    top_n = matches[0][1]
    top_tier = [c for c, n in matches if n == top_n]

    if len(top_tier) == 1:
        return top_tier[0]
    # Ambiguous — multiple equally-specific matches (e.g. 'Cherry Tomato'
    # matches both 'Cherry' and 'Tomato' if both exist as 1-token canonicals)
    return None


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

        # Pre-compute canonical token-sets for the subset-match tier
        import re as _re
        word_re = _re.compile(r'[A-Za-z]{3,}')
        def _stems(s):
            out = set()
            for t in word_re.findall(s or ''):
                low = t.lower()
                if len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
                    low = low[:-1]
                out.add(low)
            return out
        all_canonicals = list(Product.objects.values_list('canonical_name', flat=True))
        canonicals_with_stems = [(c, _stems(c)) for c in all_canonicals]

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
            # class that token-based scorers miss)
            if suggested is None:
                subset_canon = _find_subset_canonical(raw_desc, canonicals_with_stems)
                if subset_canon:
                    suggested = Product.objects.filter(canonical_name=subset_canon).first()
                    if suggested:
                        tier = 'subset_match'
                        score = 95
                        with_subset_suggestion += 1

            if suggested is None:
                without_suggestion += 1

            # 3. Upsert into proposal queue
            existing = ProductMappingProposal.objects.filter(
                vendor=vendor, raw_description=raw_desc,
            ).first()

            if existing is None:
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
            elif existing.status == 'pending':
                # Refresh suggestion if we found something better
                if suggested is not None and existing.suggested_product != suggested:
                    if apply_changes:
                        existing.suggested_product = suggested
                        existing.score = int(score) if score else None
                        existing.confidence_tier = tier
                        existing.save()
                    pending_updated += 1
                else:
                    unchanged += 1
            # else: approved/rejected — don't touch

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
