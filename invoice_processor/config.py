import os
from dotenv import load_dotenv

# Always resolve .env and relative paths from the project root (parent of this file's dir)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

def _resolve(path: str) -> str:
    """Make a path absolute, anchoring relative paths at the project root."""
    return path if os.path.isabs(path) else os.path.join(_PROJECT_ROOT, path)

# Path to your Google service account credentials JSON file
CREDENTIALS_PATH = _resolve(os.getenv("GOOGLE_CREDENTIALS_PATH", "invoice_processor/credentials/service_account.json"))

# Your Google Sheets spreadsheet ID (from the URL: /spreadsheets/d/<ID>/edit)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# The tab name for the current month's inventory (update each month)
ACTIVE_SHEET_TAB = os.getenv("ACTIVE_SHEET_TAB", "Synergy Jan 2026")

# The tab name for the item mapping table
MAPPING_TAB = os.getenv("MAPPING_TAB", "Item Mapping")

# Google Drive folder ID for the root "Kitchen Invoices" folder
# (from the URL when you open that folder in Drive: /folders/<ID>)
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID", "")

# Google Drive folder ID for the New Invoices inbox
DRIVE_INBOX_FOLDER_ID = os.getenv("DRIVE_INBOX_FOLDER_ID", "")

# Columns in the inventory sheet (1-indexed)
COL_PRODUCT      = 2   # B
COL_VENDOR       = 3   # C
COL_UNIT_PRICE   = 5   # E
COL_CASE_SIZE    = 6   # F
