"""Audit Inventory — multi-tier truth-seeking audit on the Product catalog.

Implements the audit workflow articulated 2026-05-25:
  Tier 1 — schema completeness    (is the field populated?)
  Tier 2 — semantic correctness    (does the populated value make operational sense?)
  Tier 3 — paper-truth sweep       (OCR cache vs stored fields)

Tiers requiring human-in-the-loop (flagged in output but not automated here):
  Tier 4 — paper image inspection
  Tier 7 — code-path trace

Outputs:
  - Markdown summary to stdout (operator-readable)
  - Timestamped JSON snapshot to .audits/<YYYYMMDD_HHMMSS>.json (durable artifact)

Usage:
  python manage.py audit_inventory                     # full audit
  python manage.py audit_inventory --no-tier3          # skip OCR sweep (fast)
  python manage.py audit_inventory --category Proteins # one category only
  python manage.py audit_inventory --json out.json     # additional JSON output path
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

from myapp.models import InvoiceLineItem, Product


# ── Tier 1 — schema completeness ────────────────────────────────────────────


def run_tier1(category_filter: str = '') -> dict:
    """Per-category coverage of the inventory-relevant Product fields."""
    qs = Product.objects.all()
    if category_filter:
        qs = qs.filter(category=category_filter)

    by_cat: dict[str, dict] = defaultdict(lambda: {
        'total': 0,
        'inventory_class_filled': 0,
        'default_case_size_filled': 0,
        'inventory_unit_descriptor_filled': 0,
        'inventory_class_distribution': Counter(),
    })

    for p in qs:
        cat = p.category or '(blank)'
        d = by_cat[cat]
        d['total'] += 1
        if p.inventory_class:
            d['inventory_class_filled'] += 1
        if p.default_case_size:
            d['default_case_size_filled'] += 1
        if p.inventory_unit_descriptor:
            d['inventory_unit_descriptor_filled'] += 1
        d['inventory_class_distribution'][p.inventory_class or 'BLANK'] += 1

    # Convert Counters to dicts for JSON
    for cat in by_cat:
        by_cat[cat]['inventory_class_distribution'] = dict(by_cat[cat]['inventory_class_distribution'])

    total = sum(d['total'] for d in by_cat.values())
    return {
        'tier': 1,
        'name': 'schema_completeness',
        'total_products': total,
        'by_category': dict(by_cat),
    }


# ── Tier 2 — semantic correctness ───────────────────────────────────────────

NON_WEIGHED_CATEGORIES = {'Smallwares', 'Chemicals', 'Coffee/Concessions', 'Beverages'}
WEIGHED_CATEGORIES = {'Proteins'}


def _check_normalization_bypass(case_size: str):
    """Pattern: stored case_size differs from what _normalize_pack_size would produce.

    Catches the bypass class: when the parser's spatial_matcher or rank_pair path
    stored the raw OCR token without routing through normalization. Highest-leverage
    Tier 2 pattern — single check covers the entire Sysco DocAI fusion class.

    Returns (suggested_normalized, is_bypass) — (None, False) if no bypass.
    """
    if not case_size:
        return None, False
    # invoice_processor/parser.py imports line_math at module level expecting
    # invoice_processor/ on sys.path (matches the synergy_sync.py pattern).
    import sys
    ip_path = str(Path(settings.BASE_DIR) / 'invoice_processor')
    if ip_path not in sys.path:
        sys.path.insert(0, ip_path)
    try:
        from parser import _normalize_pack_size  # type: ignore
    except ImportError:
        return None, False
    normalized = _normalize_pack_size(case_size)
    if normalized != case_size and '/' in normalized and '/' not in case_size:
        return normalized, True
    return None, False


def run_tier2(category_filter: str = '') -> dict:
    """Surface products where stored fields are populated but semantically wrong.

    Bug classes detected:
      A. K (P/#) populated on non-weighed categories — empty container + content "lbs"
      B. inventory_class=counted_* on volume-container with K computed (fluid OZ → weight)
      C. inventory_unit_descriptor implausibly large for category
      D. case_size bypassed _normalize_pack_size (catches Sysco fusion class)
    """
    suspects = []
    qs = Product.objects.all()
    if category_filter:
        qs = qs.filter(category=category_filter)

    for p in qs:
        latest = (InvoiceLineItem.objects
                  .filter(product=p)
                  .exclude(match_confidence='non_product')
                  .exclude(math_flagged=True)
                  .order_by('-invoice_date', '-id')
                  .first())

        # Bug D: case_size on latest ILI bypassed normalization
        # This fires WITHOUT needing OCR, catches the fusion class at Tier 2
        if latest and latest.case_size:
            suggested, is_bypass = _check_normalization_bypass(latest.case_size)
            if is_bypass:
                suspects.append({
                    'severity': 4,
                    'product_id': p.id,
                    'product': p.canonical_name,
                    'category': p.category,
                    'class': p.inventory_class or '',
                    'unit_descriptor': p.inventory_unit_descriptor or '',
                    'stored_case_size': latest.case_size,
                    'suggested_case_size': suggested,
                    'pattern': 'normalization_bypassed',
                    'detail': f'Stored case_size "{latest.case_size}" would normalize to '
                              f'"{suggested}" via _normalize_pack_size. The storage path '
                              f'(spatial_matcher / rank_pair) bypassed normalization.',
                })

        if not latest or not latest.unit_price:
            continue

        # Predict the K (P/#) value the synergy_sync calc_price_per_lb would write
        K = None
        K_path = None
        if latest.price_per_pound:
            K = float(latest.price_per_pound)
            K_path = 'stored_ppp'
        elif latest.case_total_weight_lb and float(latest.case_total_weight_lb) > 0:
            K = round(float(latest.unit_price) / float(latest.case_total_weight_lb), 4)
            K_path = 'case_total_weight_lb'

        if K is None:
            continue

        # Bug A: non-weighed category with K populated
        if p.category in NON_WEIGHED_CATEGORIES:
            suspects.append({
                'severity': 3,
                'product_id': p.id,
                'product': p.canonical_name,
                'category': p.category,
                'class': p.inventory_class or '',
                'unit_descriptor': p.inventory_unit_descriptor or '',
                'K_value': K,
                'K_path': K_path,
                'pattern': 'K_on_non_weighed_category',
                'detail': f'K (P/#) populated on {p.category}; operationally meaningless. '
                          f'K computed via {K_path}.',
            })
            continue  # avoid double-flagging same product

        # Bug B: fluid-container with K coincidentally computed
        if p.inventory_class and p.inventory_class != 'weighed':
            ud = (p.inventory_unit_descriptor or '').lower()
            container_words = ['container', 'jar', 'bottle', 'btl', 'gal', 'gallon',
                               'jug', 'pack', 'bag', 'sleeve', 'pail', 'tub', 'can',
                               'qt', 'quart', 'pt', 'pint']
            if any(w in ud for w in container_words):
                suspects.append({
                    'severity': 2,
                    'product_id': p.id,
                    'product': p.canonical_name,
                    'category': p.category,
                    'class': p.inventory_class,
                    'unit_descriptor': p.inventory_unit_descriptor or '',
                    'K_value': K,
                    'K_path': K_path,
                    'pattern': 'K_on_counted_container',
                    'detail': f'Container item with K computed; K is misleading for counted '
                              f'items. K_path={K_path}.',
                })

        # Bug C: implausibly large unit_descriptor
        ud = p.inventory_unit_descriptor or ''
        m = re.search(r'(\d+\.?\d*)\s*oz', ud, re.IGNORECASE)
        if m and p.category in ('Coffee/Concessions', 'Beverages'):
            size = float(m.group(1))
            if size > 50:
                suspects.append({
                    'severity': 3,
                    'product_id': p.id,
                    'product': p.canonical_name,
                    'category': p.category,
                    'class': p.inventory_class or '',
                    'unit_descriptor': ud,
                    'pattern': 'descriptor_implausible_size',
                    'detail': f'unit_descriptor "{ud}" — {size} oz implausibly large for '
                              f'{p.category} (typical max ~46oz).',
                })

    return {
        'tier': 2,
        'name': 'semantic_correctness',
        'suspect_count': len(suspects),
        'suspects': suspects,
    }


# ── Tier 3 — paper-truth sweep ──────────────────────────────────────────────


def _load_ocr_caches(cache_dir: Path) -> dict:
    """Load all OCR cache raw_text into memory."""
    caches: dict[str, dict] = {}
    if not cache_dir.exists():
        return caches
    for fname in os.listdir(cache_dir):
        if not fname.endswith('_docai_ocr.json'):
            continue
        try:
            with open(cache_dir / fname) as f:
                data = json.load(f)
            rt = data.get('raw_text', '')
            if rt:
                sha = fname.split('_')[0]
                caches[sha] = {
                    'text': rt,
                    'vendor': data.get('vendor', ''),
                    'date': data.get('invoice_date', ''),
                }
        except (json.JSONDecodeError, OSError):
            continue
    return caches


def _build_supc_index(caches: dict) -> dict:
    """Map each SUPC found in any cache to the list of cache SHAs it appears in."""
    supc_in_caches = defaultdict(list)
    for sha, ch in caches.items():
        for m in re.finditer(r'\b(\d{7,10})\b', ch['text']):
            supc_in_caches[m.group(1)].append(sha)
    return dict(supc_in_caches)


def _find_product_in_caches(product, latest_ili, caches: dict, supc_index: dict) -> tuple:
    """Locate an OCR snippet containing this product's invoice line.
    Returns (snippet_str, cache_sha) or (None, None).
    """
    # Strategy 1: SUPC anchor
    supc = latest_ili.vendor_item_code
    if supc and supc in supc_index:
        sha = supc_index[supc][0]
        text = caches[sha]['text']
        idx = text.find(supc)
        if idx >= 0:
            start = max(0, idx - 250)
            end = min(len(text), idx + 150)
            return text[start:end], sha

    # Strategy 2: distinctive tokens from raw_description
    raw = latest_ili.raw_description or ''
    excluded = {'CUSTOMER', 'TRUCK', 'STOP', 'SYNERGY', 'HOUSES', 'PORTION', 'SLICE',
                'OUT', 'GROUP', 'TOTAL', 'INVOICE'}
    tokens = [t for t in re.findall(r'[A-Z]{4,}', raw) if t not in excluded]
    if len(tokens) >= 2:
        t1, t2 = tokens[0], tokens[1]
        vendor_name = latest_ili.vendor.name if latest_ili.vendor else ''
        for sha, ch in caches.items():
            if vendor_name and ch['vendor'] and vendor_name != ch['vendor']:
                continue
            if t1 in ch['text'] and t2 in ch['text']:
                idx = ch['text'].find(t1)
                start = max(0, idx - 150)
                end = min(len(ch['text']), idx + 200)
                return ch['text'][start:end], sha

    return None, None


def run_tier3(category_filter: str = '') -> dict:
    """Compare stored ILI values against OCR cache raw_text. Surface discrepancies."""
    cache_dir = Path(settings.BASE_DIR) / '.ocr_cache'
    caches = _load_ocr_caches(cache_dir)
    if not caches:
        return {
            'tier': 3,
            'name': 'paper_truth_sweep',
            'error': f'No OCR caches loaded from {cache_dir}',
            'discrepancies': [],
        }
    supc_index = _build_supc_index(caches)

    qs = Product.objects.all()
    if category_filter:
        qs = qs.filter(category=category_filter)

    discrepancies = []
    no_ili = 0
    no_cache_match = 0
    verified_clean = 0

    for p in qs:
        ili = (InvoiceLineItem.objects
               .filter(product=p)
               .exclude(match_confidence='non_product')
               .exclude(math_flagged=True)
               .order_by('-invoice_date', '-id')
               .first())
        if not ili:
            no_ili += 1
            continue

        snippet, sha = _find_product_in_caches(p, ili, caches, supc_index)
        if not snippet:
            no_cache_match += 1
            continue

        # Pattern detection
        item_discs = _detect_discrepancies(p, ili, snippet)
        if item_discs:
            for d in item_discs:
                d['product_id'] = p.id
                d['product'] = p.canonical_name
                d['category'] = p.category
                d['vendor'] = ili.vendor.name if ili.vendor else ''
                d['invoice_date'] = str(ili.invoice_date)
                d['invoice_number'] = ili.invoice_number or ''
                d['stored'] = {
                    'case_size': ili.case_size,
                    'case_pack_count': ili.case_pack_count,
                    'case_pack_unit_size': float(ili.case_pack_unit_size)
                                           if ili.case_pack_unit_size is not None else None,
                    'case_pack_unit_uom': ili.case_pack_unit_uom,
                    'case_total_weight_lb': float(ili.case_total_weight_lb)
                                            if ili.case_total_weight_lb is not None else None,
                    'unit_descriptor': p.inventory_unit_descriptor,
                    'quantity': float(ili.quantity) if ili.quantity is not None else None,
                    'unit_price': float(ili.unit_price) if ili.unit_price else None,
                }
                d['ocr_snippet'] = snippet[:300]
                d['cache_sha'] = sha
                discrepancies.append(d)
        else:
            verified_clean += 1

    return {
        'tier': 3,
        'name': 'paper_truth_sweep',
        'coverage': {
            'total': qs.count(),
            'verified_clean': verified_clean,
            'discrepancies_found': len(discrepancies),
            'no_ili_history': no_ili,
            'no_cache_match': no_cache_match,
        },
        'discrepancies': discrepancies,
    }


def _detect_discrepancies(p, ili, snippet: str) -> list:
    """Apply the discrepancy-detection patterns to one product's ILI vs OCR snippet."""
    discs = []
    cs = ili.case_size or ''
    cpc = ili.case_pack_count
    cps = float(ili.case_pack_unit_size) if ili.case_pack_unit_size is not None else None
    cpu = ili.case_pack_unit_uom or ''
    ctwl = float(ili.case_total_weight_lb) if ili.case_total_weight_lb is not None else None
    qty = ili.quantity

    # Pattern A — number-token fusion: cpc=1 + cps > 50 + OZ
    if cpc == 1 and cps and cps > 50 and cpu.upper() == 'OZ':
        # Suggest the likely split
        cps_str = str(cps).rstrip('0').rstrip('.')
        # Try to find a clean split: leading 2 digits as common pack, rest as size
        suggested = None
        int_part = str(int(cps)) if cps == int(cps) else cps_str.split('.')[0]
        dec_part = cps_str.split('.')[1] if '.' in cps_str else ''
        for pc in [288, 240, 200, 192, 180, 160, 150, 144, 120, 100, 96, 80, 72, 60, 48, 36, 32, 30, 24, 20, 18, 16, 15, 12, 10, 8, 6, 5, 4, 3, 2]:
            pc_s = str(pc)
            if int_part.startswith(pc_s) and len(int_part) > len(pc_s):
                rem = int_part[len(pc_s):]
                size_str = f'{rem}.{dec_part}' if dec_part else rem
                try:
                    size = float(size_str)
                except ValueError:
                    continue
                if 0.5 <= size <= 64:
                    suggested = f'{pc}/{size_str}OZ'
                    break
        discs.append({
            'severity': 4,
            'pattern': 'number_token_fusion',
            'detail': f'cpc=1 cps={cps} OZ — OCR fusion of "N/M OZ" into single token. '
                      f'Suggested split: {suggested or "needs manual review"}.',
            'suggested_fix': suggested,
        })

    # Pattern B — case_size string token fusion
    if cs:
        cs_clean = cs.strip().replace(' ', '')
        m = re.match(r'^(\d{2,4})\.?(\d{1,2})?\s*(?:OZ|0Z|LBS?|#)$', cs_clean)
        if m and not re.match(r'^\d+/\d', cs_clean):
            whole = m.group(1)
            if len(whole) >= 3 and int(whole[:2]) in (6, 8, 10, 12, 15, 18, 20, 24, 30, 36, 40, 48, 50, 60, 72):
                discs.append({
                    'severity': 4,
                    'pattern': 'case_size_token_fusion',
                    'detail': f'case_size "{cs}" looks like pack-count + unit-size fused '
                              f'(leading {whole[:2]} matches common pack count).',
                })

    # Pattern C — fluid OZ as weight (specific shape)
    if cpu.upper() == 'OZ' and ctwl is not None and ctwl > 0:
        ud = (p.inventory_unit_descriptor or '').lower()
        fluid_signals = ['container', 'bottle', 'btl', 'fl ', 'gallon', 'qt', 'pt', 'jar']
        if any(w in ud for w in fluid_signals) and p.inventory_class != 'weighed':
            discs.append({
                'severity': 3,
                'pattern': 'fluid_oz_as_weight',
                'detail': f'case_total_weight_lb={ctwl} from fluid-OZ container; drives K '
                          f'to operationally meaningless value.',
            })

    # Pattern D — qty mismatch (Sysco multi-CS)
    qty_match = re.search(r'\b(\d{1,2})\s*CS\b', snippet)
    if qty_match and qty is not None:
        ocr_qty = int(qty_match.group(1))
        stored_qty = int(qty)
        if stored_qty != ocr_qty and ocr_qty > 1:
            discs.append({
                'severity': 3,
                'pattern': 'qty_mismatch',
                'detail': f'Stored qty={stored_qty}, OCR shows {ocr_qty} CS.',
            })

    return discs


# ── Aggregation + reporting ─────────────────────────────────────────────────


def aggregate_report(t1: dict, t2: dict, t3: dict) -> dict:
    """Combine the three tiers into one structured artifact."""
    report = {
        'generated_at': datetime.now().isoformat(),
        'tier1_schema': t1,
        'tier2_semantic': t2,
        'tier3_paper_truth': t3,
    }

    # Cross-tier summary
    summary = {
        'total_products': t1.get('total_products', 0),
        'tier1_categories': len(t1.get('by_category', {})),
        'tier2_suspects': t2.get('suspect_count', 0),
        'tier3_discrepancies': len(t3.get('discrepancies', [])),
        'tier3_verified_clean': t3.get('coverage', {}).get('verified_clean', 0),
        'tier3_no_ili': t3.get('coverage', {}).get('no_ili_history', 0),
        'tier3_no_cache': t3.get('coverage', {}).get('no_cache_match', 0),
    }

    # Severity distribution across tiers
    sev_counts = Counter()
    by_pattern = Counter()
    by_category_sev4 = defaultdict(list)
    by_category_sev3 = defaultdict(list)
    for d in t2.get('suspects', []):
        sev_counts[d['severity']] += 1
        by_pattern[d['pattern']] += 1
    for d in t3.get('discrepancies', []):
        sev_counts[d['severity']] += 1
        by_pattern[d['pattern']] += 1
        if d['severity'] == 4:
            by_category_sev4[d['category']].append(d['product'])
        elif d['severity'] == 3:
            by_category_sev3[d['category']].append(d['product'])

    summary['severity_distribution'] = dict(sev_counts)
    summary['by_pattern'] = dict(by_pattern)
    summary['sev4_products_by_category'] = {k: sorted(set(v)) for k, v in by_category_sev4.items()}
    report['summary'] = summary
    return report


def print_markdown_summary(report: dict, stdout):
    """Human-readable summary for CLI."""
    s = report['summary']
    stdout.write('═' * 72 + '\n')
    stdout.write(f'  AUDIT INVENTORY — Truth Health Report\n')
    stdout.write(f'  Generated: {report["generated_at"]}\n')
    stdout.write('═' * 72 + '\n\n')

    stdout.write(f'COVERAGE\n')
    stdout.write(f'  Total products            {s["total_products"]}\n')
    stdout.write(f'  Tier 3 verified clean     {s["tier3_verified_clean"]}\n')
    stdout.write(f'  Tier 3 discrepancies      {s["tier3_discrepancies"]}\n')
    stdout.write(f'  Tier 3 no ILI history     {s["tier3_no_ili"]}  (unverifiable)\n')
    stdout.write(f'  Tier 3 no cache match     {s["tier3_no_cache"]}  (unverifiable)\n')
    stdout.write(f'  Tier 2 semantic suspects  {s["tier2_suspects"]}\n')
    stdout.write('\n')

    stdout.write(f'SEVERITY DISTRIBUTION\n')
    for sev in sorted(s['severity_distribution'].keys(), reverse=True):
        n = s['severity_distribution'][sev]
        label = {4: 'CRITICAL (count off by factor)',
                 3: 'HIGH (descriptor/class/qty error)',
                 2: 'MEDIUM (audit-noise)'}.get(sev, 'OTHER')
        stdout.write(f'  Severity {sev}: {n}  — {label}\n')
    stdout.write('\n')

    stdout.write(f'BY BUG PATTERN\n')
    for pat, n in sorted(s['by_pattern'].items(), key=lambda x: -x[1]):
        stdout.write(f'  {pat:<32} {n}\n')
    stdout.write('\n')

    if s['sev4_products_by_category']:
        stdout.write(f'SEVERITY 4 — products by category\n')
        for cat, prods in sorted(s['sev4_products_by_category'].items()):
            stdout.write(f'  [{cat}] ({len(prods)})\n')
            for prod in prods:
                stdout.write(f'    • {prod}\n')
        stdout.write('\n')

    stdout.write('TIER 1 — schema completeness by category\n')
    for cat, d in sorted(report['tier1_schema']['by_category'].items()):
        total = d['total']
        if total == 0:
            continue
        ic = d['inventory_class_filled']
        dcs = d['default_case_size_filled']
        iud = d['inventory_unit_descriptor_filled']
        stdout.write(f'  {cat:<22} {total:>4}p  class={ic}/{total} ({100*ic//total}%)  '
                     f'def_cs={dcs}/{total} ({100*dcs//total}%)  '
                     f'unit_desc={iud}/{total} ({100*iud//total}%)\n')
    stdout.write('\n')
    stdout.write('═' * 72 + '\n')
    stdout.write('Tier 4 (paper image) + Tier 7 (code trace) are human-in-the-loop.\n')
    stdout.write('Use the JSON output to drive a /audit/ Django view for drill-down.\n')
    stdout.write('═' * 72 + '\n')


class Command(BaseCommand):
    help = "Multi-tier truth-seeking audit of the Product catalog."

    def add_arguments(self, parser):
        parser.add_argument('--json', default=None,
                            help='Additional JSON output path (always also writes to .audits/).')
        parser.add_argument('--category', default='',
                            help='Limit audit to one Product.category.')
        parser.add_argument('--no-tier3', action='store_true',
                            help='Skip Tier 3 (paper-truth sweep). Faster, lower-coverage.')
        parser.add_argument('--retention-dir',
                            default=str(Path(settings.BASE_DIR) / '.audits'),
                            help='Directory for timestamped audit JSON snapshots.')

    def handle(self, *args, **opts):
        cat_filter = opts['category']

        self.stdout.write(self.style.HTTP_INFO('Running Tier 1 — schema completeness...'))
        t1 = run_tier1(cat_filter)

        self.stdout.write(self.style.HTTP_INFO('Running Tier 2 — semantic correctness...'))
        t2 = run_tier2(cat_filter)

        if opts['no_tier3']:
            t3 = {'tier': 3, 'name': 'paper_truth_sweep', 'skipped': True,
                  'coverage': {}, 'discrepancies': []}
            self.stdout.write(self.style.WARNING('Skipping Tier 3 per --no-tier3'))
        else:
            self.stdout.write(self.style.HTTP_INFO('Running Tier 3 — paper-truth sweep...'))
            t3 = run_tier3(cat_filter)

        report = aggregate_report(t1, t2, t3)

        # Write to retention dir
        retention_dir = Path(opts['retention_dir'])
        retention_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        retention_path = retention_dir / f'audit_{ts}.json'
        with open(retention_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(f'Wrote durable artifact: {retention_path}'))

        # Optional extra JSON output
        if opts['json']:
            with open(opts['json'], 'w') as f:
                json.dump(report, f, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(f'Wrote JSON: {opts["json"]}'))

        # Print human-readable summary
        self.stdout.write('\n')
        print_markdown_summary(report, self.stdout)
