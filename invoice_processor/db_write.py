"""
Database write layer — replaces append_to_data_sheet() in sheets.py.

Writes processed invoice line items to the Django database.
Must be called from within a Django context (settings configured).
"""
import os
import re
import sys
import django
from decimal import Decimal, InvalidOperation
from datetime import datetime

# Bootstrap Django if not already set up
if not os.environ.get('DJANGO_SETTINGS_MODULE'):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'myproject.settings'
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    django.setup()

from myapp.models import Vendor, Product, ProductMappingProposal, InvoiceLineItem
from django.db.models import Avg


# Phase 2 of mapper safety: fuzzy tiers don't auto-commit FKs. They land
# as ILI rows with product=NULL + match_confidence='<tier>_pending' and
# create a ProductMappingProposal queue entry for human review. The
# Django /mapping-review/ UI surfaces the queue. On approval, the FK is
# attached and a ProductMapping row is created so future invoices auto-
# resolve. Tiers below auto-commit deterministically as before.
#
# subset_match (mapper tier 6d) — canonical tokens are all contained in
# the raw description. Strong signal but reviewable: 'Apple Cider' could
# subset-match 'Apple' canonical when there's no Apple Cider canonical
# yet. Quarantine surfaces the suggestion for Sean to confirm/override.
_FUZZY_TIERS = {'vendor_fuzzy', 'fuzzy', 'stripped_fuzzy', 'subset_match'}


# B-CorruptSection guard (2026-05-11): some extractor paths emit
# section labels containing "GROUP TOTAL", "TOTAL", or non-canonical
# values like "HAZARD" / "DISPENSER BEVERAGE" because _find_sections
# (spatial_matcher) is more permissive than canonicalize_sysco_section.
# When section_hint contains those non-canonical labels, IVS section
# reconciliation creates parallel ghost-section entries (one canonical
# with printed_total + no items, one corrupt with items + no printed_total).
# Fix: at db_write boundary, normalize through canonicalize_sysco_section
# and store empty when result isn't a known canonical. Empty falls
# through to orphan handling rather than poisoning the section graph.
def _normalize_section_hint(label) -> str:
    if not label:
        return ''
    try:
        # Imported lazily so this module remains importable without the
        # full invoice_processor path being set up.
        from spatial_matcher import (canonicalize_sysco_section,
                                      _CANONICAL_SYSCO_SECTIONS)
    except Exception:
        return str(label)[:60]
    canon = canonicalize_sysco_section(str(label))
    if canon in _CANONICAL_SYSCO_SECTIONS:
        return canon[:60]
    # Defensive: explicit junk markers shouldn't survive even if a
    # future canonicalize change would let them through.
    upper = str(label).upper()
    if 'GROUP TOTAL' in upper or upper.startswith('TOTAL'):
        return ''
    return ''


# Phase 3d (Sean 2026-05-02): boilerplate-rejection guard.
#
# OCR captures invoice headers/addresses/customer-names as text that
# accidentally pairs with adjacent SUPC codes during column-dump parsing.
# The mapper hits a SUPC code-tier match (legitimate Product), but the
# raw_description is "SYNERGY HOUSES" or "TRUCK STOP" — not a real
# product line. Today's db_write attaches the FK regardless, producing
# silent mismaps:
#   "SYNERGY HOUSES"           → Fries Frozen
#   "WEST CHESTER PA 19382"    → Lays
#   "CUSTOMER'S ORIGINAL"      → Bread, White
#   "TRUCK STOP"               → Wrap, White
# audit_real_suspects surfaces these AFTER the fact; the guard prevents
# them at write time. Tags rows as 'unmatched' (no FK) so they show up
# in the unmapped queue + don't pollute spend/cost/sheet sync.
_BOILERPLATE_RE = re.compile(
    r"^CUSTOMER'?S\s+ORIGINAL$"
    r"|^TRUCK\s+STOP$"
    r"|^SYNERGY\s+HOUSE[S]?$"
    r"|^THE\s+WENTWORTH\s+SYNERGY"
    r"|^JFT\s+COMMUNIT(?:Y|IES)$"
    r"|^WEST\s+CHESTER\s+PA"
    r"|^PHILADELPHIA,?\s+PA"
    r"|^CHESTER,?\s+PA"
    r"|^P\.?O\.?\s*BOX"
    r"|^\d{2,5}\s+[A-Z]\s+(?:CHURCH|BROAD|MAIN|MARKET|HIGH|HEMLOCK)"  # street addr
    r"|^\d{3}[-.]\d{3}[-.]\d{4}$"   # phone number
    r"|^\([0-9]{3}\)\s*\d{3}[-.]\d{4}$"
    r"|^[A-Z]{2}\s+\d{5}(?:-\d{4})?$"  # state ZIP
    r"|^DRIVER'?S\s+SIGN"
    r"|^CONFIDENTIAL\s+PROPERTY",
    re.IGNORECASE,
)

def _is_boilerplate_raw_description(raw_desc: str) -> bool:
    """True when raw_description matches known invoice boilerplate
    (header/address/customer-name/phone). At db_write time, refuse to
    auto-attach a Product FK to these rows even if a code tier hit —
    they aren't real products."""
    if not raw_desc:
        return False
    return bool(_BOILERPLATE_RE.match(raw_desc.strip()))


# Phase 3e (Sean 2026-05-02): inventory_class type-check guard.
#
# Catches catastrophic class-mismatch mismaps the boilerplate guard misses.
# The umbrella bug class:
#   "DAIRY YOGURT GREEK" (case_size 12/4OZ) → mapped to Shrimp (Proteins)
#   "MAYONNAISE 1 GAL"   (case_size 1 GAL)  → mapped to Mayo (counted_with_weight)
# When the raw line item has clear inventory-class signal (volume regex on
# case_size or raw_description) AND the candidate Product has a populated
# inventory_class that disagrees, we refuse the FK attach. Tags as
# 'unmatched_class_mismatch' so the row surfaces in unmapped queue +
# audits without polluting downstream cost/spend/sheet sync.
#
# Conservative — fires only when BOTH sides have determined class. Empty
# Product.inventory_class (122 unset products as of backfill) → bypass.
_VOLUME_UNIT_RE = re.compile(
    r'(?:^|[^A-Za-z])(GAL|GALLON|QT|QUART|PT|PINT|FL\s*OZ|FLOZ)(?=$|[^A-Za-z])',
    re.IGNORECASE,
)

# Phase 3f (Sean 2026-05-02): protein keyword inference for class guard.
#
# Seafood + butchered cuts shipped by-the-pound are unambiguously WEIGHED
# in this domain (industry $/lb pricing, scale at receiving). When a
# raw_description mentions one of these AND the case_size carries an LB
# signal, infer weighed. Catches the SHRIMP → Uncrustables PBJ class of
# mismaps the volume-only Phase 3e helper missed.
#
# **Two signals required** — keyword alone isn't enough. ANCHOVIES (in
# 28 OZ jars) is correctly counted_with_weight even though ANCHOVY is
# a seafood word; same for canned tuna, jarred herring, sliced deli
# meats. The LB signal in case_size separates the bulk-weighed format
# from the canned/jarred/sliced counted formats.
#
# Excluded from list: BEEF/PORK/CHICKEN/TURKEY (appear in many counted
# formats — sandwiches, nuggets, frozen patties); ANCHOVY/SARDINE/HERRING
# (canned/jarred); PROSCIUTTO/PEPPERONI/SALAMI/CAPOCOLLA (deli-sliced
# default). Word-boundary regex prevents CHOPSTICKS / CHOPPED false
# positives.
_PROTEIN_WEIGHED_KEYWORDS_RE = re.compile(
    r'\b('
    r'SHRIMP|PRAWN|LOBSTER|SCALLOP|CRAB|'
    r'SALMON|TUNA|TILAPIA|HALIBUT|TROUT|'
    r'BACON|'
    r'BRISKET|RIBEYE|TENDERLOIN|SIRLOIN|FILET\s+MIGNON|'
    r'PORK\s+BELLY|PORK\s+SHOULDER|PORK\s+LOIN|'
    r'LAMB\s+SHOULDER|LAMB\s+CHOP|VEAL\s+CHOP'
    r')\b',
    re.IGNORECASE,
)
_LB_SIGNAL_RE = re.compile(
    r'(?:^|[^A-Za-z])(\d+(?:\.\d+)?)\s*(LB|LBS|POUND)\b',
    re.IGNORECASE,
)


def _infer_raw_inventory_class(raw_desc: str, case_size: str) -> str | None:
    """Best-effort class inference from raw line-item signals.

    Volume signal (GAL/QT/PT/FL OZ) → counted_with_volume.
    Protein keyword + LB signal (SHRIMP+10LB, BACON+15LB) → weighed.
    Volume wins when both fire (a 1 GAL salmon-flavored stock is volume).

    Returns None when no strong signal exists (don't enforce → bypass)."""
    candidates = (case_size or '', raw_desc or '')
    for text in candidates:
        if text and _VOLUME_UNIT_RE.search(text):
            return 'counted_with_volume'
    # Protein keyword (raw_description only — case_size never contains
    # protein words) + LB signal (either side) → weighed. Both gates
    # required so canned/jarred/sliced seafood formats stay untagged.
    if raw_desc and _PROTEIN_WEIGHED_KEYWORDS_RE.search(raw_desc):
        for text in candidates:
            if text and _LB_SIGNAL_RE.search(text):
                return 'weighed'
    return None


def _is_class_mismatch(product, raw_desc: str, case_size: str) -> bool:
    """True when raw line item's inferred class disagrees with the
    product's inventory_class. Conservative: only fires on confident
    signals on both sides."""
    if product is None or not product.inventory_class:
        return False
    raw_class = _infer_raw_inventory_class(raw_desc, case_size)
    if raw_class is None:
        return False
    return raw_class != product.inventory_class


def _check_price_anomaly(product, vendor, unit_price: Decimal) -> bool:
    """
    Check if a price is anomalous compared to the 90-day historical average
    for this product+vendor. Returns True if price is >2x or <0.5x the average.

    B6 (Sean 2026-05-08): excludes math_flagged rows from the baseline.
    Without this, math-anomaly rows poison the average — the flagger can't
    detect drift against a corrupted baseline (false negatives) and clean
    rows look anomalous against the corrupted baseline (false positives).
    Closes the feedback loop per Trust LAW.
    """
    from datetime import timedelta
    cutoff = datetime.now().date() - timedelta(days=90)

    avg_result = (
        InvoiceLineItem.objects
        .filter(
            product=product,
            vendor=vendor,
            unit_price__isnull=False,
            unit_price__gt=0,
            invoice_date__gte=cutoff,
        )
        .exclude(math_flagged=True)
        .aggregate(avg_price=Avg('unit_price'))
    )

    avg_price = avg_result.get('avg_price')
    if avg_price is None or avg_price == 0:
        return False  # no history — can't flag

    ratio = float(unit_price) / float(avg_price)
    return ratio > 2.0 or ratio < 0.5


def write_invoice_to_db(vendor_name: str, invoice_date: str,
                        items: list[dict], source_file: str = '',
                        invoice_number: str = '') -> int:
    """
    Persist parsed and mapped invoice line items to the database.

    Args:
        vendor_name:    canonical vendor name (e.g. "Sysco")
        invoice_date:   ISO date string "YYYY-MM-DD" or ""
        items:          list of dicts from map_items() — each has
                        canonical, raw_description, unit_price, case_size_raw
        source_file:    original filename for provenance tracking
        invoice_number: vendor-extracted invoice number (Phase 4c primary
                        dedup key). Empty when vendor lacks reliable
                        extraction; falls back to source_file-based key.

    Returns:
        Number of rows written.
    """
    vendor = None
    if vendor_name:
        vendor, _ = Vendor.objects.get_or_create(name=vendor_name)

    parsed_date = None
    if invoice_date:
        try:
            parsed_date = datetime.strptime(invoice_date, '%Y-%m-%d').date()
            # Reject dates more than 18 months old or in the future
            today = datetime.now().date()
            from datetime import timedelta
            if parsed_date > today + timedelta(days=7):
                print(f"  [!] Rejecting future date {parsed_date} — likely OCR error")
                parsed_date = None
            elif parsed_date < today - timedelta(days=548):
                print(f"  [!] Rejecting old date {parsed_date} — more than 18 months ago")
                parsed_date = None
        except ValueError:
            pass

    # Phase 4a (Sean 2026-05-06): self-healing canonical FK alongside dedup.
    # Build the VendorPriceList candidate index ONCE per call to amortize the
    # tokenize cost across all incoming rows. Empty list when the vendor has
    # no catalog yet — the FK assignment becomes a no-op for that vendor.
    # See `project_self_healing_raw_descriptions.md` + `feedback_event_driven_pricing.md`.
    vpl_candidates = []
    if vendor is not None:
        from invoice_processor.canonical_match import build_candidate_index
        vpl_candidates = build_candidate_index(vendor)

    written = 0
    for item in items:
        canonical = item.get('canonical')
        product   = Product.objects.filter(canonical_name=canonical).first() if canonical else None
        raw_desc_for_check = item.get('raw_description', '')

        # Phase 3d (Sean 2026-05-02): boilerplate-rejection guard.
        # If the raw_description matches known invoice header/address/
        # customer-name boilerplate, refuse to auto-attach the FK even
        # if mapper produced a canonical (typically via SUPC code tier
        # on an adjacent column). Tag as 'unmatched' so the row surfaces
        # in the unmapped queue + doesn't pollute spend/cost/sheet sync.
        if product is not None and _is_boilerplate_raw_description(raw_desc_for_check):
            print(f"  [!] Boilerplate rejection: raw {raw_desc_for_check!r} "
                  f"would have mapped to {canonical!r} — refusing FK attach.")
            product = None
            item = {**item, 'confidence': 'unmatched'}
            canonical = None  # also clear so drift-detection below doesn't fire

        # Phase 3e (Sean 2026-05-02): inventory_class type-check guard.
        # Reject FK attach when raw line item's volume/weight signal
        # disagrees with the candidate Product's inventory_class. Catches
        # CHOBANI YOGURT → Shrimp class jumps + GAL/QT-packed liquids
        # mismapped to weighed proteins. Bypass when either side lacks
        # determined class (122 of 549 Products are still 'review/blank').
        if product is not None and _is_class_mismatch(
                product, raw_desc_for_check, item.get('case_size_raw', '')):
            print(f"  [!] Class mismatch: raw {raw_desc_for_check!r} "
                  f"(cs={item.get('case_size_raw', '')!r}) → {canonical!r} "
                  f"[{product.inventory_class}] — refusing FK attach.")
            product = None
            item = {**item, 'confidence': 'unmatched_class_mismatch'}
            canonical = None

        # Sheet/DB drift detection: the mapper returned a canonical string
        # but no Product with that canonical_name exists in the DB. This
        # happens when sheet col F references a Product that was renamed
        # or merged in the DB without a corresponding sheet update — the
        # forward-looking damage class from upstream renames per
        # `feedback_upstream_downstream_planning.md`. Re-tag the row's
        # confidence to surface it loudly in audits + discover_unmapped
        # rather than letting it silently land as a regular 'unmatched'
        # mixed in with truly-novel items. The product FK stays None —
        # db_write never creates Products from mapper output (Product
        # creation is reserved for the curation flow).
        if canonical and product is None:
            print(f"  [!] Sheet/DB drift: canonical {canonical!r} "
                  f"returned by mapper has no Product in DB — "
                  f"tagging row as 'unmatched_drift'")
            item = {**item, 'confidence': 'unmatched_drift'}

        # Phase 2 — Fuzzy quarantine. Fuzzy tiers don't auto-attach FKs.
        # The product= we just looked up is "the mapper's suggestion"; we
        # detach it from the ILI write below and instead create a pending
        # ProductMappingProposal so a human reviews via /mapping-review/
        # before the FK is committed.
        is_fuzzy_quarantine = (
            product is not None
            and item.get('confidence', '') in _FUZZY_TIERS
        )
        if is_fuzzy_quarantine:
            suggested_product = product
            product = None  # detach from this ILI write
            # Re-tag confidence so the row is distinguishable
            new_conf = item.get('confidence') + '_pending'
            score = item.get('score')
            item = {**item, 'confidence': new_conf}
            # Queue a proposal for human review. unique_together guarantees
            # one row per (vendor, raw_description) — respect existing
            # pending or rejected proposals (don't churn the queue).
            raw_for_proposal = item.get('raw_description', '')
            if vendor and raw_for_proposal:
                existing_proposal = ProductMappingProposal.objects.filter(
                    vendor=vendor, raw_description=raw_for_proposal,
                ).first()
                if existing_proposal is None:
                    ProductMappingProposal.objects.create(
                        vendor=vendor,
                        raw_description=raw_for_proposal,
                        suggested_product=suggested_product,
                        score=int(score) if score is not None else None,
                        confidence_tier=item['confidence'].replace('_pending', ''),
                        source='mapper_quarantine',
                        status='pending',
                    )
                # If existing is 'pending' or 'rejected', leave it alone.
                # 'approved' shouldn't happen here (would have hit
                # vendor_exact via the resulting ProductMapping row).

        # Always preserve the raw description — it's the original invoice text
        # and is critical for auditing, even when the item is mapped to a product.
        raw_desc = item.get('raw_description', '')

        unit_price = None
        price_raw  = item.get('unit_price')
        if price_raw not in (None, ''):
            try:
                unit_price = Decimal(str(price_raw))
            except InvalidOperation:
                pass

        extended = None
        ext_raw = item.get('extended_amount')
        if ext_raw not in (None, ''):
            try:
                extended = Decimal(str(ext_raw))
            except InvalidOperation:
                pass
        # Fall back to unit_price when parser didn't supply extended_amount
        # (Sysco lines where qty=1 and unit_price == extended_amount)
        if extended is None and unit_price is not None:
            extended = unit_price

        # Parser emits price_per_unit as $/lb for Sysco catch-weight
        # (parser.py:891) and Exceptional per-lb lines (parser.py:1313).
        # Other vendors don't populate it; field stays null.
        price_per_pound = None
        ppp_raw = item.get('price_per_unit')
        if ppp_raw not in (None, ''):
            try:
                price_per_pound = Decimal(str(ppp_raw))
            except InvalidOperation:
                pass

        # ── Structured invoice-line schema (Phase 1, 2026-05-02) ──────────
        # Capture fields that spatial_matcher (PBM/Exc/FA/Del) and parser
        # already extract but db_write was previously dropping. Each helper
        # is a small Decimal/Integer coerce — parser output may be float,
        # int, str, or None. None propagates through.
        def _to_decimal(raw):
            if raw in (None, ''):
                return None
            try:
                return Decimal(str(raw))
            except InvalidOperation:
                return None

        def _to_int(raw):
            if raw in (None, ''):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        quantity_val = _to_decimal(item.get('quantity'))
        purchase_uom_val = (item.get('unit_of_measure') or item.get('purchase_uom') or '')[:10]
        case_pack_count_val = _to_int(item.get('case_pack_count'))
        case_pack_unit_size_val = _to_decimal(item.get('case_pack_unit_size'))
        case_pack_unit_uom_val = (item.get('case_pack_unit_uom') or '')[:10]
        case_total_weight_lb_val = _to_decimal(item.get('case_total_weight_lb'))
        count_per_lb_low_val = _to_int(item.get('count_per_lb_low'))
        count_per_lb_high_val = _to_int(item.get('count_per_lb_high'))

        confidence = item.get('confidence', '')
        score = item.get('score')
        match_score = int(score) if score is not None else None

        # Price anomaly detection: flag if price is >2x or <0.5x historical avg
        price_flagged = False
        if product and unit_price and unit_price > 0:
            price_flagged = _check_price_anomaly(product, vendor, unit_price)

        # Case size: parser/spatial extracts from the invoice's pack column.
        # When that extraction fails (OCR artifacts, non-standard units,
        # short canonical names with no pack token) fall back to the
        # Product's curated default_case_size. This keeps ILI rows usable
        # for IUP/P# math instead of leaving them blank. Parser quality
        # is still honestly measurable via the subset where inheritance
        # wasn't needed.
        incoming_cs = item.get('case_size_raw', '') or ''
        if not incoming_cs and product is not None and product.default_case_size:
            incoming_cs = product.default_case_size

        # Upsert: if a record for the same (vendor, date, product/description)
        # already exists, update it rather than creating a duplicate.
        # This makes re-processing invoices safe — prices get refreshed in place.
        # Structured pack-fields fallback for vendors whose parser doesn't
        # extract them upstream (PBM, Colonial). When incoming_cs has a
        # decomposable shape ('10/12CT', '21.5LB', '8/38OZ') and the parser
        # didn't already populate the structured fields, decompose now.
        # Skipped when parser already supplied values (Sysco/Exceptional/
        # FarmArt) so authoritative line-item data isn't overwritten by a
        # default_case_size fallback inference.
        if (case_pack_count_val is None and incoming_cs):
            try:
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in sys.path:
                    sys.path.insert(0, _here)
                from parser import _structured_pack_from_case_size
                fallback = _structured_pack_from_case_size(incoming_cs)
                if fallback:
                    case_pack_count_val = fallback.get('case_pack_count')
                    case_pack_unit_size_val = (
                        fallback.get('case_pack_unit_size')
                        if case_pack_unit_size_val is None
                        else case_pack_unit_size_val
                    )
                    case_pack_unit_uom_val = (
                        fallback.get('case_pack_unit_uom') or ''
                        if not case_pack_unit_uom_val
                        else case_pack_unit_uom_val
                    )
                    if case_total_weight_lb_val is None:
                        case_total_weight_lb_val = fallback.get('case_total_weight_lb')
            except Exception:
                pass

        # Phase 4f (2026-05-17): vendor_item_code (Sysco SUPC, Farm Art
        # code, etc.) becomes a first-class persisted field. Captured at
        # parse time from the item dict; used by the primary dedup key
        # below so reprocess upserts existing rows reliably even when
        # raw_description has drifted across parser versions.
        vendor_item_code = (item.get('sysco_item_code')
                            or item.get('vendor_item_code')
                            or item.get('item_code')
                            or '')
        common_fields = dict(
            unit_price=unit_price,
            extended_amount=extended,
            price_per_pound=price_per_pound,
            case_size=incoming_cs,
            source_file=source_file,
            invoice_number=invoice_number,
            product=product,
            raw_description=raw_desc,
            vendor_item_code=vendor_item_code,
            match_confidence=confidence,
            match_score=match_score,
            price_flagged=price_flagged,
            # B6: line-math validation flag set by parser/spatial/rank-pair via
            # invoice_processor/line_math.py. Catch-weight aware. Downstream
            # filters exclude math_flagged=True from price-anomaly baseline,
            # category-spend, COGs, recipe cost. Per Trust LAW.
            math_flagged=bool(item.get('math_flagged')),
            section_hint=_normalize_section_hint(item.get('section')),
            # Structured invoice-line schema (Phase 1)
            quantity=quantity_val,
            purchase_uom=purchase_uom_val,
            case_pack_count=case_pack_count_val,
            case_pack_unit_size=case_pack_unit_size_val,
            case_pack_unit_uom=case_pack_unit_uom_val,
            case_total_weight_lb=case_total_weight_lb_val,
            count_per_lb_low=count_per_lb_low_val,
            count_per_lb_high=count_per_lb_high_val,
        )

        # Phase 4b (Sean 2026-05-06): canonical-FK-based primary dedup.
        # Compute the FK that THIS row would resolve to. When (vendor,
        # source_file, canonical_FK, date) finds an existing row, that's the
        # SAME line — even if raw_description varies between parser runs
        # (the duplicate-ingestion bug found 2026-05-06: 5 partial extractions
        # of 290f7f produced 28 ILIs because raw_description differed).
        # Falls back to the existing keys when FK can't help (vendor lacks
        # catalog, raw_description has no plausible match, or empty source_file).
        # See `project_self_healing_raw_descriptions.md`.
        incoming_fk = None
        if vpl_candidates:
            from invoice_processor.canonical_match import find_canonical_match
            incoming_fk, _ = find_canonical_match(raw_desc, vpl_candidates)

        existing = None
        duplicates_to_merge: list = []
        if parsed_date:
            # PHASE 4f PRIMARY KEY (Sean 2026-05-17):
            #   (vendor, invoice_number, vendor_item_code)
            # The vendor_item_code (Sysco SUPC, etc.) is stable across
            # parser-version drift in the raw_description token cluster.
            # When populated, this is the strongest dedup key — bypasses
            # the desc-based Phase 4d/4e paths entirely. When empty (other
            # vendors lacking item-code extraction), Phase 4d takes over.
            #
            # invoice_date INTENTIONALLY EXCLUDED from the key: multi-photo
            # Sysco invoices have date drift across OCR caches (the date
            # token's OCR result varies per photo). A single invoice can
            # have rows at two different invoice_dates in the DB. Phase 4f
            # ignores date so a SUPC/invoice_number pair matches regardless.
            # invoice_number is the authoritative stable id; date is
            # advisory.
            if vendor_item_code and invoice_number:
                cand_qs = InvoiceLineItem.objects.filter(
                    vendor=vendor,
                    invoice_number=invoice_number,
                    vendor_item_code=vendor_item_code,
                )
                # Multi-row item (qty>1 on multiple cases with same SUPC
                # printed twice — rare) handled by matching unit_price too.
                # Single-match: take it. Multi-match: pick lowest id, mark
                # rest as duplicates_to_merge.
                cand_list = list(cand_qs.order_by('id'))
                if len(cand_list) == 1:
                    existing = cand_list[0]
                elif len(cand_list) > 1:
                    # Prefer unit_price match if disambiguates
                    price_matches = [c for c in cand_list
                                     if c.unit_price == common_fields.get('unit_price')]
                    if price_matches:
                        existing = price_matches[0]
                        duplicates_to_merge = [c for c in price_matches[1:]]
                    else:
                        existing = cand_list[0]
                        duplicates_to_merge = cand_list[1:]

            # PHASE 4f-USER-EDIT (Sean 2026-05-18): when Phase 4f SUPC
            # dedup didn't match, check for a user_edited row with same
            # (invoice_number, unit_price, extended_amount). User audit
            # edits often have empty or wrong vendor_item_code (Sean
            # corrected the desc/price manually without setting the
            # SUPC field, or backfill assigned a UPC fragment that's
            # not what the current parser emits). The parser then
            # re-emits the same item with its proper SUPC — Phase 4f
            # sees the different SUPCs as different items → creates a
            # duplicate alongside the user_edited row.
            #
            # Trust LAW: the user_edited row is authoritative. Match to
            # it; the existing.user_edited skip in the field-overwrite
            # block (commit ec41af4) preserves its values. Net effect:
            # one row remains (the user_edited one), no duplicate
            # created, audit edit survives.
            #
            # Origin: INV 775662001 — Sean's user_edited Rice Arborio
            # (no SUPC) coexisted with the parser's new Rice Arborio
            # (SUPC 2145985). Phase 4f saw different SUPCs → kept
            # both. 4 similar duplicates accumulated, contributing
            # $157.71 (18.06%) gap.
            if existing is None and invoice_number:
                up = common_fields.get('unit_price')
                ext = common_fields.get('extended_amount')
                if up is not None and ext is not None:
                    ue_matches = list(InvoiceLineItem.objects.filter(
                        vendor=vendor,
                        invoice_number=invoice_number,
                        user_edited=True,
                        unit_price=up,
                        extended_amount=ext,
                    ).order_by('id'))
                    if ue_matches:
                        existing = ue_matches[0]

            # PRIMARY KEY (Phase 4c, Sean 2026-05-10; Phase 4d 2026-05-12):
            #   (vendor, canonical_FK, invoice_number, invoice_date,
            #    normalized_raw_description)
            # invoice_number is stable across re-photo + reprocess cycles —
            # eliminates the duplicate-accumulation bug where source_file
            # variants (raw filename / cache hash / hash+N) created separate
            # rows for the same logical invoice line.
            #
            # Phase 4d (2026-05-12) adds normalized raw_description as a
            # tiebreaker. Without it, multiple distinct SKUs that mapper-
            # collapsed onto the same generic Product/VPL share a primary
            # key and overwrite each other (last-write-wins). Reference:
            # INV 775872298 had 3 Gatorade SUPCs (RASP COOL BLUE, LMN/LM,
            # ORANGE) all at $39.99 mapping to Product 143 "Gatorade" via
            # SUPC code tier — pre-4d collapsed to 1 DB row, losing $79.98
            # of real items. Normalized desc (uppercase + collapsed
            # whitespace) preserves distinct SKUs while still collapsing
            # re-photo cycles (where OCR produces matching descriptions).
            def _normalize_desc(s):
                # Strip ALL whitespace + uppercase. Aggressive enough to
                # collapse OCR variations like 'DAIRY MILK 2%, 4/1-GAL'
                # vs 'DAIRY MILK 2 % , 4 / 1 - GAL' (Phase 4b behavior),
                # but still preserves distinct SKUs that have different
                # tokens or different SUPC codes embedded.
                #
                # Phase 4e (2026-05-17): also drop a leading single-letter
                # qty-column marker (Sysco prints C/F/T/E/P/Ο at x<0.17).
                # Older parser versions kept these in the raw_description
                # ('E 115 LB SYS REL BACON ...'); the fixed parser drops
                # them ('115 LB SYS REL BACON ...'). Without normalizing
                # away the prefix here, reprocess can't match these
                # legacy rows and creates duplicates.
                parts = (s or '').upper().split()
                if parts and len(parts[0]) == 1 and (
                        parts[0].isalpha() or parts[0] == 'Ο'):
                    parts = parts[1:]
                return ''.join(parts)

            if incoming_fk is not None and invoice_number:
                norm_incoming = _normalize_desc(raw_desc)
                candidates = list(InvoiceLineItem.objects.filter(
                    vendor=vendor,
                    canonical_vendor_pricelist=incoming_fk,
                    invoice_number=invoice_number,
                    invoice_date=parsed_date,
                ))
                existing = next(
                    (c for c in candidates
                     if _normalize_desc(c.raw_description) == norm_incoming),
                    None,
                )
            # PRIMARY KEY (legacy, Phase 4b — source_file based):
            # Used when invoice_number can't be extracted (vendors lacking
            # extraction logic) — preserves pre-4c behavior.
            #
            # Tolerant of multi-photo +N suffix variants. `reprocess_ocr_cache`
            # uses 'HASH+N' format for merged multi-photo invoices;
            # `reprocess_invoices` uses bare 'HASH'.
            #
            # Phase 4d (2026-05-12) gate: SKIP Phase 4b when invoice_number
            # is present, because Phase 4d above already handled that case
            # authoritatively — including the case where it intentionally
            # returned existing=None (different normalized raw_desc =
            # distinct SKU sharing the same FK). Falling through to Phase
            # 4b would re-collapse those distinct SKUs by the looser
            # (FK + source_file) key, defeating Phase 4d. Reference: INV
            # 775872298 Gatorade case.
            if (existing is None
                    and incoming_fk is not None
                    and source_file
                    and not invoice_number):
                existing = InvoiceLineItem.objects.filter(
                    vendor=vendor,
                    canonical_vendor_pricelist=incoming_fk,
                    source_file=source_file,
                    invoice_date=parsed_date,
                ).first()
                if existing is None:
                    bare_hash = source_file.split('+', 1)[0]
                    existing = InvoiceLineItem.objects.filter(
                        vendor=vendor,
                        canonical_vendor_pricelist=incoming_fk,
                        source_file__startswith=bare_hash,
                        invoice_date=parsed_date,
                    ).first()
            # Fallback 1: raw_description match (stable across mapping changes;
            # finds rows with no FK assigned yet — pre-Phase-4a era data).
            # Use normalized comparison so legacy descs with leading single-
            # letter qty-column markers (E/C/F/Ο) match the fixed parser's
            # clean output. Origin: 2026-05-17 reprocess attempt — without
            # this, every row whose legacy desc has a single-letter prefix
            # creates a duplicate on reprocess.
            #
            # When multiple candidates match the normalized desc, defer to
            # Fallback 2's collapse-on-match logic — leave `existing` None
            # so Fallback 2 can pick the oldest and mark the rest for
            # deletion. Single-match case sets `existing` here directly.
            #
            # Exception for rows with no product (e.g. fee ILIs like
            # 'Sysco Fuel Surcharge'): Fallback 2 requires `product` to
            # be populated and would skip these. For productless rows
            # we must collapse-on-match here OR they accumulate
            # duplicates on every reprocess cycle. Origin: INV 775687424
            # accumulated 2 each of Fuel/CC/Tax fee rows ($56.48 total)
            # across reprocess attempts (2026-05-17).
            if existing is None:
                norm_incoming = _normalize_desc(raw_desc)
                # When invoice_number is set, scope by invoice_number ONLY
                # (drop the date filter). Multi-photo invoices can have OCR
                # date drift — same invoice_number ends up with rows at 2
                # different invoice_dates in the DB. Same fix Phase 4f
                # received for the same reason.
                if invoice_number:
                    cand_qs = InvoiceLineItem.objects.filter(
                        vendor=vendor, invoice_number=invoice_number)
                else:
                    cand_qs = InvoiceLineItem.objects.filter(
                        vendor=vendor, invoice_date=parsed_date)
                matches = [c for c in cand_qs
                           if _normalize_desc(c.raw_description) == norm_incoming]
                # Tiebreak by unit_price: when multiple rows share the
                # normalized desc but represent DISTINCT line items at
                # different prices, narrow to rows matching the incoming
                # unit_price first. Farm Art INV 1650121 had two
                # 'PPR PEPPERS , RED , 11 # X FANCY' lines — one at
                # \$21/case (\$20.79 ext) and one at \$4.60/case (\$9.11
                # ext). Pre-fix Fallback 1 matched both as same row,
                # overwriting the first with the second's values.
                incoming_unit = common_fields.get('unit_price')
                if len(matches) > 1 and incoming_unit is not None:
                    price_matches = [c for c in matches
                                     if c.unit_price == incoming_unit]
                    if len(price_matches) >= 1:
                        matches = price_matches
                if len(matches) == 1:
                    existing = matches[0]
                elif len(matches) > 1 and not product:
                    # Productless rows can't be caught by Fallback 2 —
                    # collapse here. Keep lowest-id, mark rest for delete.
                    matches.sort(key=lambda c: c.id)
                    existing = matches[0]
                    duplicates_to_merge = matches[1:]
            # Fallback 2 (Phase 4c, Sean 2026-05-10): COLLAPSE-ON-MATCH.
            # Match on (vendor, product, date, unit_price, quantity). When
            # multiple existing rows match this key (signaling re-photo
            # duplicates that the primary keys missed), MERGE them: keep
            # the oldest (lowest id) as the survivor, mark the rest for
            # deletion. Pre-4c logic used .first() and silently left the
            # duplicates orphaned — observed 2026-05-10: Farm Art 1654186
            # accumulated 13 duplicate rows across 3 ingest cycles for the
            # same 20-item invoice.
            #
            # Including quantity in the key distinguishes legitimate multi-
            # row cases (same product, same price, but qty=2 second case)
            # from duplicates (same product, same price, qty=1 each ingest).
            #
            # Phase 4d gate (2026-05-12): when Phase 4d was authoritative
            # (invoice_number + incoming_fk both present), it already
            # decided. Otherwise Fallback 2 runs as safety net — but with
            # normalized raw_description filtering so distinct SKUs that
            # mapper-collide on the same generic product (Gatorade case,
            # where incoming_fk is None because the generic Product has no
            # VPL) aren't collapsed by the (product, price, qty) shape
            # alone. Re-photo OCR whitespace/case variations still collapse
            # via normalization match.
            phase_4d_was_authoritative = (
                incoming_fk is not None and bool(invoice_number)
            )
            if existing is None and product and not phase_4d_was_authoritative:
                candidates = list(InvoiceLineItem.objects.filter(
                    vendor=vendor, product=product, invoice_date=parsed_date,
                    unit_price=common_fields.get('unit_price'),
                    quantity=common_fields.get('quantity'),
                ).order_by('id'))
                norm_incoming = _normalize_desc(raw_desc)
                candidates = [
                    c for c in candidates
                    if _normalize_desc(c.raw_description) == norm_incoming
                ]
                if candidates:
                    existing = candidates[0]
                    duplicates_to_merge = candidates[1:]
        target_ili = None
        if parsed_date:
            if existing:
                # Trust LAW: user-edited rows are ground truth. Reprocess
                # must not overwrite them — Sean's paper-truth audit edits
                # (InvoiceLineEdit trail) represent direct human-verified
                # values that outrank any parser output. Skip the field
                # overwrite entirely; the row is what it is. Duplicate
                # cleanup below still runs (orphan dedup is structural,
                # not value-mutating).
                if not getattr(existing, 'user_edited', False):
                    # Fields where a non-None historical value beats a None
                    # incoming value — backfill or prior write may have
                    # populated these from richer sources than the current
                    # parser pass produces.
                    preserve_if_none_fields = {
                        'price_per_pound',
                        'quantity', 'purchase_uom',
                        'case_pack_count', 'case_pack_unit_size',
                        'case_pack_unit_uom', 'case_total_weight_lb',
                        'count_per_lb_low', 'count_per_lb_high',
                    }
                    for field, value in common_fields.items():
                        if (field in preserve_if_none_fields
                                and (value is None or value == '')
                                and getattr(existing, field, None) not in (None, '')):
                            continue
                        setattr(existing, field, value)
                    existing.save()
                target_ili = existing
                # Phase 4c (Sean 2026-05-10): collapse-on-match.
                # When Fallback 2 found additional rows matching the loose
                # key, delete them now — they're re-photo-cycle duplicates
                # that the previous .first()-pick-one logic left orphaned.
                # Cascades through InvoiceLineEdit FK; preserves nothing
                # (these duplicates have no audit history worth keeping).
                if duplicates_to_merge:
                    dup_ids = [d.id for d in duplicates_to_merge]
                    InvoiceLineItem.objects.filter(id__in=dup_ids).delete()
                    print(f"  [✓] Collapsed {len(dup_ids)} duplicate ILI row(s) "
                          f"for {product.canonical_name if product else '?'} "
                          f"(kept #{existing.id}, deleted {dup_ids})")
            else:
                target_ili = InvoiceLineItem.objects.create(
                    vendor=vendor,
                    invoice_date=parsed_date,
                    **common_fields,
                )
        else:
            target_ili = InvoiceLineItem.objects.create(
                vendor=vendor,
                invoice_date=parsed_date,
                **common_fields,
            )

        # Phase 4a (Sean 2026-05-06): assign canonical FK on the resolved row.
        # `incoming_fk` was already computed above for dedup; reuse it here
        # rather than re-tokenizing. Pricing-as-event-driven LAW
        # (`feedback_event_driven_pricing.md`): this only sets identity;
        # never modifies price/qty/ext fields. Skip if FK already set —
        # preserves manual corrections from mapping-review (don't overwrite
        # human-confirmed mappings).
        if (target_ili is not None
                and target_ili.canonical_vendor_pricelist_id is None
                and incoming_fk is not None):
            target_ili.canonical_vendor_pricelist = incoming_fk
            target_ili.save(update_fields=['canonical_vendor_pricelist'])

        # Track C orphan cleanup: a pre-existing placeholder row
        # '[Sysco #NNN]' for the same (vendor, date, SUPC) is superseded
        # whenever the current write produces either:
        #   (a) a mapped row (product resolved), OR
        #   (b) a row with a real raw_description (not the placeholder form)
        # In both cases the new row carries strictly more information than
        # the placeholder. Deleting the placeholder prevents double-counting
        # in category spend, cost-coverage, and reconciliation metrics.
        #
        # Known limitation: when the OLD row has a non-placeholder but
        # wrong raw_description (e.g. parser stole an adjacent desc), and
        # the NEW row carries a correct desc, both survive. The SUPC isn't
        # stored as its own ILI column, so we can't dedup by SUPC at DB
        # level. Post-reprocess cleanup handles those cases.
        supc = item.get('sysco_item_code', '')
        is_placeholder_write = (raw_desc == f'[Sysco #{supc}]')
        if supc and parsed_date and not is_placeholder_write:
            placeholder_desc = f'[Sysco #{supc}]'
            InvoiceLineItem.objects.filter(
                vendor=vendor,
                invoice_date=parsed_date,
                raw_description=placeholder_desc,
            ).delete()

        written += 1

    return written
