"""Phase A shadow extraction: run rank-pair v2 against cached invoices,
compare to current ILI state. Output diff metrics. NO DB WRITES.

For each OCR-cached invoice (Farm Art only in this iteration):
  1. Run `rank_pair.extract_farmart_rank` → "truth" rows
  2. Pull ILIs currently persisted from this source_file
  3. Categorize each DB ILI:
       CLEAN          — DB description and qty/unit match the SAME rank-pair row
       DRIFT_CASCADE  — DB description matches one row but qty/unit match a different row
       NO_PAIR        — description matches a rank-pair row but qty/unit don't match any
       NO_MATCH       — DB ILI has no plausible rank-pair counterpart

Surfaces:
  - Per-invoice rank-pair row count, ACH math pass rate, ambiguous-row count
  - Per-invoice DB ILI categorization counts
  - Cross-invoice aggregates (totals + drift cascade rate)

Validates the algorithm before any production code path swap. Read-only against
the .ocr_cache directory + ILI table; never writes.

Usage:
    python manage.py audit_rank_pair_shadow
    python manage.py audit_rank_pair_shadow --vendor "Farm Art" --limit 10
    python manage.py audit_rank_pair_shadow --hash 602972db --verbose
"""
import json
import re
from pathlib import Path
from collections import Counter
from statistics import median

from django.conf import settings
from django.core.management.base import BaseCommand

from invoice_processor.rank_pair import (
    detect_layout_farmart,
    extract_farmart_rank,
    detect_layout_sysco,
    extract_sysco_rank,
    diagnostic_summary,
    _x_mid,
    _y_mid,
    _QTY_RE,
    _PRICE_RE,
)


_VENDOR_DISPATCH = {
    "Farm Art": (detect_layout_farmart, extract_farmart_rank),
    "FarmArt": (detect_layout_farmart, extract_farmart_rank),
    "Sysco": (detect_layout_sysco, extract_sysco_rank),
}
from myapp.models import Vendor, InvoiceLineItem

_DESC_TOKEN_RE = re.compile(r"[A-Z]+")


def _jaccard(a_str: str, b_str: str) -> float:
    a = set(_DESC_TOKEN_RE.findall((a_str or "").upper()))
    b = set(_DESC_TOKEN_RE.findall((b_str or "").upper()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _categorize(ili, rp_rows: list[dict]) -> tuple[str, int | None, int | None]:
    """Match DB ILI against rank-pair truth rows.

    Returns (category, desc_match_idx, pair_match_idx).
    """
    if not rp_rows:
        return ("NO_MATCH", None, None)

    # Sysco rank-pair rows use 'quantity' field; Farm Art uses 'qty'
    def _rp_qty(rp):
        if 'qty' in rp:
            return float(rp.get('qty') or 0)
        return float(rp.get('quantity') or 0)

    db_qty = float(ili.quantity or 0)
    db_unit = float(ili.unit_price or 0)
    db_desc = ili.raw_description or ""

    best_desc_idx, best_desc_score = None, 0.0
    for i, rp in enumerate(rp_rows):
        s = _jaccard(db_desc, rp.get("raw_description", ""))
        if s > best_desc_score:
            best_desc_score = s
            best_desc_idx = i

    best_pair_idx = None
    for i, rp in enumerate(rp_rows):
        if (abs(_rp_qty(rp) - db_qty) < 0.01
                and abs(rp["unit_price"] - db_unit) < 0.01):
            best_pair_idx = i
            break

    if best_desc_score < 0.3:
        return ("NO_MATCH", None, None)
    if best_pair_idx is None:
        return ("NO_PAIR", best_desc_idx, None)
    if best_desc_idx == best_pair_idx:
        return ("CLEAN", best_desc_idx, best_pair_idx)
    return ("DRIFT_CASCADE", best_desc_idx, best_pair_idx)


def _compute_tilt(tokens: list[dict], cfg: dict) -> float | None:
    # Sysco layout has no qty_x band — return None for tilt.
    if "qty_x" not in cfg or "unit_x" not in cfg:
        return None

    qtys = sorted(
        [t for t in tokens
         if cfg["qty_x"][0] <= _x_mid(t) <= cfg["qty_x"][1]
         and _QTY_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    units = sorted(
        [t for t in tokens
         if cfg["unit_x"][0] <= _x_mid(t) <= cfg["unit_x"][1]
         and _PRICE_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    n = min(len(qtys), len(units))
    if n == 0:
        return None
    tilts = [_y_mid(units[i]) - _y_mid(qtys[i]) for i in range(n)]
    return median(tilts)


class Command(BaseCommand):
    help = "Shadow rank-pair v2 extraction across cached invoices vs current ILI state"

    def add_arguments(self, parser):
        parser.add_argument("--vendor", default="Farm Art",
                            help="Vendor name (Farm Art or Sysco)")
        parser.add_argument("--hash", default=None,
                            help="Restrict to a single source_file hash prefix")
        parser.add_argument("--limit", type=int, default=None,
                            help="Process at most N invoices (debug)")
        parser.add_argument("--verbose", action="store_true",
                            help="Print per-invoice details")
        parser.add_argument("--cache-dir", default=None,
                            help=f"OCR cache dir (default: <BASE_DIR>/.ocr_cache/)")

    def handle(self, *args, **opts):
        cache_dir = (Path(opts["cache_dir"]) if opts["cache_dir"]
                     else Path(settings.BASE_DIR) / ".ocr_cache")
        vendor_name = opts["vendor"]
        if vendor_name not in _VENDOR_DISPATCH:
            self.stdout.write(self.style.WARNING(
                f"Vendor '{vendor_name}' not supported. Available: "
                f"{sorted(_VENDOR_DISPATCH.keys())}"))
            return
        detect_layout_fn, extract_rank_fn = _VENDOR_DISPATCH[vendor_name]
        try:
            vendor = Vendor.objects.get(name=vendor_name)
        except Vendor.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Vendor '{vendor_name}' not found"))
            return

        cache_files = sorted(cache_dir.glob("*_docai_ocr.json"))
        if opts["hash"]:
            cache_files = [c for c in cache_files if c.name.startswith(opts["hash"])]
        if opts["limit"]:
            cache_files = cache_files[:opts["limit"]]

        agg = Counter()
        db_categories = Counter()
        ach_total = Counter()
        per_invoice_results = []

        if opts["verbose"]:
            self.stdout.write(
                "{:<14}{:<12}{:>5} {:>3} {:>3} {:>8} {:>5} {:>5} {:>4} {:>5} {:>5} {:>4}".format(
                    "hash", "date", "toks", "RP", "DB", "tilt", "ACH+", "ACH-", "amb",
                    "clean", "drift", "misc"))
            self.stdout.write("-" * 95)

        for cf in cache_files:
            try:
                data = json.loads(cf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("vendor") != vendor_name:
                continue

            agg["files_seen"] += 1
            pages = data.get("pages") or []
            tokens = []
            for p in pages:
                tokens.extend(p.get("tokens") or [])
            if not tokens:
                agg["files_no_layout"] += 1
                continue

            cfg = detect_layout_fn(tokens)
            if cfg is None:
                agg["files_no_layout"] += 1
                continue
            agg["files_with_layout"] += 1

            rp_rows = extract_rank_fn(pages)
            summary = diagnostic_summary(rp_rows)
            agg["rp_rows_total"] += summary["row_count"]
            ach_total["pass"] += summary["ach_pass"]
            ach_total["fail"] += summary["ach_fail"]
            ach_total["no_ext"] += summary["ach_no_ext"]
            ach_total["ambiguous"] += summary["ambiguous"]

            tilt = _compute_tilt(tokens, cfg)

            h = cf.name.split("_")[0][:12]
            db_ilis = list(InvoiceLineItem.objects.filter(
                vendor=vendor, source_file__startswith=h).order_by("id"))
            cats = Counter()
            for ili in db_ilis:
                cat, _, _ = _categorize(ili, rp_rows)
                cats[cat] += 1
                db_categories[cat] += 1

            per_invoice_results.append({
                "hash": h,
                "date": str(data.get("invoice_date") or ""),
                "tokens": len(tokens),
                "rp_rows": summary["row_count"],
                "db_ilis": len(db_ilis),
                "tilt": tilt,
                "ach_pass": summary["ach_pass"],
                "ach_fail": summary["ach_fail"],
                "ambiguous": summary["ambiguous"],
                "categories": cats,
            })

            if opts["verbose"]:
                tilt_str = format(tilt, "+.4f") if tilt is not None else "n/a"
                misc = cats["NO_MATCH"] + cats["NO_PAIR"]
                self.stdout.write(
                    "{:<14}{:<12}{:>5} {:>3} {:>3} {:>8} {:>5} {:>5} {:>4} {:>5} {:>5} {:>4}".format(
                        h, str(data.get("invoice_date") or ""),
                        len(tokens), summary["row_count"], len(db_ilis), tilt_str,
                        summary["ach_pass"], summary["ach_fail"], summary["ambiguous"],
                        cats["CLEAN"], cats["DRIFT_CASCADE"], misc))

        # Summary
        self.stdout.write("")
        self.stdout.write("=" * 95)
        self.stdout.write(f"Vendor                : {vendor_name}")
        self.stdout.write(f"Files seen            : {agg['files_seen']}")
        self.stdout.write(f"Files with layout     : {agg['files_with_layout']}")
        self.stdout.write(f"Files no layout       : {agg['files_no_layout']}")
        self.stdout.write(f"Total RP rows         : {agg['rp_rows_total']}")
        self.stdout.write(f"ACH math: pass        : {ach_total['pass']}")
        self.stdout.write(f"ACH math: fail        : {ach_total['fail']}")
        self.stdout.write(f"ACH math: no ext      : {ach_total['no_ext']}")
        self.stdout.write(f"Ambiguous rows        : {ach_total['ambiguous']}")
        if agg["rp_rows_total"] > 0:
            ach_rate = ach_total["pass"] / agg["rp_rows_total"]
            self.stdout.write(f"ACH math pass rate    : {ach_rate:.1%}")
            amb_rate = ach_total["ambiguous"] / agg["rp_rows_total"]
            self.stdout.write(f"Ambiguity rate        : {amb_rate:.1%}")
        self.stdout.write("")
        self.stdout.write("DB ILI categorization:")
        total_db = sum(db_categories.values())
        for cat in ["CLEAN", "DRIFT_CASCADE", "NO_PAIR", "NO_MATCH"]:
            n = db_categories[cat]
            pct = (n / total_db * 100) if total_db else 0
            self.stdout.write(f"  {cat:<18} : {n:>5}  ({pct:.1f}%)")
        self.stdout.write(f"  {'TOTAL':<18} : {total_db:>5}")
