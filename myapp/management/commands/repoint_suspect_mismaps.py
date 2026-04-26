"""Repoint suspect-mismap InvoiceLineItems to the live mapper's current
canonical answer.

Surfaced 2026-04-26 audit: 245 of 350 zero-token-overlap suspects had
the live mapper producing a different (correct) canonical than the
stored one — a tier of corruption that would directly poison Thursday's
month-end inventory dollar values (Pringles stored as Bottled Water,
Cream Cheese stored as Mozzarella, the spice cluster all swapped, etc.).

Algorithm per suspect ILI:
  1. Run mapper.resolve_item against the raw description
  2. If mapper produces a DIFFERENT canonical at a high-confidence tier
     (code/vendor_exact/vendor_fuzzy/exact/fuzzy/stripped_fuzzy), repoint:
       - ILI.product → new canonical's Product
       - ILI.match_confidence → 'auto_repoint' (new audit-trail tier)
       - ProductMapping.update_or_create(vendor, description=raw) so
         future invoices auto-resolve at vendor_exact
  3. If mapper returns subset_match, unmatched, or no canonical → SKIP
     (those need human eyes; the queue + manual review handles them)

Atomic per-row + dry-run by default. Inspect output before --apply.

Usage:
    python manage.py repoint_suspect_mismaps                # dry-run
    python manage.py repoint_suspect_mismaps --apply
    python manage.py repoint_suspect_mismaps --apply --limit 50
"""
import sys
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import InvoiceLineItem, ProductMapping, Product


# Tiers we trust for auto-repoint. Anything outside this set (subset_match,
# unmatched, blank) is flagged for human review instead of repointed.
TRUSTED_TIERS = frozenset({
    'code', 'vendor_exact', 'vendor_fuzzy',
    'exact', 'fuzzy', 'stripped_fuzzy',
})

# Compound-pair noise filter (mirrors audit_real_suspects logic).
# When raw and canonical actually share a compound concept (HONEYDEW vs
# Honey Dew), they're not really suspect — skip them.
_COMPOUND_PAIRS = [
    ('honeydew',   {'honey', 'dew'}),
    ('swisschard', {'swiss', 'chard'}),
    ('blackberry', {'black', 'berry'}),
    ('blueberry',  {'blue', 'berry'}),
    ('strawberry', {'straw', 'berry'}),
    ('cranberry',  {'cran', 'berry'}),
    ('hotdog',     {'hot', 'dog'}),
    ('hamburger',  {'ham', 'burger'}),
    ('cheesecake', {'cheese', 'cake'}),
    ('cornbread',  {'corn', 'bread'}),
    ('pepperjack', {'pepper', 'jack'}),
]


def _import_mapper():
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    import mapper
    from mapper import _stem_text, _strip_sysco_prefix
    from abbreviations import expand_abbreviations
    return mapper, _stem_text, _strip_sysco_prefix, expand_abbreviations


def _stems(text, stemmer):
    return set(stemmer(text or '').split())


def _has_overlap(raw, canon, stemmer):
    rs = _stems(raw, stemmer)
    cs = _stems(canon, stemmer)
    if rs & cs:
        return True
    for word, parts in _COMPOUND_PAIRS:
        if word in rs and parts & cs:
            return True
        if word in cs and parts & rs:
            return True
    return False


class Command(BaseCommand):
    help = 'Repoint suspect-mismap ILIs to the mapper\'s current best canonical.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes. Default is dry-run.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N rows (staged apply).')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        limit = opts['limit']
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'=== {mode} mode ===\n')

        mapper, stem_text, strip_prefix, expand_abbr = _import_mapper()
        mappings = mapper.load_mappings(force_refresh=True)

        # Walk all mapped ILIs; collect suspects (zero-overlap with stored canon)
        self.stdout.write('Identifying suspects...')
        suspects = []
        for ili in (InvoiceLineItem.objects.filter(product__isnull=False)
                    .select_related('product', 'vendor')
                    .only('id', 'raw_description', 'product_id',
                          'product__canonical_name', 'vendor__name',
                          'vendor_id', 'match_confidence', 'section_hint').iterator()):
            raw = ili.raw_description or ''
            canon = ili.product.canonical_name if ili.product else ''
            if not raw or not canon:
                continue
            if raw.startswith('[Sysco #'):
                continue
            cleaned = expand_abbr(raw)
            if ili.vendor and 'sysco' in ili.vendor.name.lower():
                cleaned = strip_prefix(cleaned)
            if _has_overlap(cleaned, canon, stem_text):
                continue
            suspects.append(ili)

        self.stdout.write(f'  Suspect ILIs: {len(suspects)}')
        if limit:
            suspects = suspects[:limit]
            self.stdout.write(f'  Limiting to first {limit}')

        # Triage each suspect
        repointable = []   # (ili, new_canonical_str, new_tier)
        ambiguous   = []   # (ili, reason)
        agreeing    = []   # naming-false-positives — leave alone
        bucket = Counter()

        for ili in suspects:
            item = {'sysco_item_code': '', 'raw_description': ili.raw_description,
                    'section': ili.section_hint or ''}
            vname = ili.vendor.name if ili.vendor else ''
            r = mapper.resolve_item(item, mappings, vendor=vname)
            new_canon = r.get('canonical')
            new_tier = r.get('confidence', '')
            stored = ili.product.canonical_name

            if not new_canon:
                ambiguous.append((ili, 'mapper unmatched'))
                bucket['ambiguous_unmatched'] += 1
            elif new_canon == stored:
                agreeing.append((ili, stored))
                bucket['agree_naming_blind'] += 1
            elif new_tier not in TRUSTED_TIERS:
                ambiguous.append((ili, f'mapper tier={new_tier} (not trusted for auto-repoint)'))
                bucket[f'ambiguous_tier_{new_tier}'] += 1
            else:
                repointable.append((ili, new_canon, new_tier))
                bucket['repointable'] += 1

        # Report bucket counts
        self.stdout.write('')
        self.stdout.write('=== Triage ===')
        for k in ['repointable', 'agree_naming_blind', 'ambiguous_unmatched']:
            self.stdout.write(f'  {bucket[k]:>4}  {k}')
        for k, v in sorted(bucket.items()):
            if k.startswith('ambiguous_tier_'):
                self.stdout.write(f'  {v:>4}  {k}')

        # Cluster repoints by (stored → new) pair for clear reporting
        cluster = Counter((ili.product.canonical_name, new) for ili, new, _ in repointable)
        self.stdout.write('')
        self.stdout.write('=== Top repoint clusters (stored → new) ===')
        for (stored, new), n in cluster.most_common(20):
            self.stdout.write(f'  {n:>3}  {stored!r}  →  {new!r}')

        if not apply_changes:
            self.stdout.write('')
            self.stdout.write(f'(Dry-run — would repoint {len(repointable)} ILIs. Re-run with --apply.)')
            return

        # Apply
        self.stdout.write('')
        self.stdout.write('=== APPLYING ===')
        # Cache canonical-name → Product lookups to avoid N queries
        target_cache = {}
        for _, new_canon, _ in repointable:
            if new_canon not in target_cache:
                target_cache[new_canon] = Product.objects.filter(canonical_name=new_canon).first()

        n_ili = n_pm_created = n_pm_updated = 0
        skipped_no_target = 0
        with transaction.atomic():
            # Per-row repoint
            for ili, new_canon, new_tier in repointable:
                target = target_cache.get(new_canon)
                if target is None:
                    skipped_no_target += 1
                    continue
                old_product_id = ili.product_id
                ili.product = target
                ili.match_confidence = 'auto_repoint'
                ili.save(update_fields=['product', 'match_confidence'])
                n_ili += 1
                # Mirror to ProductMapping so future invoices route correctly
                if ili.vendor_id:
                    pm, created = ProductMapping.objects.update_or_create(
                        vendor_id=ili.vendor_id,
                        description=ili.raw_description,
                        defaults={'product': target},
                    )
                    if created:
                        n_pm_created += 1
                    else:
                        n_pm_updated += 1

        self.stdout.write(f'  ILIs repointed:           {n_ili}')
        self.stdout.write(f'  ProductMappings created:  {n_pm_created}')
        self.stdout.write(f'  ProductMappings updated:  {n_pm_updated}')
        if skipped_no_target:
            self.stdout.write(self.style.WARNING(
                f'  Skipped (target Product missing): {skipped_no_target}'))
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Done. Re-run audit_real_suspects to verify.'))
