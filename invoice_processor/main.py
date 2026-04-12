"""
Invoice processor — main entry point.

Usage:
  python main.py <image_path> [--refresh-mappings] [--dry-run]

Examples:
  python main.py ~/invoices/sysco_march.jpg
  python main.py ~/invoices/sysco_march.jpg --dry-run
  python main.py ~/invoices/sysco_march.jpg --refresh-mappings
"""
import sys
import json
import argparse
from ocr import extract_text
from parser import parse_invoice
from mapper import map_items
from sheets import update_price, append_to_data_sheet
from drive import archive_invoice


def main():
    parser = argparse.ArgumentParser(description="Process an invoice image.")
    parser.add_argument("image_path", help="Path to the invoice image file")
    parser.add_argument("--refresh-mappings", action="store_true",
                        help="Force reload of item mappings from Google Sheet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and map without writing to Sheets or Drive")
    args = parser.parse_args()

    print(f"\nProcessing: {args.image_path}")
    print("=" * 60)

    # Step 1: OCR
    print("1. Running OCR...")
    raw_text = extract_text(args.image_path)

    # Step 2: Parse
    print("2. Parsing invoice...")
    parsed = parse_invoice(raw_text)
    print(f"   Vendor: {parsed['vendor']}")
    print(f"   Date:   {parsed['invoice_date']}")
    print(f"   Items:  {len(parsed['items'])} line items found")

    if not parsed["invoice_date"]:
        print("\n  [!] Could not detect invoice date. Please enter it (YYYY-MM-DD):")
        parsed["invoice_date"] = input("  > ").strip()

    # Step 3: Map items to canonical names
    print("\n3. Mapping items...")
    mapped_items = map_items(parsed["items"], force_refresh=args.refresh_mappings)

    matched   = [i for i in mapped_items if i["confidence"] != "unmatched"]
    unmatched = [i for i in mapped_items if i["confidence"] == "unmatched"]
    print(f"   Matched: {len(matched)}, Unmatched: {len(unmatched)}")

    if args.dry_run:
        print("\n[DRY RUN] Mapped results:")
        print(json.dumps(mapped_items, indent=2))
        print("\n[DRY RUN] Skipping Sheets update and Drive archiving.")
        return

    # Step 4a: Append all items to the Data Sheets transaction log
    print("\n4. Writing to Data Sheets log...")
    append_to_data_sheet(parsed["vendor"], parsed["invoice_date"], mapped_items)

    # Step 4b: Update prices in the monthly inventory tab (matched items only)
    print("\n5. Updating inventory prices...")
    for item in matched:
        if item.get("needs_review"):
            print(f"  [skip] '{item['raw_description']}' flagged for review")
            continue
        update_price(
            canonical_name=item["canonical"],
            vendor=parsed["vendor"],
            unit_price=item["unit_price"],
            case_size=item.get("case_size_raw") or None,
        )

    # Step 5: Archive to Drive
    print("\n5. Archiving to Google Drive...")
    archive_invoice(
        local_image_path=args.image_path,
        vendor=parsed["vendor"],
        invoice_date_str=parsed["invoice_date"],
    )

    print("\nDone.\n")


if __name__ == "__main__":
    main()
