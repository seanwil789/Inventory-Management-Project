"""Audit ProductMapping rows for canonical drift — find PMs whose
description tokens better match a different (more-specific) canonical
than the one they currently point at.

Closes the "curated mapping was created before the sub-canonical
existed" drift class. Sean (2026-05-02) caught the symptom on Corn:

  pm_id=185  desc='FROZEN CORN, 12/2.5-LB' → Corn (id=26)
  But canonical 'Corn, Frozen' (id=...) exists with tokens {corn, frozen}.
  Tier 6d subset_match would prefer Corn, Frozen as more-specific.

  pm_id=343  desc='Flour Corn Masa Harina' → Corn
  But canonical 'Masa Harina' exists.

Mapper tier hierarchy is REACTIVE: tier 2 (vendor_exact) shortcuts to
the curated PM target, never reaches tier 6d's specificity logic.
This audit is REFLECTIVE: it walks the curated PM history and proposes
re-points when better canonicals now exist.

Algorithm:
  1. Load full canonical pool (category_map keys).
  2. For each ProductMapping with product set:
     - Run pm.description through resolve_item with EMPTY tiers 1-5
       (code_map={}, desc_map={}, vendor_desc_map={}). Only tier 6 runs.
     - If tier 6 returns a different canonical → propose re-point.
  3. Surface proposals with confidence tier (subset_match >
     stripped_fuzzy > stripped_fuzzy-stem > stripped_fuzzy-char).

Per `feedback_no_destroy_before_research.md`: dry-run default; --apply
commits via repoint_product_mappings logic; never auto-creates Products
or deletes anything.

Usage:
    python manage.py audit_pm_canonical_drift              # dry-run, list all
    python manage.py audit_pm_canonical_drift --apply      # commit re-points
    python manage.py audit_pm_canonical_drift --vendor Sysco
    python manage.py audit_pm_canonical_drift --tier subset_match
"""
from __future__ import annotations

import os
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import Product, ProductMapping


def _import_mapper():
    ip_dir = os.path.join(settings.BASE_DIR, 'invoice_processor')
    if ip_dir not in sys.path:
        sys.path.insert(0, ip_dir)
    import mapper as m
    return m


class Command(BaseCommand):
    help = "Audit ProductMapping rows for canonical drift (more-specific canonical now exists)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit re-points (default is dry-run).')
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor.')
        parser.add_argument('--tier', default='',
                            help='Filter to one confidence tier '
                                 '(subset_match, stripped_fuzzy).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after N proposals (default: no limit).')

    def handle(self, *args, **opts):
        apply_writes = opts['apply']
        vendor_filter = opts['vendor']
        tier_filter = opts['tier']
        limit = opts['limit'] or float('inf')

        mapper = _import_mapper()
        # Build category_map from ALL Products, not just those with
        # ProductMapping rows. mapper._load_from_db() filters by PM
        # presence so newly-created canonicals (Corn, Frozen / Masa
        # Harina) are absent from its pool. The audit's whole point is
        # to surface canonicals that don't have PMs yet — so we must
        # include them here.
        category_map = {}
        for p in Product.objects.all():
            category_map[p.canonical_name] = {
                'category': p.category or '',
                'primary_descriptor': p.primary_descriptor or '',
                'secondary_descriptor': p.secondary_descriptor or '',
            }
        if not category_map:
            self.stderr.write('No Products — nothing to audit.')
            return

        # Empty tiers 1-5 force resolve_item to fall through to tier 6
        empty_tiers = {
            'code_map': {},
            'desc_map': {},
            'vendor_desc_map': {},
            'category_map': category_map,
        }

        qs = (ProductMapping.objects
              .exclude(product=None)
              .select_related('product', 'vendor'))
        if vendor_filter:
            qs = qs.filter(vendor__name=vendor_filter)

        proposals = []
        scanned = 0
        for pm in qs.iterator():
            if len(proposals) >= limit:
                break
            scanned += 1
            current = pm.product.canonical_name
            vendor_name = pm.vendor.name if pm.vendor else ''
            item = {
                'raw_description': pm.description or '',
                'sysco_item_code': '',  # force tier 1 miss too
            }
            try:
                result = mapper.resolve_item(item, empty_tiers, vendor=vendor_name)
            except Exception:
                continue
            proposed = result.get('canonical')
            confidence = result.get('confidence', '')
            if not proposed:
                continue
            if proposed == current:
                continue
            # Tier 6 returned a DIFFERENT canonical → potential drift
            if tier_filter and confidence != tier_filter:
                continue
            proposals.append({
                'pm_id': pm.id,
                'vendor': vendor_name,
                'description': pm.description,
                'current': current,
                'proposed': proposed,
                'confidence': confidence,
                'score': result.get('score'),
            })

        # Output
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== audit_pm_canonical_drift ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))
        self.stdout.write(f'PMs scanned:     {scanned}')
        self.stdout.write(f'Drift proposals: {len(proposals)}')

        # Group by confidence tier
        from collections import defaultdict
        by_tier: dict[str, list[dict]] = defaultdict(list)
        for p in proposals:
            by_tier[p['confidence']].append(p)

        self.stdout.write('')
        self.stdout.write('By confidence tier:')
        for tier, items in sorted(by_tier.items(), key=lambda kv: -len(kv[1])):
            self.stdout.write(f'  {tier:25s} {len(items):>4}')

        # Detail (group by tier, then sort by current canonical for readability)
        for tier in sorted(by_tier):
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_LABEL(f'--- {tier} ---'))
            items = sorted(by_tier[tier], key=lambda p: (p['current'], p['proposed']))
            for p in items:
                self.stdout.write(
                    f'  pm_id={p["pm_id"]:5d} {p["vendor"][:18]:18s} '
                    f'score={p["score"]}  current={p["current"]!r} '
                    f'→ {p["proposed"]!r}'
                )
                self.stdout.write(
                    f'    desc={p["description"][:65]!r}'
                )

        if apply_writes and proposals:
            with transaction.atomic():
                for p in proposals:
                    new_product = Product.objects.filter(
                        canonical_name=p['proposed']
                    ).first()
                    if new_product:
                        ProductMapping.objects.filter(id=p['pm_id']).update(
                            product=new_product
                        )
            self.stdout.write(self.style.SUCCESS(
                f'\nRe-pointed {len(proposals)} ProductMapping rows.'
            ))
        elif proposals:
            self.stdout.write(self.style.WARNING(
                f'\nDry-run — re-run with --apply to commit {len(proposals)} re-points.'
            ))
