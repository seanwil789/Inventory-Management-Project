"""Flag InvoiceLineItem rows where the invoice-side signal (section_hint)
disagrees with the product-side signal (Product.category).

Complements the existing audits:
  - audit_suspect_mappings — token overlap (raw_description vs canonical_name)
  - audit_canonical_typos — near-duplicate canonicals
  - audit_semantic_mismatches (this) — category/section mismatch

Catches cases the token-overlap audit can miss: when the raw and canonical
share a common token but represent different domains (e.g. "KEYSTON CLEANER"
mapped to a Proteins product because both contain "CHICKEN" somewhere).

Skips ambiguous umbrella sections (CANNED & DRY, FROZEN) by allowing
multiple valid product categories per section.

Usage:
    python manage.py audit_semantic_mismatches
    python manage.py audit_semantic_mismatches --vendor sysco
    python manage.py audit_semantic_mismatches --show-all  # include ambiguous matches
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


# Section header (from Sysco invoices) → set of product categories that
# are semantically compatible. A mapping outside the set is a mismatch.
# Umbrella sections map to larger sets; hazard/chemical/paper/dairy are strict.
SECTION_TO_VALID_CATEGORIES = {
    'PRODUCE': {'Produce'},
    # Sysco DAIRY section maps to unified Dairy (Cheese collapsed in 0035).
    # Flags protein-only items (like striploin) that only leak here via
    # backfill OCR column noise.
    'DAIRY': {'Dairy'},
    'CHEMICAL & JANITORIAL': {'Chemicals'},
    'HAZARD': {'Chemicals'},
    'PAPER & DISP': {'Smallwares'},      # Paper/Disposable renamed to Smallwares (0057)
    'SUPPLY & EQUIPMENT': {'Smallwares'},
    'MEATS': {'Proteins'},
    'POULTRY': {'Proteins'},
    'DISPENSER BEVERAGE': {'Coffee/Concessions'},  # Beverages cat doesn't exist
    # Ambiguous umbrellas — many categories legitimate
    # Condiments/Sauces unified into Drystock (0052); Beverages cat doesn't exist
    'CANNED & DRY': {'Drystock', 'Spices', 'Bakery', 'Coffee/Concessions'},
    'FROZEN': {'Proteins', 'Produce', 'Bakery', 'Drystock'},
}

# Sections where the allowed set is intentionally broad — a mismatch has
# lower signal. Hidden behind --show-all to reduce noise.
AMBIGUOUS_SECTIONS = {'CANNED & DRY', 'FROZEN'}


class Command(BaseCommand):
    help = 'Flag ILI rows where section_hint disagrees with Product.category.'

    def add_arguments(self, parser):
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor (substring match on name).')
        parser.add_argument('--show-all', action='store_true',
                            help='Include ambiguous-umbrella mismatches (CANNED & DRY, FROZEN).')
        parser.add_argument('--limit', type=int, default=50,
                            help='Max rows to print (default 50).')

    def handle(self, *args, **opts):
        qs = (InvoiceLineItem.objects
              .exclude(section_hint='')
              .exclude(product__isnull=True)
              .exclude(product__category='')
              .select_related('product', 'vendor'))
        if opts['vendor']:
            qs = qs.filter(vendor__name__icontains=opts['vendor'])

        show_all = opts['show_all']
        limit = opts['limit']

        mismatches: list[dict] = []
        dedup: set[tuple[str, str]] = set()  # (raw_desc, canonical) pairs already seen
        unknown_sections = Counter()

        for ili in qs.iterator():
            section = ili.section_hint
            valid = SECTION_TO_VALID_CATEGORIES.get(section)
            if valid is None:
                unknown_sections[section] += 1
                continue
            if not show_all and section in AMBIGUOUS_SECTIONS:
                continue
            cat = ili.product.category
            if cat in valid:
                continue
            key = (ili.raw_description or '', ili.product.canonical_name)
            if key in dedup:
                continue
            dedup.add(key)
            mismatches.append({
                'section': section,
                'raw': ili.raw_description or '',
                'canonical': ili.product.canonical_name,
                'category': cat,
                'expected_cats': sorted(valid),
                'vendor': ili.vendor.name if ili.vendor else '',
                'tier': ili.match_confidence,
            })

        self.stdout.write(self.style.HTTP_INFO('=== Semantic mismatches ==='))
        self.stdout.write(
            f'{len(mismatches)} unique (raw → canonical) pairs flagged.')

        if show_all:
            self.stdout.write('  (including ambiguous CANNED & DRY / FROZEN sections)')
        else:
            self.stdout.write('  (ambiguous sections hidden — pass --show-all to include)')

        if unknown_sections:
            self.stdout.write(self.style.WARNING(
                f'\n  Unknown section_hint values (not in mapping table): '
                f'{dict(unknown_sections)}'))

        # Group by section
        by_section: dict[str, list] = {}
        for m in mismatches:
            by_section.setdefault(m['section'], []).append(m)

        printed = 0
        for section in sorted(by_section):
            rows = by_section[section]
            expected = sorted(SECTION_TO_VALID_CATEGORIES[section])
            self.stdout.write(self.style.HTTP_INFO(
                f'\n--- {section} (expected: {", ".join(expected)}) — {len(rows)} mismatches ---'))
            for m in rows:
                if printed >= limit:
                    break
                self.stdout.write(
                    f"  [{m['vendor'][:10]:<10}] {m['raw'][:50]!r:52} "
                    f"→ {m['canonical'][:28]:<30}  cat={m['category']}  [{m['tier']}]")
                printed += 1
            if printed >= limit:
                self.stdout.write(f'  ... ({len(mismatches) - printed} more; raise --limit to see)')
                break

        if not mismatches:
            self.stdout.write(self.style.SUCCESS(
                '\n✔ No semantic mismatches. Sections and categories agree.'))
        else:
            self.stdout.write(self.style.WARNING(
                f'\nFix paths:'
                f'\n  - If canonical is wrong: route through Mapping Review tab '
                f'(audit_suspect_mappings --write-to-review).'
                f'\n  - If section_hint is stale (pre-backfill), re-run '
                f'backfill_section_hints.'
                f'\n  - If section→category mapping is wrong for your vocabulary, '
                f'update SECTION_TO_VALID_CATEGORIES in this command.'))
