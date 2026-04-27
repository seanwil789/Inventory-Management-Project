"""
Reprocess archived invoices — compare Vision+regex vs Document AI results.

Walks the entire Drive archive hierarchy under DRIVE_ROOT_FOLDER_ID:
  Kitchen Invoices / YYYY / MM MonthName YYYY / Vendor / Week N / files

For each invoice image/PDF:
  1. Downloads to temp file
  2. Runs Vision+regex pipeline
  3. Runs Document AI pipeline
  4. Compares vendor, date, and line-item counts side by side

Usage:
  python reprocess_archive.py                # comparison mode (read-only)
  python reprocess_archive.py --live         # rewrite database + sheets with DocAI results
  python reprocess_archive.py --vendor Sysco # only reprocess one vendor
"""
import os
import sys
import json
import tempfile
import argparse
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from googleapiclient.http import MediaIoBaseDownload
from config import DRIVE_ROOT_FOLDER_ID, DOCAI_PROCESSOR_ID
from drive import get_drive_client

MAX_RETRIES = 3
from ocr import extract_text
from parser import parse_invoice
from docai import parse_with_docai
from mapper import load_mappings, map_items
from db_write import write_invoice_to_db
from synergy_sync import sync_prices_from_items

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def _retry_drive_call(fn, *args, **kwargs):
    """Retry a Drive API call up to MAX_RETRIES times on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"   [retry {attempt}/{MAX_RETRIES}] Drive API error: {e}")
            time.sleep(2 * attempt)


def list_subfolders(drive, folder_id: str) -> list[dict]:
    """List immediate subfolders of a Drive folder."""
    results = _retry_drive_call(
        drive.files().list(
            q=(f"'{folder_id}' in parents "
               f"and mimeType = 'application/vnd.google-apps.folder' "
               f"and trashed = false"),
            fields="files(id, name)",
            pageSize=200,
        ).execute
    )
    return results.get("files", [])


def list_files(drive, folder_id: str) -> list[dict]:
    """List non-folder files in a Drive folder."""
    results = _retry_drive_call(
        drive.files().list(
            q=(f"'{folder_id}' in parents "
               f"and mimeType != 'application/vnd.google-apps.folder' "
               f"and trashed = false"),
            fields="files(id, name, mimeType)",
            pageSize=500,
        ).execute
    )
    return results.get("files", [])


def download_file(drive, file_id: str, file_name: str) -> str:
    """Download a Drive file to a temp path, with retry on failure."""
    ext = os.path.splitext(file_name)[1] or ".jpg"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = drive.files().get_media(fileId=file_id)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            downloader = MediaIoBaseDownload(tmp, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            tmp.close()
            return tmp.name
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"   [retry {attempt}/{MAX_RETRIES}] Download error: {e}")
            time.sleep(2 * attempt)


def walk_archive(drive, root_id: str, vendor_filter: str = None):
    """
    Generator that yields (file_dict, vendor_name, folder_path) for every
    invoice file in the archive hierarchy.

    Hierarchy: Root / YYYY / MM MonthName YYYY / Vendor / Week N / files
    """
    year_folders = list_subfolders(drive, root_id)
    year_folders.sort(key=lambda f: f["name"])

    for year_folder in year_folders:
        # Skip non-year folders
        if not year_folder["name"].isdigit():
            continue

        month_folders = list_subfolders(drive, year_folder["id"])
        month_folders.sort(key=lambda f: f["name"])

        for month_folder in month_folders:
            vendor_folders = list_subfolders(drive, month_folder["id"])

            for vendor_folder in vendor_folders:
                vendor_name = vendor_folder["name"]

                if vendor_filter and vendor_filter.lower() != vendor_name.lower():
                    continue

                week_folders = list_subfolders(drive, vendor_folder["id"])

                for week_folder in week_folders:
                    files = list_files(drive, week_folder["id"])
                    path = f"{year_folder['name']}/{month_folder['name']}/{vendor_name}/{week_folder['name']}"

                    for f in files:
                        ext = os.path.splitext(f["name"])[1].lower()
                        if ext in SUPPORTED_EXTENSIONS:
                            yield f, vendor_name, path


def compare_results(vision_result: dict, docai_result: dict) -> dict:
    """Compare two parse results and return a summary."""
    v_items = vision_result.get("items", [])
    d_items = docai_result.get("items", []) if docai_result else []

    return {
        "vision_vendor": vision_result.get("vendor", "?"),
        "docai_vendor": docai_result.get("vendor", "?") if docai_result else "FAILED",
        "vision_date": vision_result.get("invoice_date", ""),
        "docai_date": docai_result.get("invoice_date", "") if docai_result else "",
        "vision_items": len(v_items),
        "docai_items": len(d_items),
        "vendor_match": (
            vision_result.get("vendor") == docai_result.get("vendor")
            if docai_result else False
        ),
        "date_match": (
            vision_result.get("invoice_date") == docai_result.get("invoice_date")
            if docai_result else False
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Reprocess archived invoices: compare Vision+regex vs Document AI"
    )
    parser.add_argument("--live", action="store_true",
                        help="Live mode: rewrite database and sheets with DocAI results")
    parser.add_argument("--vendor", type=str, default=None,
                        help="Only reprocess invoices for this vendor")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after processing N invoices (useful for testing)")
    args = parser.parse_args()

    if not DRIVE_ROOT_FOLDER_ID:
        print("[!] DRIVE_ROOT_FOLDER_ID not set in .env")
        return

    if not DOCAI_PROCESSOR_ID:
        print("[!] DOCAI_PROCESSOR_ID not set in .env — cannot run comparison")
        return

    drive = get_drive_client()
    mode = "LIVE" if args.live else "COMPARISON"
    print(f"=== Archive Reprocessor [{mode} MODE] ===\n")

    if args.vendor:
        print(f"Filtering to vendor: {args.vendor}\n")

    # Load mappings once for live mode
    mappings = None
    if args.live:
        print("Loading item mappings...")
        mappings = load_mappings(force_refresh=True)
        print(f"  {len(mappings.get('desc_map', {}))} description mappings, "
              f"{len(mappings.get('code_map', {}))} item code mappings loaded.\n")

    results = []
    processed = 0
    errors = 0

    print("Scanning archive folders...\n")

    for drive_file, vendor_name, folder_path in walk_archive(drive, DRIVE_ROOT_FOLDER_ID, args.vendor):
        if args.limit and processed >= args.limit:
            print(f"\n[Limit reached: {args.limit} invoices]")
            break

        file_name = drive_file["name"]
        file_id = drive_file["id"]
        print(f"{'='*70}")
        print(f"  {folder_path}/{file_name}")
        print(f"{'='*70}")

        tmp_path = None
        try:
            # Download
            print("  Downloading...")
            tmp_path = download_file(drive, file_id, file_name)

            # Vision+regex pipeline
            print("  Running Vision+regex...")
            raw_text = extract_text(tmp_path)
            vision_result = parse_invoice(raw_text)
            print(f"    Vendor: {vision_result['vendor']}  |  "
                  f"Date: {vision_result['invoice_date'] or '?'}  |  "
                  f"Items: {len(vision_result['items'])}")

            # Document AI pipeline
            print("  Running Document AI...")
            docai_result = parse_with_docai(tmp_path)
            if docai_result:
                print(f"    Vendor: {docai_result['vendor']}  |  "
                      f"Date: {docai_result['invoice_date'] or '?'}  |  "
                      f"Items: {len(docai_result['items'])}")
            else:
                print("    [FAILED] Document AI returned None")

            # Compare
            comparison = compare_results(vision_result, docai_result)
            comparison["file"] = f"{folder_path}/{file_name}"
            results.append(comparison)

            # Highlight differences
            if not comparison["vendor_match"]:
                print(f"  ** VENDOR DIFF: Vision={comparison['vision_vendor']} vs DocAI={comparison['docai_vendor']}")
            if not comparison["date_match"]:
                print(f"  ** DATE DIFF: Vision={comparison['vision_date']} vs DocAI={comparison['docai_date']}")
            item_diff = comparison["docai_items"] - comparison["vision_items"]
            if item_diff != 0:
                print(f"  ** ITEM COUNT DIFF: DocAI found {'+' if item_diff > 0 else ''}{item_diff} items")

            # Show item-level detail for comparison
            if not args.live:
                if vision_result["items"] or (docai_result and docai_result["items"]):
                    print(f"\n  {'Vision+regex items:':<40} {'DocAI items:'}")
                    print(f"  {'-'*38}   {'-'*38}")
                    v_items = vision_result.get("items", [])
                    d_items = docai_result.get("items", []) if docai_result else []
                    max_rows = max(len(v_items), len(d_items))
                    for i in range(min(max_rows, 15)):  # cap at 15 rows
                        v_desc = v_items[i]["raw_description"][:35] if i < len(v_items) else ""
                        v_price = f"${v_items[i]['unit_price']:.2f}" if i < len(v_items) and v_items[i].get("unit_price") else ""
                        d_desc = d_items[i]["raw_description"][:35] if i < len(d_items) else ""
                        d_price = f"${d_items[i]['unit_price']:.2f}" if i < len(d_items) and d_items[i].get("unit_price") else ""
                        print(f"  {v_desc:<30} {v_price:>7}   {d_desc:<30} {d_price:>7}")
                    if max_rows > 15:
                        print(f"  ... and {max_rows - 15} more rows")

            # Live mode: rewrite with DocAI results
            if args.live and docai_result and docai_result.get("items"):
                parsed = docai_result
                if not parsed["invoice_date"]:
                    print("  [!] No date detected — skipping live write for this file")
                else:
                    print("\n  [LIVE] Mapping items...")
                    mapped_items = map_items(parsed["items"], mappings=mappings, vendor=parsed["vendor"])
                    matched = sum(1 for i in mapped_items if i["confidence"] != "unmatched")
                    print(f"  [LIVE] Matched: {matched} / {len(mapped_items)}")

                    print("  [LIVE] Writing to database...")
                    db_rows = write_invoice_to_db(
                        parsed["vendor"], parsed["invoice_date"], mapped_items,
                        source_file=file_name,
                    )
                    print(f"  [LIVE] {db_rows} rows written to database")

                    print("  [LIVE] Syncing prices...")
                    price_summary = sync_prices_from_items(
                        mapped_items, vendor=parsed["vendor"],
                        invoice_date=parsed["invoice_date"],
                    )
                    if price_summary.get("skipped_wrong_month"):
                        print(f"  [LIVE] Price sync skipped (invoice not in active tab month)")
                    else:
                        print(f"  [LIVE] Updated: {price_summary['updated']}  |  "
                              f"No match: {price_summary['skipped_no_match']}  |  "
                              f"No price: {price_summary['skipped_no_price']}")

            processed += 1

        except Exception as e:
            print(f"  [ERROR] {e}")
            import traceback
            traceback.print_exc()
            errors += 1

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"SUMMARY — {processed} invoices processed, {errors} errors")
    print(f"{'='*70}")

    if results:
        vendor_matches = sum(1 for r in results if r["vendor_match"])
        date_matches = sum(1 for r in results if r["date_match"])
        more_items = sum(1 for r in results if r["docai_items"] > r["vision_items"])
        fewer_items = sum(1 for r in results if r["docai_items"] < r["vision_items"])
        same_items = sum(1 for r in results if r["docai_items"] == r["vision_items"])
        docai_failed = sum(1 for r in results if r["docai_vendor"] == "FAILED")

        total = len(results)
        print(f"\nVendor detection:  {vendor_matches}/{total} match  "
              f"({total - vendor_matches} differ)")
        print(f"Date detection:    {date_matches}/{total} match  "
              f"({total - date_matches} differ)")
        print(f"Item counts:       DocAI found more: {more_items}  |  "
              f"Fewer: {fewer_items}  |  Same: {same_items}")
        if docai_failed:
            print(f"DocAI failures:    {docai_failed}")

        # Total items comparison
        total_vision = sum(r["vision_items"] for r in results)
        total_docai = sum(r["docai_items"] for r in results)
        print(f"\nTotal line items:  Vision={total_vision}  |  DocAI={total_docai}  "
              f"(diff: {total_docai - total_vision:+d})")

        # Files where DocAI did notably better or worse
        big_diffs = [r for r in results if abs(r["docai_items"] - r["vision_items"]) >= 3]
        if big_diffs:
            print(f"\nNotable differences (>=3 items):")
            for r in big_diffs:
                diff = r["docai_items"] - r["vision_items"]
                print(f"  {'+' if diff > 0 else ''}{diff:>3} items  {r['file']}")

    # Save detailed results to JSON
    report_path = os.path.join(os.path.dirname(__file__), "reprocess_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nDetailed report saved to: {report_path}")


if __name__ == "__main__":
    main()
