"""Quantify photo tilt per OCR-cached invoice.

Tilt is the y-coordinate offset between same-row tokens that lie on
opposite sides of the page (left-edge qty column vs right-edge price
column). On untilted scans, tokens at the same row have ≈same y across
the page width. On tilted phone photos, the y of the right column
shifts up or down vs the left column — by enough to push prices into
the wrong y-cluster (`feedback_methodologies.md` #11 / spatial drift).

Method:
  1. For each OCR cache file: walk its pages[].tokens
  2. Identify left-band tokens (text matches qty pattern e.g. "1.000",
     x-band is configurable per-vendor, default Farm Art's 0.04-0.16)
  3. Identify right-band tokens (text matches price pattern, x ≥ 0.7)
  4. Pair them by y-rank (1st left with 1st right, etc.) — assumes both
     lists are 1:1 with rows, which holds for full invoices
  5. Compute median (y_right - y_left). Distribute and report.

Output: per-invoice tilt metric + summary stats. Read-only, no
extraction or DB writes.

Usage:
    python manage.py audit_invoice_tilt
    python manage.py audit_invoice_tilt --threshold 0.005 --show 20
"""
import json
import re
import statistics
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings


_QTY_RE = re.compile(r'^\d+\.\d{3}$')           # 1.000, 2.000, ...
_PRICE_RE = re.compile(r'^\$?\d+\.\d{2,4}$')   # 4.70, 40.49, ...


def _y_mid(t):
    return (t["y_min"] + t["y_max"]) / 2


def estimate_tilt(tokens, qty_x_band=(0.02, 0.18), price_x_min=0.70):
    """Return (tilt_offset, n_pairs_used). offset > 0 means right column
    is BELOW left column (price y is higher than its same-row qty y).
    None if not enough data."""
    qty_toks = [
        t for t in tokens
        if qty_x_band[0] <= ((t["x_min"] + t["x_max"]) / 2) <= qty_x_band[1]
        and _QTY_RE.fullmatch(t.get("text") or "")
    ]
    price_toks = [
        t for t in tokens
        if ((t["x_min"] + t["x_max"]) / 2) >= price_x_min
        and _PRICE_RE.fullmatch(t.get("text") or "")
    ]
    qty_toks.sort(key=_y_mid)
    price_toks.sort(key=_y_mid)
    if len(qty_toks) < 3 or len(price_toks) < 3:
        return (None, 0)
    n = min(len(qty_toks), len(price_toks))
    diffs = [_y_mid(price_toks[i]) - _y_mid(qty_toks[i]) for i in range(n)]
    return (statistics.median(diffs), n)


class Command(BaseCommand):
    help = "Measure photo tilt per OCR-cached invoice (read-only)."

    def add_arguments(self, parser):
        parser.add_argument('--cache-dir', default=None,
                            help='OCR cache dir (default: <repo>/.ocr_cache/)')
        parser.add_argument('--threshold', type=float, default=0.005,
                            help='Flag invoices with |tilt| > threshold (default 0.005)')
        parser.add_argument('--show', type=int, default=15,
                            help='Show top N most-tilted invoices')
        parser.add_argument('--vendor', default=None,
                            help='Filter to invoices with this vendor name in cache')

    def handle(self, *args, **opts):
        cache_dir = (Path(opts['cache_dir']) if opts['cache_dir']
                     else Path(settings.BASE_DIR) / '.ocr_cache')
        if not cache_dir.is_dir():
            self.stdout.write(self.style.ERROR(
                f'Cache dir not found: {cache_dir}'))
            return

        ocr_files = list(cache_dir.glob('*_docai_ocr.json'))
        self.stdout.write(f"\nOCR cache: {cache_dir}")
        self.stdout.write(f"Files:     {len(ocr_files)}")
        self.stdout.write(f"Threshold: |tilt| > {opts['threshold']}\n")

        per_invoice = []  # (filename, vendor, date, tilt, n_pairs)
        for cf in ocr_files:
            try:
                data = json.loads(cf.read_text())
            except Exception:
                continue
            vendor = data.get('vendor', '?')
            if opts['vendor'] and opts['vendor'].lower() not in vendor.lower():
                continue
            inv_date = data.get('invoice_date', '?')
            pages = data.get('pages', []) or []
            tokens = []
            for page in pages:
                tokens.extend(page.get('tokens') or [])
            tilt, n = estimate_tilt(tokens)
            if tilt is None:
                continue
            per_invoice.append((cf.name, vendor, inv_date, tilt, n))

        if not per_invoice:
            self.stdout.write("No usable invoices found.")
            return

        tilts = [p[3] for p in per_invoice]
        self.stdout.write(f"Usable invoices: {len(per_invoice)}")
        self.stdout.write(f"Tilt distribution:")
        self.stdout.write(f"  min:    {min(tilts):+.5f}")
        self.stdout.write(f"  median: {statistics.median(tilts):+.5f}")
        self.stdout.write(f"  max:    {max(tilts):+.5f}")
        self.stdout.write(f"  stdev:  {statistics.stdev(tilts):.5f}" if len(tilts) > 1 else "")

        flagged = [p for p in per_invoice if abs(p[3]) > opts['threshold']]
        self.stdout.write(f"\nFlagged (|tilt| > {opts['threshold']}): "
                          f"{len(flagged)} ({100*len(flagged)/len(per_invoice):.1f}%)")
        self.stdout.write("")

        # Per-vendor breakdown
        from collections import defaultdict
        by_v = defaultdict(list)
        for _, vendor, _, tilt, _ in per_invoice:
            by_v[vendor].append(tilt)
        self.stdout.write(f"{'Vendor':30s} {'n':>5s} {'median':>10s} "
                          f"{'max abs':>10s} {'>thresh':>8s}")
        self.stdout.write("-" * 70)
        for vendor in sorted(by_v):
            ts = by_v[vendor]
            n_flag = sum(1 for t in ts if abs(t) > opts['threshold'])
            self.stdout.write(
                f"{vendor:30s} {len(ts):>5d} {statistics.median(ts):>+10.5f} "
                f"{max(abs(t) for t in ts):>10.5f} {n_flag:>8d}"
            )

        self.stdout.write("")
        self.stdout.write(f"Top {opts['show']} most-tilted invoices:")
        per_invoice.sort(key=lambda r: -abs(r[3]))
        for fn, v, d, tilt, n in per_invoice[:opts['show']]:
            self.stdout.write(
                f"  {v:25s} {d}  tilt={tilt:+.5f}  pairs={n:3d}  {fn[:32]}"
            )
