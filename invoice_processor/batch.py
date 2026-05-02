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

from config import DRIVE_INBOX_FOLDER_ID, DOCAI_PROCESSOR_ID
from ocr import extract_text
from parser import parse_invoice
from mapper import load_mappings, map_items
from db_write import write_invoice_to_db
from drive import archive_invoice, get_drive_client
from csv_ingest import ingest_csv
from synergy_sync import sync_prices_from_items
from docai import parse_with_docai, ocr_with_docai

SUPPORTED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/jpg", "application/pdf"
}

# ── Invoice total cache for budget sync ─────────────────────────────────
import json

_INVOICE_TOTALS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".invoice_totals"
)


def _cache_invoice_total(vendor, invoice_date, total, source_file):
    """
    Cache an invoice total for later budget sync pickup.
    Stored as JSON files in .invoice_totals/ keyed by year-month.

    Dedup rules (per #1 March-cache-double-count fix, 2026-05-02):
      1. Same (vendor, date, source_file) → already cached, skip
      2. Same (vendor, date, round(total, 2)) AND existing has source='budget_csv'
         → REPLACE the budget_csv entry with this pipeline write (better
         provenance — the CSV fallback was a placeholder).
      3. Same (vendor, date, round(total, 2)) AND existing has the same
         filename source → skip (already cached, source_file may differ
         in case from earlier runs).
    """
    os.makedirs(_INVOICE_TOTALS_DIR, exist_ok=True)
    month_key = str(invoice_date)[:7]  # "2026-04"
    cache_file = os.path.join(_INVOICE_TOTALS_DIR, f"{month_key}.json")

    # Load existing
    entries = []
    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            entries = json.load(f)

    rounded_total = round(total, 2)
    new_entry = {
        "vendor": vendor,
        "date": str(invoice_date),
        "total": rounded_total,
        "source_file": source_file,
    }

    # Rule 1: Same source_file → already cached
    for e in entries:
        if (e["vendor"] == vendor
                and e["date"] == str(invoice_date)
                and e["source_file"] == source_file):
            return

    # Rule 2: Same (vendor, date, total) AND existing is budget_csv → replace it
    replaced = False
    for i, e in enumerate(entries):
        if (e["vendor"] == vendor
                and e["date"] == str(invoice_date)
                and round(float(e.get("total", 0)), 2) == rounded_total
                and e.get("source") == "budget_csv"):
            entries[i] = new_entry
            replaced = True
            print(f"   [budget] Replaced budget_csv entry with pipeline write: "
                  f"${total:.2f} {vendor} {invoice_date}")
            break

    # Rule 3: Same (vendor, date, total) AND existing has filename → skip
    # (covers the case where reprocess re-emits the same OCR cache with a
    # different source_file due to upsert key drift)
    if not replaced:
        for e in entries:
            if (e["vendor"] == vendor
                    and e["date"] == str(invoice_date)
                    and round(float(e.get("total", 0)), 2) == rounded_total):
                # Already have an amount-matching entry for this vendor+date.
                # Skip this write to prevent double-counting.
                return

    if not replaced:
        entries.append(new_entry)

    with open(cache_file, "w") as f:
        json.dump(entries, f, indent=2)

    if not replaced:
        print(f"   [budget] Cached invoice total: ${total:.2f} for {vendor} {invoice_date}")


def _ensure_monthly_tab():
    """
    Check if the current month's Synergy tab exists. If not, create it
    automatically. This runs at the start of every batch so the first
    invoice of a new month triggers tab creation — no manual step needed.
    """
    from datetime import date
    from calendar import month_name as mn

    today = date.today()
    expected_tab = f"Synergy {mn[today.month][:3]} {today.year}"

    try:
        from synergy_sync import _list_synergy_tabs, create_month_sheet
        from sheets import get_sheets_client

        client = get_sheets_client()
        existing = _list_synergy_tabs(client)

        if expected_tab not in existing:
            print(f"[auto] Creating new monthly tab '{expected_tab}'...")
            create_month_sheet(today.year, today.month)
            # Immediately populate the fresh tab with latest-known prices
            # for every product that has ANY invoice history. The tab was
            # duplicated from last month with prices+on-hand cleared, so
            # without this step most rows show blank until a matching
            # current-month invoice arrives — and some products are
            # ordered only every few months. Carryover fills those gaps
            # on day one. Prices remain static thereafter unless a
            # current-month invoice refreshes them via sync_prices_from_items.
            print(f"[auto] Populating '{expected_tab}' with latest-known prices...")
            try:
                from synergy_sync import refresh_stale_carryover
                summary = refresh_stale_carryover(sheet_tab=expected_tab)
                print(f"[auto] Carryover: refreshed {summary['refreshed']}, "
                      f"no history {summary['skipped_no_history']}.")
            except Exception as e:
                print(f"[!] Carryover refresh skipped: {e}")
            print()
        # else: tab already exists, nothing to do
    except Exception as e:
        # Don't let tab creation failure block invoice processing
        print(f"[!] Could not auto-create monthly tab: {e}")
        print(f"    Run manually: python synergy_sync.py --create-month {today.year} {today.month}")
        print()
CSV_MIME_TYPE = "text/csv"


def _backfill_descriptions(docai_items: list[dict], vision_items: list[dict]):
    """
    Fill in missing descriptions on DocAI items by matching them to Vision items.

    Matching strategy: for each DocAI item missing a description, find a Vision
    item with the same price (within $0.01 tolerance). Uses each Vision item
    at most once to avoid duplicate matches.
    """
    used_vision = set()

    for di in docai_items:
        if di.get("raw_description", "").strip():
            continue  # already has a description

        di_price = di.get("unit_price")
        if di_price is None:
            continue

        # Find best Vision match by price
        best_idx = None
        best_diff = float("inf")

        for vi_idx, vi in enumerate(vision_items):
            if vi_idx in used_vision:
                continue
            vi_price = vi.get("unit_price")
            if vi_price is None:
                continue
            diff = abs(di_price - vi_price)
            if diff < best_diff:
                best_diff = diff
                best_idx = vi_idx

        if best_idx is not None and best_diff <= 0.01:
            vi = vision_items[best_idx]
            vi_desc = vi.get("raw_description", "").strip()
            if vi_desc:
                di["raw_description"] = vi_desc
                # Also grab case size if DocAI missed it
                if not di.get("case_size_raw") and vi.get("case_size_raw"):
                    di["case_size_raw"] = vi["case_size_raw"]
                used_vision.add(best_idx)


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

        # 2. Parse invoice
        #    Strategy by vendor:
        #    - Sysco: DocAI OCR → Sysco parser (DocAI text + our parser = no merges)
        #    - Other vendors: DocAI entity extraction (handles Farm Art, Exceptional
        #      better than Vision+parser since their parsers need updating)
        #    Falls back to Vision+regex if DocAI is unavailable.
        parsed = None
        parse_method = "Vision+regex"
        raw_text = None

        if DOCAI_PROCESSOR_ID:
            print("2. Running Document AI...")
            docai_ocr = ocr_with_docai(tmp_path)
            if docai_ocr and docai_ocr.get("raw_text"):
                vendor = docai_ocr["vendor"]
                print(f"   [DocAI] vendor={vendor}, date={docai_ocr['invoice_date'] or '?'}")

                if vendor in ("Sysco", "Exceptional Foods", "Farm Art"):
                    # Sysco + Exceptional + Farm Art: DocAI OCR text + vendor-specific parser
                    # These vendors have structured column layouts that our parsers
                    # handle better than DocAI entity extraction
                    raw_text = docai_ocr["raw_text"]
                    parsed = parse_invoice(raw_text, vendor=vendor,
                                           pages=docai_ocr.get("pages"))
                    parse_method = f"DocAI OCR + {vendor} parser"
                    # Use DocAI's vendor/date detection
                    if docai_ocr["vendor"] != "Unknown":
                        parsed["vendor"] = docai_ocr["vendor"]
                    if docai_ocr["invoice_date"]:
                        parsed["invoice_date"] = docai_ocr["invoice_date"]
                else:
                    # Other vendors: use DocAI entity extraction
                    parsed = parse_with_docai(tmp_path)
                    if parsed and parsed.get("items"):
                        parse_method = "DocAI entities"
                    else:
                        parsed = None
            else:
                print("   [DocAI] OCR failed, falling back to Vision API...")

        if parsed is None:
            print("2. Running Vision API OCR...")
            raw_text = extract_text(tmp_path)
            print("3. Parsing invoice...")
            parsed = parse_invoice(raw_text)
        print(f"   Vendor : {parsed['vendor']}")
        print(f"   Date   : {parsed['invoice_date'] or '(not detected)'}")
        print(f"   Items  : {len(parsed['items'])} line items")
        print(f"   Method : [{parse_method}]")

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
            # Also catches documents the parser explicitly rejected via the
            # rejected_reason flag (pick sheets, packing slips).
            rej = parsed.get("rejected_reason")
            if rej:
                print(f"   [!] REJECTED: {rej} — archiving without DB write.")
            else:
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

        print("\n   Syncing prices to Synergy sheet...")
        price_summary = sync_prices_from_items(
            mapped_items, vendor=parsed["vendor"],
            invoice_date=parsed["invoice_date"],
        )
        if price_summary.get("skipped_wrong_month"):
            print(f"   Price sync — Skipped (invoice not in active tab month)")
        else:
            print(f"   Price sync — Updated: {price_summary['updated']}  |  "
                  f"No match: {price_summary['skipped_no_match']}  |  "
                  f"No price: {price_summary['skipped_no_price']}")

        # 5b. Cache invoice total for budget sync
        invoice_total = parsed.get("invoice_total")
        if invoice_total and parsed["invoice_date"]:
            _cache_invoice_total(
                parsed["vendor"], parsed["invoice_date"],
                invoice_total, file_name,
            )

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

    # ── Auto-create monthly Synergy tab if needed ──────────────────────────
    _ensure_monthly_tab()

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
