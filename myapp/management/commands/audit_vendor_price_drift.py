"""Compare InvoiceLineItem unit_price against VendorPriceList list price.

Two-stage classification:

  1. MATH PATTERN — what's the relationship between qty, unit_price, and
     extended_amount on this ILI?
       math_holds         — qty × up ≈ ext           (clean per-unit)
       ach_holds          — qty × up × 0.99 ≈ ext    (vendor applied 1% ACH on line)
       parser_suspect     — qty > 1 AND up == ext    (parser put line total in up)
       qty_anomaly        — ratio ≈ 0.5x / 2x / 4x   (qty captured at wrong scale)
       unknown            — none of the above (small bucket)

  2. DRIFT vs CSV — once we know math pattern, derive a "real per-unit"
     and compare to the closest VendorPriceList unit option.
       aligned            — |diff| ≤ tolerance
       drift              — |diff| > tolerance
       no_csv             — no SKU match in price list

The simple version of this cmd lumped math-pattern issues together with
real drift, hiding both. This version surfaces:
  - top drift across ALL math classes (real signals + parser suspects)
  - math-anomaly rows separately (so they don't masquerade as drift)

Per `feedback_methodologies.md` #11: we found 13 "parser bug" candidates
on first pass, only 3 verified cleanly. The rest were market trajectory
or no-CSV-entry. Surface the math distinction; don't conflate.

Usage:
    python manage.py audit_vendor_price_drift --vendor "Farm Art"
    python manage.py audit_vendor_price_drift --vendor "Farm Art" --days 60
    python manage.py audit_vendor_price_drift --vendor "Farm Art" --threshold 0.15
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from collections import defaultdict, Counter

from django.core.management.base import BaseCommand, CommandError

from myapp.models import Vendor, InvoiceLineItem, VendorPriceList


# Vendor-annotation suffixes the matcher should ignore. Order matters —
# longer patterns first so e.g. "*NO SPLITS*" matches before "*NO SPLIT".
_ANNOTATION_PATTERNS = [
    r'\*+\s*NO\s*SPLITS?\s*\*+',
    r'\*+\s*NO\s*HALF\s*CASES?',
    r'\*+\s*NO\s*SPLITS?',
    r'\*+\s*LOCAL\s*\*?',
    r'"LOCAL"?',
    r'\bNO\s*SPLITS?\b',
]


def normalize_desc(s):
    """Normalize a vendor description for matching (see test cases)."""
    if not s:
        return ''
    s = s.upper().strip()
    for pat in _ANNOTATION_PATTERNS:
        s = re.sub(pat, '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*([,./#"*()\-])\s*', r'\1', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' .,*"')


def classify_math(ili):
    """Return ('math_class', real_per_unit_estimate).

    real_per_unit is our best guess at the correct per-unit price for the
    ILI given its math_class. For math_holds/ach_holds, that's just up.
    For parser_suspect (up==ext, qty>1), it's ext/qty. For qty_anomaly,
    we still surface ext/qty but flag uncertainty.
    """
    qty = float(ili.quantity or 0)
    up = float(ili.unit_price or 0)
    ext = float(ili.extended_amount or 0)

    if qty <= 0 or up <= 0 or ext <= 0:
        return ('unknown', up if up > 0 else None)

    expected = qty * up
    if abs(expected - ext) < 0.05:
        return ('math_holds', up)

    ratio = ext / expected if expected else 0

    # ACH-discounted line: ext = qty * up * 0.99
    if 0.985 <= ratio <= 0.995:
        return ('ach_holds', up)

    # Parser suspect: up == ext and qty > 1 (canonical Farm Art bug case)
    if abs(up - ext) < 0.05 and qty > 1:
        return ('parser_suspect', ext / qty)

    # qty_anomaly: ratio is near a small integer multiple (0.5x, 2x, 4x)
    for target in (0.5, 0.25, 0.20, 2.0, 4.0):
        if abs(ratio - target) < 0.025:
            return ('qty_anomaly', ext / qty if qty else up)

    return ('unknown', up)


def best_csv_match(real_per_unit, options):
    """Pick the unit option whose price is closest to real_per_unit."""
    if not options:
        return (None, None, None)
    best = min(options, key=lambda u_p: abs(u_p[1] - Decimal(str(real_per_unit))))
    unit, expected = best
    if expected == 0:
        return (unit, expected, None)
    diff_pct = (Decimal(str(real_per_unit)) - expected) / expected * 100
    return (unit, expected, float(diff_pct))


class Command(BaseCommand):
    help = "Audit ILI unit_price vs VendorPriceList — math-pattern + drift two-stage."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', required=True)
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--tolerance', type=float, default=0.02,
                            help='Aligned-band as decimal (default 0.02 = 2%%)')
        parser.add_argument('--threshold', type=float, default=0.15,
                            help='Top-drift report threshold (default 0.15 = 15%%)')
        parser.add_argument('--show', type=int, default=30)

    def handle(self, *args, **opts):
        try:
            vendor = Vendor.objects.get(name=opts['vendor'])
        except Vendor.DoesNotExist:
            raise CommandError(f"Vendor not found: {opts['vendor']!r}")

        # CSV lookup keyed on normalized description
        price_lookup = defaultdict(list)
        for entry in VendorPriceList.objects.filter(vendor=vendor):
            expected = entry.list_price * (1 - entry.ach_discount_pct)
            price_lookup[normalize_desc(entry.raw_description)].append(
                (entry.unit, expected)
            )

        if not price_lookup:
            raise CommandError(f"No VendorPriceList entries for {vendor.name}.")

        cutoff = date.today() - timedelta(days=opts['days'])
        # B6: exclude math_flagged rows from drift baseline. Anomaly rows
        # in the baseline produce false positives (real prices look
        # anomalous against a corrupted average) and false negatives.
        ilis = list(InvoiceLineItem.objects
                    .filter(vendor=vendor, invoice_date__gte=cutoff,
                            unit_price__isnull=False)
                    .exclude(unit_price=0)
                    .exclude(math_flagged=True)
                    .select_related('canonical_vendor_pricelist')
                    .order_by('-invoice_date'))

        # Per-row classification + drift
        rows = []  # (ili, math_class, real_per_unit, csv_unit, csv_expected, diff_pct, drift_class, lookup_path)
        math_counts = Counter()
        drift_counts = Counter()
        lookup_path_counts = Counter()  # 'fk' / 'normalize_desc' / 'no_match'

        for ili in ilis:
            math_class, real_pu = classify_math(ili)
            math_counts[math_class] += 1

            # Phase 4 consumer (Sean 2026-05-06): prefer canonical FK lookup
            # when populated. Indexed FK is faster + more reliable than
            # fuzzy raw_description matching. Falls back to the normalize_desc
            # path when FK is null (pre-backfill rows + below-threshold matches).
            options = []
            lookup_path = 'no_match'
            if ili.canonical_vendor_pricelist_id is not None:
                vpl = ili.canonical_vendor_pricelist
                expected = vpl.list_price * (1 - vpl.ach_discount_pct)
                options = [(vpl.unit, expected)]
                lookup_path = 'fk'
            else:
                options = price_lookup.get(normalize_desc(ili.raw_description), [])
                lookup_path = 'normalize_desc' if options else 'no_match'
            lookup_path_counts[lookup_path] += 1

            csv_unit, csv_expected, diff_pct = best_csv_match(real_pu or 0, options)
            if not options:
                drift_class = 'no_csv'
            elif diff_pct is None:
                drift_class = 'no_csv'
            elif abs(diff_pct) <= opts['tolerance'] * 100:
                drift_class = 'aligned'
            else:
                drift_class = 'drift'
            drift_counts[drift_class] += 1
            rows.append((ili, math_class, real_pu, csv_unit, csv_expected,
                         diff_pct, drift_class, lookup_path))

        total = len(rows)

        # Report header
        self.stdout.write(f"\nVendor:           {vendor.name}")
        self.stdout.write(f"Window:           last {opts['days']} days")
        self.stdout.write(f"VPL entries:      {sum(len(v) for v in price_lookup.values())} "
                          f"across {len(price_lookup)} normalized descriptions")
        self.stdout.write(f"ILIs analyzed:    {total}")
        self.stdout.write(f"Tolerance:        ±{opts['tolerance'] * 100:.1f}%")
        self.stdout.write(f"Drift threshold:  ≥{opts['threshold'] * 100:.0f}%")

        # Math-pattern distribution
        self.stdout.write("")
        self.stdout.write("=== Math-pattern distribution ===")
        for mc in ('math_holds', 'ach_holds', 'parser_suspect', 'qty_anomaly', 'unknown'):
            n = math_counts[mc]
            pct = 100 * n / total if total else 0
            self.stdout.write(f"  {mc:24s}: {n:4d} ({pct:5.1f}%)")

        # Drift distribution
        self.stdout.write("")
        self.stdout.write("=== Drift distribution ===")
        for dc in ('aligned', 'drift', 'no_csv'):
            n = drift_counts[dc]
            pct = 100 * n / total if total else 0
            self.stdout.write(f"  {dc:24s}: {n:4d} ({pct:5.1f}%)")

        # Lookup-path distribution — proves the FK is doing work
        self.stdout.write("")
        self.stdout.write("=== Lookup path (canonical FK vs raw_description fallback) ===")
        for lp in ('fk', 'normalize_desc', 'no_match'):
            n = lookup_path_counts[lp]
            pct = 100 * n / total if total else 0
            self.stdout.write(f"  {lp:24s}: {n:4d} ({pct:5.1f}%)")

        # 2-D matrix
        self.stdout.write("")
        self.stdout.write("=== Math × Drift cross-tab ===")
        self.stdout.write(f"  {'':24s} {'aligned':>9s} {'drift':>9s} {'no_csv':>9s}")
        cross = defaultdict(lambda: Counter())
        for _, mc, _, _, _, _, dc, _ in rows:
            cross[mc][dc] += 1
        for mc in ('math_holds', 'ach_holds', 'parser_suspect', 'qty_anomaly', 'unknown'):
            row = cross[mc]
            self.stdout.write(f"  {mc:24s} {row['aligned']:>9d} {row['drift']:>9d} "
                              f"{row['no_csv']:>9d}")

        # Top drift across ALL math classes (E — broad drift sweep)
        self.stdout.write("")
        self.stdout.write(f"=== Top {opts['show']} drift rows (any math class, "
                          f"|diff%| ≥ {opts['threshold'] * 100:.0f}%) ===")
        threshold_pct = opts['threshold'] * 100
        big_drift = [r for r in rows if r[5] is not None and abs(r[5]) >= threshold_pct]
        big_drift.sort(key=lambda r: -abs(r[5]))
        for ili, mc, real_pu, csv_unit, csv_expected, diff_pct, _, _ in big_drift[:opts['show']]:
            desc = (ili.raw_description or '')[:36]
            prod = (ili.product.canonical_name if ili.product else '(unmapped)')[:18]
            self.stdout.write(
                f"  {ili.invoice_date}  qty={ili.quantity if ili.quantity is not None else '   ?':>5}  up=${ili.unit_price or 0:>6.2f}  "
                f"real=${real_pu:>6.2f}  vs CSV {csv_unit or '?':>10s} "
                f"${csv_expected or 0:>6.2f}  {diff_pct:+7.1f}%  "
                f"[{mc:13s}]  {desc:36s}  → {prod}"
            )

        # Math anomalies (parser_suspect + qty_anomaly), all drift classes
        anomalies = [r for r in rows if r[1] in ('parser_suspect', 'qty_anomaly')]
        if anomalies:
            self.stdout.write("")
            self.stdout.write(f"=== Math anomalies — {len(anomalies)} rows ===")
            self.stdout.write("(parser_suspect: up==ext, qty>1 — possibly fixable)")
            self.stdout.write("(qty_anomaly: ratio ≈ 0.5x/2x/4x — qty captured wrong)")
            for ili, mc, real_pu, csv_unit, csv_expected, diff_pct, dc, _ in sorted(
                    anomalies, key=lambda r: r[1]):
                desc = (ili.raw_description or '')[:38]
                csv_str = (f"CSV {csv_unit:>10s} ${csv_expected:>5.2f} ({diff_pct:+5.1f}%)"
                           if csv_unit else "no CSV match")
                self.stdout.write(
                    f"  [{mc:13s}] {ili.invoice_date} "
                    f"qty={ili.quantity if ili.quantity is not None else '   ?':>5} "
                    f"up=${ili.unit_price or 0:>6.2f} ext=${ili.extended_amount or 0:>6.2f} "
                    f"real=${real_pu or 0:>6.2f}  {csv_str}  {desc}"
                )
