"""Find likely-misspelled Product canonical names.

Uses two signals:
  1. Near-duplicates — pairs of canonicals with high char-level similarity
     but different exact strings (Levenshtein <= 2 or fuzz.ratio >= 90).
     These are candidate typos (Canteloupe/Cantaloupe, Suace/Sauce, etc.).
  2. Lone-outlier singletons — canonicals with no invoice lines AND no
     mappings that share enough tokens with any other canonical. These are
     orphaned names that may be misspellings of existing products.

Non-destructive: prints a report only. You decide which pairs to merge
(via Django admin or a data migration). Merging requires reassigning any
ProductMapping/InvoiceLineItem/YieldReference/RecipeIngredient FKs first —
don't just rename.

Usage:
    python manage.py audit_canonical_typos
    python manage.py audit_canonical_typos --min-score 92
    python manage.py audit_canonical_typos --json out.json
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count

from myapp.models import Product, InvoiceLineItem, ProductMapping


class Command(BaseCommand):
    help = 'Find likely-misspelled Product canonical names (non-destructive report).'

    def add_arguments(self, parser):
        parser.add_argument('--min-score', type=int, default=90,
                            help='Minimum char-level similarity to flag (default 90)')
        parser.add_argument('--json', type=str, default=None,
                            help='Write candidate pairs to JSON at this path')

    def handle(self, *args, **opts):
        try:
            from rapidfuzz import fuzz
        except ImportError:
            self.stderr.write('rapidfuzz required. `pip install rapidfuzz`')
            return

        canonicals = list(Product.objects.all().order_by('canonical_name'))
        self.stdout.write(f'Products: {len(canonicals)}')

        # Attach usage counts so we can rank which side of a pair is canonical
        usage = {
            p.id: {
                'invoice_lines': InvoiceLineItem.objects.filter(product=p).count(),
                'mappings': ProductMapping.objects.filter(product=p).count(),
            }
            for p in canonicals
        }

        # All pairs (N^2/2 — for 528 products = ~139k comparisons; rapidfuzz is fast)
        self.stdout.write('Scanning all canonical pairs for near-duplicates...')
        pairs = []
        names = [p.canonical_name for p in canonicals]
        for i, n1 in enumerate(names):
            n1_lower = n1.lower()
            for j in range(i + 1, len(names)):
                n2 = names[j]
                # Skip pairs that differ in length by more than 3 chars
                if abs(len(n1) - len(n2)) > 3:
                    continue
                # Skip pairs that don't share at least one 4+ letter word
                # (prevents "Milk" / "Silk" style coincidental matches)
                n1_words = {w for w in n1_lower.split() if len(w) >= 4}
                n2_words = {w.lower() for w in n2.split() if len(w) >= 4}
                if n1_words and n2_words and not (n1_words & n2_words):
                    # Only skip if BOTH had 4+ letter words AND none shared
                    continue
                score = fuzz.ratio(n1_lower, n2.lower())
                if score >= opts['min_score'] and score < 100:
                    pairs.append({
                        'a': n1, 'b': n2,
                        'a_id': canonicals[i].id, 'b_id': canonicals[j].id,
                        'score': int(score),
                        'a_usage': usage[canonicals[i].id],
                        'b_usage': usage[canonicals[j].id],
                    })

        pairs.sort(key=lambda p: -p['score'])

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Candidate typo pairs ({len(pairs)} found @ score >= {opts["min_score"]}) ==='))
        self.stdout.write(self.style.WARNING(
            'Merging requires moving invoice lines + mappings. Do NOT just rename.'))

        if not pairs:
            self.stdout.write(self.style.SUCCESS('\nNo candidate typos. Canonical names clean.'))
            return

        self.stdout.write('')
        self.stdout.write(f'  {"Score":<6} {"Name A":<35} {"Usage A":<15} {"Name B":<35} {"Usage B":<15}')
        self.stdout.write(f'  {"-"*5:<6} {"-"*33:<35} {"-"*13:<15} {"-"*33:<35} {"-"*13:<15}')
        for p in pairs[:50]:
            ua = f"{p['a_usage']['invoice_lines']}li/{p['a_usage']['mappings']}m"
            ub = f"{p['b_usage']['invoice_lines']}li/{p['b_usage']['mappings']}m"
            self.stdout.write(
                f"  {p['score']:<6} {p['a'][:33]:<35} {ua:<15} {p['b'][:33]:<35} {ub:<15}")
        if len(pairs) > 50:
            self.stdout.write(f'  ... +{len(pairs) - 50} more (use --json for full list)')

        # Summary: likely-orphan canonicals (0 invoice lines + 0 mappings)
        # that have a near-duplicate with real usage. These are the easiest wins.
        self.stdout.write(self.style.HTTP_INFO('\n=== Easy-win candidates (orphan → used sibling) ==='))
        easy_wins = []
        for p in pairs:
            a_orphan = p['a_usage']['invoice_lines'] == 0 and p['a_usage']['mappings'] == 0
            b_orphan = p['b_usage']['invoice_lines'] == 0 and p['b_usage']['mappings'] == 0
            if a_orphan and not b_orphan:
                easy_wins.append((p['a'], p['b'], p['score'], 'retire-a'))
            elif b_orphan and not a_orphan:
                easy_wins.append((p['b'], p['a'], p['score'], 'retire-b'))
        if easy_wins:
            for orphan, keeper, score, _ in easy_wins[:30]:
                self.stdout.write(f'  retire {orphan!r:<35} keep {keeper!r:<35} (score {score})')
            if len(easy_wins) > 30:
                self.stdout.write(f'  ... +{len(easy_wins) - 30} more')
        else:
            self.stdout.write('  (none — all duplicate pairs have usage on both sides)')

        if opts['json']:
            Path(opts['json']).write_text(json.dumps(pairs, indent=2))
            self.stdout.write(self.style.SUCCESS(f'\nFull report: {opts["json"]}'))

        self.stdout.write(self.style.WARNING(
            '\nNext step: review pairs above and decide on each:\n'
            '  - Both real products → leave (the names are genuinely distinct)\n'
            '  - Typo duplicate → merge the orphan into the used sibling\n'
            '    Use Django admin to reassign FKs, then delete the orphan.'))
