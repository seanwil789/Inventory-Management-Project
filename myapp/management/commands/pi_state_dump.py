"""Read-only state dump for session-start scour.

Emits a JSON snapshot of every read-only invariant the scour wants to
check on the Pi. Replaces N separate ``tailscale ssh sean@server "..."``
calls with one structured payload that gets parsed in-thread.

Why this command exists: per ``feedback_sandbox_friction.md``, the
sandbox's per-call approval prompt on remote shells is load-bearing.
Adding a broad ``Bash(tailscale ssh sean@server:*)`` allow rule would
delete that friction across the board. This command is the narrow
substitute — one audited code path that the harness can allow with a
single specific rule, while arbitrary remote shell still prompts.

Usage:
    python manage.py pi_state_dump          # JSON to stdout
    python manage.py pi_state_dump --pretty # human-readable indent

Read-only by design — no write paths, no destructive flags. If a future
need surfaces (e.g., 'I want pi_state_dump to also clear stale logs'),
build a separate command. Do not extend this one beyond reads.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count, Min, Max

from myapp.models import (
    Product,
    InvoiceLineItem,
    ProductMapping,
    ProductMappingProposal,
    Recipe,
    RecipeIngredient,
    Menu,
    MenuFreetextComponent,
    YieldReference,
    StandardPortionReference,
    Census,
)


def _shell(cmd, timeout=5):
    """Run a shell command read-only, return stdout stripped or None."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _git_state(repo_root):
    head = _shell(f"cd {repo_root} && git rev-parse --short HEAD") or "?"
    branch = _shell(f"cd {repo_root} && git rev-parse --abbrev-ref HEAD") or "?"
    upstream = _shell(
        f"cd {repo_root} && git rev-parse --abbrev-ref @{{u}} 2>/dev/null"
    )
    ahead_behind = None
    if upstream:
        ab = _shell(
            f"cd {repo_root} && git rev-list --left-right --count {upstream}...HEAD"
        )
        if ab:
            behind, ahead = ab.split()
            ahead_behind = {"ahead": int(ahead), "behind": int(behind)}
    dirty = _shell(f"cd {repo_root} && git status --porcelain")
    return {
        "head": head,
        "branch": branch,
        "upstream": upstream,
        "ahead_behind": ahead_behind,
        "working_tree_clean": (dirty == "" or dirty is None),
        "dirty_count": len(dirty.splitlines()) if dirty else 0,
    }


def _systemd_state():
    return {
        "django_service_active": _shell("systemctl is-active django.service") == "active",
        "django_service_enabled": _shell("systemctl is-enabled django.service") == "enabled",
    }


def _crontab():
    out = _shell("crontab -l")
    if not out:
        return []
    return [line for line in out.splitlines() if line and not line.startswith("#")]


def _recent_logs(repo_root, limit=10):
    logs_dir = Path(repo_root) / "logs"
    if not logs_dir.is_dir():
        return []
    files = sorted(
        (p for p in logs_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [
        {"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
        for p in files
    ]


def _table_counts():
    return {
        "Product": Product.objects.count(),
        "InvoiceLineItem": InvoiceLineItem.objects.count(),
        "ProductMapping": ProductMapping.objects.count(),
        "ProductMappingProposal": ProductMappingProposal.objects.count(),
        "Recipe": Recipe.objects.count(),
        "RecipeIngredient": RecipeIngredient.objects.count(),
        "Menu": Menu.objects.count(),
        "MenuFreetextComponent": MenuFreetextComponent.objects.count(),
        "YieldReference": YieldReference.objects.count(),
        "StandardPortionReference": StandardPortionReference.objects.count(),
        "Census": Census.objects.count(),
    }


def _ili_summary():
    total = InvoiceLineItem.objects.count()
    mapped = InvoiceLineItem.objects.filter(product__isnull=False).count()
    dates = InvoiceLineItem.objects.aggregate(
        Min("invoice_date"), Max("invoice_date")
    )
    return {
        "total": total,
        "mapped": mapped,
        "mapped_pct": round(mapped / total, 4) if total else 0.0,
        "date_min": dates["invoice_date__min"].isoformat() if dates["invoice_date__min"] else None,
        "date_max": dates["invoice_date__max"].isoformat() if dates["invoice_date__max"] else None,
    }


def _pmp_summary():
    by_status = {
        r["status"]: r["n"]
        for r in ProductMappingProposal.objects.values("status").annotate(n=Count("id"))
    }
    by_source = {
        r["source"]: r["n"]
        for r in ProductMappingProposal.objects.values("source").annotate(n=Count("id"))
    }
    return {
        "total": ProductMappingProposal.objects.count(),
        "pending": by_status.get("pending", 0),
        "approved": by_status.get("approved", 0),
        "rejected": by_status.get("rejected", 0),
        "by_source": by_source,
    }


def _ri_summary():
    total = RecipeIngredient.objects.count()
    null_qty = RecipeIngredient.objects.filter(quantity__isnull=True).count()
    fk_attached = RecipeIngredient.objects.filter(product__isnull=False).count()
    return {
        "total": total,
        "null_qty": null_qty,
        "product_fk_attached": fk_attached,
        "product_fk_pct": round(fk_attached / total, 4) if total else 0.0,
    }


def _menu_summary():
    total = Menu.objects.count()
    with_recipe = Menu.objects.filter(recipe__isnull=False).count()
    return {
        "total": total,
        "with_recipe_fk": with_recipe,
        "with_recipe_pct": round(with_recipe / total, 4) if total else 0.0,
    }


def _confidence_histogram():
    return {
        r["match_confidence"]: r["n"]
        for r in InvoiceLineItem.objects.values("match_confidence")
        .annotate(n=Count("id"))
        .order_by("-n")
    }


def _inventory_class_distribution():
    return {
        (r["inventory_class"] or ""): r["n"]
        for r in Product.objects.values("inventory_class")
        .annotate(n=Count("id"))
        .order_by("-n")
    }


def _structured_field_coverage():
    total = InvoiceLineItem.objects.count() or 1
    fields = [
        "quantity",
        "purchase_uom",
        "case_pack_count",
        "case_pack_unit_size",
        "case_pack_unit_uom",
        "case_total_weight_lb",
        "count_per_lb_low",
        "count_per_lb_high",
        "price_per_pound",
        "section_hint",
    ]
    out = {}
    for f in fields:
        # blank charfields read as "" not NULL; exclude both
        qs = InvoiceLineItem.objects.exclude(**{f"{f}__isnull": True})
        if InvoiceLineItem._meta.get_field(f).get_internal_type() == "CharField":
            qs = qs.exclude(**{f: ""})
        n = qs.count()
        out[f] = {"populated": n, "pct": round(n / total, 4)}
    return out


def _cost_coverage():
    """Recipes (is_current=True) at 100% cost coverage."""
    recipes = Recipe.objects.filter(is_current=True)
    total = recipes.count()
    if not total:
        return {"recipes_current": 0, "fully_priced": 0, "coverage_pct": 0.0}
    fully = sum(1 for r in recipes if r.estimated_cost_breakdown().get("coverage", 0) >= 1.0)
    return {
        "recipes_current": total,
        "fully_priced": fully,
        "coverage_pct": round(fully / total, 4),
    }


class Command(BaseCommand):
    help = "Emit a read-only JSON snapshot of Pi state for session-start scour."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pretty",
            action="store_true",
            help="Indent JSON for human reading (default: single-line).",
        )

    def handle(self, *args, **opts):
        from django.conf import settings

        repo_root = str(settings.BASE_DIR)

        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hostname": _shell("hostname") or "?",
            "git": _git_state(repo_root),
            "systemd": _systemd_state(),
            "tables": _table_counts(),
            "ili": _ili_summary(),
            "pmp": _pmp_summary(),
            "ri": _ri_summary(),
            "menus": _menu_summary(),
            "match_confidence_histogram": _confidence_histogram(),
            "inventory_class_distribution": _inventory_class_distribution(),
            "structured_field_coverage": _structured_field_coverage(),
            "cost_coverage": _cost_coverage(),
            "cron_entries": _crontab(),
            "recent_logs": _recent_logs(repo_root),
        }

        indent = 2 if opts["pretty"] else None
        self.stdout.write(json.dumps(payload, indent=indent, default=str))
