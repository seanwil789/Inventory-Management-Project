"""Filtered version of `audit_suspect_mappings` — surfaces only the
GENUINE wrong mappings, hiding the compound-word + abbreviation
false positives that the raw audit produces.

Why this exists: `audit_suspect_mappings` flags any (canonical, raw_desc)
pair with zero stem overlap. ~70% of those flags are correct mappings
where the raw is a compound/concatenated/abbreviated form of the
canonical's words:

    HONEYDEWS    → Honey Dew      (compound)
    UNCRUST      → Uncrustables   (brand truncation)
    CINN TST     → Cinnamon Toast (vendor abbreviation)
    DRISCOL      → (Driscoll)     (brand prefix noise)

This command applies the same normalization the production mapper uses
before declaring overlap: vendor abbreviation expansion (BRST→Breast,
CHKN→Chicken), Sysco brand prefix stripping (WHLFCLS, BBRLIMP, etc.),
and compound-word splitting (honeydew→{honey, dew}). What's left after
those filters is the ~30-50 genuine mismaps worth fixing.

Usage:
    python manage.py audit_real_suspects                 # only genuine
    python manage.py audit_real_suspects --min-count 2   # 2+ occurrences
    python manage.py audit_real_suspects --vendor Sysco
    python manage.py audit_real_suspects --json out.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from myapp.models import InvoiceLineItem


# Compound-word pairs — when raw contains the LHS, treat any of the RHS
# tokens as overlap with the canonical. Mirrors the gating logic in
# `repoint_suspect_mismaps`. Centralized here so the audit + repoint
# stay in sync; if we add a new pair, both pick it up via this module.
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
    ('eggroll',    {'egg', 'roll'}),
    ('cheesesteak',{'cheese', 'steak'}),
]


def _import_mapper_helpers():
    """Pull the production mapper's stemming + Sysco-prefix-strip + abbrev
    expansion. The audit must use the SAME normalization pipeline as the
    mapper, otherwise it flags differences that aren't real."""
    p = str(settings.BASE_DIR / 'invoice_processor')
    if p not in sys.path:
        sys.path.insert(0, p)
    from mapper import _stem_text, _strip_sysco_prefix
    from abbreviations import expand_abbreviations
    return _stem_text, _strip_sysco_prefix, expand_abbreviations


def _stems(text: str, stem_fn) -> set[str]:
    """Stemmed token set. Stem_fn already applies the production mapper's
    stemming rules (rries→rry, atoes→ato, plural-s, etc.)."""
    return set(stem_fn(text or '').split())


def _is_rescued_after_normalization(cleaned: str, canonical: str,
                                     stem_fn) -> bool:
    """True when the post-normalization (abbreviation expansion + Sysco
    prefix strip) form of the raw description shares enough content with
    the canonical to call the mapping plausibly correct.

    Two ways to rescue:

    1. **2+ shared tokens** between cleaned and canonical. A single
       overlapping token (e.g., the modifier "ground" or the protein
       "chicken") is too weak — it lets PAPRIKA→Cinnamon-Ground and
       MEATBALL CHKN→Chicken-Thigh slip through. Two tokens means the
       cleaned form has substantial overlap, not just a coincidental
       qualifier.

    2. **Compound match** with the all-parts rule:
       cleaned has the compound word AND canonical has either
       (a) the compound word itself (e.g. STRAWBERRY→Strawberries),
       or (b) ALL parts of the compound (e.g. HONEYDEW→"Honey Dew").
       Single-part overlap (HONEYDEW→Berry) does NOT rescue.
    """
    cs = _stems(cleaned, stem_fn)
    canon_s = _stems(canonical, stem_fn)

    # Rule 1: 2+ shared tokens
    if len(cs & canon_s) >= 2:
        return True

    # Rule 2: compound match (with all-parts requirement)
    for word, parts in _COMPOUND_PAIRS:
        if word in cs:
            if word in canon_s:                 # canonical has the compound itself
                return True
            if parts.issubset(canon_s):         # canonical has all parts
                return True
        if word in canon_s:
            if word in cs:
                return True
            if parts.issubset(cs):
                return True
    return False


class Command(BaseCommand):
    help = ("Suspect-mapping audit with compound-word + abbreviation filtering "
            "applied. Surfaces only genuine mismaps.")

    def add_arguments(self, parser):
        parser.add_argument('--min-count', type=int, default=1,
                            help='Only report (product, raw_desc) pairs seen N+ times (default 1)')
        parser.add_argument('--vendor', type=str, default=None,
                            help='Restrict to one vendor')
        parser.add_argument('--json', type=str, default=None,
                            help='Write full report as JSON to this path')
        parser.add_argument('--show-filtered', action='store_true',
                            help='Also print compound-word/abbrev cases the audit suppressed '
                                 '(useful for debugging the filter).')

    def handle(self, *args, **opts):
        stem_fn, strip_prefix, expand_abbr = _import_mapper_helpers()

        qs = (InvoiceLineItem.objects
              .filter(product__isnull=False)
              .exclude(raw_description='')
              .select_related('product', 'vendor'))
        if opts['vendor']:
            qs = qs.filter(vendor__name__icontains=opts['vendor'])

        groups: dict[tuple, dict] = defaultdict(
            lambda: {'count': 0, 'dates': [], 'vendors': set(),
                     'canonical': '', 'raw_desc': '', 'product_id': None})
        filtered_groups: dict[tuple, dict] = defaultdict(
            lambda: {'count': 0, 'canonical': '', 'raw_desc': '',
                     'filter_reason': ''})

        total_scanned = 0
        raw_suspect = 0          # would be flagged by audit_suspect_mappings
        genuine_suspect = 0      # not rescued by compound/abbrev filter
        filtered_out = 0         # rescued by compound/abbrev filter

        for ili in qs.iterator():
            total_scanned += 1
            canonical = ili.product.canonical_name
            raw = ili.raw_description

            # Skip SUPC placeholders — code-tier matches have no English
            # content in the raw and aren't legitimately suspect.
            if raw.startswith('[Sysco #'):
                continue

            # Stage 1: cheap audit — direct stem overlap on raw text.
            # If overlap exists, it's not even a "raw suspect."
            if _stems(raw, stem_fn) & _stems(canonical, stem_fn):
                continue

            raw_suspect += 1

            # Stage 2: compound-aware overlap check. Apply abbreviation
            # expansion + Sysco prefix strip first so the compound match
            # sees English-token form. Compound rule REQUIRES the
            # canonical to contain either the full compound word OR all
            # parts — single-part overlap (e.g. STRAWBERRY → Berry only)
            # does NOT rescue, because that's how false positives sneak
            # through (PAPRIKA → Cinnamon-Ground rescued by 'ground'
            # alone is not a real match).
            cleaned = expand_abbr(raw)
            if ili.vendor and 'sysco' in ili.vendor.name.lower():
                cleaned = strip_prefix(cleaned)

            if _is_rescued_after_normalization(cleaned, canonical, stem_fn):
                # Filter rescued this mapping — it's actually correct
                filtered_out += 1
                if opts['show_filtered']:
                    fk = (ili.product_id, raw.strip().lower())
                    fg = filtered_groups[fk]
                    fg['count'] += 1
                    fg['canonical'] = canonical
                    fg['raw_desc'] = raw
                    fg['filter_reason'] = self._classify_filter(
                        raw, cleaned, canonical, stem_fn)
                continue

            # Stage 3: genuine suspect — passed all filters
            genuine_suspect += 1
            key = (ili.product_id, raw.strip().lower())
            g = groups[key]
            g['count'] += 1
            g['canonical'] = canonical
            g['raw_desc'] = raw
            g['product_id'] = ili.product_id
            if ili.vendor:
                g['vendors'].add(ili.vendor.name)
            g['dates'].append(ili.invoice_date.isoformat() if ili.invoice_date else '')

        filtered = sorted(
            [(k, v) for k, v in groups.items() if v['count'] >= opts['min_count']],
            key=lambda kv: -kv[1]['count'],
        )

        # Header
        self.stdout.write(self.style.HTTP_INFO('=== Real-suspect audit ==='))
        self.stdout.write(
            f'Total scanned:                  {total_scanned} ILI\n'
            f'Raw zero-overlap (pre-filter):  {raw_suspect}\n'
            f'Filtered out (compound/abbrev): {filtered_out}\n'
            f'Genuine suspect rows:           {genuine_suspect}\n'
            f'Unique (product, raw) pairs:    {len(groups)}\n'
            f'Pairs at min-count={opts["min_count"]}:           {len(filtered)}')

        if opts['show_filtered'] and filtered_groups:
            self.stdout.write(self.style.HTTP_INFO(
                '\n=== Filtered out (rescued by normalization) ==='))
            sorted_fil = sorted(filtered_groups.items(),
                                key=lambda kv: -kv[1]['count'])[:30]
            for (pid, _), fg in sorted_fil:
                self.stdout.write(
                    f'  [×{fg["count"]:3d}]  {fg["canonical"]:<30} ← '
                    f'"{fg["raw_desc"][:45]}"  [{fg["filter_reason"]}]')
            if len(filtered_groups) > 30:
                self.stdout.write(f'  ... + {len(filtered_groups)-30} more')

        if not filtered:
            self.stdout.write(self.style.SUCCESS(
                '\nNo genuine suspect mappings — queue is clean.'))
            return

        self.stdout.write(self.style.HTTP_INFO(
            '\n=== Genuine suspects (by frequency, highest first) ==='))
        for (pid, _), g in filtered[:50]:
            vendors = ', '.join(sorted(g['vendors'])) or '—'
            dates = sorted(set(g['dates']))
            date_range = (f"{dates[0][:7]} → {dates[-1][:7]}"
                          if len(dates) > 1
                          else (dates[0] if dates else '—'))
            self.stdout.write(
                f'  [×{g["count"]:3d}]  {g["canonical"]:<35} '
                f'←  "{g["raw_desc"][:50]}"')
            self.stdout.write(
                f'           vendors: {vendors:<30}  dates: {date_range}')

        if len(filtered) > 50:
            self.stdout.write(
                f'  ... + {len(filtered)-50} more (use --json for full list)')

        if opts['json']:
            out = [
                {
                    'count': v['count'],
                    'canonical': v['canonical'],
                    'raw_description': v['raw_desc'],
                    'product_id': v['product_id'],
                    'vendors': sorted(v['vendors']),
                    'first_date': min(v['dates']) if v['dates'] else None,
                    'last_date': max(v['dates']) if v['dates'] else None,
                }
                for (_, _), v in filtered
            ]
            Path(opts['json']).write_text(json.dumps(out, indent=2))
            self.stdout.write(self.style.SUCCESS(
                f'\nFull report written to {opts["json"]} ({len(out)} entries)'))

        self.stdout.write(self.style.WARNING(
            '\nFix path: each genuine suspect is a wrong ProductMapping.\n'
            '  - For one-off mismaps: run `repoint_suspect_mismaps --apply`\n'
            '    to push to the auto_repoint tier where the live mapper\n'
            '    has a different (correct) canonical at a trusted tier.\n'
            "  - For mismaps the mapper can't auto-resolve: open\n"
            '    /mapping-review/ and fix manually, OR edit the Item\n'
            '    Mapping sheet col F + run `cleanup_mappings.py --apply`\n'
            '    + `manage.py reprocess_invoices`.'))

    def _classify_filter(self, raw: str, cleaned: str, canonical: str,
                         stem_fn) -> str:
        """Best-guess label for WHY a row got rescued by the filter
        (used only with --show-filtered, for human readability)."""
        rs = _stems(raw, stem_fn)
        cs = _stems(canonical, stem_fn)
        cleaned_stems = _stems(cleaned, stem_fn)
        if cleaned_stems & cs and not (rs & cs):
            return 'abbreviation/prefix-strip'
        for word, parts in _COMPOUND_PAIRS:
            if word in rs and parts & cs:
                return f'compound: {word}={"+".join(sorted(parts))}'
            if word in cs and parts & rs:
                return f'reverse-compound: {word}={"+".join(sorted(parts))}'
        return 'other'
