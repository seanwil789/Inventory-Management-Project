"""Detect candidate Sysco brand-prefix tokens from unmatched invoice rows.

Sysco descriptions are noisy: a brand-code prefix (WHLFCLS, GRECOSN, etc.)
appears before the actual product name. The mapper's _SYSCO_PREFIX_RE strips
known prefixes. Unknown prefixes leak through and reduce fuzzy match quality.

This command scans raw_description on UNMATCHED Sysco InvoiceLineItem rows,
extracts candidate prefix tokens (first ALL-CAPS 5-10 char word that doesn't
appear in any canonical name), and counts frequency. High-frequency candidates
are likely real brand prefixes that should be added to _SYSCO_PREFIX_RE.

Non-destructive: reports candidates only. You decide which to add.

Usage:
    python manage.py audit_sysco_prefixes
    python manage.py audit_sysco_prefixes --min-count 3
"""
from __future__ import annotations

import re
from collections import Counter

from django.core.management.base import BaseCommand
from myapp.models import InvoiceLineItem, Product


class Command(BaseCommand):
    help = 'Detect candidate Sysco brand-prefix tokens from unmatched rows.'

    def add_arguments(self, parser):
        parser.add_argument('--min-count', type=int, default=2,
                            help='Minimum appearances to report (default 2)')

    def handle(self, *args, **opts):
        # Build a set of tokens known to appear in legitimate canonical names.
        # A candidate prefix should NOT be a real product word.
        canonical_tokens = set()
        for p in Product.objects.all():
            for t in re.findall(r'[A-Za-z]{3,}', p.canonical_name):
                canonical_tokens.add(t.upper())

        self.stdout.write(f'Known canonical tokens: {len(canonical_tokens)}')

        # Tokens already stripped by _SYSCO_PREFIX_RE (rough list; source of
        # truth is the regex in mapper.py but enumeration works for filtering)
        known_prefixes = {
            'WHLFCLS', 'WHLFIMP', 'GRECOSN', 'COOPR', 'PATRPCK',
            'EMBASSA', 'KONTOS', 'FLEISHM', 'AREZCLS', 'AREZIMP',
            'CALMINI', 'DELMNT', 'SPART', 'INTL', 'PACKER', 'LEPORT',
            'PORTCLS', 'PORTPRD', 'ALTACUC', 'VERSTNR', 'STERAMN',
            'MILLBAK', 'HIGHBAK', 'SUPRPTZ', 'INAUGTHOM', 'THRCRAB',
            'MAEPLOY', 'MINMAID', 'JDMTCLS', 'CASACLS', 'SIMPLOT',
            'PILLSBY', 'HORMEL', 'KEYSTON', 'ECOLAB', 'HEINZ',
            'REGINA', 'ROLAND', 'GATORADE', 'LABELLA',
        }

        unmatched = (InvoiceLineItem.objects
                     .filter(match_confidence='unmatched', vendor__name='Sysco')
                     .exclude(raw_description=''))
        self.stdout.write(f'Unmatched Sysco rows: {unmatched.count()}')

        # Extract first ALL-CAPS 5-10 char token from each description
        candidate_prefixes = Counter()
        candidate_examples = {}
        _PREFIX_CANDIDATE_RE = re.compile(r'\b([A-Z]{5,10})\b')

        for ili in unmatched.iterator():
            desc = ili.raw_description.strip()
            m = _PREFIX_CANDIDATE_RE.search(desc)
            if not m:
                continue
            token = m.group(1)
            if token in canonical_tokens or token in known_prefixes:
                continue
            # Skip common English words that might be mis-classified as prefixes
            if token in {'FRESH', 'FROZEN', 'DRIED', 'WHOLE', 'SLICED',
                         'GROUND', 'COOKED', 'SHRED', 'GRAT', 'SHRDD'}:
                continue
            candidate_prefixes[token] += 1
            if token not in candidate_examples:
                candidate_examples[token] = desc[:70]

        filtered = [(t, n) for t, n in candidate_prefixes.most_common()
                    if n >= opts['min_count']]

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Candidate unknown prefixes ({len(filtered)} @ count >= {opts["min_count"]}) ==='))
        if not filtered:
            self.stdout.write(self.style.SUCCESS('\nNo new prefix candidates above threshold.'))
            return

        self.stdout.write(f'  {"Count":<6} {"Token":<12} Example')
        self.stdout.write(f'  {"-"*5:<6} {"-"*10:<12} {"-"*50}')
        for t, n in filtered[:40]:
            ex = candidate_examples.get(t, '')
            self.stdout.write(f'  {n:<6} {t:<12} {ex}')
        if len(filtered) > 40:
            self.stdout.write(f'  ... +{len(filtered) - 40} more')

        self.stdout.write(self.style.WARNING(
            '\nTo adopt a candidate, add it to _SYSCO_PREFIX_RE in\n'
            'invoice_processor/mapper.py. After adding, run:\n'
            '  python manage.py reprocess_invoices\n'
            'to re-run the mapper on cached OCR with the new prefix list.'))
