"""
Google Sheets read/write operations.
"""
import time
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from config import CREDENTIALS_PATH, SPREADSHEET_ID, ACTIVE_SHEET_TAB, COL_PRODUCT, COL_VENDOR, COL_UNIT_PRICE, COL_CASE_SIZE

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API_TIMEOUT = 60  # seconds
MAX_RETRIES = 3
_RETRIABLE_CODES = {429, 500, 502, 503, 504}


def _retry_sheets_call(fn, *args, **kwargs):
    """Retry a Sheets API call on transient errors with exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except HttpError as e:
            if e.resp.status in _RETRIABLE_CODES and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_RETRIES}] Sheets API {e.resp.status}, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  [retry {attempt}/{MAX_RETRIES}] Sheets API error: {e}")
                time.sleep(2 * attempt)
            else:
                raise


def get_sheets_client():
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH, scopes=SCOPES
    )
    http = httplib2.Http(timeout=API_TIMEOUT)
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
    return build("sheets", "v4", http=http).spreadsheets()


def get_sheet_values(spreadsheet_id: str, range_: str) -> list[list]:
    """Read a range from any sheet. Returns list of rows."""
    client = get_sheets_client()
    result = _retry_sheets_call(
        client.values().get(
            spreadsheetId=spreadsheet_id,
            range=range_,
        ).execute
    )
    return result.get("values", [])


def find_product_row(canonical_name: str, vendor: str, sheet_tab: str = None) -> int | None:
    """
    Search the inventory sheet for the row matching canonical_name + vendor.
    Returns 1-based row number, or None if not found.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    rows = get_sheet_values(SPREADSHEET_ID, f"{tab}!A:G")

    for i, row in enumerate(rows, start=1):
        product_col = COL_PRODUCT - 1   # 0-indexed
        vendor_col  = COL_VENDOR - 1

        product = row[product_col].strip() if len(row) > product_col and row[product_col] else ""
        v       = row[vendor_col].strip()  if len(row) > vendor_col  and row[vendor_col]  else ""

        if product.lower() == canonical_name.lower() and v.lower() == vendor.lower():
            return i

    return None


def update_price(canonical_name: str, vendor: str, unit_price: float,
                 case_size: str = None, sheet_tab: str = None) -> bool:
    """
    Find the row for canonical_name + vendor and update Unit Price (and Case Size if provided).
    Returns True if updated, False if the row wasn't found.
    """
    tab = sheet_tab or ACTIVE_SHEET_TAB
    row = find_product_row(canonical_name, vendor, tab)

    if row is None:
        print(f"  [!] Row not found for '{canonical_name}' / '{vendor}' in tab '{tab}'")
        return False

    client = get_sheets_client()

    # Always update Unit Price
    price_cell = f"{tab}!E{row}"  # Column E = Unit Price
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=price_cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[unit_price]]},
    ).execute()

    # Optionally update Case Size if it changed
    if case_size is not None:
        size_cell = f"{tab}!F{row}"  # Column F = Case Size
        client.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=size_cell,
            valueInputOption="USER_ENTERED",
            body={"values": [[case_size]]},
        ).execute()

    print(f"  [✓] Updated '{canonical_name}' → ${unit_price:.2f}" +
          (f" (case size: {case_size})" if case_size else ""))
    return True


DATA_SHEET_TAB = "Data Sheets"


def append_to_data_sheet(vendor: str, invoice_date: str, items: list[dict],
                         skip_if_exists: bool = False) -> int:
    """
    Append raw line items to the Data Sheets transaction log.
    Columns: Vendor | Category | Item Description | Unit Price | Unit | Invoice Date

    If skip_if_exists=True, checks whether this vendor+date already has rows
    in the log and skips the append to prevent duplicates during reprocessing.

    Returns the number of rows appended.
    """
    client = get_sheets_client()

    rows = []
    for item in items:
        rows.append([
            vendor,
            item.get("category", ""),              # Category (from Sheet3 taxonomy)
            item.get("canonical") or item.get("raw_description", ""),
            item.get("unit_price", ""),
            item.get("case_size_raw", ""),
            invoice_date,
        ])

    if not rows:
        return 0

    # Dedup check: skip if this vendor+date already has entries
    if skip_if_exists and vendor and invoice_date:
        existing = get_sheet_values(SPREADSHEET_ID, f"{DATA_SHEET_TAB}!A:F")
        for row in existing[1:]:  # skip header
            if (len(row) >= 6
                    and row[0].strip() == vendor
                    and row[5].strip() == invoice_date):
                print(f"  [skip] '{vendor}' / {invoice_date} already in '{DATA_SHEET_TAB}' — skipping append")
                return 0

    _retry_sheets_call(
        client.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{DATA_SHEET_TAB}!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute
    )

    print(f"  [✓] Appended {len(rows)} rows to '{DATA_SHEET_TAB}'")
    return len(rows)
