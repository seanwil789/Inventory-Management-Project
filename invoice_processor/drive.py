"""
Google Drive operations: find/create folder hierarchy and archive invoice files.

Hierarchy: Kitchen Invoices / YYYY / MM MonthName YYYY / Vendor / Week N MM.DD - MM.DD
"""
import os
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2 import service_account
from config import CREDENTIALS_PATH, DRIVE_ROOT_FOLDER_ID

SCOPES = ["https://www.googleapis.com/auth/drive"]

_folder_cache: dict[tuple, str] = {}   # (name, parent_id) → folder_id

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def get_drive_client():
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials)


def _find_or_create_folder(drive, name: str, parent_id: str) -> str:
    """Return the folder ID for `name` under `parent_id`, creating it if needed."""
    cache_key = (name, parent_id)
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    query = (
        f"name = '{name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = drive.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        folder_id = files[0]["id"]
    else:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = drive.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]

    _folder_cache[cache_key] = folder_id
    return folder_id


def _week_label(invoice_date: datetime) -> str:
    """
    Build a week label matching your existing format: "Week N MM.DD - MM.DD"
    Week 1 starts on the 1st of the month.
    """
    day = invoice_date.day
    week_num = ((day - 1) // 7) + 1

    # Week start = Monday of that ISO week, clamped to month start
    week_start = invoice_date - timedelta(days=invoice_date.weekday())
    if week_start.month != invoice_date.month:
        week_start = invoice_date.replace(day=1)

    week_end = week_start + timedelta(days=6)
    if week_end.month != invoice_date.month:
        # Clamp to month end
        import calendar
        last_day = calendar.monthrange(invoice_date.year, invoice_date.month)[1]
        week_end = invoice_date.replace(day=last_day)

    return f"Week {week_num} {week_start.strftime('%-m.%-d')} - {week_end.strftime('%-m.%-d')}"


def archive_invoice(file_id: str, file_name: str,
                    vendor: str, invoice_date_str: str,
                    inbox_folder_id: str) -> None:
    """
    Move a file that's already in the Drive inbox into the archive hierarchy.
    Uses files().update() to change parents — no upload, no quota required.

    invoice_date_str: YYYY-MM-DD
    """
    drive = get_drive_client()
    date  = datetime.strptime(invoice_date_str, "%Y-%m-%d")

    year_folder  = str(date.year)
    month_folder = f"{date.month:02d} {MONTH_NAMES[date.month]} {date.year}"
    week_folder  = _week_label(date)

    # Build the destination folder path
    year_id   = _find_or_create_folder(drive, year_folder,  DRIVE_ROOT_FOLDER_ID)
    month_id  = _find_or_create_folder(drive, month_folder, year_id)
    vendor_id = _find_or_create_folder(drive, vendor,       month_id)
    week_id   = _find_or_create_folder(drive, week_folder,  vendor_id)

    # Move: add new parent, remove old parent (inbox) — no bytes transferred
    drive.files().update(
        fileId=file_id,
        addParents=week_id,
        removeParents=inbox_folder_id,
        fields="id, parents",
    ).execute()

    print(f"   Moved to: {year_folder}/{month_folder}/{vendor}/{week_folder}/{file_name}")
