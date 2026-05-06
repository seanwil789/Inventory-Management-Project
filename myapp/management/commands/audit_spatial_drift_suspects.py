"""Detect ILI rows where spatial_matcher likely bound prices to the wrong row.

Confirmed root cause 2026-05-05: photo-tilt + y-coord clustering causes
the price column to drift across rows on tilted invoices. See
`project_spatial_drift_finding.md`.

Detection method:
  1. Per (vendor, product) historical median unit_price (n >= min_history).
  2. Flag ILIs whose actual / median ratio is outside [low, high] band.
  3. Per source_file, find adjacent flagged ILI pairs (id-ordered) where
     SWAPPING the prices would put both rows back in [swap_low, swap_high]
     of their own median — high-confidence swap pair.
  4. Report per-vendor flagged counts + swap-pair candidates with full
     row context (DB id, date, qty, current up, expected median, swap fit).

Conservative by design: strict thresholds prefer false-negatives over
false-positives. Borderline drift won't show up; obvious drift will.

Usage:
    python manage.py audit_spatial_drift_suspects
    python manage.py audit_spatial_drift_suspects --vendor "Farm Art"
    python manage.py audit_spatial_drift_suspects --threshold-low 0.4 --show 30

Per `feedback_methodologies.md` #11 — surfaces candidates for verification.
This command does NOT auto-fix. Backfill is a separate decision after
photo verification of each candidate.
"""
from collections import defaultdict
from statistics import median

from django.core.management.base import BaseCommand

from myapp.models import Vendor, InvoiceLineItem


def compute_medians(min_history=3):
    """Return {(vendor_id, product_id): median_unit_price}."""
    prices = defaultdict(list)
    for vid, pid, up in (InvoiceLineItem.objects
                          .filter(unit_price__isnull=False, product__isnull=False)
                          .exclude(unit_price=0)
                          .values_list('vendor_id', 'product_id', 'unit_price')):
        prices[(vid, pid)].append(float(up))
    return {k: median(v) for k, v in prices.items() if len(v) >= min_history}


def flag_ilis(vendor, medians, threshold_low, threshold_high):
    """Return [(ili, ratio, median)] for vendor's flagged ILIs."""
    flagged = []
    qs = (InvoiceLineItem.objects
          .filter(vendor=vendor, unit_price__isnull=False,
                  product__isnull=False)
          .exclude(unit_price=0)
          .order_by('source_file', 'id'))
    for ili in qs:
        med = medians.get((vendor.id, ili.product_id))
        if not med:
            continue
        ratio = float(ili.unit_price) / med
        if ratio < threshold_low or ratio > threshold_high:
            flagged.append((ili, ratio, med))
    return flagged, qs.count()


def find_swap_pairs(flagged, swap_low, swap_high):
    """Group flagged by source_file, find adjacent pairs that swap-fit."""
    by_src = defaultdict(list)
    for entry in flagged:
        by_src[entry[0].source_file].append(entry)
    pairs = []
    for src, rows in by_src.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r[0].id)
        for i in range(len(rows) - 1):
            a_ili, _, a_med = rows[i]
            b_ili, _, b_med = rows[i + 1]
            a_up = float(a_ili.unit_price)
            b_up = float(b_ili.unit_price)
            # If swapping makes both fit
            if (a_med and b_med
                    and swap_low <= b_up / a_med <= swap_high
                    and swap_low <= a_up / b_med <= swap_high):
                pairs.append((a_ili, b_ili, a_med, b_med))
    return pairs


class Command(BaseCommand):
    help = "Detect ILIs where spatial_matcher likely drifted prices to the wrong row."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default=None,
                            help='Limit to one vendor (default: all)')
        parser.add_argument('--threshold-low', type=float, default=0.5,
                            help='Flag below this ratio of median (default 0.5)')
        parser.add_argument('--threshold-high', type=float, default=2.0,
                            help='Flag above this ratio of median (default 2.0)')
        parser.add_argument('--swap-low', type=float, default=0.7)
        parser.add_argument('--swap-high', type=float, default=1.4)
        parser.add_argument('--min-history', type=int, default=3,
                            help='Min n for median computation (default 3)')
        parser.add_argument('--show', type=int, default=20,
                            help='Show top N swap pairs by combined off-ratio')

    def handle(self, *args, **opts):
        medians = compute_medians(opts['min_history'])
        self.stdout.write(f"\n# product medians (vendor x product, "
                          f"n >= {opts['min_history']}): {len(medians)}\n")
        self.stdout.write(f"Thresholds: flag if ratio < {opts['threshold_low']} "
                          f"or > {opts['threshold_high']}\n")
        self.stdout.write(f"Swap fit:   {opts['swap_low']} ≤ ratio ≤ {opts['swap_high']}\n\n")

        if opts['vendor']:
            try:
                vendors = [Vendor.objects.get(name=opts['vendor'])]
            except Vendor.DoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f"Vendor not found: {opts['vendor']!r}"))
                return
        else:
            vendors = Vendor.objects.order_by('name')

        self.stdout.write(
            f"{'Vendor':30s} {'Total':>7s} {'Flagged':>8s} "
            f"{'Swap-pair':>10s} {'%flagged':>9s}")
        self.stdout.write("-" * 75)

        all_swap_pairs = []
        for vendor in vendors:
            flagged, total = flag_ilis(vendor, medians,
                                        opts['threshold_low'],
                                        opts['threshold_high'])
            pairs = find_swap_pairs(flagged,
                                    opts['swap_low'], opts['swap_high'])
            all_swap_pairs.extend([(vendor.name, *p) for p in pairs])
            pct = (100 * len(flagged) / total) if total else 0
            self.stdout.write(
                f"{vendor.name:30s} {total:>7d} {len(flagged):>8d} "
                f"{len(pairs):>10d} {pct:>8.1f}%")

        self.stdout.write("")
        self.stdout.write("=" * 90)
        self.stdout.write(f"Total swap-pair candidates: {len(all_swap_pairs)}")
        self.stdout.write("=" * 90)

        if not all_swap_pairs:
            return

        self.stdout.write(f"\nTop {opts['show']} swap-pair candidates "
                          f"(sorted by combined off-ratio):\n")

        scored = []
        for vname, a, b, a_med, b_med in all_swap_pairs:
            a_off = abs(float(a.unit_price) / a_med - 1)
            b_off = abs(float(b.unit_price) / b_med - 1)
            scored.append((a_off + b_off, vname, a, b, a_med, b_med))
        scored.sort(reverse=True)

        for sc, vname, a, b, a_med, b_med in scored[:opts['show']]:
            a_ratio = float(a.unit_price) / a_med
            b_ratio = float(b.unit_price) / b_med
            a_swapped = float(b.unit_price) / a_med
            b_swapped = float(a.unit_price) / b_med
            self.stdout.write(f"[{vname}]")
            self.stdout.write(
                f"  Row A: id={a.id} {a.invoice_date} "
                f"qty={a.quantity} up=${a.unit_price}  "
                f"vs median ${a_med:.2f} ({a_ratio:.2f}x)")
            self.stdout.write(f"         desc: {(a.raw_description or '')[:65]}")
            self.stdout.write(
                f"  Row B: id={b.id} {b.invoice_date} "
                f"qty={b.quantity} up=${b.unit_price}  "
                f"vs median ${b_med:.2f} ({b_ratio:.2f}x)")
            self.stdout.write(f"         desc: {(b.raw_description or '')[:65]}")
            self.stdout.write(
                f"  → if swapped: A={a_swapped:.2f}x, B={b_swapped:.2f}x  "
                f"(fits when both in [{opts['swap_low']}, {opts['swap_high']}])")
            self.stdout.write("")
