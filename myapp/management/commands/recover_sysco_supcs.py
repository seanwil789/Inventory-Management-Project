"""Recover unmapped Sysco SUPC codes via cross-invoice OCR context.

The parser's catch-weight hardening recovered ~400 ILI rows whose
raw_description is the '[Sysco #NNN]' placeholder — real products with
real dollar value but SUPC codes not in code_map. This command scans ALL
cached Sysco OCR text for each unmapped SUPC, finds the strongest
nearby product-description context, fuzzy-matches it against existing
canonical Products, and proposes mappings.

Strategies (in priority order):
  1. `inline`: the SUPC appears on the SAME line as letters → that line
     is the product description (e.g. 'SANITIZER OASIS 146 MULTI QU
     6100536' → Floor Cleaner).
  2. `nearby`: the SUPC is standalone; scan ±3 lines for a real-looking
     description line. Lower confidence because column-dump OCR can put
     adjacent items' descriptions near the wrong code.

Junk filter rejects contexts matching 'GROUP TOTAL', 'MANIFEST#',
section headers (****), 'PURCHASE ORDER', 'TERMS', etc.
Shared-across-codes check rejects descriptions that appear for multiple
different SUPCs (a strong sign of column-dump misalignment).

Output:
  - HIGH (inline + fuzzy score >= 90): suggested for auto-apply
  - MEDIUM (score >= 75): surfaced for review
  - LOW: left for manual mapping or SUPC CSV import

By default, dry-run. With --apply, writes high-confidence mappings
to the local code_map cache (fast iteration) and to ProductMapping
table (persistent; picks up on next mapping refresh). Updated 2026-
05-02: was Google Sheets Item Mapping tab; sheet retired.

2026-05-02: MEDIUM confidence tier now enqueues into the Django
/mapping-review/ queue (ProductMappingProposal, source='supc_recovery')
instead of the retired sheet's Mapping Review tab. No-context SUPCs
also enqueue with suggested_product=None so the reviewer can invent
a canonical inline. Approve via the UI to re-point.

Usage:
  python manage.py recover_sysco_supcs                   # dry-run
  python manage.py recover_sysco_supcs --apply           # write cache + sheet
  python manage.py recover_sysco_supcs --apply --cache-only  # skip sheet write
  python manage.py recover_sysco_supcs --min-score 95    # tighter threshold
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem, Product


_PLACEHOLDER_RE = re.compile(r'^\[Sysco\s*#(\d+)\]$')
_JUNK_CTX = re.compile(
    r'GROUP\s*TOTAL|INVOICE\s+ADJUSTMENTS|\*{3,}|MANIFEST|PURCHASE ORDER|'
    r'EXTENDED|CASES\s+SPLIT|TERMS|OPEN:|CLOSE:|REMIT TO|PAYABLE ON|'
    r'CONFIDENTIAL|FRESH["\'\s]*MENU',
    re.IGNORECASE,
)


def _discover_contexts(cache_dir: Path, codes_needed: set) -> dict[str, list[tuple[str, str]]]:
    """Scan all Sysco OCR caches, return {code: [(source, description), ...]}."""
    contexts = defaultdict(list)
    for p in glob.glob(str(cache_dir / '*_docai_ocr.json')):
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get('vendor') != 'Sysco':
            continue
        lines = (d.get('raw_text') or '').splitlines()
        for i, line in enumerate(lines):
            for code in codes_needed:
                if code not in line:
                    continue
                if _JUNK_CTX.search(line):
                    continue
                letters = len(re.findall(r'[A-Za-z]{3,}', line))
                if letters >= 2:
                    contexts[code].append(('inline', line.strip()))
                else:
                    for j in range(max(0, i - 3), min(len(lines), i + 4)):
                        if j == i:
                            continue
                        nb = lines[j].strip()
                        if _JUNK_CTX.search(nb):
                            continue
                        nb_letters = re.findall(r'[A-Za-z]{4,}', nb)
                        if len(nb_letters) >= 2 and not re.search(r'\d{6,}', nb):
                            contexts[code].append(('nearby', nb[:100]))
                            break
    return contexts


def _pick_best_context(ctxs: list[tuple[str, str]], shared_descs: set) -> tuple[str, str] | None:
    """Pick the best (source, description) pair from context candidates."""
    good = [(s, d) for (s, d) in ctxs if d not in shared_descs]
    if not good:
        return None
    inlines = [d for s, d in good if s == 'inline']
    if inlines:
        return ('inline', Counter(inlines).most_common(1)[0][0])
    nearbys = [d for s, d in good if s == 'nearby']
    if nearbys:
        return ('nearby', Counter(nearbys).most_common(1)[0][0])
    return None


class Command(BaseCommand):
    help = 'Recover unmapped Sysco SUPCs via cross-invoice OCR context.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write high-confidence mappings to the local '
                                 'code_map cache AND to Google Sheets.')
        parser.add_argument('--cache-only', action='store_true',
                            help='With --apply: write to local cache only, '
                                 'skip Google Sheets write. Changes revert on '
                                 'next mapping refresh (TTL 1 hour).')
        parser.add_argument('--write-review', action='store_true',
                            help='Write MEDIUM-confidence (75-89 score) SUPC '
                                 'mapping suggestions to the Mapping Review tab '
                                 'for human approval. Writes the discovered '
                                 'context as raw_description + the fuzzy-matched '
                                 'canonical as Suggested Product, with Approve '
                                 'status blank so Sean reviews each.')
        parser.add_argument('--min-score', type=int, default=90,
                            help='Minimum fuzzy score for high-confidence (default 90).')

    def handle(self, *args, **opts):
        sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
        from rapidfuzz import process, fuzz, utils as fuzz_utils  # noqa: E402
        from mapper import _strip_sysco_prefix  # noqa: E402

        cache_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        mapping_cache = Path(settings.BASE_DIR) / 'invoice_processor' / 'mappings' / 'item_mappings.json'
        min_score = opts['min_score']

        # 1. Collect placeholder codes + dollar totals
        placeholder_info = {}
        for ili in InvoiceLineItem.objects.filter(
            vendor__name='Sysco', product__isnull=True,
            raw_description__regex=r'^\[Sysco\s*#\d+\]$',
        ):
            m = _PLACEHOLDER_RE.match(ili.raw_description)
            if not m:
                continue
            code = m.group(1)
            info = placeholder_info.setdefault(code, {'dollars': 0.0, 'row_count': 0})
            info['dollars'] += float(ili.unit_price or 0)
            info['row_count'] += 1

        if not placeholder_info:
            self.stdout.write('No unmapped Sysco placeholders found.')
            return

        total_dollars = sum(v['dollars'] for v in placeholder_info.values())
        self.stdout.write(
            f'Placeholders: {len(placeholder_info)} unique SUPCs, '
            f'${total_dollars:,.2f} total unmapped dollars'
        )

        # 2. Discover contexts for each code
        contexts = _discover_contexts(cache_dir, set(placeholder_info.keys()))

        # 3. Detect shared-across-codes descriptions (unreliable)
        desc_to_codes = defaultdict(set)
        for code, ctxs in contexts.items():
            for _, desc in ctxs:
                desc_to_codes[desc].add(code)
        shared = {d for d, cs in desc_to_codes.items() if len(cs) > 1}

        # 4. Fuzzy-match each code's best context against canonicals
        canonicals = list(Product.objects.values_list('canonical_name', flat=True))
        high_conf = []       # (code, dollars, ctx, canonical, score, src)
        medium_conf = []
        no_context = []

        for code, info in placeholder_info.items():
            ctxs = contexts.get(code, [])
            picked = _pick_best_context(ctxs, shared)
            if not picked:
                no_context.append(code)
                continue
            src, desc = picked
            stripped = _strip_sysco_prefix(desc.upper())
            result = process.extractOne(stripped, canonicals,
                                         scorer=fuzz.token_set_ratio,
                                         processor=fuzz_utils.default_process)
            score = result[1] if result else 0
            canon = result[0] if result else None

            if score >= min_score and src == 'inline':
                high_conf.append((code, info['dollars'], desc, canon, score, src))
            elif score >= 95 and src == 'nearby':
                # Very-high nearby matches are acceptable for auto-apply
                high_conf.append((code, info['dollars'], desc, canon, score, src))
            elif score >= 75:
                medium_conf.append((code, info['dollars'], desc, canon, score, src))
            else:
                medium_conf.append((code, info['dollars'], desc, canon, score, src))

        high_conf.sort(key=lambda x: -x[1])
        medium_conf.sort(key=lambda x: -x[1])

        self.stdout.write(
            f'\n  High-confidence (auto-apply eligible): {len(high_conf)} codes, '
            f'${sum(h[1] for h in high_conf):,.2f}'
        )
        self.stdout.write(
            f'  Medium-confidence (review):            {len(medium_conf)} codes, '
            f'${sum(h[1] for h in medium_conf):,.2f}'
        )
        self.stdout.write(f'  No usable context:                     {len(no_context)}\n')

        self.stdout.write(self.style.HTTP_INFO('\n=== HIGH-CONFIDENCE MAPPINGS ==='))
        for code, d, ctx, canon, score, src in high_conf:
            self.stdout.write(
                f'  ${d:>7.2f} #{code} [{src}] → {canon!r} ({score:.0f})  ctx: {ctx[:60]}'
            )

        if medium_conf:
            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== MEDIUM-CONFIDENCE (review) — top 10 ==='))
            for code, d, ctx, canon, score, src in medium_conf[:10]:
                self.stdout.write(
                    f'  ${d:>7.2f} #{code} [{src}] → {canon!r} ({score:.0f})  ctx: {ctx[:50]}'
                )

        # No-context SUPCs also go to the review tab when --write-review is
        # active: even without a cross-invoice description hint, the SUPC
        # code itself is enough for Sean to look up on the Sysco portal
        # and type the canonical directly. The apply_approved col G
        # extraction (from Notes 'code=NNN') carries the SUPC into Item
        # Mapping so future invoices resolve via code-tier match.
        no_context_supcs_for_review = []
        if opts['write_review'] and no_context:
            for code in no_context:
                dollars = placeholder_info[code]['dollars']
                no_context_supcs_for_review.append((code, dollars))
            no_context_supcs_for_review.sort(key=lambda x: -x[1])

        # Medium-confidence → /mapping-review/ Django queue (Sean 2026-05-02:
        # replaces the legacy sheet write since the Mapping Review tab is
        # retired). Source='supc_recovery'.
        if opts['write_review'] and (medium_conf or no_context_supcs_for_review):
            from myapp.models import Vendor, Product, ProductMappingProposal
            sysco, _ = Vendor.objects.get_or_create(name='Sysco')

            enqueued = converged = skipped = 0
            for code, d, ctx, canon, score, src in medium_conf:
                target = Product.objects.filter(canonical_name=canon).first()
                if target is None:
                    skipped += 1
                    continue
                raw_desc = f'[Sysco #{code}] ctx: {ctx[:100]}'
                notes = (f'SUPC recovery · source={src} · code={code} · '
                         f'${d:.2f}/row · Top candidate: {canon} ({int(score)})')
                _, created, did_converge = ProductMappingProposal.get_or_create_dedup(
                    vendor=sysco,
                    raw_description=raw_desc,
                    suggested_product=target,
                    source='supc_recovery',
                    defaults=dict(
                        score=int(score),
                        confidence_tier=src,  # 'inline' or 'nearby'
                        status='pending',
                        notes=notes,
                    ),
                )
                if created: enqueued += 1
                elif did_converge: converged += 1

            for code, dollars in no_context_supcs_for_review:
                raw_desc = f'[Sysco #{code}] (no OCR context)'
                notes = (f'SUPC recovery · no context · code={code} · '
                         f'${dollars:.2f}/row · LOOK UP ON SYSCO PORTAL')
                # No suggested_product (suggested=None signals "human invents")
                _, created, _ = ProductMappingProposal.get_or_create_dedup(
                    vendor=sysco,
                    raw_description=raw_desc,
                    suggested_product=None,
                    source='supc_recovery',
                    defaults=dict(
                        score=None,
                        confidence_tier='no_context',
                        status='pending',
                        notes=notes,
                    ),
                )
                if created: enqueued += 1

            self.stdout.write(self.style.SUCCESS(
                f'\n✔ Enqueued {enqueued} SUPC-recovery proposal(s) in '
                f'/mapping-review/?status=unresolved'
                + (f' (converged {converged} with same-target proposals)' if converged else '')
                + (f' — skipped {skipped} for missing canonical' if skipped else '')
            ))

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                f'\nDry run. Re-run with --apply to write high-confidence '
                f'mappings to the code_map cache' +
                ('' if opts['cache_only'] else ' and Google Sheets Item Mapping tab') + '.'))
            return

        # 5. Apply high-confidence mappings to local code_map cache
        try:
            with open(mapping_cache) as f:
                cache = json.load(f)
        except Exception as e:
            self.stderr.write(f'Could not load {mapping_cache}: {e}')
            return

        code_map = cache.setdefault('code_map', {})
        before_count = len(code_map)
        for code, _d, _ctx, canon, _score, _src in high_conf:
            code_map[code] = canon
        with open(mapping_cache, 'w') as f:
            json.dump(cache, f, indent=2)
        self.stdout.write(self.style.SUCCESS(
            f'\n✔ Updated {mapping_cache.name}: {before_count} → {len(code_map)} code entries'
        ))

        # 6. Write to ProductMapping table (Sean 2026-05-02: replaces the
        # legacy Item Mapping sheet write — that tab is retired).
        if opts['cache_only']:
            self.stdout.write(self.style.WARNING(
                'Skipped ProductMapping write (--cache-only). '
                'Changes will revert on next mapping refresh (TTL 1 hour).'
            ))
            return

        from myapp.models import Vendor, Product, ProductMapping
        sysco, _ = Vendor.objects.get_or_create(name='Sysco')

        created = updated = skipped = 0
        for code, _d, ctx, canon, _score, _src in high_conf:
            target = Product.objects.filter(canonical_name=canon).first()
            if target is None:
                skipped += 1
                continue
            # Use SUPC as the description (matches the ProductMapping
            # convention for code-tier hits — desc serves as fallback when
            # the same product appears via raw text without SUPC).
            pm, was_created = ProductMapping.objects.update_or_create(
                vendor=sysco,
                description=ctx[:200],
                defaults={'product': target, 'supc': code},
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f'✔ ProductMapping table: created {created}, updated {updated}'
            + (f', skipped {skipped} for missing canonical' if skipped else '')
            + '.'
        ))
        self.stdout.write(
            '\nNext: run `python manage.py reprocess_invoices` to re-map '
            'existing [Sysco #NNN] rows now that these SUPCs resolve.'
        )
