"""
Reprocess only JPG invoices from the Drive archive with updated DocAI + Vision fallback.
Skips PDFs (which have the merge-vendor bug on multi-page scans).
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import django
django.setup()

from googleapiclient.http import MediaIoBaseDownload
from config import DRIVE_ROOT_FOLDER_ID, DOCAI_PROCESSOR_ID
from drive import get_drive_client
from docai import ocr_with_docai, parse_with_docai
from ocr import extract_text
from parser import parse_invoice
from mapper import load_mappings, map_items
from db_write import write_invoice_to_db
from sheets import append_to_data_sheet
from synergy_sync import sync_prices_from_items
from reprocess_archive import walk_archive, download_file

def main():
    drive = get_drive_client()
    print("=== JPG Reprocessor [LIVE MODE] ===\n")
    print("Loading item mappings...")
    mappings = load_mappings(force_refresh=True)
    print(f"  {len(mappings.get('desc_map', {}))} desc mappings, "
          f"{len(mappings.get('code_map', {}))} code mappings\n")

    processed = 0
    errors = 0
    total_items = 0
    total_with_desc = 0
    total_matched = 0

    for drive_file, vendor_name, folder_path in walk_archive(drive, DRIVE_ROOT_FOLDER_ID):
        file_name = drive_file["name"]
        ext = os.path.splitext(file_name)[1].lower()

        # Only process JPGs
        if ext not in ('.jpg', '.jpeg'):
            continue

        file_id = drive_file["id"]
        print(f"[{processed+1}] {folder_path}/{file_name}")

        tmp_path = None
        try:
            tmp_path = download_file(drive, file_id, file_name)

            # Sysco: DocAI OCR + Sysco parser. Others: DocAI entity extraction.
            docai_ocr = ocr_with_docai(tmp_path)
            if not docai_ocr or not docai_ocr.get("raw_text"):
                try:
                    raw_text = extract_text(tmp_path)
                    parsed = parse_invoice(raw_text)
                except Exception:
                    print(f"  Both OCR methods failed — skipping")
                    errors += 1
                    continue
            else:
                vendor = docai_ocr["vendor"]
                if vendor == "Sysco":
                    parsed = parse_invoice(docai_ocr["raw_text"], vendor=vendor)
                    if docai_ocr["vendor"] != "Unknown":
                        parsed["vendor"] = docai_ocr["vendor"]
                    if docai_ocr["invoice_date"]:
                        parsed["invoice_date"] = docai_ocr["invoice_date"]
                else:
                    # Non-Sysco: DocAI entity extraction
                    parsed = parse_with_docai(tmp_path)
                    if not parsed or not parsed.get("items"):
                        # Fall back to Vision
                        raw_text = extract_text(tmp_path)
                        parsed = parse_invoice(raw_text, vendor=vendor)

            items = parsed.get("items", [])
            if not items:
                print(f"  No items found — skipping")
                errors += 1
                continue

            n_desc = sum(1 for i in items if i.get("raw_description", "").strip())
            still_missing = len(items) - n_desc

            # Map items
            mapped = map_items(items, mappings=mappings, vendor=parsed["vendor"])
            matched = sum(1 for i in mapped if i["confidence"] != "unmatched")

            # Write to DB
            if parsed.get("invoice_date"):
                db_rows = write_invoice_to_db(
                    parsed["vendor"], parsed["invoice_date"], mapped,
                    source_file=file_name,
                )

                # Sync prices
                sync_prices_from_items(
                    mapped, vendor=parsed["vendor"],
                    invoice_date=parsed["invoice_date"],
                )
            else:
                db_rows = 0

            total_items += len(items)
            total_with_desc += n_desc
            total_matched += matched
            processed += 1

            status = "OK" if still_missing == 0 else f"{still_missing} no desc"
            print(f"  {len(items)} items, {n_desc} desc, {matched} mapped, {db_rows} written — {status}")

        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    print(f"\n{'=' * 60}")
    print(f"DONE — {processed} JPGs processed, {errors} errors")
    print(f"{'=' * 60}")
    print(f"Total items:       {total_items}")
    print(f"With description:  {total_with_desc} ({total_with_desc/max(total_items,1)*100:.1f}%)")
    print(f"Mapped to product: {total_matched} ({total_matched/max(total_items,1)*100:.1f}%)")
    still = total_items - total_with_desc
    print(f"Still missing desc: {still} ({still/max(total_items,1)*100:.1f}%)")


if __name__ == "__main__":
    main()
