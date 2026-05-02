"""Repoint ProductMapping rows to a different canonical Product.

Used when a curated ProductMapping was created at a time when the right
canonical didn't yet exist (or when conflation accumulated). Each pair
specifies a ProductMapping by id and the destination canonical_name.

Per Sean's `feedback_no_destroy_before_research.md` + `feedback_upstream_
downstream_planning.md` (LAW: enumerate downstream consumers before
upstream changes):

  1. ProductMapping rewrites change which canonical the mapper resolves
     to for that (vendor, description) key. After repoint, future
     invoices map directly via vendor_exact tier.
  2. Existing ILI rows attached to the old canonical are NOT touched —
     they stay with the old product FK. Use cleanup_canonical_conflation
     to detach those separately if needed.
  3. Old canonical's other ProductMappings are unaffected.

Usage:
    python manage.py repoint_product_mappings \\
        --pairs '185:Corn, Frozen,297:Corn, Frozen,343:Masa Harina'
    python manage.py repoint_product_mappings \\
        --pairs '185:Corn, Frozen' --apply

Pairs format: comma-separated `pm_id:canonical_name`. Canonical names
may contain commas — the parser splits on `,NN:` (digit+colon). Use
--from-file for cleaner input on long lists.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import Product, ProductMapping


def _parse_pairs(raw: str) -> list[tuple[int, str]]:
    """Parse '185:Corn, Frozen,297:Corn, Frozen,343:Masa Harina' into
    [(185, 'Corn, Frozen'), (297, 'Corn, Frozen'), (343, 'Masa Harina')].

    Splits on lookahead for `,N:` to handle canonical names with commas.
    """
    if not raw:
        return []
    # Use look-behind to split on `,NNN:` boundaries (next ID + colon)
    parts = re.split(r',(?=\d+:)', raw.strip())
    pairs = []
    for p in parts:
        m = re.match(r'^(\d+):(.+)$', p.strip())
        if not m:
            continue
        pairs.append((int(m.group(1)), m.group(2).strip()))
    return pairs


class Command(BaseCommand):
    help = "Repoint ProductMapping rows by id to new canonical names."

    def add_arguments(self, parser):
        parser.add_argument('--pairs', default='',
                            help='Comma-separated pm_id:canonical_name pairs.')
        parser.add_argument('--from-file', default='',
                            help='Read pairs from a file (one per line, '
                                 '"pm_id:canonical_name" format).')
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes (default is dry-run).')

    def handle(self, *args, **opts):
        pairs_raw = opts['pairs']
        if opts['from_file']:
            with open(opts['from_file']) as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith('#')]
            pairs_raw = ','.join(lines)

        pairs = _parse_pairs(pairs_raw)
        if not pairs:
            self.stderr.write('No pairs provided. Use --pairs or --from-file.')
            return

        apply_writes = opts['apply']

        # Validate every canonical exists before any writes.
        unknown = []
        targets: dict[int, Product] = {}
        for pm_id, cname in pairs:
            p = Product.objects.filter(canonical_name=cname).first()
            if not p:
                unknown.append((pm_id, cname))
            else:
                targets[pm_id] = p
        if unknown:
            self.stderr.write(self.style.ERROR(
                'Unknown canonical(s) — aborting:'
            ))
            for pm_id, cname in unknown:
                self.stderr.write(f'  pm_id={pm_id} → {cname!r}')
            return

        # Validate every pm_id exists.
        existing_ids = set(ProductMapping.objects.filter(
            id__in=[p[0] for p in pairs]
        ).values_list('id', flat=True))
        missing_pms = [p for p in pairs if p[0] not in existing_ids]
        if missing_pms:
            self.stderr.write(self.style.ERROR(
                'Missing ProductMapping ids — aborting:'
            ))
            for pm_id, cname in missing_pms:
                self.stderr.write(f'  pm_id={pm_id}')
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== repoint_product_mappings ({"APPLY" if apply_writes else "DRY-RUN"}) ===\n'
        ))

        for pm_id, cname in pairs:
            pm = ProductMapping.objects.get(id=pm_id)
            old = pm.product.canonical_name if pm.product else '(none)'
            v = pm.vendor.name if pm.vendor else '(none)'
            self.stdout.write(
                f'  pm_id={pm_id:5d} {v[:18]:18s} supc={pm.supc!r:14s}'
            )
            self.stdout.write(
                f'    desc={pm.description[:60]!r}'
            )
            self.stdout.write(
                f'    {old!r} → {cname!r}'
            )

        if apply_writes:
            with transaction.atomic():
                for pm_id, cname in pairs:
                    new_product = targets[pm_id]
                    ProductMapping.objects.filter(id=pm_id).update(product=new_product)
            self.stdout.write(self.style.SUCCESS(
                f'\nRe-pointed {len(pairs)} ProductMapping rows.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'\nDry-run — re-run with --apply to commit {len(pairs)} repoints.'
            ))
