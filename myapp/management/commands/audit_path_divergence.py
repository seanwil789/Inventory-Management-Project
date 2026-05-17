"""Path-divergence audit — first execution of
project_parser_improvement_methodology.

For every cached invoice with `pages` data, run all three extraction paths
(text-parser, rank-pair, spatial-matcher) independently and compare each
path's items_sum to the **printed invoice_total** captured on the matching
InvoiceValidationStatus row.

Winner = path with smallest distance-from-truth (|path.sum - printed_total|).
Production picker accuracy = does the path closest to truth match what
landed in production's items_sum?

Surfaces:
  - Per-path mean distance from printed truth (per vendor)
  - Per-path "winner" count (closest to truth among multi-path invoices)
  - Production picker accuracy — fraction of invoices where the picker
    chose the path closest to printed truth
  - Top-N invoices where the picker missed the closest path

Read-only diagnostic. No DB writes, no Pi writes. Writes a JSON report to
`.claude/path_divergence_report.json` for lineage.

Usage:
    python manage.py audit_path_divergence
    python manage.py audit_path_divergence --vendor sysco
    python manage.py audit_path_divergence --limit 20
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


def _safe_sum(items):
    """Sum `extended_amount` across an items list, tolerating missing/bad values."""
    total = 0.0
    for it in items or []:
        v = it.get('extended_amount') if isinstance(it, dict) else None
        try:
            total += float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            pass
    return round(total, 2)


class Command(BaseCommand):
    help = "Compare text-parser / rank-pair / spatial extraction paths across the OCR cache corpus."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default='',
                            help='Substring match on vendor name (case-insensitive)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Process at most N caches (0 = all)')
        parser.add_argument('--report-json',
                            default='.claude/path_divergence_report.json',
                            help='Output JSON report path (relative to repo root)')

    def handle(self, *args, vendor='', limit=0, report_json='', **kw):
        # Bootstrap invoice_processor on sys.path
        ip_path = str(settings.BASE_DIR / 'invoice_processor')
        if ip_path not in sys.path:
            sys.path.insert(0, ip_path)

        from parser import (_parse_sysco, _parse_farmart, _parse_exceptional,
                            _parse_pbm, _parse_delaware_linen, _parse_colonial)
        from rank_pair import extract_sysco_rank, extract_farmart_rank
        from spatial_matcher import (match_sysco_spatial, match_farmart_spatial,
                                     match_pbm_spatial, match_exceptional_spatial,
                                     match_delaware_spatial)
        from myapp.models import InvoiceValidationStatus

        # cache_sha → IVS lookup. IVS.cache_hashes is a JSONField list of
        # 16-char SHA prefixes (same truncation we apply below). Build the
        # full map once; cache iteration is O(1) lookups thereafter.
        ivs_by_sha = {}
        for ivs in InvoiceValidationStatus.objects.select_related('vendor').all():
            for sha in (ivs.cache_hashes or []):
                # Defensive: cache_hashes may carry full SHA or 16-char prefix
                # depending on writer. Normalize to 16-char prefix for join.
                key = sha[:16] if isinstance(sha, str) and len(sha) >= 16 else sha
                if key and key not in ivs_by_sha:
                    ivs_by_sha[key] = ivs

        TEXT = {
            'Sysco': _parse_sysco,
            'Farm Art': _parse_farmart,
            'FarmArt': _parse_farmart,
            'Exceptional Foods': _parse_exceptional,
            'Philadelphia Bakery Merchants': _parse_pbm,
            'Delaware County Linen': _parse_delaware_linen,
            'Colonial Village Meat Markets': _parse_colonial,
        }
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

        cache_dir = settings.BASE_DIR / '.ocr_cache'
        ocr_files = sorted(cache_dir.glob('*_docai_ocr.json'))

        results = []
        skipped_no_vendor = 0
        skipped_no_pages = 0

        for cache_path in ocr_files:
            if limit and len(results) >= limit:
                break
            try:
                with open(cache_path) as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            v = (cache.get('vendor') or '').strip()
            if not v:
                skipped_no_vendor += 1
                continue
            if vendor and vendor.lower() not in v.lower():
                continue

            text = cache.get('raw_text') or cache.get('text') or ''
            pages = cache.get('pages') or []

            text_items, rank_items, spatial_items = [], [], []
            errors = {}

            if v in TEXT and text:
                try:
                    text_items = TEXT[v](text)
                except Exception as e:
                    errors['text'] = f'{type(e).__name__}: {str(e)[:160]}'
            if v in RANK and pages:
                try:
                    rank_items = RANK[v](pages)
                except Exception as e:
                    errors['rank'] = f'{type(e).__name__}: {str(e)[:160]}'
            if v in SPATIAL and pages:
                try:
                    spatial_items = SPATIAL[v](pages)
                except Exception as e:
                    errors['spatial'] = f'{type(e).__name__}: {str(e)[:160]}'

            if not pages:
                skipped_no_pages += 1

            # _parse_pbm returns list[dict] post-2026-05-08 refactor; older
            # tuple-returning variants are wrapped, so iterating items is safe.
            sha16 = cache_path.stem.split('_')[0][:16]
            ivs = ivs_by_sha.get(sha16)
            printed_total = float(ivs.invoice_total) if (ivs and ivs.invoice_total is not None) else None
            production_sum = float(ivs.items_sum) if (ivs and ivs.items_sum is not None) else None

            results.append({
                'sha': sha16,
                'vendor': v,
                'invoice_date': cache.get('invoice_date', ''),
                'has_pages': bool(pages),
                'has_text': bool(text),
                'text':    {'n': len(text_items),    'sum': _safe_sum(text_items)},
                'rank':    {'n': len(rank_items),    'sum': _safe_sum(rank_items)},
                'spatial': {'n': len(spatial_items), 'sum': _safe_sum(spatial_items)},
                'errors':  errors,
                # IVS join — printed truth + what production picker delivered
                'ivs_id': ivs.id if ivs else None,
                'invoice_number': ivs.invoice_number if ivs else None,
                'printed_total': printed_total,
                'production_sum': production_sum,
                'ivs_status': ivs.status if ivs else None,
            })

        # ── Per-IVS aggregation ──────────────────────────────────────
        # Critical correction (2026-05-17): an invoice may span multiple OCR
        # caches (one per page for Sysco multi-page; rare singletons). Each
        # path's items_sum needs to be summed ACROSS all caches for the same
        # IVS before comparing to printed_total. Treating each cache
        # independently makes single-page output look catastrophically below
        # full-invoice truth — that was the bug in the v1 winner framing.
        ivs_groups: dict = defaultdict(list)
        orphans = []
        for r in results:
            if r['ivs_id']:
                ivs_groups[r['ivs_id']].append(r)
            else:
                orphans.append(r)

        aggregated = []
        for ivs_id, cache_rows in ivs_groups.items():
            first = cache_rows[0]
            agg_row = {
                'ivs_id': ivs_id,
                'vendor': first['vendor'],
                'invoice_number': first['invoice_number'],
                'invoice_date': first['invoice_date'],
                'ivs_status': first['ivs_status'],
                'printed_total': first['printed_total'],
                'production_sum': first['production_sum'],
                'n_caches': len(cache_rows),
                'cache_shas': [c['sha'] for c in cache_rows],
                'text':    {'n': sum(c['text']['n']    for c in cache_rows),
                            'sum': round(sum(c['text']['sum']    for c in cache_rows), 2)},
                'rank':    {'n': sum(c['rank']['n']    for c in cache_rows),
                            'sum': round(sum(c['rank']['sum']    for c in cache_rows), 2)},
                'spatial': {'n': sum(c['spatial']['n'] for c in cache_rows),
                            'sum': round(sum(c['spatial']['sum'] for c in cache_rows), 2)},
            }
            aggregated.append(agg_row)

        # Per-vendor aggregation, now keyed off invoice-level (aggregated) rows
        by_vendor: dict = defaultdict(list)
        for a in aggregated:
            by_vendor[a['vendor']].append(a)

        vendor_summaries = {}
        for v, rows in sorted(by_vendor.items()):
            # Truth-anchored analysis: only invoices with a printed_total can
            # be scored against ground truth. Others go into a separate bucket
            # so we don't conflate "no truth available" with "paths agreed."
            with_truth = [r for r in rows if r['printed_total'] is not None
                                          and r['printed_total'] > 0]

            # Per-path distance-from-truth aggregation (only over invoices
            # where THAT path produced output)
            path_distances: dict = {p: [] for p in ('text', 'rank', 'spatial')}
            picker_distances = []
            picker_correct = 0
            picker_missed = 0  # picker chose worse path than another available
            picker_no_data = 0  # picker chose a path that returned 0
            divergences = []
            path_winner_count = {'text': 0, 'rank': 0, 'spatial': 0, 'tie': 0,
                                  'no_path_fired': 0}

            for r in with_truth:
                truth = r['printed_total']
                paths_with_sum = {p: r[p]['sum'] for p in ('text', 'rank', 'spatial')
                                  if r[p]['n'] > 0}
                # Distance of each available path from printed truth (signed
                # not used here — abs makes "closest" unambiguous)
                dists = {p: abs(s - truth) for p, s in paths_with_sum.items()}
                for p, d in dists.items():
                    path_distances[p].append({'dist': d, 'truth': truth,
                                              'rel': d / truth})

                # Pick winner: smallest distance. Ties broken by "rank > spatial > text"
                # only as a deterministic ordering — both winners share equal dist.
                if not dists:
                    path_winner_count['no_path_fired'] += 1
                    continue
                min_dist = min(dists.values())
                winners = [p for p, d in dists.items() if d == min_dist]
                winner = (winners[0] if len(winners) == 1
                          else ('rank' if 'rank' in winners
                                else 'spatial' if 'spatial' in winners
                                else 'text'))
                if len(winners) > 1:
                    path_winner_count['tie'] += 1
                else:
                    path_winner_count[winner] = path_winner_count.get(winner, 0) + 1

                # Picker accuracy: compare production_sum to truth, then ask
                # whether any raw path was MATERIALLY closer to truth.
                # "Materially" = >$1 better AND >1% relative improvement —
                # avoids flagging rounding noise as picker errors.
                if r['production_sum'] is not None:
                    prod_dist = abs(r['production_sum'] - truth)
                    picker_distances.append({'dist': prod_dist, 'truth': truth,
                                              'rel': prod_dist / truth})
                    improvement_threshold_abs = 1.00
                    improvement_threshold_rel = 0.01
                    materially_better = [
                        p for p, d in dists.items()
                        if (prod_dist - d) > improvement_threshold_abs
                        and (prod_dist - d) / truth > improvement_threshold_rel
                    ]
                    if materially_better:
                        picker_missed += 1
                    else:
                        picker_correct += 1

                # Inter-path divergence (kept for back-compat with prior report)
                if len(paths_with_sum) >= 2 and max(paths_with_sum.values()) > 0:
                    mx = max(paths_with_sum.values())
                    mn = min(paths_with_sum.values())
                    rel_div = (mx - mn) / mx if mx > 0 else 0.0
                else:
                    rel_div = 0.0

                divergences.append({
                    **r,
                    'rel_div': round(rel_div, 4),
                    'truth_dist': {p: round(d, 2) for p, d in dists.items()},
                    'truth_rel':  {p: round(d / truth, 4) for p, d in dists.items()},
                    'winner': winner,
                    'production_dist': (round(abs(r['production_sum'] - truth), 2)
                                        if r['production_sum'] is not None else None),
                    'picker_missed_closer_by': (
                        round(prod_dist - min_dist, 2)
                        if r['production_sum'] is not None
                        and (prod_dist - min_dist) > 0
                        else None
                    ),
                })

            # Sort divergences by picker-miss-gap descending — surfaces where
            # the picker chose the most-wrong path most loudly.
            divergences.sort(
                key=lambda d: (-(d.get('picker_missed_closer_by') or 0),
                                -d['rel_div'])
            )

            def _mean(lst, key='rel'):
                return (round(sum(x[key] for x in lst) / len(lst), 4)
                        if lst else 0.0)

            # Path-availability stats: how often does each path produce ANY items
            path_present = {p: sum(1 for r in rows if r[p]['n'] > 0)
                            for p in ('text', 'rank', 'spatial')}

            vendor_summaries[v] = {
                'invoices': len(rows),
                'invoices_with_truth': len(with_truth),
                'invoices_no_ivs': sum(1 for r in rows if r['ivs_id'] is None),
                'invoices_ivs_no_total': sum(1 for r in rows
                                              if r['ivs_id'] is not None
                                              and r['printed_total'] is None),
                'path_present': path_present,
                'path_winner_count': path_winner_count,
                'path_mean_rel_distance': {
                    p: _mean(path_distances[p]) for p in ('text', 'rank', 'spatial')
                },
                'path_sample_n': {
                    p: len(path_distances[p]) for p in ('text', 'rank', 'spatial')
                },
                'picker_correct': picker_correct,
                'picker_missed':  picker_missed,
                'picker_mean_rel_distance': _mean(picker_distances),
                'top_10_divergent': divergences[:10],
            }

        report = {
            'total_caches_processed': len(results),
            'orphan_caches_no_ivs': len(orphans),
            'invoices_aggregated': len(aggregated),
            'skipped_no_vendor': skipped_no_vendor,
            'skipped_no_pages': skipped_no_pages,
            'filters': {'vendor': vendor, 'limit': limit},
            'by_vendor': vendor_summaries,
        }

        out = settings.BASE_DIR / report_json
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        # Pretty stdout summary — truth-anchored metrics
        self.stdout.write(f"Processed {len(results)} OCR caches → "
                          f"{len(aggregated)} invoices (aggregated by IVS), "
                          f"{len(orphans)} orphan caches")
        self.stdout.write(f"(skipped {skipped_no_vendor} no-vendor, "
                          f"{skipped_no_pages} no-pages)")
        self.stdout.write("")
        hdr = (f"{'Vendor':<32}{'N':>5}{'WithTruth':>11}"
               f"{'TxtDist':>9}{'RnkDist':>9}{'SptDist':>9}"
               f"{'TxtWin':>8}{'RnkWin':>8}{'SptWin':>8}{'Tie':>6}"
               f"{'PickerOK':>10}{'PickerMiss':>11}{'PickerErr':>10}")
        self.stdout.write(hdr)
        self.stdout.write("-" * len(hdr))
        for v, info in sorted(vendor_summaries.items()):
            wc = info['path_winner_count']
            pmd = info['path_mean_rel_distance']
            self.stdout.write(
                f"{v[:32]:<32}"
                f"{info['invoices']:>5}"
                f"{info['invoices_with_truth']:>11}"
                f"{pmd['text']:>9.2%}"
                f"{pmd['rank']:>9.2%}"
                f"{pmd['spatial']:>9.2%}"
                f"{wc.get('text',0):>8}"
                f"{wc.get('rank',0):>8}"
                f"{wc.get('spatial',0):>8}"
                f"{wc.get('tie',0):>6}"
                f"{info['picker_correct']:>10}"
                f"{info['picker_missed']:>11}"
                f"{info['picker_mean_rel_distance']:>10.2%}"
            )
        self.stdout.write("")
        self.stdout.write("Legend: *Dist = mean |path.sum - printed_total| / printed_total")
        self.stdout.write("        *Win  = times the path was closest to printed truth")
        self.stdout.write("        PickerOK/Miss = invoices where production picked the closest path / missed a materially-closer one")
        self.stdout.write("        PickerErr = mean |production.items_sum - printed_total| / printed_total")
        self.stdout.write("")
        self.stdout.write(f"Report written to {out}")
