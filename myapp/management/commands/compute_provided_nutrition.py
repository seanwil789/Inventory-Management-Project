"""
compute_provided_nutrition — roll up the macros PROVIDED from orders vs. target.

Sums per-100g macros (from matched Products) across food invoice lines in a
window, weight-normalized to grams, ÷ eaters ÷ days  =>  provided-per-resident-
per-day, compared to the sourced reference target (2,800 kcal moderately-active
male, 20s). Reports a COVERAGE % (share of food spend whose line carries both a
macro profile AND a parsed weight) so the number never overstates its own
completeness.

This is PROVISION, not consumption (waste tracking is a later phase). Default
window is the last 28 days of available order data (robust to a frozen dev DB).
"""
from datetime import timedelta, date

from django.core.management.base import BaseCommand
from django.db.models import Sum, Max

from myapp.models import InvoiceLineItem, Census

NONFOOD = {"Smallwares", "Chemicals", "Pseudo"}
LB_TO_G = 453.592
TARGET = {"kcal": 2800, "protein": 100, "fat": 93, "carb": 350, "fiber": 39}
MACRO_MAP = [("kcal", "kcal_per_100g"), ("protein", "protein_g_per_100g"),
             ("fat", "fat_g_per_100g"), ("carb", "carb_g_per_100g"),
             ("fiber", "fiber_g_per_100g")]


class Command(BaseCommand):
    help = "Roll up provided nutrition from orders vs. the reference target."

    def add_arguments(self, p):
        p.add_argument("--days", type=int, default=28, help="Window length (default 28).")
        p.add_argument("--end", type=str, default="", help="Window end YYYY-MM-DD (default: latest order date).")
        p.add_argument("--eaters", type=int, default=0, help="Override eater count (default: latest Census x0.80).")

    def handle(self, *a, **o):
        food = (InvoiceLineItem.objects
                .exclude(product__isnull=True)
                .exclude(product__category__in=NONFOOD))

        end = date.fromisoformat(o["end"]) if o["end"] else food.aggregate(m=Max("invoice_date"))["m"]
        if not end:
            self.stdout.write("No food order data found."); return
        start = end - timedelta(days=o["days"] - 1)
        win = food.filter(invoice_date__gte=start, invoice_date__lte=end)

        # Eaters: latest census headcount x 0.80 baseline, unless overridden.
        if o["eaters"]:
            eaters = o["eaters"]
            eaters_src = "override"
        else:
            c = Census.objects.order_by("-date").first()
            eaters = round((c.headcount if c else 30) * 0.80)
            eaters_src = f"census {c.date} x0.80" if c else "default 30"

        all_spend = float(win.aggregate(s=Sum("extended_amount"))["s"] or 0)
        matched = win.filter(product__kcal_per_100g__isnull=False, quantity__isnull=False)

        # Roll up macros. Weight semantics differ by item type:
        #   weighed/catch-weight (uom=LB): quantity IS the pounds (case_total_weight_lb
        #     is the same number — multiplying them squares the weight).
        #   cased (counted): grams = cases(quantity) x lb-per-case(case_total_weight_lb).
        totals = {k: 0.0 for k, _ in MACRO_MAP}
        cov_spend = 0.0
        for ili in matched.select_related("product"):
            weighed = ili.product.inventory_class == "weighed" or (ili.purchase_uom or "").upper() == "LB"
            if weighed:
                grams = float(ili.quantity) * LB_TO_G
            elif ili.case_total_weight_lb is not None:
                grams = float(ili.quantity) * float(ili.case_total_weight_lb) * LB_TO_G
            else:
                continue                              # cased item with no parsed weight — uncomputable
            cov_spend += float(ili.extended_amount or 0)
            for k, field in MACRO_MAP:
                v = getattr(ili.product, field)
                if v is not None:
                    totals[k] += grams * float(v) / 100.0
        coverage = cov_spend / all_spend if all_spend else 0.0

        days = o["days"]
        self.stdout.write(f"=== Provided nutrition — {start} .. {end} ({days}d), {eaters} eaters ({eaters_src}) ===")
        self.stdout.write(f"Coverage: {coverage*100:.0f}% of food spend (${cov_spend:,.0f} of ${all_spend:,.0f}) "
                          f"carries macros + weight. Numbers below scale UP as coverage rises.")
        self.stdout.write(f"{'macro':<9}{'provided/eater/day':>20}{'target':>10}{'% of target':>13}")
        for k, _ in MACRO_MAP:
            per = totals[k] / eaters / days if eaters and days else 0
            unit = "" if k == "kcal" else "g"
            pct = per / TARGET[k] * 100 if TARGET[k] else 0
            self.stdout.write(f"{k:<9}{per:>17.0f}{unit:<3}{TARGET[k]:>8}{unit:<2}{pct:>11.0f}%")
        self.stdout.write("NOTE: provision (not consumption); coverage-limited — a partial floor, "
                          "not the full provided figure. Raise coverage via the review queue + weight backfill.")
