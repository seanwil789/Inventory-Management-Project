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

# Fields the merge transfers from loser → keeper when keeper is empty.
# Excludes raw_description (descriptions are extractor-format-specific and
# the keeper's choice is intentional via picker), source_file (keeper's
# slot is what stays), and id/created_at (DB-managed).
_MERGE_FIELDS = _STRUCTURED_FIELDS + (
    'product_id',                       # transfer mapping if keeper unmapped
    'canonical_vendor_pricelist_id',    # transfer FK if keeper missing
    'match_confidence', 'match_score', 'match_reason',
    'section_hint',
)


def _structured_score(ili) -> int:
    return sum(
        1 for f in _STRUCTURED_FIELDS
        if getattr(ili, f, None) not in (None, '', 0)
    )


def _pick_keeper(rows):
    """Choose which row to keep from a true-dup group.

    Returns (keeper, [losers]).

    Priority:
      1. Has product FK (preserves mappings — most-load-bearing field for
         downstream cost/category reports). Surfaced 2026-05-07: cross-source
         pairs where old JPG row had product FK and new HASH row didn't —
         picker without this priority would keep HASH and lose the mapping.
      2. Bare-hash source format (matches new HASH convention)
      3. Most structured fields populated
      4. Most recent updated_at (newest write usually has latest data)
      5. Lowest id (deterministic tiebreaker)
    """
    def sort_key(ili):
        bare_hash = '+' not in (ili.source_file or '')
        return (
            0 if ili.product_id else 1,       # product FK first
            0 if bare_hash else 1,            # bare hash second
            -_structured_score(ili),           # more structured fields
            -(ili.updated_at.timestamp() if getattr(ili, 'updated_at', None) else 0),
            ili.id,                            # deterministic tiebreaker
        )
    sorted_rows = sorted(rows, key=sort_key)
    return sorted_rows[0], sorted_rows[1:]


def _merge_loser_into_keeper(keeper, loser) -> list[str]:
    """Copy any populated loser fields into keeper's empty fields.

    Non-destructive — never overwrites a populated keeper field. Returns
    the list of field names actually transferred (for logging).
    """
    transferred = []
    for f in _MERGE_FIELDS:
        loser_val = getattr(loser, f, None)
        if loser_val in (None, '', 0):
            continue
        keeper_val = getattr(keeper, f, None)
        if keeper_val in (None, '', 0):
            setattr(keeper, f, loser_val)
            transferred.append(f)
    return transferred


class Command(BaseCommand):
    help = 'Collapse duplicate ILI rows with same (vendor, FK, date) but different source_file variants.'

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default=None,
                            help='Limit to one vendor (e.g. "Farm Art")')
        parser.add_argument('--apply', action='store_true',
                            help='Delete loser rows (default: dry-run)')
        parser.add_argument('--show', type=int, default=20,
                            help='Number of group examples to print')
        parser.add_argument('--merge-cross-source', action='store_true',
                            help='Also collapse cross-source-type groups (JPG/PDF '
                                 'vs HASH) with identical qty/up/ext, merging '
                                 'product FK + structured fields from losers '
                                 'into keeper before delete. Off by default — '
                                 'explicit opt-in required.')

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

        cross_merge_count = 0   # cross-source with identical values, mergeable
        cross_merge_extras = 0
        cross_merge_examples = []

        for key, rows in dup_groups.items():
            qtys = {ili.quantity for ili in rows}
            ups = {ili.unit_price for ili in rows}
            exts = {ili.extended_amount for ili in rows}
            values_identical = (len(qtys) == 1 and len(ups) == 1
                                and len(exts) == 1)

            prefixes = {_hash_prefix(r.source_file) for r in rows}
            same_hash = (None not in prefixes and len(prefixes) == 1)

            if same_hash:
                # Pattern A territory
                if values_identical:
                    true_dup_count += 1
                    true_dup_extras += len(rows) - 1
                    if len(true_dup_examples) < opts['show']:
                        true_dup_examples.append((key, rows))
                else:
                    variance_count += 1
                    if len(variance_examples) < 10:
                        variance_examples.append((key, rows))
            else:
                # Cross-source-type
                if values_identical:
                    # Mergeable under --merge-cross-source
                    cross_merge_count += 1
                    cross_merge_extras += len(rows) - 1
                    if len(cross_merge_examples) < 5:
                        cross_merge_examples.append((key, rows))
                else:
                    cross_source_count += 1
                    if len(cross_source_examples) < 5:
                        cross_source_examples.append((key, rows))

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
        merge_label = ('CROSS-SOURCE MERGE'
                       if opts.get('merge_cross_source') else
                       'CROSS-SOURCE MERGE (skip; pass --merge-cross-source)')
        self.stdout.write(self.style.SUCCESS(
            f'  {merge_label}: {cross_merge_count} groups, '
            f'{cross_merge_extras} extra rows '
            f'(JPG/PDF vs HASH, identical values — merges product FK + structured fields)'))
        self.stdout.write(self.style.WARNING(
            f'  CROSS-SOURCE VARIANCE (skip): {cross_source_count} groups '
            f'(different sources, value variance — manual review)'))

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

        if cross_merge_examples:
            self.stdout.write('')
            self.stdout.write('Sample cross-source MERGE candidates (identical values across sources):')
            for key, rows in cross_merge_examples[:5]:
                v_id, fk_id, dt = key
                keeper, losers = _pick_keeper(rows)
                self.stdout.write(f'  v={v_id} fk={fk_id} date={dt}: '
                                  f'keep id={keeper.id} sf={keeper.source_file!r} '
                                  f'product={(keeper.product.canonical_name if keeper.product else "(unmapped)")[:20]}')
                for l in losers:
                    prod = l.product.canonical_name if l.product else '(unmapped)'
                    self.stdout.write(f'    delete id={l.id} sf={l.source_file!r} '
                                      f'product={prod[:20]}')

        if cross_source_examples:
            self.stdout.write('')
            self.stdout.write('Sample cross-source VARIANCE groups (skipped — manual review):')
            for key, rows in cross_source_examples[:5]:
                v_id, fk_id, dt = key
                self.stdout.write(f'  v={v_id} fk={fk_id} date={dt}: '
                                  f'{len(rows)} rows different sources + variance')
                for ili in rows[:3]:
                    prod = ili.product.canonical_name if ili.product else '(unmapped)'
                    self.stdout.write(f'    id={ili.id} sf={ili.source_file!r} '
                                      f'qty={ili.quantity} up={ili.unit_price} '
                                      f'product={prod[:25]}')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '\nDry-run — re-run with --apply to delete loser rows.'))
            return

        # Apply — collapse Pattern A always; cross-source merge requires opt-in
        deleted = 0
        merged_with_transfer = 0
        merge_cs = opts.get('merge_cross_source', False)
        with transaction.atomic():
            for key, rows in dup_groups.items():
                qtys = {ili.quantity for ili in rows}
                ups = {ili.unit_price for ili in rows}
                exts = {ili.extended_amount for ili in rows}
                if not (len(qtys) == 1 and len(ups) == 1 and len(exts) == 1):
                    continue  # variance — never auto-collapse

                prefixes = {_hash_prefix(r.source_file) for r in rows}
                same_hash = (None not in prefixes and len(prefixes) == 1)

                if same_hash:
                    # Pattern A — straight delete, no merge needed (same hash
                    # means same OCR cache, same data on both sides)
                    keeper, losers = _pick_keeper(rows)
                    for l in losers:
                        l.delete()
                        deleted += 1
                elif merge_cs:
                    # Cross-source — merge before delete
                    keeper, losers = _pick_keeper(rows)
                    transferred_any = False
                    for l in losers:
                        transferred = _merge_loser_into_keeper(keeper, l)
                        if transferred:
                            transferred_any = True
                        l.delete()
                        deleted += 1
                    if transferred_any:
                        keeper.save()
                        merged_with_transfer += 1
        self.stdout.write(self.style.SUCCESS(
            f'\nDeleted {deleted} duplicate ILI rows.'
            + (f' Merged data on {merged_with_transfer} keepers.'
               if merge_cs else '')))
