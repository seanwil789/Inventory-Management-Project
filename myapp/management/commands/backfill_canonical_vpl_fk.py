"""Backfill `canonical_vendor_pricelist` FK on existing InvoiceLineItem rows.

For each ILI with vendor.price_list_entries.count() > 0, fuzzy-match the ILI's
raw_description against VendorPriceList[vendor] entries at Jaccard t=0.55.
Match → attach FK. No match → leave null (surfaces in mapping-review queue
later).

Threshold validated empirically (`project_self_healing_raw_descriptions.md`):
  - t=0.55 captures whitespace/annotation variants without false-merging
    distinct items on single-run invoices
  - 96% backfill rate observed on Pi dry-run (2,747 of 2,857 ILIs got candidates)
  - Singletons / very-short descriptions remain null and queue for review

Pricing-as-event-driven LAW (`feedback_event_driven_pricing.md`):
  - Backfill ONLY assigns the FK identity pointer.
  - ILI price/qty/ext fields are NEVER modified by this command.

Usage:
    python manage.py backfill_canonical_vpl_fk --dry-run
    python manage.py backfill_canonical_vpl_fk --vendor "Farm Art" --apply
    python manage.py backfill_canonical_vpl_fk --apply --threshold 0.55
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import InvoiceLineItem, Vendor, VendorPriceList


_TOKEN_RE = re.compile(r"[A-Z][A-Z]+|\d+(?:[/.,-]\d+)*%?")

# Normalize whitespace inside compound tokens BEFORE tokenizing. Without this,
# parser variants like "4 / 1 - GAL" tokenize to {"4", "1", "GAL"} while the
# catalog's "4/1-GAL" tokenizes to {"4/1-GAL", "GAL"} — they share only "GAL"
# and Jaccard collapses despite identical semantic content. Empirical inspection
# on Pi (2026-05-06) showed 38+ of 82 unmatched ILIs were this false-negative
# pattern.
#
# Strategy: any '/' or '-' surrounded by whitespace AND alphanumeric on both
# sides gets its whitespace collapsed. Iterate until stable to handle chained
# patterns like "1-1 / 9 - LB" → "1-1/9-LB".
# Lookbehind/lookahead so word chars stay unconsumed — adjacent matches don't
# steal each other's anchors. Pattern: word boundary, optional space, operator,
# optional space, word boundary. Replacement collapses the spaces but leaves
# the surrounding word chars in place.
_OPERATOR_SPACE_RE = re.compile(r"(?<=\w)\s+([/-])\s*(?=\w)|(?<=\w)\s*([/-])\s+(?=\w)")
_PERCENT_SPACE_RE = re.compile(r"(\d+)\s+%")  # "2 %" → "2%"


def _normalize_tokens(s: str) -> str:
    """Collapse whitespace inside numeric/compound tokens for stable matching.

    Lookaround (lookbehind/lookahead) means adjacent matches don't share
    consumed characters, so chained patterns like "1-1 / 9 - LB" collapse in a
    single pass instead of needing iteration.
    """
    if not s:
        return ""
    # Either capture group is non-empty depending on which alternation matched
    out = _OPERATOR_SPACE_RE.sub(lambda m: m.group(1) or m.group(2), s)
    out = _PERCENT_SPACE_RE.sub(r"\1%", out)
    return out


def _tokenize(s: str) -> frozenset:
    if not s:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(_normalize_tokens(s).upper()))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_match(ili_tokens: frozenset, candidates: list[tuple[VendorPriceList, frozenset]],
               threshold: float) -> tuple[VendorPriceList | None, float]:
    """Return (best_vpl, score) above threshold, or (None, best_score_seen)."""
    best_vpl, best_s = None, 0.0
    for vpl, vpl_tokens in candidates:
        s = _jaccard(ili_tokens, vpl_tokens)
        if s > best_s:
            best_s = s
            best_vpl = vpl
    if best_s < threshold:
        return (None, best_s)
    return (best_vpl, best_s)


class Command(BaseCommand):
    help = "Backfill canonical_vendor_pricelist FK on existing ILIs via fuzzy match"

    def add_arguments(self, parser):
        parser.add_argument("--vendor", default=None,
                            help="Restrict to a single vendor (default: all vendors with VPL)")
        parser.add_argument("--threshold", type=float, default=0.55,
                            help="Jaccard threshold for FK attach (default 0.55)")
        parser.add_argument("--apply", action="store_true",
                            help="Apply changes (default: dry-run, no DB writes)")
        parser.add_argument("--reset", action="store_true",
                            help="Re-run on already-FK-attached ILIs (default: skip them)")

    def handle(self, *args, **opts):
        threshold = opts["threshold"]
        apply_changes = opts["apply"]

        if opts["vendor"]:
            try:
                vendors = [Vendor.objects.get(name=opts["vendor"])]
            except Vendor.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Vendor '{opts['vendor']}' not found"))
                return
        else:
            # Only vendors that have VendorPriceList entries can do anything useful
            vendors = list(Vendor.objects.filter(price_list_entries__isnull=False).distinct())

        mode = "APPLY" if apply_changes else "DRY RUN"
        self.stdout.write(f"Backfill canonical FK [{mode}] threshold={threshold:.2f}")
        self.stdout.write("")
        self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>9} {:>9}".format(
            "vendor", "ILIs", "VPL", "skipped", "matched", "no-match"))
        self.stdout.write("-" * 80)

        global_matched = global_no_match = global_skipped = 0
        per_vendor_unmatched_samples = []

        for vendor in vendors:
            vpl_qs = list(VendorPriceList.objects.filter(vendor=vendor))
            if not vpl_qs:
                continue
            candidates = [(vpl, _tokenize(vpl.raw_description)) for vpl in vpl_qs]

            ilis_qs = InvoiceLineItem.objects.filter(vendor=vendor)
            if not opts["reset"]:
                ilis_qs = ilis_qs.filter(canonical_vendor_pricelist__isnull=True)
            ilis = list(ilis_qs)

            matched_count = no_match_count = skipped_count = 0
            unmatched_samples = []

            with transaction.atomic():
                for ili in ilis:
                    toks = _tokenize(ili.raw_description or "")
                    if len(toks) < 2:
                        skipped_count += 1
                        continue
                    best_vpl, score = best_match(toks, candidates, threshold)
                    if best_vpl is None:
                        no_match_count += 1
                        if len(unmatched_samples) < 5:
                            unmatched_samples.append(
                                (ili.raw_description or "", score))
                        continue
                    matched_count += 1
                    if apply_changes:
                        ili.canonical_vendor_pricelist = best_vpl
                        ili.save(update_fields=["canonical_vendor_pricelist"])
                if not apply_changes:
                    transaction.set_rollback(True)

            self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>9} {:>9}".format(
                vendor.name[:33],
                InvoiceLineItem.objects.filter(vendor=vendor).count(),
                len(vpl_qs), skipped_count, matched_count, no_match_count))

            global_matched += matched_count
            global_no_match += no_match_count
            global_skipped += skipped_count
            if unmatched_samples:
                per_vendor_unmatched_samples.append((vendor.name, unmatched_samples))

        self.stdout.write("-" * 80)
        self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>9} {:>9}".format(
            "TOTAL", "—", "—", global_skipped, global_matched, global_no_match))

        # Show unmatched samples per vendor — these need attention
        if per_vendor_unmatched_samples:
            self.stdout.write("")
            self.stdout.write("Sample unmatched ILIs (would queue for mapping-review):")
            for vname, samples in per_vendor_unmatched_samples[:5]:
                self.stdout.write(f"  [{vname}]")
                for raw, score in samples:
                    self.stdout.write(f"    best={score:.3f}  {raw[:80]}")

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "DRY RUN — no changes written. Re-run with --apply to persist."))
