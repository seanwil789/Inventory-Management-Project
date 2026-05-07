"""Collapse duplicate ILI rows that share (vendor, canonical_vendor_pricelist,
invoice_date) AND share a common source_file hash prefix.

Surfaced 2026-05-07: reprocess_ocr_cache writes 'HASH+N' source_file for
multi-photo merge; reprocess_invoices writes bare 'HASH' for single-pass.
Phase 4b dedup primary key required exact source_file match → old +N rows
and new bare-hash rows didn't collide → 35 of 218 invoice hashes had
duplicates across the two formats.

The db_write tolerant-prefix lookup (commit 35ef5e1) prevents NEW duplicates.
This command cleans up existing ones.

**SAFETY CONSTRAINTS** — only collapses groups where:
    1. ALL rows share the same source_file HASH PREFIX (after stripping +N).
       This means same OCR cache content → same physical invoice.
       Skips JPG-vs-HASH and PDF-vs-HASH cross-format pairs (those need
       data-merge logic to preserve product FK from older mapped rows
       before the unmapped rank-pair-era rows get the keep slot).
    2. ALL rows have IDENTICAL qty / unit_price / extended_amount.
       Variance groups skipped — they represent drift-cascade fixes
       where rank-pair has correct values and old rows have wrong ones,
       requiring per-row decisions.

Picker for which row to keep within a true-dup group:
    1. Bare-hash source_file beats HASH+N (matches new convention)
    2. Most structured fields populated (case_pack_count, case_total_weight,
       price_per_pound, count_per_lb_low/high)
    3. Most recent updated_at (newest write usually has best data)
    4. Lowest id (deterministic tiebreaker)
"""
import re
from collections import defaultdict
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from myapp.models import InvoiceLineItem, Vendor


_HASH_RE = re.compile(r'^([0-9a-f]{12,})(\+\d+)?$')


def _hash_prefix(source_file: str) -> str | None:
    """Return the bare hash prefix if source_file is a HASH or HASH+N
    cache reference. Returns None for JPG / PDF / other formats so they
    don't collide with hash-based source_files in the dedup grouping.
    """
    if not source_file:
        return None
    m = _HASH_RE.match(source_file.strip())
    return m.group(1) if m else None


_STRUCTURED_FIELDS = (
    'case_pack_count', 'case_pack_unit_size', 'case_pack_unit_uom',
    'case_total_weight_lb', 'price_per_pound',
    'count_per_lb_low', 'count_per_lb_high',
    'quantity', 'purchase_uom',
)


def _structured_score(ili) -> int:
    return sum(
        1 for f in _STRUCTURED_FIELDS
        if getattr(ili, f, None) not in (None, '', 0)
    )


def _pick_keeper(rows):
    """Choose which row to keep from a true-dup group.

    Returns (keeper, [losers]).
    """
    def sort_key(ili):
        # Lower is better (sorted ascending — first wins)
        bare_hash = '+' not in (ili.source_file or '')
        return (
            0 if bare_hash else 1,           # bare hash first
            -_structured_score(ili),          # more structured fields first
            -(ili.updated_at.timestamp() if getattr(ili, 'updated_at', None) else 0),
            ili.id,                           # deterministic tiebreaker
        )
    sorted_rows = sorted(rows, key=sort_key)
    return sorted_rows[0], sorted_rows[1:]


class Command(BaseCommand):
    help = 'Collapse duplicate ILI rows with same (vendor, FK, date) but different source_file variants.'

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default=None,
                            help='Limit to one vendor (e.g. "Farm Art")')
        parser.add_argument('--apply', action='store_true',
                            help='Delete loser rows (default: dry-run)')
        parser.add_argument('--show', type=int, default=20,
                            help='Number of group examples to print')

    def handle(self, *args, **opts):
        qs = (InvoiceLineItem.objects
              .filter(canonical_vendor_pricelist__isnull=False)
              .exclude(invoice_date__isnull=True))
        if opts['vendor']:
            try:
                v = Vendor.objects.get(name=opts['vendor'])
            except Vendor.DoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f'Vendor not found: {opts["vendor"]!r}'))
                return
            qs = qs.filter(vendor=v)

        # Group by (vendor, FK, date)
        groups = defaultdict(list)
        for ili in qs:
            key = (ili.vendor_id, ili.canonical_vendor_pricelist_id,
                   ili.invoice_date)
            groups[key].append(ili)

        # Identify groups with > 1 row
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

        true_dup_count = 0      # Pattern A: same hash prefix, identical values
        variance_count = 0      # Same hash prefix, different qty/up/ext
        cross_source_count = 0  # Different source types (JPG/PDF vs HASH) — skipped
        true_dup_extras = 0
        true_dup_examples = []
        variance_examples = []
        cross_source_examples = []

        for key, rows in dup_groups.items():
            # Constraint 1: all rows must share a common hash prefix
            prefixes = {_hash_prefix(r.source_file) for r in rows}
            if None in prefixes or len(prefixes) > 1:
                # Cross-source-type pair (JPG vs HASH, etc.) — skip for now;
                # Option 2 will handle these with data-merge logic.
                cross_source_count += 1
                if len(cross_source_examples) < 5:
                    cross_source_examples.append((key, rows))
                continue
            # Constraint 2: identical qty/up/ext
            qtys = {ili.quantity for ili in rows}
            ups = {ili.unit_price for ili in rows}
            exts = {ili.extended_amount for ili in rows}
            if len(qtys) == 1 and len(ups) == 1 and len(exts) == 1:
                true_dup_count += 1
                true_dup_extras += len(rows) - 1
                if len(true_dup_examples) < opts['show']:
                    true_dup_examples.append((key, rows))
            else:
                variance_count += 1
                if len(variance_examples) < 10:
                    variance_examples.append((key, rows))

        self.stdout.write(self.style.WARNING(
            f'\n=== dedup_canonical_fk_groups '
            f'{"APPLY" if opts["apply"] else "DRY-RUN"} ==='))
        self.stdout.write(f'Vendor filter:        '
                          f'{opts["vendor"] or "(all)"}')
        self.stdout.write(f'ILIs scanned:         {qs.count()}')
        self.stdout.write(f'Distinct (V,FK,D):    {len(groups)}')
        self.stdout.write(f'Groups w/ duplicates: {len(dup_groups)}')
        self.stdout.write(self.style.SUCCESS(
            f'  TRUE DUPS (collapse): {true_dup_count} groups, '
            f'{true_dup_extras} extra rows to delete '
            f'(same hash prefix, identical values)'))
        self.stdout.write(self.style.WARNING(
            f'  VARIANCE (skip):      {variance_count} groups '
            f'(same hash prefix, different qty/up/ext — drift-cascade review)'))
        self.stdout.write(self.style.WARNING(
            f'  CROSS-SOURCE (skip):  {cross_source_count} groups '
            f'(JPG/PDF vs HASH — needs data-merge logic to preserve product FK)'))

        if true_dup_examples:
            self.stdout.write('')
            self.stdout.write('Sample true-dup groups (would collapse):')
            for key, rows in true_dup_examples[:5]:
                v_id, fk_id, dt = key
                keeper, losers = _pick_keeper(rows)
                self.stdout.write(f'  v={v_id} fk={fk_id} date={dt}: '
                                  f'{len(rows)} rows → keep id={keeper.id} '
                                  f'(sf={keeper.source_file!r}), '
                                  f'delete {[l.id for l in losers]}')

        if variance_examples:
            self.stdout.write('')
            self.stdout.write('Sample variance groups (skipped — review manually):')
            for key, rows in variance_examples[:5]:
                v_id, fk_id, dt = key
                self.stdout.write(f'  v={v_id} fk={fk_id} date={dt}: '
                                  f'{len(rows)} rows w/ different qty/up/ext')
                for ili in rows[:3]:
                    self.stdout.write(f'    id={ili.id} sf={ili.source_file!r} '
                                      f'qty={ili.quantity} up={ili.unit_price} '
                                      f'ext={ili.extended_amount}')

        if cross_source_examples:
            self.stdout.write('')
            self.stdout.write('Sample cross-source-type groups (skipped — Option 2 territory):')
            for key, rows in cross_source_examples[:5]:
                v_id, fk_id, dt = key
                self.stdout.write(f'  v={v_id} fk={fk_id} date={dt}: '
                                  f'{len(rows)} rows from different source types')
                for ili in rows[:3]:
                    prod = ili.product.canonical_name if ili.product else '(unmapped)'
                    self.stdout.write(f'    id={ili.id} sf={ili.source_file!r} '
                                      f'qty={ili.quantity} up={ili.unit_price} '
                                      f'product={prod[:25]}')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to delete loser rows.'))
            return

        # Apply — only collapse rows that pass BOTH gates
        deleted = 0
        with transaction.atomic():
            for key, rows in dup_groups.items():
                # Gate 1: same hash prefix
                prefixes = {_hash_prefix(r.source_file) for r in rows}
                if None in prefixes or len(prefixes) > 1:
                    continue
                # Gate 2: identical qty/up/ext
                qtys = {ili.quantity for ili in rows}
                ups = {ili.unit_price for ili in rows}
                exts = {ili.extended_amount for ili in rows}
                if not (len(qtys) == 1 and len(ups) == 1 and len(exts) == 1):
                    continue
                keeper, losers = _pick_keeper(rows)
                for l in losers:
                    l.delete()
                    deleted += 1
        self.stdout.write(self.style.SUCCESS(
            f'\nDeleted {deleted} duplicate ILI rows.'))
