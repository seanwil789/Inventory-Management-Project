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
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from invoice_processor.canonical_match import (
    tokenize as _tokenize,
    jaccard as _jaccard,
    find_canonical_match,
    build_candidate_index,
)
from myapp.models import InvoiceLineItem, Vendor, VendorPriceList


class Command(BaseCommand):
    help = "Backfill canonical_vendor_pricelist FK on existing ILIs via fuzzy match"

    def add_arguments(self, parser):
        parser.add_argument("--vendor", default=None,
                            help="Restrict to a single vendor (default: all vendors with VPL)")
        parser.add_argument("--threshold", type=float, default=0.65,
                            help="Auto-attach Jaccard threshold (default 0.65). "
                                 "Empirical sampling on Pi 2026-05-06 showed 0.55-0.65 "
                                 "band has ~40%% false-positive rate (size/format "
                                 "discriminator failures); 0.65+ has ~99%% accuracy.")
        parser.add_argument("--review-threshold", type=float, default=0.55,
                            help="Borderline floor (default 0.55). Matches between "
                                 "review-threshold and threshold are surfaced as a "
                                 "review queue, not auto-attached.")
        parser.add_argument("--apply", action="store_true",
                            help="Apply changes (default: dry-run, no DB writes)")
        parser.add_argument("--reset", action="store_true",
                            help="Re-run on already-FK-attached ILIs (default: skip them)")

    def handle(self, *args, **opts):
        threshold = opts["threshold"]
        review_threshold = opts["review_threshold"]
        if review_threshold > threshold:
            self.stdout.write(self.style.ERROR(
                "--review-threshold must be <= --threshold"))
            return
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
        self.stdout.write(
            f"Backfill canonical FK [{mode}] auto-attach >= {threshold:.2f}, "
            f"review-queue {review_threshold:.2f}-{threshold:.2f}")
        self.stdout.write("")
        self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>8} {:>8} {:>8}".format(
            "vendor", "ILIs", "VPL", "skipped", "attached", "review", "no-match"))
        self.stdout.write("-" * 88)

        global_attached = global_review = global_no_match = global_skipped = 0
        per_vendor_review_samples = []
        per_vendor_unmatched_samples = []

        for vendor in vendors:
            candidates = build_candidate_index(vendor)
            if not candidates:
                continue
            vpl_qs = [c[0] for c in candidates]

            ilis_qs = InvoiceLineItem.objects.filter(vendor=vendor)
            if not opts["reset"]:
                ilis_qs = ilis_qs.filter(canonical_vendor_pricelist__isnull=True)
            ilis = list(ilis_qs)

            attached_count = review_count = no_match_count = skipped_count = 0
            review_samples = []
            unmatched_samples = []

            with transaction.atomic():
                for ili in ilis:
                    toks = _tokenize(ili.raw_description or "")
                    if len(toks) < 2:
                        skipped_count += 1
                        continue
                    # Use review_threshold as the lower floor for any candidate
                    best_vpl, score = find_canonical_match(
                        ili.raw_description or "", candidates, review_threshold)
                    if best_vpl is None:
                        no_match_count += 1
                        if len(unmatched_samples) < 5:
                            unmatched_samples.append(
                                (ili.raw_description or "", score))
                        continue
                    if score < threshold:
                        # Borderline: surface for review, do NOT auto-attach
                        review_count += 1
                        if len(review_samples) < 10:
                            review_samples.append(
                                (ili, best_vpl, score))
                        continue
                    attached_count += 1
                    if apply_changes:
                        ili.canonical_vendor_pricelist = best_vpl
                        ili.save(update_fields=["canonical_vendor_pricelist"])
                if not apply_changes:
                    transaction.set_rollback(True)

            self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>8} {:>8} {:>8}".format(
                vendor.name[:33],
                InvoiceLineItem.objects.filter(vendor=vendor).count(),
                len(vpl_qs), skipped_count, attached_count, review_count, no_match_count))

            global_attached += attached_count
            global_review += review_count
            global_no_match += no_match_count
            global_skipped += skipped_count
            if review_samples:
                per_vendor_review_samples.append((vendor.name, review_samples))
            if unmatched_samples:
                per_vendor_unmatched_samples.append((vendor.name, unmatched_samples))

        self.stdout.write("-" * 88)
        self.stdout.write("{:<35} {:>6} {:>6} {:>8} {:>8} {:>8} {:>8}".format(
            "TOTAL", "—", "—", global_skipped, global_attached, global_review,
            global_no_match))

        # Borderline review queue — these score in [review_threshold, threshold).
        # Likely correct but need human verification (size/format discriminator
        # failures cluster here, e.g. PEPPERS RED 15# vs catalog 11#).
        if per_vendor_review_samples:
            self.stdout.write("")
            self.stdout.write(
                f"REVIEW QUEUE (score {review_threshold:.2f}-{threshold:.2f}, "
                f"NOT auto-attached):")
            for vname, samples in per_vendor_review_samples[:5]:
                self.stdout.write(f"  [{vname}]")
                for ili, vpl, score in samples:
                    self.stdout.write(
                        f"    score={score:.3f}  ILI: "
                        f"{(ili.raw_description or '')[:60]}")
                    self.stdout.write(
                        f"                 catalog: "
                        f"{(vpl.raw_description or '')[:60]}  (sku={vpl.sku})")

        # Below review_threshold — no plausible canonical, queue for mapping-review.
        if per_vendor_unmatched_samples:
            self.stdout.write("")
            self.stdout.write(
                f"Sample unmatched ILIs (score < {review_threshold:.2f}, "
                f"queue for mapping-review):")
            for vname, samples in per_vendor_unmatched_samples[:5]:
                self.stdout.write(f"  [{vname}]")
                for raw, score in samples:
                    self.stdout.write(f"    best={score:.3f}  {raw[:80]}")

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "DRY RUN — no changes written. Re-run with --apply to persist."))
