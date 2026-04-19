"""
Format a Synergy tab to be customer-facing.

Applies:
  - Title row (row 1): merged, centered, bold
  - Header row (row 2): bold, dark background, white text, frozen
  - Fix header labels: "Column 5" → "Case Price", "Column 8" → "On Hand"
  - Section dividers: bold subcategory text + top border on section transitions
  - Currency formatting on price columns (E, I, J, K)
  - Column widths sized to content
  - Delete junk rows at end of sheet
  - Clean number formatting throughout

Usage:
  python format_sheet.py                          # format active tab
  python format_sheet.py --tab "Synergy Apr 2026" # format specific tab
  python format_sheet.py --dry-run                # preview only
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID, ACTIVE_SHEET_TAB


# ── Color palette ────────────────────────────────────────────────────────────
# RGB floats (0–1) for the Sheets API

DARK_HEADER = {"red": 0.216, "green": 0.278, "blue": 0.31}    # #37474f  blue-grey 800
WHITE       = {"red": 1.0, "green": 1.0, "blue": 1.0}
TITLE_BG    = {"red": 0.102, "green": 0.137, "blue": 0.494}   # #1a237e  indigo 900
SECTION_BG  = {"red": 0.93, "green": 0.94, "blue": 0.95}      # #edf0f2  light grey
BORDER_CLR  = {"red": 0.7, "green": 0.7, "blue": 0.7}         # #b3b3b3  medium grey


def _get_sheet_id(client, tab_name):
    meta = client.get(
        spreadsheetId=SPREADSHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties))"
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"], s["properties"].get("gridProperties", {})
    return None, None


def _col(letter):
    """Convert column letter to 0-based index."""
    return ord(letter.upper()) - ord('A')


def format_sheet(tab_name=None, dry_run=False):
    tab = tab_name or ACTIVE_SHEET_TAB
    client = get_sheets_client()

    sheet_id, grid_props = _get_sheet_id(client, tab)
    if sheet_id is None:
        print(f"[!] Tab '{tab}' not found")
        return

    total_rows = grid_props.get("rowCount", 500)
    total_cols = grid_props.get("columnCount", 26)

    print(f"Formatting '{tab}' (sheetId={sheet_id}, {total_rows} rows, {total_cols} cols)")

    # ── Read all data to find section boundaries ─────────────────────────
    rows = get_sheet_values(SPREADSHEET_ID, f"'{tab}'!A1:K{total_rows}")
    print(f"  Read {len(rows)} rows of data")

    # Find section transition rows (where col A value changes)
    section_first_rows = []  # 0-based row indices
    prev_section = ""
    for i, row in enumerate(rows):
        if i < 2:  # skip title + header
            continue
        sub_cat = row[0].strip() if row and row[0] else ""
        if sub_cat and sub_cat.lower() not in ("sub category", "column 1") and sub_cat != prev_section:
            section_first_rows.append(i)  # 0-based
            prev_section = sub_cat

    print(f"  Found {len(section_first_rows)} section transitions")

    # Find junk rows to delete (from "Column 1" onward)
    junk_start = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Column 1":
            junk_start = i  # 0-based
            break

    # Also find trailing blank rows before junk
    if junk_start:
        while junk_start > 0 and (not rows[junk_start - 1] or all(c.strip() == "" for c in rows[junk_start - 1])):
            junk_start -= 1

    requests = []

    # ── 0. Delete junk rows at end ───────────────────────────────────────
    if junk_start and junk_start < len(rows):
        print(f"  Deleting junk rows {junk_start + 1}–{len(rows)} (0-based {junk_start}–{len(rows) - 1})")
        if not dry_run:
            requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": junk_start,
                        "endIndex": total_rows,
                    }
                }
            })

    # ── 1. Title row (row 1, index 0) ────────────────────────────────────
    # Merge A1:K1, set title text
    title_text = tab.replace("Synergy ", "Synergy Inventory — ").replace(
        "Apr", "April").replace("Mar", "March").replace("Feb", "February").replace(
        "Jan", "January").replace("May", "May").replace("Jun", "June").replace(
        "Jul", "July").replace("Aug", "August").replace("Sep", "September").replace(
        "Oct", "October").replace("Nov", "November").replace("Dec", "December")

    print(f"  Title: '{title_text}'")

    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 11,
            },
            "mergeType": "MERGE_ALL",
        }
    })

    requests.append({
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 1,
            },
            "rows": [{
                "values": [{
                    "userEnteredValue": {"stringValue": title_text},
                    "userEnteredFormat": {
                        "backgroundColor": TITLE_BG,
                        "textFormat": {
                            "foregroundColor": WHITE,
                            "fontSize": 16,
                            "bold": True,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    },
                }]
            }],
            "fields": "userEnteredValue,userEnteredFormat",
        }
    })

    # Fill rest of merged title row with background color
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 1, "endColumnIndex": 11,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": TITLE_BG,
                }
            },
            "fields": "userEnteredFormat.backgroundColor",
        }
    })

    # Set title row height
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": 0, "endIndex": 1,
            },
            "properties": {"pixelSize": 48},
            "fields": "pixelSize",
        }
    })

    # ── 2. Header row (row 2, index 1) ───────────────────────────────────
    header_labels = [
        "Sub Category", "Product", "Vendor", "Location",
        "Case Price", "Case Size", "Unit", "On Hand",
        "IUP", "P/#", "Δ IUP vs Prior"
    ]

    header_values = [{
        "userEnteredValue": {"stringValue": label},
        "userEnteredFormat": {
            "backgroundColor": DARK_HEADER,
            "textFormat": {
                "foregroundColor": WHITE,
                "fontSize": 10,
                "bold": True,
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    } for label in header_labels]

    requests.append({
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1, "endRowIndex": 2,
                "startColumnIndex": 0, "endColumnIndex": 11,
            },
            "rows": [{"values": header_values}],
            "fields": "userEnteredValue,userEnteredFormat",
        }
    })

    # Freeze header row
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 2},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Set header row height
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": 1, "endIndex": 2,
            },
            "properties": {"pixelSize": 32},
            "fields": "pixelSize",
        }
    })

    # ── 3. Column widths ─────────────────────────────────────────────────
    col_widths = {
        "A": 120,   # Sub Category
        "B": 220,   # Product
        "C": 150,   # Vendor
        "D": 80,    # Location
        "E": 90,    # Case Price
        "F": 80,    # Case Size
        "G": 50,    # Unit
        "H": 70,    # On Hand
        "I": 80,    # IUP
        "J": 70,    # P/#
        "K": 120,   # Δ IUP
    }

    for letter, width in col_widths.items():
        idx = _col(letter)
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": idx, "endIndex": idx + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # ── 4. Currency formatting on price columns ──────────────────────────
    data_end = junk_start if junk_start else len(rows)
    currency_format = {
        "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}
    }

    for col_letter in ["E", "I", "J"]:
        idx = _col(col_letter)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2, "endRowIndex": data_end,
                    "startColumnIndex": idx, "endColumnIndex": idx + 1,
                },
                "cell": {"userEnteredFormat": currency_format},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # ── 5. Data rows — base formatting ───────────────────────────────────
    # Set consistent font on all data rows
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 2, "endRowIndex": data_end,
                "startColumnIndex": 0, "endColumnIndex": 11,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"fontSize": 10},
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat.textFormat.fontSize,userEnteredFormat.verticalAlignment",
        }
    })

    # Center-align specific columns
    for col_letter in ["D", "F", "G", "H"]:
        idx = _col(col_letter)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2, "endRowIndex": data_end,
                    "startColumnIndex": idx, "endColumnIndex": idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {"horizontalAlignment": "CENTER"}
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        })

    # Right-align price columns
    for col_letter in ["E", "I", "J"]:
        idx = _col(col_letter)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2, "endRowIndex": data_end,
                    "startColumnIndex": idx, "endColumnIndex": idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {"horizontalAlignment": "RIGHT"}
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        })

    # ── 6. Section dividers — bold subcategory + top border ──────────────
    border_style = {
        "style": "SOLID",
        "width": 1,
        "color": BORDER_CLR,
    }

    for row_idx in section_first_rows:
        # Bold the subcategory cell (col A)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0, "endColumnIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 10},
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        })

        # Top border across entire row for section transition
        requests.append({
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0, "endColumnIndex": 11,
                },
                "top": border_style,
            }
        })

    # ── 7. Bottom border on last data row ────────────────────────────────
    requests.append({
        "updateBorders": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": data_end - 1, "endRowIndex": data_end,
                "startColumnIndex": 0, "endColumnIndex": 11,
            },
            "bottom": {
                "style": "SOLID_MEDIUM",
                "width": 2,
                "color": DARK_HEADER,
            },
        }
    })

    # ── 8. Thin vertical borders between columns (header + data) ────────
    for col_idx in range(1, 11):
        requests.append({
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1, "endRowIndex": data_end,
                    "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
                },
                "left": {
                    "style": "SOLID",
                    "width": 1,
                    "color": {"red": 0.85, "green": 0.85, "blue": 0.85},
                },
            }
        })

    # ── Execute ──────────────────────────────────────────────────────────
    if dry_run:
        print(f"\n  [DRY RUN] Would send {len(requests)} formatting requests")
        return

    print(f"\n  Sending {len(requests)} formatting requests...")

    # Sheets API has a limit of ~100 requests per batch — split if needed
    BATCH_SIZE = 80
    for i in range(0, len(requests), BATCH_SIZE):
        batch = requests[i:i + BATCH_SIZE]
        client.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": batch},
        ).execute()
        if len(requests) > BATCH_SIZE:
            print(f"    Sent batch {i // BATCH_SIZE + 1} ({len(batch)} requests)")

    print(f"\n  [✓] Formatting complete for '{tab}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Format a Synergy sheet tab")
    parser.add_argument("--tab", default=None, help="Tab name (default: active tab)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    format_sheet(tab_name=args.tab, dry_run=args.dry_run)
