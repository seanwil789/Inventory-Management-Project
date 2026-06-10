"""
Match canonical Products to USDA FoodData Central → populate per-100g macros.

DRY-RUN by default — prints what it *would* do. --apply writes only the
high-confidence AUTO tier; everything below the bar is left unmatched and
reported as the review queue (never auto-written — confident-wrong bulk
write is the failure mode we're guarding against).

Tiers:
  AUTO   (score >= --min-auto)   -> write fdc_id + macros, confidence='auto'
  REVIEW (--min-review .. auto)  -> reported only; your call later
  NONE   (below / no FDC hit)    -> reported only

Examples:
  manage.py match_products_to_fdc --sample 25          # dry-run a random 25
  manage.py match_products_to_fdc --apply              # write all AUTO matches
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from myapp.models import Product
from invoice_processor import fdc_match

NONFOOD = {"Smallwares", "Chemicals", "Pseudo"}
MACRO_FIELDS = ["kcal_per_100g", "protein_g_per_100g", "fat_g_per_100g",
                "carb_g_per_100g", "fiber_g_per_100g"]


def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(round(float(v), 2)))
    except (InvalidOperation, ValueError, TypeError):
        return None


class Command(BaseCommand):
    help = "Match canonical Products to USDA FDC and populate per-100g macros."

    def add_arguments(self, p):
        p.add_argument("--apply", action="store_true", help="Write AUTO-tier matches (default: dry-run).")
        p.add_argument("--sample", type=int, default=0, help="Process a random N (dry-run calibration).")
        p.add_argument("--limit", type=int, default=0, help="Process at most N (ordered).")
        p.add_argument("--min-auto", type=float, default=0.85, help="Auto-apply threshold (default 0.85).")
        p.add_argument("--min-review", type=float, default=0.55, help="Review-queue floor (default 0.55).")
        p.add_argument("--refresh", action="store_true", help="Re-match products already 'auto' (never 'reviewed').")

    def handle(self, *a, **o):
        qs = Product.objects.exclude(category__in=NONFOOD).exclude(nutrition_confidence="reviewed")
        if not o["refresh"]:
            qs = qs.filter(nutrition_confidence="")          # skip already-matched
        qs = qs.order_by("canonical_name")
        if o["sample"]:
            qs = qs.order_by("?")[:o["sample"]]
        elif o["limit"]:
            qs = qs[:o["limit"]]
        products = list(qs)

        mode = "APPLY" if o["apply"] else "DRY-RUN"
        self.stdout.write(f"=== match_products_to_fdc [{mode}] — {len(products)} products "
                          f"(auto>={o['min_auto']}, review>={o['min_review']}) ===")

        tally = {"AUTO": 0, "REVIEW": 0, "NONE": 0}
        applied = 0
        cleared = 0
        for p in products:
            try:
                best = fdc_match.match_product(p.canonical_name)
            except Exception as e:
                self.stdout.write(f"  [ERROR ] {p.canonical_name!r}  ({str(e)[:70]})")
                tally["NONE"] += 1
                continue
            if best is None or best.get("score", 0) < o["min_review"]:
                tier = "NONE"
                self.stdout.write(f"  [NONE  ] {p.canonical_name!r}  (no usable FDC match)")
            else:
                tier = "AUTO" if best["score"] >= o["min_auto"] else "REVIEW"
                m = best.get("macros", {})
                self.stdout.write(
                    f"  [{tier:6}] {p.canonical_name!r}  ->  {best['description']!r} "
                    f"[{best['data_type']} #{best['fdc_id']}] score={best['score']} "
                    f"| kcal={m.get('kcal_per_100g')} prot={m.get('protein_g_per_100g')}")
                if tier == "AUTO" and o["apply"]:
                    p.fdc_id = str(best["fdc_id"])
                    for f in MACRO_FIELDS:
                        setattr(p, f, _dec(m.get(f)))
                    p.nutrition_confidence = "auto"
                    p.save(update_fields=["fdc_id", "nutrition_confidence"] + MACRO_FIELDS)
                    applied += 1
            # Self-correcting refresh: if a previously-auto product no longer
            # clears AUTO, clear its stale macros back to unmatched.
            if o["apply"] and tier != "AUTO" and p.nutrition_confidence == "auto":
                p.fdc_id = ""
                for f in MACRO_FIELDS:
                    setattr(p, f, None)
                p.nutrition_confidence = ""
                p.save(update_fields=["fdc_id", "nutrition_confidence"] + MACRO_FIELDS)
                self.stdout.write(f"           ^ cleared stale auto-match (now {tier})")
                cleared += 1
            tally[tier] += 1

        self.stdout.write(f"--- {tally['AUTO']} AUTO / {tally['REVIEW']} REVIEW / {tally['NONE']} NONE ---")
        if o["apply"]:
            self.stdout.write(self.style.SUCCESS(
                f"Applied {applied} AUTO matches" + (f"; cleared {cleared} stale." if cleared else ".")))
        else:
            self.stdout.write("Dry-run — nothing written. Re-run with --apply to write AUTO tier.")
