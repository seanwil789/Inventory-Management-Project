"""
Database write layer — replaces append_to_data_sheet() in sheets.py.

Writes processed invoice line items to the Django database.
Must be called from within a Django context (settings configured).
"""
import os
import sys
import django
from decimal import Decimal, InvalidOperation
from datetime import datetime

# Bootstrap Django if not already set up
if not os.environ.get('DJANGO_SETTINGS_MODULE'):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'myproject.settings'
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    django.setup()

from myapp.models import Vendor, Product, InvoiceLineItem
from django.db.models import Avg


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

        confidence = item.get('confidence', '')
        score = item.get('score')
        match_score = int(score) if score is not None else None

        # Price anomaly detection: flag if price is >2x or <0.5x historical avg
        price_flagged = False
        if product and unit_price and unit_price > 0:
            price_flagged = _check_price_anomaly(product, vendor, unit_price)

        # Upsert: if a record for the same (vendor, date, product/description)
        # already exists, update it rather than creating a duplicate.
        # This makes re-processing invoices safe — prices get refreshed in place.
        common_fields = dict(
            unit_price=unit_price,
            extended_amount=extended,
            price_per_pound=price_per_pound,
            case_size=item.get('case_size_raw', ''),
            source_file=source_file,
            product=product,
            raw_description=raw_desc,
            match_confidence=confidence,
            match_score=match_score,
            price_flagged=price_flagged,
            section_hint=(item.get('section') or '')[:60],
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
                for field, value in common_fields.items():
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
        # Triggered by parser extraction improvements (e.g. inline-prefix
        # capture, catch-weight column-dump pass) that produce rows with
        # real descriptions on SUPCs that were previously only seen as
        # placeholders.
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
