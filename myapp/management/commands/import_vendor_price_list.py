"""Ingest a vendor-published order-guide CSV into VendorPriceList.

Captures vendor list prices per SKU per order unit. Distinct from
ProductMapping (raw_description → canonical Product). Same SKU may
appear at multiple units (CASE / HALF_CASE / EACH / LB) — each row
reveals the vendor's sub-case-premium structure.

Default column convention matches Farm Art's order guide
(Item Number / Display Name / Unit / Price). Override with --col-* flags
for other vendors.

Usage:
    # Dry-run (preview)
    python manage.py import_vendor_price_list \\
        --vendor "Farm Art" --csv "data/vendor_exports/Synergy - Default Order Guide (1).csv"

    # Apply with Farm Art's 1% ACH discount
    python manage.py import_vendor_price_list \\
        --vendor "Farm Art" --csv path/to.csv --ach-discount 0.01 --apply

    # Other vendors with different column names
    python manage.py import_vendor_price_list \\
        --vendor "Sysco" --csv sysco_order.csv \\
        --col-sku SUPC --col-desc "Item Description" --col-unit UOM \\
        --col-price "Catalog Price" --apply

Idempotent — re-ingest upserts on (vendor, sku, unit). Captured_at
gets updated on each apply, so the row reflects the latest CSV.
"""
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.models import Vendor, VendorPriceList


class Command(BaseCommand):
    help = "Ingest vendor order-guide CSV → VendorPriceList rows."

    def add_arguments(self, parser):
        parser.add_argument('--vendor', required=True,
                            help='Vendor canonical name (e.g. "Farm Art")')
        parser.add_argument('--csv', required=True,
                            help='Path to CSV file')
        parser.add_argument('--apply', action='store_true',
                            help='Commit changes (default: dry-run)')
        parser.add_argument('--ach-discount', type=str, default='0',
                            help='ACH discount as decimal (e.g. 0.01 = 1%%). Default 0.')
        parser.add_argument('--captured-at', type=str, default=None,
                            help='Capture date (YYYY-MM-DD). Default: today.')
        parser.add_argument('--col-sku', default='Item Number')
        parser.add_argument('--col-desc', default='Display Name')
        parser.add_argument('--col-unit', default='Unit')
        parser.add_argument('--col-price', default='Price')

    def handle(self, *args, **opts):
        try:
            vendor = Vendor.objects.get(name=opts['vendor'])
        except Vendor.DoesNotExist:
            raise CommandError(f"Vendor not found: {opts['vendor']!r}. "
                               f"Existing vendors: "
                               f"{list(Vendor.objects.values_list('name', flat=True))}")

        csv_path = Path(opts['csv'])
        if not csv_path.is_file():
            raise CommandError(f"CSV not found: {csv_path}")

        try:
            ach = Decimal(opts['ach_discount'])
        except InvalidOperation:
            raise CommandError(f"--ach-discount must be a decimal: {opts['ach_discount']!r}")
        if not (0 <= ach < 1):
            raise CommandError("--ach-discount must be in [0, 1)")

        if opts['captured_at']:
            try:
                captured = datetime.strptime(opts['captured_at'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError("--captured-at must be YYYY-MM-DD")
        else:
            captured = date.today()

        # Parse + validate CSV
        rows = []
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            required = [opts['col_sku'], opts['col_desc'], opts['col_unit'], opts['col_price']]
            missing = [c for c in required if c not in reader.fieldnames]
            if missing:
                raise CommandError(
                    f"CSV missing columns {missing}. Found: {reader.fieldnames}. "
                    f"Use --col-sku/--col-desc/--col-unit/--col-price to override."
                )
            for line_no, r in enumerate(reader, start=2):
                sku = (r[opts['col_sku']] or '').strip()
                desc = (r[opts['col_desc']] or '').strip()
                unit = (r[opts['col_unit']] or '').strip()
                price_str = (r[opts['col_price']] or '').strip()
                if not (sku and desc and unit and price_str):
                    self.stdout.write(self.style.WARNING(
                        f"  line {line_no}: skipped (missing field): {r}"))
                    continue
                try:
                    price = Decimal(price_str)
                except InvalidOperation:
                    self.stdout.write(self.style.WARNING(
                        f"  line {line_no}: skipped (bad price {price_str!r})"))
                    continue
                rows.append((sku, desc, unit, price))

        # Compare against existing entries
        existing = {(p.sku, p.unit): p
                    for p in VendorPriceList.objects.filter(vendor=vendor)}

        to_create, to_update, unchanged = [], [], 0
        for sku, desc, unit, price in rows:
            key = (sku, unit)
            if key in existing:
                p = existing[key]
                if (p.list_price != price or p.raw_description != desc
                        or p.ach_discount_pct != ach):
                    to_update.append((p, sku, desc, unit, price))
                else:
                    unchanged += 1
            else:
                to_create.append((sku, desc, unit, price))

        # Stale entries: in DB but not in current CSV
        seen_keys = {(sku, unit) for sku, _, unit, _ in rows}
        stale = [p for k, p in existing.items() if k not in seen_keys]

        # Report
        self.stdout.write(f"\nVendor:        {vendor.name}")
        self.stdout.write(f"CSV:           {csv_path}")
        self.stdout.write(f"CSV rows:      {len(rows)}")
        self.stdout.write(f"Captured at:   {captured}")
        self.stdout.write(f"ACH discount:  {ach * 100}%")
        self.stdout.write("")
        self.stdout.write(f"  to create:   {len(to_create)}")
        self.stdout.write(f"  to update:   {len(to_update)}")
        self.stdout.write(f"  unchanged:   {unchanged}")
        self.stdout.write(f"  stale (in DB, not in CSV): {len(stale)}")
        self.stdout.write("")

        if to_create[:5]:
            self.stdout.write("  Sample creates:")
            for sku, desc, unit, price in to_create[:5]:
                self.stdout.write(f"    {sku:8s} {unit:12s} ${price}  {desc[:50]}")
        if to_update[:5]:
            self.stdout.write("  Sample updates:")
            for p, sku, desc, unit, price in to_update[:5]:
                self.stdout.write(
                    f"    {sku:8s} {unit:12s} "
                    f"${p.list_price} → ${price}  {desc[:50]}")
        if stale[:5]:
            self.stdout.write("  Sample stale (would be retained, not deleted):")
            for p in stale[:5]:
                self.stdout.write(f"    {p.sku:8s} {p.unit:12s} ${p.list_price}  "
                                  f"{p.raw_description[:50]}")

        if not opts['apply']:
            self.stdout.write(self.style.WARNING("\nDry-run — re-run with --apply to commit."))
            return

        with transaction.atomic():
            for sku, desc, unit, price in to_create:
                VendorPriceList.objects.create(
                    vendor=vendor, sku=sku, raw_description=desc,
                    unit=unit, list_price=price,
                    ach_discount_pct=ach, captured_at=captured,
                    source_file=csv_path.name,
                )
            for p, sku, desc, unit, price in to_update:
                p.raw_description = desc
                p.list_price = price
                p.ach_discount_pct = ach
                p.captured_at = captured
                p.source_file = csv_path.name
                p.save()

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: {len(to_create)} created, {len(to_update)} updated."
        ))
