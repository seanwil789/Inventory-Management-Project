"""
Google Sheets read/write operations.
"""
import time
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from config import CREDENTIALS_PATH

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
