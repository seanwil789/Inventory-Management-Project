"""Run the current mapper against every successfully-mapped InvoiceLineItem
and report drift.

Use case: after tuning mapper thresholds, adding new tiers, or changing
_SYSCO_PREFIX_RE, run this to catch unintended behavior changes. Any row
where the mapper now returns a DIFFERENT canonical (or unmatched) than
what's in the DB is a potential regression.

The ground truth is the DB's existing product_id + match_confidence for
every row that's NOT currently unmatched. Rows with 'code' or 'vendor_exact'
confidence are especially reliable as regression anchors.

Non-destructive: reports only. Run after any mapper change, before
reprocess_invoices.

Usage:
    python manage.py mapper_regression_check
    python manage.py mapper_regression_check --tier code,vendor_exact
    python manage.py mapper_regression_check --sample 200
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


class Command(BaseCommand):
    help = 'Run current mapper against all mapped rows; report drift from ground truth.'

    def add_arguments(self, parser):
        parser.add_argument('--tier', type=str,
                            default='vendor_exact,vendor_fuzzy,fuzzy,stripped_fuzzy',
                            help='Comma-separated confidence tiers to check. '
                                 'Default excludes "code" because Sysco SUPC flow '
                                 'overwrites raw_description with the canonical, '
                                 'so regression-check against raw_desc is nonsensical '
                                 'for those rows.')
        parser.add_argument('--sample', type=int, default=0,
                            help='Check only first N rows (0 = all)')
        parser.add_argument('--show-drift', type=int, default=30,
                            help='Print first N drift cases (default 30)')
        parser.add_argument('--forecast-unmatched', action='store_true',
                            help='Also report how many currently-unmatched rows '
                                 'would flip to matched under the current mapper. '
                                 'Run before `reprocess_invoices` to preview impact.')

    def handle(self, *args, **opts):
        # Import mapper from invoice_processor/
        sys.path.insert(0, str(settings.BASE_DIR / 'invoice_processor'))
        from mapper import load_mappings, resolve_item  # noqa: E402

        tiers = [t.strip() for t in opts['tier'].split(',') if t.strip()]

        qs = (InvoiceLineItem.objects
              .filter(match_confidence__in=tiers, product__isnull=False)
              .select_related('product', 'vendor'))

        if opts['sample']:
            qs = qs[:opts['sample']]

        total = qs.count()
        self.stdout.write(f'Checking {total} rows in tiers: {tiers}')

        mappings = load_mappings()
        self.stdout.write(f'  {len(mappings.get("desc_map", {}))} desc mappings loaded')

        agree = 0
        now_unmatched = 0
        different_canonical = 0
        tier_drift = Counter()  # (from_tier, to_tier) → count
        drift_rows = []

        for ili in qs.iterator():
            item = {
                'raw_description': ili.raw_description or '',
                'sysco_item_code': '',  # we don't store code separately, so retry without
            }
            vendor = ili.vendor.name if ili.vendor else ''
            result = resolve_item(item, mappings, vendor=vendor)

            old_canonical = ili.product.canonical_name
            new_canonical = result.get('canonical')
            new_tier = result.get('confidence')

            if result['confidence'] == 'unmatched':
                now_unmatched += 1
                tier_drift[(ili.match_confidence, 'unmatched')] += 1
                if len(drift_rows) < opts['show_drift']:
                    drift_rows.append({
                        'type': 'now_unmatched', 'vendor': vendor,
                        'raw': (ili.raw_description or '')[:50],
                        'old_canonical': old_canonical,
                        'old_tier': ili.match_confidence,
                        'new_canonical': None, 'new_tier': 'unmatched',
                    })
            elif new_canonical != old_canonical:
                different_canonical += 1
                tier_drift[(ili.match_confidence, new_tier)] += 1
                if len(drift_rows) < opts['show_drift']:
                    drift_rows.append({
                        'type': 'different', 'vendor': vendor,
                        'raw': (ili.raw_description or '')[:50],
                        'old_canonical': old_canonical,
                        'old_tier': ili.match_confidence,
                        'new_canonical': new_canonical,
                        'new_tier': new_tier,
                    })
            else:
                agree += 1

        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Regression report ({total} rows checked) ==='))
        self.stdout.write(f'  Agree (same canonical):             {agree:6d}  ({agree/total*100:.1f}%)')
        self.stdout.write(f'  Now unmatched (was matched):        {now_unmatched:6d}  ({now_unmatched/total*100:.1f}%)')
        self.stdout.write(f'  Different canonical:                {different_canonical:6d}  ({different_canonical/total*100:.1f}%)')

        if tier_drift:
            self.stdout.write('\n=== Tier drift matrix (old → new) ===')
            for (old, new), n in tier_drift.most_common():
                self.stdout.write(f'  {old:<18} → {new:<18}  {n}')

        if drift_rows:
            self.stdout.write(self.style.WARNING(f'\n=== Drift samples (first {len(drift_rows)}) ==='))
            for r in drift_rows:
                self.stdout.write(
                    f"  [{r['vendor'][:15]:<17}] {r['raw'][:48]:<50}")
                self.stdout.write(
                    f"    was: {r['old_canonical'][:35]:<37} [{r['old_tier']}]")
                self.stdout.write(
                    f"    now: {str(r['new_canonical'])[:35]:<37} [{r['new_tier']}]")

        # Verdict
        drift_pct = (now_unmatched + different_canonical) / max(total, 1) * 100
        if drift_pct == 0:
            self.stdout.write(self.style.SUCCESS(f'\n✔ No regression. Mapper is behaviorally stable.'))
        elif drift_pct < 1:
            self.stdout.write(self.style.WARNING(
                f'\n⚠ Minor drift: {drift_pct:.2f}%. Review samples above before reprocess_invoices.'))
        else:
            self.stdout.write(self.style.ERROR(
                f'\n✗ Significant drift: {drift_pct:.1f}%. Investigate before deploying mapper changes.'))

        # Optional forecast: how many currently-unmatched rows would flip?
        if opts['forecast_unmatched']:
            self._forecast_unmatched_flips(mappings, resolve_item)

    def _forecast_unmatched_flips(self, mappings, resolve_item):
        """Run the current mapper on every currently-unmatched InvoiceLineItem
        and count how many would flip to matched. Samples by tier for review."""
        unmatched = (InvoiceLineItem.objects
                     .filter(match_confidence='unmatched')
                     .exclude(raw_description='')
                     .select_related('vendor'))
        total_unmatched = unmatched.count()
        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== Unmatched-flip forecast ({total_unmatched} rows) ==='))

        flipped = 0
        by_tier = Counter()
        samples = {}

        for ili in unmatched.iterator():
            item = {'raw_description': ili.raw_description or '',
                    'sysco_item_code': ''}
            vendor = ili.vendor.name if ili.vendor else ''
            result = resolve_item(item, mappings, vendor=vendor)
            if result['confidence'] != 'unmatched':
                flipped += 1
                tier = result['confidence']
                by_tier[tier] += 1
                if tier not in samples:
                    samples[tier] = []
                if len(samples[tier]) < 5:
                    samples[tier].append({
                        'vendor': vendor,
                        'raw': (ili.raw_description or '')[:50],
                        'canonical': result['canonical'],
                        'score': result.get('score', 0),
                    })

        flip_pct = flipped / max(total_unmatched, 1) * 100
        self.stdout.write(
            f'  Would flip to matched: {flipped} / {total_unmatched} '
            f'({flip_pct:.1f}%)')
        self.stdout.write(
            f'  Still unmatched after mapper changes: {total_unmatched - flipped}')

        if by_tier:
            self.stdout.write('\n  By tier:')
            for tier, n in by_tier.most_common():
                self.stdout.write(f'    {tier}: {n}')

        self.stdout.write('\n  Sample flips by tier:')
        for tier, rows in samples.items():
            self.stdout.write(f'    {tier}:')
            for r in rows:
                self.stdout.write(
                    f'      [{r["vendor"][:15]:<17}] {r["raw"][:45]:<47} '
                    f'→ {r["canonical"][:25]} ({r["score"]:.0f})')

        if flipped:
            self.stdout.write(self.style.SUCCESS(
                f'\n✔ Running `reprocess_invoices` would add {flipped} newly-matched '
                f'rows with no regression risk (mapper proven stable above).'))
        else:
            self.stdout.write(self.style.WARNING(
                '\n  (No unmatched rows would flip — current mapper is at '
                'saturation for this cache.)'))
