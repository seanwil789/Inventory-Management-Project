"""
Batch invoice processor — reads from a Google Drive inbox folder.

Workflow:
  1. List all files in the Drive "New Invoices" folder
  2. Process any Sysco CSV exports first — updates the SUPC code library in
     Item Mapping, then archives the CSV to Drive
  3. Download each image/PDF to a temp file
  4. OCR → parse → map → append to Data Sheets → archive to Drive hierarchy
  5. Delete each file from the inbox once processed

Usage:
  python batch.py                     # process all invoices in Drive inbox
  python batch.py --dry-run           # preview without writing anything
  python batch.py --refresh-mappings  # reload item mappings from sheet first
"""
import os
import sys
import tempfile
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from googleapiclient.http import MediaIoBaseDownload
import io

from config import DRIVE_INBOX_FOLDER_ID
from ocr import extract_text
from parser import parse_invoice
from mapper import load_mappings, map_items
from sheets import append_to_data_sheet
from db_write import write_invoice_to_db
from drive import archive_invoice, get_drive_client
from csv_ingest import ingest_csv
from synergy_sync import sync_prices_from_items

SUPPORTED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/jpg", "application/pdf"
}
CSV_MIME_TYPE = "text/csv"


def list_inbox_files() -> tuple[list[dict], list[dict]]:
    """
    Return (csv_files, invoice_files) currently in the Drive inbox folder.
    CSVs are Sysco portal exports; invoice files are images/PDFs for OCR.
    Both lists are deduplicated by filename.
    """
    drive = get_drive_client()
    results = drive.files().list(
        q=f"'{DRIVE_INBOX_FOLDER_ID}' in parents and trashed = false",
        fields="files(id, name, mimeType)",
    ).execute()

    files = results.get("files", [])

    seen = set()
    csv_files     = []
    invoice_files = []

    for f in files:
        name  = f["name"]
        mime  = f.get("mimeType", "")
        is_csv     = mime == CSV_MIME_TYPE or name.lower().endswith(".csv")
        is_invoice = (mime in SUPPORTED_MIME_TYPES or
                      any(name.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".pdf")))

        if name in seen:
            print(f"   [skip] Duplicate filename in inbox: '{name}' — skipping second copy")
            continue
        seen.add(name)

        if is_csv:
            csv_files.append(f)
        elif is_invoice:
            invoice_files.append(f)

    return csv_files, invoice_files


def download_file(file_id: str, file_name: str) -> str:
    """Download a Drive file to a temp path. Returns the local temp file path."""
    drive   = get_drive_client()
    request = drive.files().get_media(fileId=file_id)
    ext     = os.path.splitext(file_name)[1] or ".jpg"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    downloader = MediaIoBaseDownload(tmp, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    tmp.close()
    return tmp.name


def delete_from_inbox(file_id: str, file_name: str):
    """Permanently delete a file from the inbox folder after successful processing."""
    drive = get_drive_client()
    drive.files().delete(fileId=file_id).execute()
    print(f"   Removed '{file_name}' from New Invoices inbox")


def process_csv(drive_file: dict, dry_run: bool) -> bool:
    """
    Download a Sysco CSV from Drive, run the SUPC code ingestor, then archive.
    Returns True on success, False on failure.
    """
    file_id   = drive_file["id"]
    file_name = drive_file["name"]

    print(f"\n{'='*60}")
    print(f"CSV: {file_name}")
    print(f"{'='*60}")

    tmp_path = None
    try:
        print("1. Downloading CSV from Drive...")
        tmp_path = download_file(file_id, file_name)

        print("2. Ingesting SUPC codes into Item Mapping...")
        summary = ingest_csv(tmp_path, dry_run=dry_run)
        print(f"   Total: {summary['total']}  |  "
              f"Matched: {summary['matched']}  |  "
              f"New stubs: {summary['added']}  |  "
              f"Already mapped: {summary['skipped']}")

        if dry_run:
            print("\n[DRY RUN] Skipping Drive archive and inbox deletion.")
            return True

        print("\n3. Archiving CSV to Drive...")
        # Archive under Vendor=Sysco, date derived from filename if possible
        import re as _re
        date_match = _re.search(r'(\w{3})\s+(\d{2})\s+(\d{4})', file_name)
        if date_match:
            from datetime import datetime
            try:
                invoice_date = datetime.strptime(
                    f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}",
                    "%b %d %Y"
                ).strftime("%Y-%m-%d")
            except ValueError:
                invoice_date = ""
        else:
            invoice_date = ""

        try:
            if invoice_date:
                archive_invoice(file_id, file_name, "Sysco", invoice_date, DRIVE_INBOX_FOLDER_ID)
            else:
                delete_from_inbox(file_id, file_name)
        except Exception as archive_err:
            print(f"   [!] Could not archive/delete CSV from Drive: {archive_err}")
            print(f"       You can manually delete '{file_name}' from the New Invoices folder.")

        return True

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def process_one(drive_file: dict, dry_run: bool, mappings: dict) -> bool:
    """
    Download and process a single invoice from Drive.
    Returns True on success, False on failure.
    """
    file_id   = drive_file["id"]
    file_name = drive_file["name"]

    print(f"\n{'='*60}")
    print(f"File: {file_name}")
    print(f"{'='*60}")

    tmp_path = None
    try:
        # 1. Download from Drive inbox
        print("1. Downloading from Drive...")
        tmp_path = download_file(file_id, file_name)

        # 2. OCR
        print("2. Running OCR...")
        raw_text = extract_text(tmp_path)

        # 3. Parse
        print("3. Parsing invoice...")
        parsed = parse_invoice(raw_text)
        print(f"   Vendor : {parsed['vendor']}")
        print(f"   Date   : {parsed['invoice_date'] or '(not detected)'}")
        print(f"   Items  : {len(parsed['items'])} line items")

        if not parsed["invoice_date"]:
            if sys.stdin.isatty():
                print("\n   [!] Could not detect invoice date.")
                print("   Enter date manually (YYYY-MM-DD) or press Enter to skip:")
                parsed["invoice_date"] = input("   > ").strip()
            else:
                print("\n   [!] Could not detect invoice date — running non-interactively, skipping.")

        if not parsed["items"]:
            # Non-itemized pages (e.g. Sysco cover/totals pages) — archive and
            # remove from inbox so the inbox stays clean, but don't write to sheets.
            print("   [!] No line items found — treating as non-itemized page.")
            if dry_run:
                print("\n[DRY RUN] Would archive non-itemized page and remove from inbox.")
                return True
            if parsed["invoice_date"]:
                print("\n   Archiving non-itemized page to Drive...")
                archive_invoice(file_id, file_name, parsed["vendor"],
                                parsed["invoice_date"], DRIVE_INBOX_FOLDER_ID)
            else:
                print("\n   Removing from inbox (no date, skipping archive)...")
                delete_from_inbox(file_id, file_name)
            return True

        # 4. Map items
        print("\n4. Mapping items...")
        mapped_items = map_items(parsed["items"], mappings=mappings, vendor=parsed["vendor"])
        matched   = sum(1 for i in mapped_items if i["confidence"] != "unmatched")
        unmatched = len(mapped_items) - matched
        print(f"   Matched: {matched}  |  Unmatched: {unmatched}")

        if dry_run:
            print("\n[DRY RUN] Skipping Data Sheets write, Drive archive, and inbox deletion.")
            return True

        # 5. Write to database (and Data Sheets for Synergy sync)
        print("\n5. Writing to database...")
        db_rows = write_invoice_to_db(
            parsed["vendor"], parsed["invoice_date"], mapped_items,
            source_file=file_name,
        )
        print(f"   [✓] {db_rows} rows written to database")

        print("   Syncing to Data Sheets log...")
        append_to_data_sheet(parsed["vendor"], parsed["invoice_date"], mapped_items)

        print("\n   Syncing prices to Synergy sheet...")
        price_summary = sync_prices_from_items(mapped_items, vendor=parsed["vendor"])
        print(f"   Price sync — Updated: {price_summary['updated']}  |  "
              f"No match: {price_summary['skipped_no_match']}  |  "
              f"No price: {price_summary['skipped_no_price']}")

        # 6. Move original file from inbox to archive hierarchy in Drive
        if parsed["invoice_date"]:
            print("\n6. Archiving to Google Drive...")
            archive_invoice(file_id, file_name, parsed["vendor"],
                            parsed["invoice_date"], DRIVE_INBOX_FOLDER_ID)
        else:
            print("\n6. Skipping Drive archive (no date) — removing from inbox...")
            delete_from_inbox(file_id, file_name)

        return True

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def main():
    parser = argparse.ArgumentParser(
        description="Process all invoices in the Google Drive New Invoices folder."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to Sheets, Drive, or deleting files")
    parser.add_argument("--refresh-mappings", action="store_true",
                        help="Reload item mappings from Google Sheet before processing")
    args = parser.parse_args()

    if not DRIVE_INBOX_FOLDER_ID:
        print("[!] DRIVE_INBOX_FOLDER_ID is not set in .env")
        return

    print(f"Checking Drive inbox for new files...")
    csv_files, invoice_files = list_inbox_files()

    if not csv_files and not invoice_files:
        print("No files found in the New Invoices folder.")
        print("Drop invoice images or Sysco CSV exports into that folder and run again.")
        return

    if args.dry_run:
        print("[DRY RUN MODE — nothing will be written or deleted]\n")

    # ── Step 1: Process Sysco CSVs first to enrich the code library ──────────
    csv_success, csv_failed = 0, 0
    if csv_files:
        print(f"\nFound {len(csv_files)} Sysco CSV export(s) — updating code library first...")
        for drive_file in csv_files:
            ok = process_csv(drive_file, args.dry_run)
            if ok:
                csv_success += 1
            else:
                csv_failed += 1
        # Reload mappings so newly added codes are available for invoice processing
        args.refresh_mappings = True

    # ── Step 2: Process invoice images/PDFs ──────────────────────────────────
    success, failed = 0, 0
    if invoice_files:
        print(f"\nFound {len(invoice_files)} invoice(s) to process.")
        print("Loading item mappings...")
        mappings = load_mappings(force_refresh=args.refresh_mappings)
        print(f"  {len(mappings.get('desc_map', {}))} description mappings, "
              f"{len(mappings.get('code_map', {}))} item code mappings loaded.")

        for drive_file in invoice_files:
            ok = process_one(drive_file, args.dry_run, mappings)
            if ok:
                success += 1
            else:
                failed += 1
    elif not csv_files:
        print("No invoice images found.")

    print(f"\n{'='*60}")
    if csv_files:
        print(f"CSVs     — Processed: {csv_success}  |  Failed: {csv_failed}")
    if invoice_files:
        print(f"Invoices — Processed: {success}  |  Failed: {failed}")
    if failed or csv_failed:
        print("Failed files remain in the New Invoices folder for manual review.")
    print()


if __name__ == "__main__":
    main()
