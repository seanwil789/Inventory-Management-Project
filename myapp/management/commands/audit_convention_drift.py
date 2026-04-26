"""Audit canonical-name convention drift across (category, primary)
groups. Surfaces taxonomy debt — multiple naming patterns coexisting in
the same group — before it ossifies into the kind of mess we just swept
out of the Pastas primary.

Algorithm per group with 4+ canonicals:
  1. Bucket each canonical by its "shape":
       - comma-prefix:  '<word>, <rest>' → bucket by the prefix word
       - comma-suffix:  '<rest>, <word>' → bucket by the suffix word
                        (only when prefix isn't a likely common prefix)
       - single-word:   no comma, no spaces
       - multi-word-no-comma: no comma but multiple words
  2. Identify the dominant comma-prefix (most-common prefix with 2+ uses)
  3. Flag drift when: dominant prefix exists AND covers <100% of group AND
     there are 2+ outliers
  4. Print distribution + outliers + suggested unification for each
     drifted group. Uniform groups are skipped (only verbose output).

The detector is a SURFACER, not an enforcer — Sean reviews each report
and decides whether to rename. Many groups are legitimately heterogeneous
(Drystock/Spices is a soup of one-off canonicals).

Usage:
    python manage.py audit_convention_drift                 # only-drift
    python manage.py audit_convention_drift --verbose       # all groups
    python manage.py audit_convention_drift --min-group N   # threshold
"""
import re
from collections import defaultdict, Counter

from django.core.management.base import BaseCommand

from myapp.models import Product


def _classify(canonical_name):
    """Return (shape_kind, key_token) for a canonical name.

    shape_kind in {'comma-prefix', 'comma-suffix', 'single-word',
                   'multi-word'}.
    key_token is the prefix (for comma-prefix), suffix (for comma-suffix),
    the lone word (for single-word), or None (for multi-word).
    """
    if ',' in canonical_name:
        head, _, tail = canonical_name.partition(',')
        head = head.strip()
        # If the head is a single token and the tail has 1+ tokens, treat
        # as comma-prefix. The head becomes the bucket key.
        if head and ' ' not in head:
            return ('comma-prefix', head)
        # Otherwise treat the LAST comma segment as suffix
        last_seg = canonical_name.rsplit(',', 1)[-1].strip()
        if last_seg and ' ' not in last_seg:
            return ('comma-suffix', last_seg)
        return ('comma-prefix', head)
    if ' ' in canonical_name.strip():
        return ('multi-word', None)
    return ('single-word', canonical_name.strip())


class Command(BaseCommand):
    help = 'Surface canonical-naming convention drift across taxonomy groups.'

    def add_arguments(self, parser):
        parser.add_argument('--min-group', type=int, default=4,
                            help='Skip groups with fewer than N canonicals (default 4).')
        parser.add_argument('--verbose', action='store_true',
                            help='Also print uniform groups (default: only drifted).')
        parser.add_argument('--dominance-threshold', type=float, default=0.4,
                            help='Pattern needs this share of group to be flagged dominant (default 0.4).')

    def handle(self, *args, **opts):
        min_group = opts['min_group']
        verbose = opts['verbose']
        dom_threshold = opts['dominance_threshold']

        # Build groups: (category, primary) → list of canonical names
        groups = defaultdict(list)
        for p in Product.objects.all().only('canonical_name', 'category', 'primary_descriptor'):
            key = (p.category or '(none)', p.primary_descriptor or '(none)')
            groups[key].append(p.canonical_name)

        drifted = uniform = small = 0
        for (cat, pri), canonicals in sorted(groups.items()):
            if len(canonicals) < min_group:
                small += 1
                continue

            # Classify each canonical
            shapes = [_classify(c) for c in canonicals]
            kinds = Counter(s[0] for s in shapes)
            prefix_counts = Counter(s[1] for s in shapes if s[0] == 'comma-prefix')
            suffix_counts = Counter(s[1] for s in shapes if s[0] == 'comma-suffix')

            # Find dominant comma-prefix (must have 2+ uses)
            dom_prefix = None
            dom_n = 0
            if prefix_counts:
                top_prefix, top_n = prefix_counts.most_common(1)[0]
                if top_n >= 2:
                    dom_prefix = top_prefix
                    dom_n = top_n

            n = len(canonicals)
            dom_share = (dom_n / n) if dom_prefix else 0.0
            outliers = [c for c, s in zip(canonicals, shapes)
                        if not (s[0] == 'comma-prefix' and s[1] == dom_prefix)]
            has_drift = (dom_prefix and dom_share >= dom_threshold and
                         dom_share < 1.0 and len(outliers) >= 2)

            if has_drift:
                drifted += 1
                self.stdout.write(self.style.WARNING(
                    f'\n[DRIFT] {cat}/{pri} ({n} canonicals)'))
                self.stdout.write(f'  Dominant pattern: {dom_prefix!r}, X — {dom_n}/{n} ({dom_share:.0%})')
                self.stdout.write(f'  Distribution by shape:')
                for kind, count in kinds.most_common():
                    self.stdout.write(f'    {count:>3} {kind}')
                if prefix_counts:
                    self.stdout.write(f'  Comma-prefixes:')
                    for pref, count in prefix_counts.most_common():
                        marker = ' ← dominant' if pref == dom_prefix else ''
                        self.stdout.write(f'    {count:>3} {pref!r}, X{marker}')
                if suffix_counts:
                    self.stdout.write(f'  Comma-suffixes:')
                    for suf, count in suffix_counts.most_common():
                        self.stdout.write(f'    {count:>3} X, {suf!r}')
                self.stdout.write(f'  Outliers ({len(outliers)} not on dominant pattern):')
                for o in outliers[:8]:
                    self.stdout.write(f'    - {o!r}')
                if len(outliers) > 8:
                    self.stdout.write(f'    ... +{len(outliers) - 8} more')
                self.stdout.write(f'  → Consider renaming outliers to {dom_prefix!r}, X.')
            else:
                uniform += 1
                if verbose:
                    if dom_prefix and dom_share == 1.0:
                        msg = f"all {n} use '{dom_prefix}, X'"
                    elif kinds.get('single-word', 0) == n:
                        msg = f'all {n} single-word'
                    else:
                        msg = f'{n} canonicals — heterogeneous'
                    self.stdout.write(f'\n[OK]   {cat}/{pri} ({n}) — {msg}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Summary: {drifted} drifted, {uniform} uniform/heterogeneous, {small} below min-group threshold ({min_group}).'))
