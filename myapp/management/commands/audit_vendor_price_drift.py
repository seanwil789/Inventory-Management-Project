"""Compare recent InvoiceLineItem unit_price against VendorPriceList list price.

Surfaces vendor-pricing anomalies: ILI lines where the actual paid price
diverges from the vendor's quoted list price (with optional ACH-discount
adjustment) by more than a threshold.

For each recent ILI on a given vendor:
  1. Find VendorPriceList entries with matching raw_description (the SKU
     may have multiple unit options — CASE / HALF_CASE / EACH / LB).
  2. Compute expected price for each unit option = list_price * (1 - ach).
  3. Pick the unit option whose expected is closest to the actual.
  4. Classify the diff:
       - aligned:     |diff| < tolerance
       - ach-aligned: diff matches the discount fraction (vendor invoice
                      shows list price; discount lives in extended_amount)
       - off:         diff exceeds tolerance and isn't ACH-explainable
  5. Report counts + sample off lines.

Useful for: catching vendor price increases, parser bugs that mis-extract
unit_price, dual-pricing-tier surprises (Sean ordered LB but invoice shows
case rate or vice versa).

Usage:
    python manage.py audit_vendor_price_drift --vendor "Farm Art"
    python manage.py audit_vendor_price_drift --vendor "Farm Art" --days 14
    python manage.py audit_vendor_price_drift --vendor "Farm Art" --tolerance 0.05
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from myapp.models import Vendor, InvoiceLineItem, VendorPriceList


# Vendor-annotation suffixes the matcher should ignore. Order matters —
# longer patterns first so e.g. "*NO SPLITS*" matches before "*NO SPLIT".
_ANNOTATION_PATTERNS = [
    r'\*+\s*NO\s*SPLITS?\s*\*+',     # *NO SPLITS*, **NO SPLIT**
    r'\*+\s*NO\s*HALF\s*CASES?',     # * NO HALF CASES
    r'\*+\s*NO\s*SPLITS?',           # *NO SPLIT, **NO SPLITS
    r'\*+\s*LOCAL\s*\*?',            # *LOCAL, **LOCAL
    r'"LOCAL"?',                     # "LOCAL (DocAI sometimes adds quotes)
    r'\bNO\s*SPLITS?\b',             # bare "NO SPLIT" with no asterisk
]


def normalize_desc(s):
    """Normalize a vendor description for matching.

    Goal: ILI raw_description (potentially OCR-spaced or carrying
    vendor annotation markers) should normalize to the same string as
    the corresponding VendorPriceList.raw_description from the CSV.

    Steps:
      1. Uppercase
      2. Strip vendor annotation suffixes (*LOCAL, *NO SPLITS, etc.)
      3. Strip whitespace around all punctuation
      4. Collapse multiple spaces, trim
    """
    if not s:
        return ''
    s = s.upper().strip()
    for pat in _ANNOTATION_PATTERNS:
        s = re.sub(pat, '', s, flags=re.IGNORECASE)
    # Strip spaces around punctuation
    s = re.sub(r'\s*([,./#"*()\-])\s*', r'\1', s)
    # Collapse remaining whitespace
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' .,*"')


def _classify(actual, expected_options, tolerance, ach_pct):
    """Return (best_unit, best_expected, diff_pct, classification).

    best_unit          — vendor unit whose expected is closest to actual
    best_expected      — that unit's expected price (list * (1-ach))
    diff_pct           — (actual - best_expected) / best_expected, *100
    classification     — 'aligned' / 'ach_aligned' / 'off' / 'no_match'
    """
    if not expected_options:
        return (None, None, None, 'no_match')
    actual = Decimal(str(actual))
    best = min(expected_options,
               key=lambda u_e: abs(u_e[1] - actual))
    best_unit, best_expected = best
    if best_expected == 0:
        return (best_unit, best_expected, None, 'off')
    diff_pct = (actual - best_expected) / best_expected * 100

    if abs(diff_pct) <= tolerance * 100:
        return (best_unit, best_expected, diff_pct, 'aligned')
    # ACH-aligned: vendor invoice shows list price (no discount baked in);
    # diff_pct ≈ +ach_pct because expected = list * (1 - ach), actual = list.
    ach_diff = ach_pct * 100
    if abs(diff_pct - ach_diff) <= tolerance * 100:
        return (best_unit, best_expected, diff_pct, 'ach_aligned')
    return (best_unit, best_expected, diff_pct, 'off')


class Command(BaseCommand):
    help = "Audit ILI unit_price vs VendorPriceList list price for a vendor."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', required=True,
                            help='Vendor name (e.g. "Farm Art")')
        parser.add_argument('--days', type=int, default=30,
                            help='Look-back window in days (default 30)')
        parser.add_argument('--tolerance', type=float, default=0.02,
                            help='Aligned-band tolerance as decimal (default 0.02 = 2%%)')
        parser.add_argument('--show', type=int, default=20,
                            help='Number of off-by-most lines to surface (default 20)')

    def handle(self, *args, **opts):
        try:
            vendor = Vendor.objects.get(name=opts['vendor'])
        except Vendor.DoesNotExist:
            raise CommandError(f"Vendor not found: {opts['vendor']!r}")

        # Build VendorPriceList lookup keyed on NORMALIZED raw_description
        # so OCR-spaced ILIs ("PEPPERS , RED , 11 # X FANCY") and CSV
        # canonicals ("PEPPERS, RED, 11# X FANCY") collide on the same key.
        price_lookup = defaultdict(list)
        ach_pct = Decimal('0')
        for entry in VendorPriceList.objects.filter(vendor=vendor):
            ach_pct = entry.ach_discount_pct  # assume uniform per vendor
            expected = entry.list_price * (1 - entry.ach_discount_pct)
            price_lookup[normalize_desc(entry.raw_description)].append(
                (entry.unit, expected)
            )

        if not price_lookup:
            raise CommandError(f"No VendorPriceList entries for {vendor.name}. "
                               f"Run import_vendor_price_list first.")

        # Pull recent ILIs
        cutoff = date.today() - timedelta(days=opts['days'])
        ilis = (InvoiceLineItem.objects
                .filter(vendor=vendor, invoice_date__gte=cutoff,
                        unit_price__isnull=False)
                .exclude(unit_price=0)
                .order_by('-invoice_date'))

        # Classify
        buckets = {'aligned': [], 'ach_aligned': [], 'off': [], 'no_match': []}
        for ili in ilis:
            opts_for_ili = price_lookup.get(normalize_desc(ili.raw_description), [])
            best_unit, best_expected, diff_pct, cls = _classify(
                ili.unit_price, opts_for_ili,
                opts['tolerance'], ach_pct,
            )
            buckets[cls].append((ili, best_unit, best_expected, diff_pct))

        total = len(ilis)

        # Report
        self.stdout.write(f"\nVendor:           {vendor.name}")
        self.stdout.write(f"Window:           last {opts['days']} days")
        self.stdout.write(f"VendorPriceList entries: {sum(len(v) for v in price_lookup.values())} "
                          f"across {len(price_lookup)} distinct SKU descriptions")
        self.stdout.write(f"ILIs analyzed:    {total}")
        self.stdout.write(f"Tolerance:        ±{opts['tolerance'] * 100:.1f}%")
        self.stdout.write("")
        self.stdout.write(f"  aligned         : {len(buckets['aligned'])} "
                          f"({100 * len(buckets['aligned']) / total if total else 0:.1f}%)")
        self.stdout.write(f"  ach_aligned     : {len(buckets['ach_aligned'])} "
                          f"({100 * len(buckets['ach_aligned']) / total if total else 0:.1f}%)")
        self.stdout.write(f"  off (drift)     : {len(buckets['off'])} "
                          f"({100 * len(buckets['off']) / total if total else 0:.1f}%)")
        self.stdout.write(f"  no SKU in list  : {len(buckets['no_match'])} "
                          f"({100 * len(buckets['no_match']) / total if total else 0:.1f}%)")
        self.stdout.write("")

        if buckets['off']:
            self.stdout.write(f"=== Top {opts['show']} 'off' rows by abs drift% ===")
            off_sorted = sorted(buckets['off'],
                                key=lambda r: -abs(r[3] or 0))[:opts['show']]
            for ili, unit, expected, diff in off_sorted:
                desc = (ili.raw_description or '')[:42]
                prod = (ili.product.canonical_name if ili.product else '(unmapped)')[:24]
                self.stdout.write(
                    f"  {ili.invoice_date}  ${ili.unit_price:>7.2f}  "
                    f"vs ${expected:>7.2f} ({unit:>10s})  {diff:+7.1f}%  "
                    f"{desc:42s}  → {prod}"
                )

        if buckets['no_match']:
            self.stdout.write(f"\n=== Sample 'no SKU in list' (raw_descriptions not in price list) ===")
            seen = set()
            shown = 0
            for ili, _, _, _ in buckets['no_match']:
                if ili.raw_description in seen:
                    continue
                seen.add(ili.raw_description)
                self.stdout.write(f"  {ili.raw_description}")
                shown += 1
                if shown >= 10:
                    self.stdout.write(f"  ... +{len(seen) - shown if len(seen) > shown else 0} more distinct")
                    break
