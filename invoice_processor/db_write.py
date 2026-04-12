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
        except ValueError:
            pass

    written = 0
    for item in items:
        canonical = item.get('canonical')
        product   = Product.objects.filter(canonical_name=canonical).first() if canonical else None

        raw_desc  = '' if product else (item.get('canonical') or item.get('raw_description', ''))

        unit_price = None
        price_raw  = item.get('unit_price')
        if price_raw not in (None, ''):
            try:
                unit_price = Decimal(str(price_raw))
            except InvalidOperation:
                pass

        # Upsert: if a record for the same (vendor, date, product/description)
        # already exists, update it rather than creating a duplicate.
        # This makes re-processing invoices safe — prices get refreshed in place.
        if product and parsed_date:
            lookup = dict(vendor=vendor, product=product, invoice_date=parsed_date)
        elif parsed_date:
            lookup = dict(vendor=vendor, raw_description=raw_desc, invoice_date=parsed_date)
        else:
            lookup = None  # no reliable key — fall back to plain create

        if lookup:
            InvoiceLineItem.objects.update_or_create(
                defaults=dict(
                    unit_price=unit_price,
                    case_size=item.get('case_size_raw', ''),
                    source_file=source_file,
                    # re-link product in case it was unmatched on the first pass
                    product=product,
                    raw_description=raw_desc,
                ),
                **lookup,
            )
        else:
            InvoiceLineItem.objects.create(
                vendor=vendor,
                product=product,
                raw_description=raw_desc,
                unit_price=unit_price,
                case_size=item.get('case_size_raw', ''),
                invoice_date=parsed_date,
                source_file=source_file,
            )
        written += 1

    return written
