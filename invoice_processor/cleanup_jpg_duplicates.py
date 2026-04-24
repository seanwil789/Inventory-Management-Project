"""One-off: after PDF versions of a set of invoices land cleanly in the
Drive archive, find the prior JPG scans for the same (vendor, date)
combinations and trash them. Preserves nothing else.

Run only after batch.py has successfully processed the PDFs. Operates
only on dates the user provides via --dates.

Usage:
  python invoice_processor/cleanup_jpg_duplicates.py --dry-run \
      --vendor Sysco --dates 2026-03-03,2026-03-06,2026-03-09,2026-04-20

  python invoice_processor/cleanup_jpg_duplicates.py --apply \
      --vendor Sysco --dates 2026-03-03,2026-03-06,2026-03-09,2026-04-20
"""
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import django
django.setup()

from config import DRIVE_ROOT_FOLDER_ID
from drive import get_drive_client, canonical_vendor
from reprocess_archive import walk_archive


_MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December']


def _jpg_candidates_for(drive, vendor: str, dates: set[str]) -> list[dict]:
    """Walk the Drive archive, return JPG files whose date and vendor
    match any of the target (vendor, date) combinations."""
    hits = []
    for drive_file, file_vendor, folder_path in walk_archive(
            drive, DRIVE_ROOT_FOLDER_ID, vendor_filter=vendor):
        ext = os.path.splitext(drive_file["name"])[1].lower()
        if ext not in (".jpg", ".jpeg"):
            continue
        # Parse invoice date from folder path: "YYYY/MM Month YYYY/Vendor/M.D.YY"
        parts = folder_path.strip("/").split("/")
        if len(parts) < 4:
            continue
        year = parts[0]
        # Week folder name: "M.D.YY" or "Week N X.X - Y.Y" — try to parse
        week_frag = parts[3]
        # Typical: "3.03.26" or "3.3.26"
        for seg in week_frag.split():
            if "." in seg and seg.count(".") == 2:
                try:
                    m, d, y = seg.split(".")
                    full_year = int(y) + 2000 if len(y) == 2 else int(y)
                    iso = f"{full_year}-{int(m):02d}-{int(d):02d}"
                    if iso in dates:
                        hits.append({
                            "id": drive_file["id"],
                            "name": drive_file["name"],
                            "date": iso,
                            "folder": folder_path,
                        })
                    break
                except (ValueError, IndexError):
                    continue
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vendor", required=True,
                    help="Vendor name (e.g. 'Sysco')")
    ap.add_argument("--dates", required=True,
                    help="Comma-separated invoice dates (YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true",
                    help="Trash (not hard-delete) the matched JPGs")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only — no changes made")
    args = ap.parse_args()

    dates = {d.strip() for d in args.dates.split(",") if d.strip()}
    print(f"Searching Drive archive for {args.vendor} JPGs on dates: "
          f"{sorted(dates)}\n")

    drive = get_drive_client()
    vendor_canon = canonical_vendor(args.vendor)
    hits = _jpg_candidates_for(drive, vendor_canon, dates)

    if not hits:
        print("  No matching JPGs found in archive.")
        return 0

    print(f"  Found {len(hits)} matching JPG(s):")
    for h in hits:
        print(f"    [{h['date']}] {h['name']:<35}  in {h['folder']}")

    if args.apply:
        print("\n  Trashing (recoverable from Drive trash)...")
        for h in hits:
            drive.files().update(fileId=h["id"], body={"trashed": True}).execute()
            print(f"    [trashed] {h['name']}")
        print(f"\n  Done. {len(hits)} file(s) moved to Drive trash.")
    else:
        print("\n  Dry run. Re-run with --apply to trash these files.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
