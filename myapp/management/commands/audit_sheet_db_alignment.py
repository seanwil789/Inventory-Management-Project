"""Audit alignment between the Synergy Google Sheet and the DB.

Three alignment surfaces:
  1. Item Mapping tab — col F canonicals vs Product table; (vendor, desc) keys
     vs ProductMapping. Mapper used to read this directly; after Step 2 of the
     sheet→DB migration it reads ProductMapping. Sheet remains as Sean's audit
     surface per project_db_as_source_of_truth.md.
  2. Synergy monthly tab — Product names (col B) vs Product.canonical_name.
  3. Synergy monthly tab — Case Price (col E) vs latest InvoiceLineItem.unit_price
     for the matched product. Categorizes drift as either:
        - "real" drift — stale sheet price (within 5× of DB), or
        - "unit-mismatch" — >5× ratio, almost always sheet=case-price vs
          DB=per-piece price (Mop Heads case $66 vs Bar Mop $0.22 etc.) per
          project_synergy_sheet.md unit conventions.

Read-only — no sheet or DB writes. Safe to run any time.

Usage:
  python manage.py audit_sheet_db_alignment
  python manage.py audit_sheet_db_alignment --tab "Synergy Apr 2026"
  python manage.py audit_sheet_db_alignment --json
  python manage.py audit_sheet_db_alignment --skip-item-mapping
  python manage.py audit_sheet_db_alignment --pricing-only
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

from django.core.management.base import BaseCommand

from myapp.models import Product, ProductMapping, InvoiceLineItem


def _import_invoice_processor():
    """Add invoice_processor to sys.path and return the helpers we need."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, '..', '..', '..'))
    ip_dir = os.path.join(repo_root, 'invoice_processor')
    if ip_dir not in sys.path:
        sys.path.insert(0, ip_dir)
    from sheets import get_sheet_values
    from config import SPREADSHEET_ID, MAPPING_TAB, ACTIVE_SHEET_TAB
    return get_sheet_values, SPREADSHEET_ID, MAPPING_TAB, ACTIVE_SHEET_TAB


# Pricing drift categorization. Sheet col E is "case price"; DB ILI.unit_price
# semantics depend on vendor (per project_synergy_sheet.md):
#   - Sysco non-catch-weight: unit_price = case total → matches sheet col E
#   - Catch-weight (MEATS/POULTRY/SEAFOOD): unit_price = case total but
#     price_per_pound is the per-lb price
#   - Farm Art / Exceptional Foods / per-unit-priced vendors: unit_price IS
#     the case total (extended_amount), but historically pre-Apr-22 lines
#     stored per-unit there. Reprocessed lines should be aligned now.
#   - Delaware Linen: per-piece unit_price (Bar Mops $0.22) — sheet stores
#     case price ($66 for 300 mops). Inherent semantic mismatch.
# When ratio >= UNIT_MISMATCH_RATIO or <= 1/UNIT_MISMATCH_RATIO, we tag the
# row as "unit mismatch" rather than "stale" — these usually need a manual
# fix to the sheet entry's case_size + price, not a sync.
UNIT_MISMATCH_RATIO = 5.0
ALIGNED_TOLERANCE = 0.05  # within $0.05


class Command(BaseCommand):
    help = "Audit alignment between Synergy spreadsheet and DB (names + pricing)."

    def add_arguments(self, parser):
        parser.add_argument('--tab', default=None,
                            help="Synergy monthly tab to audit (default: active month).")
        parser.add_argument('--json', action='store_true',
                            help="Emit JSON output.")
        parser.add_argument('--skip-item-mapping', action='store_true',
                            help="Skip Item Mapping tab alignment.")
        parser.add_argument('--pricing-only', action='store_true',
                            help="Skip name alignment; just pricing drift on monthly tab.")
        parser.add_argument('--limit', type=int, default=20,
                            help="Max examples per drift category in human output (default 20).")

    def handle(self, *args, **opts):
        get_sheet_values, SPREADSHEET_ID, MAPPING_TAB, ACTIVE_SHEET_TAB = _import_invoice_processor()
        tab = opts['tab'] or ACTIVE_SHEET_TAB
        report = {}

        if not opts['skip_item_mapping'] and not opts['pricing_only']:
            report['item_mapping'] = self._audit_item_mapping(
                get_sheet_values, SPREADSHEET_ID, MAPPING_TAB)

        if not opts['pricing_only']:
            report['monthly_tab_names'] = self._audit_monthly_tab_names(
                get_sheet_values, SPREADSHEET_ID, tab)

        report['monthly_tab_pricing'] = self._audit_monthly_tab_pricing(
            get_sheet_values, SPREADSHEET_ID, tab)

        if opts['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self._print_human(report, opts['limit'])

    # ── Item Mapping tab ────────────────────────────────────────────────────

    def _audit_item_mapping(self, get_sheet_values, spreadsheet_id, mapping_tab):
        try:
            rows = get_sheet_values(spreadsheet_id, f"{mapping_tab}!A:G")
        except Exception as e:
            return {'error': str(e), 'tab': mapping_tab}

        data = rows[1:] if rows else []
        for r in data:
            while len(r) < 7:
                r.append("")

        sheet_canonicals: set[str] = set()
        sheet_pm_keys: set[tuple[str, str]] = set()
        sheet_supcs: set[str] = set()
        canonical_populated = 0
        empty_canonical = 0

        for r in data:
            vendor, desc, _, _, _, canonical, supc = (x.strip() for x in r[:7])
            if not desc and not canonical:
                continue
            if not canonical:
                empty_canonical += 1
                continue
            canonical_populated += 1
            sheet_canonicals.add(canonical)
            if desc:
                sheet_pm_keys.add((vendor.upper(), desc.upper()))
            if supc:
                sheet_supcs.add(supc)

        db_canonicals = set(Product.objects.values_list('canonical_name', flat=True))
        db_pm_keys: set[tuple[str, str]] = set()
        for pm in ProductMapping.objects.select_related('vendor').all():
            if pm.description:
                v = (pm.vendor.name if pm.vendor else '').upper()
                db_pm_keys.add((v, pm.description.upper()))
        db_supcs = set(ProductMapping.objects.exclude(supc='').values_list('supc', flat=True))

        orphan_canonicals = sorted(sheet_canonicals - db_canonicals)
        db_only_canonicals = sorted(db_canonicals - sheet_canonicals)
        orphan_pm_keys = sorted(sheet_pm_keys - db_pm_keys)
        db_only_pm_keys = sorted(db_pm_keys - sheet_pm_keys)
        orphan_supcs = sorted(sheet_supcs - db_supcs)
        db_only_supcs = sorted(db_supcs - sheet_supcs)

        return {
            'tab': mapping_tab,
            'sheet_canonical_populated': canonical_populated,
            'sheet_empty_canonical': empty_canonical,
            'sheet_distinct_canonicals': len(sheet_canonicals),
            'sheet_pm_keys': len(sheet_pm_keys),
            'sheet_supcs': len(sheet_supcs),
            'db_products': len(db_canonicals),
            'db_pms': len(db_pm_keys),
            'db_supcs': len(db_supcs),
            'sheet_to_db_canonical_pct': _pct(sheet_canonicals & db_canonicals, sheet_canonicals),
            'db_to_sheet_canonical_pct': _pct(sheet_canonicals & db_canonicals, db_canonicals),
            'sheet_to_db_pm_pct': _pct(sheet_pm_keys & db_pm_keys, sheet_pm_keys),
            'db_to_sheet_pm_pct': _pct(sheet_pm_keys & db_pm_keys, db_pm_keys),
            'orphan_canonicals': orphan_canonicals,
            'db_only_canonicals': db_only_canonicals,
            'orphan_pm_keys': [list(k) for k in orphan_pm_keys],
            'db_only_pm_keys': [list(k) for k in db_only_pm_keys],
            'orphan_supcs': orphan_supcs,
            'db_only_supcs': db_only_supcs,
        }

    # ── Synergy monthly tab — names ─────────────────────────────────────────

    def _audit_monthly_tab_names(self, get_sheet_values, spreadsheet_id, tab):
        try:
            rows = get_sheet_values(spreadsheet_id, f"{tab}!A:J")
        except Exception as e:
            return {'error': str(e), 'tab': tab}

        sheet_products = []
        for r in rows:
            while len(r) < 10:
                r.append("")
            _, product, vendor, *_ = (x.strip() for x in r[:10])
            if not product or product.lower() == 'product':
                continue
            sheet_products.append({'product': product, 'vendor': vendor})

        sheet_names = set(p['product'] for p in sheet_products)
        db_names = set(Product.objects.values_list('canonical_name', flat=True))
        overlap = sheet_names & db_names

        return {
            'tab': tab,
            'sheet_product_rows': len(sheet_products),
            'distinct_sheet_products': len(sheet_names),
            'distinct_db_products': len(db_names),
            'overlap': len(overlap),
            'sheet_to_db_pct': _pct(overlap, sheet_names),
            'db_to_sheet_pct': _pct(overlap, db_names),
            'orphan_products': sorted(sheet_names - db_names),
            'db_only_products': sorted(db_names - sheet_names),
        }

    # ── Synergy monthly tab — pricing drift ─────────────────────────────────

    def _audit_monthly_tab_pricing(self, get_sheet_values, spreadsheet_id, tab):
        try:
            rows = get_sheet_values(spreadsheet_id, f"{tab}!A:J")
        except Exception as e:
            return {'error': str(e), 'tab': tab}

        sheet_products = []
        for r in rows:
            while len(r) < 10:
                r.append("")
            _, product, vendor, _, case_price, case_size, *_ = (x.strip() for x in r[:10])
            if not product or product.lower() == 'product':
                continue
            sheet_products.append({
                'product': product, 'vendor': vendor,
                'case_price': case_price, 'case_size': case_size,
            })

        # Bulk fetch latest ILI per product (one query per product is fine at
        # ~500 products; could be optimized via a window function later)
        product_lookup = {p.canonical_name: p for p in Product.objects.all()}
        latest_by_product: dict[str, InvoiceLineItem] = {}
        for p in product_lookup.values():
            ili = (InvoiceLineItem.objects
                   .filter(product=p, unit_price__isnull=False)
                   .order_by('-invoice_date')
                   .first())
            if ili:
                latest_by_product[p.canonical_name] = ili

        aligned = 0
        drift_real: list[dict] = []
        drift_unit: list[dict] = []
        blank_sheet = 0
        no_db_history = 0
        unparseable = 0
        not_in_db = 0

        today = date.today()

        for sp in sheet_products:
            pname = sp['product']
            if pname not in product_lookup:
                not_in_db += 1
                continue
            sheet_price_str = sp['case_price'].replace('$', '').replace(',', '').strip()
            if not sheet_price_str:
                blank_sheet += 1
                continue
            try:
                sheet_price = float(sheet_price_str)
            except ValueError:
                unparseable += 1
                continue
            ili = latest_by_product.get(pname)
            if not ili:
                no_db_history += 1
                continue
            db_price = float(ili.unit_price)
            age_days = (today - ili.invoice_date).days if ili.invoice_date else None
            if abs(sheet_price - db_price) <= ALIGNED_TOLERANCE:
                aligned += 1
                continue
            ratio = sheet_price / max(db_price, 0.01)
            entry = {
                'product': pname, 'vendor': sp['vendor'],
                'sheet_price': sheet_price, 'db_price': db_price,
                'delta_pct': round((sheet_price - db_price) / max(db_price, 0.01) * 100, 1),
                'db_invoice_date': str(ili.invoice_date) if ili.invoice_date else None,
                'db_age_days': age_days,
                'sheet_case_size': sp['case_size'],
                'db_case_size': ili.case_size or '',
                'has_price_per_pound': ili.price_per_pound is not None,
            }
            if ratio >= UNIT_MISMATCH_RATIO or ratio <= (1 / UNIT_MISMATCH_RATIO):
                drift_unit.append(entry)
            else:
                drift_real.append(entry)

        drift_real.sort(key=lambda x: -abs(x['delta_pct']))
        drift_unit.sort(key=lambda x: -abs(x['delta_pct']))

        total_compared = aligned + len(drift_real) + len(drift_unit)
        return {
            'tab': tab,
            'sheet_product_rows': len(sheet_products),
            'aligned': aligned,
            'drift_real': drift_real,
            'drift_unit_mismatch': drift_unit,
            'blank_sheet_price': blank_sheet,
            'no_db_history': no_db_history,
            'unparseable': unparseable,
            'not_in_db': not_in_db,
            'aligned_pct': _pct_count(aligned, total_compared),
        }

    # ── Human-readable output ──────────────────────────────────────────────

    def _print_human(self, report, limit):
        sep = "=" * 72
        out = self.stdout.write

        if 'item_mapping' in report:
            r = report['item_mapping']
            out(sep)
            out(f"ITEM MAPPING TAB ({r.get('tab', '?')}) ↔ DB ALIGNMENT")
            out(sep)
            if r.get('error'):
                out(f"  ERROR: {r['error']}")
            else:
                out(f"  Sheet: {r['sheet_canonical_populated']} canonical-populated rows, "
                    f"{r['sheet_distinct_canonicals']} distinct canonicals, "
                    f"{r['sheet_supcs']} SUPCs ({r['sheet_empty_canonical']} empty-canonical rows)")
                out(f"  DB:    {r['db_products']} Products, "
                    f"{r['db_pms']} ProductMappings, "
                    f"{r['db_supcs']} SUPCs")
                out(f"  Alignment:")
                out(f"    Sheet→DB canonicals: {r['sheet_to_db_canonical_pct']}% — "
                    f"{len(r['orphan_canonicals'])} orphans on sheet")
                out(f"    DB→Sheet canonicals: {r['db_to_sheet_canonical_pct']}% — "
                    f"{len(r['db_only_canonicals'])} DB Products absent from sheet")
                out(f"    Sheet→DB (vendor,desc): {r['sheet_to_db_pm_pct']}%")
                out(f"    DB→Sheet (vendor,desc): {r['db_to_sheet_pm_pct']}%")
                if r['orphan_canonicals']:
                    out(f"\n  Orphan canonicals on sheet (col F → no DB Product):")
                    for c in r['orphan_canonicals'][:limit]:
                        out(f"    • {c}")
                    if len(r['orphan_canonicals']) > limit:
                        out(f"    ... +{len(r['orphan_canonicals']) - limit} more (use --json for full list)")
            out("")

        if 'monthly_tab_names' in report:
            r = report['monthly_tab_names']
            out(sep)
            out(f"SYNERGY MONTHLY TAB '{r.get('tab')}' — NAME ALIGNMENT")
            out(sep)
            if r.get('error'):
                out(f"  ERROR: {r['error']}")
            else:
                out(f"  Sheet: {r['sheet_product_rows']} product rows, "
                    f"{r['distinct_sheet_products']} distinct names")
                out(f"  Alignment: Sheet→DB {r['sheet_to_db_pct']}%, "
                    f"DB→Sheet {r['db_to_sheet_pct']}%")
                out(f"  Orphan products on sheet: {len(r['orphan_products'])}")
                out(f"  DB Products absent from sheet: {len(r['db_only_products'])}")
                if r['orphan_products']:
                    out(f"\n  Orphan products on sheet:")
                    for c in r['orphan_products'][:limit]:
                        out(f"    • {c}")
                    if len(r['orphan_products']) > limit:
                        out(f"    ... +{len(r['orphan_products']) - limit} more")
            out("")

        if 'monthly_tab_pricing' in report:
            r = report['monthly_tab_pricing']
            out(sep)
            out(f"SYNERGY MONTHLY TAB '{r.get('tab')}' — PRICING DRIFT")
            out(sep)
            if r.get('error'):
                out(f"  ERROR: {r['error']}")
            else:
                total_compared = r['aligned'] + len(r['drift_real']) + len(r['drift_unit_mismatch'])
                out(f"  Compared: {total_compared} products w/ both sheet price + DB ILI price")
                out(f"  Aligned (within ${ALIGNED_TOLERANCE}):   "
                    f"{r['aligned']} ({r['aligned_pct']}%)")
                out(f"  Drift — likely real (stale):  {len(r['drift_real'])}")
                out(f"  Drift — unit mismatch (>{UNIT_MISMATCH_RATIO}× ratio): "
                    f"{len(r['drift_unit_mismatch'])}")
                out(f"  Blank sheet price:            {r['blank_sheet_price']}")
                out(f"  No DB ILI history:            {r['no_db_history']}")
                out(f"  Sheet name not in DB:         {r['not_in_db']}")
                out(f"  Unparseable sheet price:      {r['unparseable']}")

                if r['drift_real']:
                    out(f"\n  Top {min(limit, len(r['drift_real']))} likely-real drift "
                        f"(re-sync candidates — sheet stale or partial-month):")
                    out(f"    {'Product':<40} {'Sheet':>9} {'DB':>9} {'Δ%':>7} {'Age':>5} DB date")
                    for e in r['drift_real'][:limit]:
                        n = e['product'][:40]
                        age = f"{e['db_age_days']}d" if e['db_age_days'] is not None else '?'
                        out(f"    {n:<40} ${e['sheet_price']:>7.2f} "
                            f"${e['db_price']:>7.2f} {e['delta_pct']:>6.1f}% "
                            f"{age:>5} {e['db_invoice_date']}")
                    if len(r['drift_real']) > limit:
                        out(f"    ... +{len(r['drift_real']) - limit} more")

                if r['drift_unit_mismatch']:
                    out(f"\n  Top {min(limit, len(r['drift_unit_mismatch']))} unit-mismatch drift "
                        f"(sheet=case price, DB=per-piece — verify case_size col F):")
                    out(f"    {'Product':<40} {'Sheet':>9} {'DB':>9}  Sheet_CS    DB_CS")
                    for e in r['drift_unit_mismatch'][:limit]:
                        n = e['product'][:40]
                        sc = (e['sheet_case_size'] or '')[:10]
                        dc = (e['db_case_size'] or '')[:10]
                        out(f"    {n:<40} ${e['sheet_price']:>7.2f} "
                            f"${e['db_price']:>7.2f}  {sc:<11} {dc}")
                    if len(r['drift_unit_mismatch']) > limit:
                        out(f"    ... +{len(r['drift_unit_mismatch']) - limit} more")
            out("")


def _pct(intersection_set, denom_set):
    return round(len(intersection_set) / max(len(denom_set), 1) * 100, 1)


def _pct_count(numerator: int, denom: int):
    return round(numerator / max(denom, 1) * 100, 1)
