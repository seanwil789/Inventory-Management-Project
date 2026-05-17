"""Per-line cross-path agreement audit (Tier 1 of trust-gate measurement plan).

For each invoice with multi-path coverage, match lines across rank-pair and
spatial-matcher outputs by normalized raw_description, then score field-level
agreement on qty / unit_price / case_size / extended_amount.

Where paths agree on a field, that's *evidence* (not proof) of per-line
correctness — two independent extraction algorithms arrived at the same
value. Where paths disagree, that's bug signal: at least one is wrong.

Text-path is omitted: the prior audit_path_divergence run showed text-path
returns $0/2 items on essentially every Sysco/Farm Art invoice — it's
functionally dead for the high-volume vendors.

Read-only diagnostic. No DB writes, no Pi writes. Writes a JSON report to
`.claude/path_agreement_report.json` for lineage.

Usage:
    python manage.py audit_path_agreement
    python manage.py audit_path_agreement --vendor sysco
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


def _normalize_desc(s):
    """Normalize raw_description for cross-path line matching.

    Aggressive normalization: lowercase, collapse whitespace, strip punctuation
    + asterisks. Two paths may produce slightly different OCR-tokenization but
    the underlying product description is the same.
    """
    if not s:
        return ''
    s = str(s).strip().lower()
    s = re.sub(r'[*,;:]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _desc_tokens(s):
    """Token set for Jaccard similarity matching. Strips Farm Art-style SKU
    prefixes (compact uppercase tokens at start) and 'united states' suffix,
    plus stop-words that don't carry product identity."""
    norm = _normalize_desc(s)
    # Strip trailing 'united states' (Farm Art origin marker)
    norm = re.sub(r'\bunited states\b\s*$', '', norm).strip()
    # Tokenize
    tokens = [t for t in re.split(r'[\s/]+', norm) if t]
    # Strip leading SKU-shaped tokens (Farm Art: 'case mil2', 'milsoy', 'garp').
    # Heuristic: drop tokens at position 0-1 that have no vowels OR are 'case'.
    while tokens and (tokens[0] == 'case'
                      or (len(tokens[0]) <= 6
                          and not re.search(r'[aeiou]', tokens[0])
                          and re.search(r'\d', tokens[0]))):
        tokens.pop(0)
    # Stop-words noise
    STOP = {'and', 'the', 'a', 'of', 'with', 'in', '-', '.', ','}
    tokens = [t for t in tokens if t not in STOP and len(t) > 1]
    return frozenset(tokens)


def _best_jaccard_match(target_tokens, candidate_token_sets, threshold=0.7):
    """Return the candidate token-set with highest Jaccard ≥ threshold against
    `target_tokens`. None if no candidate meets the bar. Used to align lines
    across paths when descriptions differ in prefix/suffix but core tokens match.

    `candidate_token_sets` is an iterable of frozensets (e.g. dict.keys()).
    """
    best_key = None
    best_score = 0.0
    for cand_tokens in candidate_token_sets:
        if not target_tokens or not cand_tokens:
            continue
        inter = len(target_tokens & cand_tokens)
        if inter == 0:
            continue
        union = len(target_tokens | cand_tokens)
        score = inter / union
        if score >= threshold and score > best_score:
            best_score = score
            best_key = cand_tokens
    return best_key


def _val(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    if v is None or v == '':
        return None
    return v


def _approx_eq(a, b, tol):
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _str_eq(a, b):
    if (a is None or a == '') and (b is None or b == ''):
        return True
    if a is None or b is None:
        return False
    return str(a).strip().upper() == str(b).strip().upper()


class Command(BaseCommand):
    help = "Per-line cross-path agreement on qty/unit_price/case_size/ext."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default='',
                            help='Substring match on vendor name')
        parser.add_argument('--report-json',
                            default='.claude/path_agreement_report.json')

    def handle(self, *args, vendor='', report_json='', **kw):
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)

        from rank_pair import extract_sysco_rank, extract_farmart_rank
        from spatial_matcher import (
            match_sysco_spatial, match_farmart_spatial,
            match_pbm_spatial, match_exceptional_spatial,
            match_delaware_spatial,
        )
        from myapp.models import InvoiceValidationStatus

        RANK = {
            'Sysco': extract_sysco_rank,
            'Farm Art': extract_farmart_rank,
            'FarmArt': extract_farmart_rank,
        }
        SPATIAL = {
            'Sysco': match_sysco_spatial,
            'Farm Art': match_farmart_spatial,
            'FarmArt': match_farmart_spatial,
            'Philadelphia Bakery Merchants': match_pbm_spatial,
            'Exceptional Foods': match_exceptional_spatial,
            'Delaware County Linen': match_delaware_spatial,
        }

        # SHA → IVS lookup (multi-page invoices have multiple cache hashes)
        ivs_by_sha = {}
        for ivs in InvoiceValidationStatus.objects.select_related('vendor').all():
            for sha in (ivs.cache_hashes or []):
                key = sha[:16] if isinstance(sha, str) and len(sha) >= 16 else sha
                if key and key not in ivs_by_sha:
                    ivs_by_sha[key] = ivs

        cache_dir = settings.BASE_DIR / '.ocr_cache'
        ocr_files = sorted(cache_dir.glob('*_docai_ocr.json'))

        # Per IVS: collected raw items per path across all cache pages
        ivs_paths: dict = defaultdict(lambda: {'rank': [], 'spatial': []})
        ivs_meta: dict = {}

        for cache_path in ocr_files:
            try:
                with open(cache_path) as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            v = (cache.get('vendor') or '').strip()
            if not v:
                continue
            if vendor and vendor.lower() not in v.lower():
                continue
            pages = cache.get('pages') or []
            if not pages:
                continue

            sha16 = cache_path.stem.split('_')[0][:16]
            ivs = ivs_by_sha.get(sha16)
            if not ivs:
                continue

            ivs_meta[ivs.id] = {
                'invoice_number': ivs.invoice_number,
                'invoice_date': str(ivs.invoice_date) if ivs.invoice_date else '',
                'vendor': v,
                'status': ivs.status,
                'printed_total': float(ivs.invoice_total) if ivs.invoice_total else None,
            }

            if v in RANK:
                try:
                    ivs_paths[ivs.id]['rank'].extend(RANK[v](pages))
                except Exception:
                    pass
            if v in SPATIAL:
                try:
                    ivs_paths[ivs.id]['spatial'].extend(SPATIAL[v](pages))
                except Exception:
                    pass

        # Per-vendor agreement aggregation
        per_vendor: dict = defaultdict(lambda: {
            'invoices': 0,
            'invoices_with_both_paths': 0,
            'rank_only_lines': 0,
            'spatial_only_lines': 0,
            'matched_lines': 0,
            'agree_qty': 0,
            'agree_unit_price': 0,
            'agree_case_size': 0,
            'agree_ext': 0,
            'all_4_agree': 0,
            'sample_disagreements': [],
        })

        for ivs_id, paths in ivs_paths.items():
            meta = ivs_meta[ivs_id]
            v = meta['vendor']
            vbucket = per_vendor[v]
            vbucket['invoices'] += 1

            rank_items = paths['rank']
            spat_items = paths['spatial']
            if not rank_items or not spat_items:
                continue
            vbucket['invoices_with_both_paths'] += 1

            # Index each path by (token_frozenset, item) for Jaccard matching.
            # First occurrence wins on duplicate token-sets.
            rank_by_tokens: dict = {}
            for it in rank_items:
                tk = _desc_tokens(it.get('raw_description'))
                if tk and tk not in rank_by_tokens:
                    rank_by_tokens[tk] = it
            spat_by_tokens: dict = {}
            for it in spat_items:
                tk = _desc_tokens(it.get('raw_description'))
                if tk and tk not in spat_by_tokens:
                    spat_by_tokens[tk] = it

            # Match each rank line to its best spatial counterpart (Jaccard ≥0.7).
            # Drains spatial_by_tokens so a spatial line can only match once.
            spat_pool = dict(spat_by_tokens)
            matched_pairs = []
            rank_only_count = 0
            for r_tokens, r_item in rank_by_tokens.items():
                # Exact-token match first (Sysco fast path), then Jaccard fallback
                if r_tokens in spat_pool:
                    matched_pairs.append((r_item, spat_pool.pop(r_tokens)))
                    continue
                best = _best_jaccard_match(r_tokens, spat_pool, threshold=0.7)
                if best is not None:
                    matched_pairs.append((r_item, spat_pool.pop(best)))
                else:
                    rank_only_count += 1
            spat_only_count = len(spat_pool)

            vbucket['matched_lines'] += len(matched_pairs)
            vbucket['rank_only_lines'] += rank_only_count
            vbucket['spatial_only_lines'] += spat_only_count

            for r, s in matched_pairs:
                # Normalized desc for sample disagreement output
                k = _normalize_desc(r.get('raw_description') or s.get('raw_description'))
                aq = _approx_eq(_val(r, 'quantity'),
                                _val(s, 'quantity'), tol=0.001)
                au = _approx_eq(_val(r, 'unit_price'),
                                _val(s, 'unit_price'), tol=0.01)
                ac = _str_eq(_val(r, 'case_size'), _val(s, 'case_size'))
                ae = _approx_eq(_val(r, 'extended_amount'),
                                _val(s, 'extended_amount'), tol=0.01)
                if aq: vbucket['agree_qty'] += 1
                if au: vbucket['agree_unit_price'] += 1
                if ac: vbucket['agree_case_size'] += 1
                if ae: vbucket['agree_ext'] += 1
                if aq and au and ac and ae:
                    vbucket['all_4_agree'] += 1
                else:
                    if len(vbucket['sample_disagreements']) < 8:
                        d = []
                        if not aq:
                            d.append(f"qty {r.get('quantity')} vs {s.get('quantity')}")
                        if not au:
                            d.append(f"unit_price {r.get('unit_price')} vs {s.get('unit_price')}")
                        if not ac:
                            d.append(f"case_size {r.get('case_size')!r} vs {s.get('case_size')!r}")
                        if not ae:
                            d.append(f"ext {r.get('extended_amount')} vs {s.get('extended_amount')}")
                        vbucket['sample_disagreements'].append({
                            'ivs_id': ivs_id,
                            'invoice_number': meta['invoice_number'],
                            'invoice_date': meta['invoice_date'],
                            'raw_desc': k[:60],
                            'disagree_on': d,
                        })

        # Compose report
        report: dict = {'by_vendor': {}}
        for v, b in sorted(per_vendor.items()):
            m = b['matched_lines']
            report['by_vendor'][v] = {
                'invoices': b['invoices'],
                'invoices_with_both_paths': b['invoices_with_both_paths'],
                'matched_lines': m,
                'rank_only_lines': b['rank_only_lines'],
                'spatial_only_lines': b['spatial_only_lines'],
                # Line-coverage: fraction of (rank+spatial) lines that BOTH paths produced
                'line_match_rate':
                    (round(m / (m + b['rank_only_lines'] + b['spatial_only_lines']), 3)
                     if (m + b['rank_only_lines'] + b['spatial_only_lines']) > 0 else None),
                'agree_qty_pct':
                    round(b['agree_qty'] / m, 3) if m else None,
                'agree_unit_price_pct':
                    round(b['agree_unit_price'] / m, 3) if m else None,
                'agree_case_size_pct':
                    round(b['agree_case_size'] / m, 3) if m else None,
                'agree_ext_pct':
                    round(b['agree_ext'] / m, 3) if m else None,
                'all_4_agree_pct':
                    round(b['all_4_agree'] / m, 3) if m else None,
                'sample_disagreements': b['sample_disagreements'],
            }

        out = settings.BASE_DIR / report_json
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        # Stdout summary
        self.stdout.write("")
        self.stdout.write(
            f"{'Vendor':<32}{'Inv':>5}{'Both':>6}{'Match':>7}"
            f"{'RnkOnly':>9}{'SptOnly':>9}{'LineMat':>9}"
            f"{'Qty':>8}{'UPx':>8}{'CS':>8}{'Ext':>8}{'All4':>8}"
        )
        self.stdout.write('-' * 117)
        for v, info in sorted(report['by_vendor'].items()):
            def pct(k):
                val = info.get(k)
                return f"{val:.1%}" if val is not None else '   —'
            self.stdout.write(
                f"{v[:32]:<32}{info['invoices']:>5}{info['invoices_with_both_paths']:>6}"
                f"{info['matched_lines']:>7}{info['rank_only_lines']:>9}{info['spatial_only_lines']:>9}"
                f"{pct('line_match_rate'):>9}"
                f"{pct('agree_qty_pct'):>8}{pct('agree_unit_price_pct'):>8}"
                f"{pct('agree_case_size_pct'):>8}{pct('agree_ext_pct'):>8}"
                f"{pct('all_4_agree_pct'):>8}"
            )
        self.stdout.write("")
        self.stdout.write("Legend: Both=invoices with both paths producing items; Match=lines matched on raw_desc")
        self.stdout.write("        RnkOnly/SptOnly=lines only one path produced; LineMat=match rate over union")
        self.stdout.write("        Qty/UPx/CS/Ext=per-field agreement when matched; All4=all 4 fields agree")
        self.stdout.write("")
        self.stdout.write(f"Report written to {out}")
