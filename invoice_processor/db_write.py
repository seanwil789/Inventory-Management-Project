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


def _check_price_anomaly(product, vendor, unit_price: Decimal) -> bool:
    """
    Check if a price is anomalous compared to the 90-day historical average
    for this product+vendor. Returns True if price is >2x or <0.5x the average.
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
        .aggregate(avg_price=Avg('unit_price'))
    )

    avg_price = avg_result.get('avg_price')
    if avg_price is None or avg_price == 0:
        return False  # no history — can't flag

    ratio = float(unit_price) / float(avg_price)
    return ratio > 2.0 or ratio < 0.5


def write_invoice_to_db(vendor_name: str, invoice_date: str,
                        items: list[dict], source_file: str = '') -> int:
    """
    Persist parsed and mapped invoice line items to the database.

    Args:
        vendor_name:  canonical vendor name (e.g. "Sysco")
        invoice_date: ISO date string "YYYY-MM-DD" or ""
        items:        list of dicts from map_items() — each has
                      canonical, raw_description, unit_price, case_size_raw
        source_file:  original filename for provenance tracking

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
        common_fields = dict(
            unit_price=unit_price,
            extended_amount=extended,
            price_per_pound=price_per_pound,
            case_size=incoming_cs,
            source_file=source_file,
            product=product,
            raw_description=raw_desc,
            match_confidence=confidence,
            match_score=match_score,
            price_flagged=price_flagged,
            section_hint=(item.get('section') or '')[:60],
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

        if product and parsed_date:
            lookup = dict(vendor=vendor, product=product, invoice_date=parsed_date)
        elif parsed_date:
            lookup = dict(vendor=vendor, raw_description=raw_desc, invoice_date=parsed_date)
        else:
            lookup = None  # no reliable key — fall back to plain create

        if lookup:
            existing = InvoiceLineItem.objects.filter(**lookup).first()
            if existing:
                # Fields where a non-None historical value beats a None incoming
                # value — backfill or prior write may have populated these from
                # richer sources than the current parser pass produces.
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
            else:
                InvoiceLineItem.objects.create(
                    vendor=vendor,
                    invoice_date=parsed_date,
                    **common_fields,
                )
        else:
            InvoiceLineItem.objects.create(
                vendor=vendor,
                invoice_date=parsed_date,
                **common_fields,
            )

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
