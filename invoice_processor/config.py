import os
import re
from calendar import month_name
from datetime import date
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


def _current_synergy_tab() -> str:
    """
    Derive the active Synergy tab name from the current date.
    Returns e.g. 'Synergy Apr 2026' for April 2026.

    Can be overridden via ACTIVE_SHEET_TAB env var if needed.
    """
    override = os.getenv("ACTIVE_SHEET_TAB")
    if override:
        return override
    today = date.today()
    return f"Synergy {month_name[today.month][:3]} {today.year}"


# The tab name for the current month's inventory (auto-detected from date)
ACTIVE_SHEET_TAB = _current_synergy_tab()

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

# Document AI processor settings
DOCAI_PROJECT_ID   = os.getenv("DOCAI_PROJECT_ID", "")
DOCAI_LOCATION     = os.getenv("DOCAI_LOCATION", "us")
DOCAI_PROCESSOR_ID = os.getenv("DOCAI_PROCESSOR_ID", "")
