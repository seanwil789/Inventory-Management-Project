"""
Restructure a Synergy tab into per-category tables with footers and a summary panel.
Rebuilds entirely from the database (source of truth).

Each major category gets:
  - Category header row (colored)
  - Column headers row
  - Data rows (sorted by subcategory)
  - Footer row with SUMPRODUCT(On Hand, IUP) = inventory value
  - Blank separator row

Summary panel on the right (column M+) shows per-category totals.

Usage:
  python restructure_sheet.py                          # restructure active tab
  python restructure_sheet.py --tab "Synergy Apr 2026"
  python restructure_sheet.py --dry-run
"""

import os
import sys
import argparse
import time

sys.path.insert(0, os.path.dirname(__file__))

from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID, ACTIVE_SHEET_TAB
from synergy_sync import calc_iup, calc_price_per_lb

# ── DB category → sheet category mapping ─────────────────────────────
SHEET_CATEGORIES = [
    ("Proteins",               ["Proteins"]),
    ("Produce",                ["Produce"]),
    ("Dairy / Cheese",         ["Dairy", "Cheese"]),
    ("Drystock",               ["Drystock", "Condiments/Sauces", "Spices"]),
    ("Bakery",                 ["Bakery"]),
    ("Coffee / Concessions",   ["Coffee/Concessions", "Beverages"]),
    ("Chemicals / Smallwares", ["Chemicals", "Paper/Disposable"]),
]

HEADER_LABELS = [
    "Sub Category", "Product", "Vendor", "Location",
    "Case Price", "Case Size", "Unit", "On Hand", "IUP", "P/#"
]

# ── Color palette (RGB floats 0–1) ──────────────────────────────────────
TITLE_BG     = {"red": 0.102, "green": 0.137, "blue": 0.494}
CAT_BG       = {"red": 0.216, "green": 0.278, "blue": 0.31}
HEADER_BG    = {"red": 0.85,  "green": 0.87,  "blue": 0.89}
FOOTER_BG    = {"red": 0.93,  "green": 0.94,  "blue": 0.95}
WHITE        = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
BORDER_CLR   = {"red": 0.7,   "green": 0.7,   "blue": 0.7}
SUMMARY_HDR  = {"red": 0.16,  "green": 0.38,  "blue": 0.56}

COL_WIDTHS = {
    0: 120, 1: 220, 2: 150, 3: 80, 4: 90,
    5: 80, 6: 50, 7: 70, 8: 90, 9: 70,
    12: 180, 13: 70, 14: 80, 15: 120,
}


def _col_letter(idx):
    return chr(65 + idx)


def _get_sheet_id(client, tab_name):
    meta = client.get(
        spreadsheetId=SPREADSHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties))"
    ).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"], s["properties"].get("gridProperties", {})
    return None, None


def _bootstrap_django():
    if not os.environ.get('DJANGO_SETTINGS_MODULE'):
        os.environ['DJANGO_SETTINGS_MODULE'] = 'myproject.settings'
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        import django
        django.setup()


def _load_products_from_db():
    """Load all products with their latest prices from the database."""
    _bootstrap_django()
    from myapp.models import Product, InvoiceLineItem
    from django.db.models import Count

    products = Product.objects.all().order_by('category', 'primary_descriptor', 'canonical_name')

    result = []
    for p in products:
        # Get latest invoice data
        latest = (
            InvoiceLineItem.objects
            .filter(product=p, unit_price__isnull=False, unit_price__gt=0)
            .order_by('-invoice_date', '-imported_at')
            .first()
        )

        # Get most frequent vendor
        top_vendor = (
            InvoiceLineItem.objects
            .filter(product=p, vendor__isnull=False)
            .values('vendor__name')
            .annotate(c=Count('id'))
            .order_by('-c')
            .first()
        )

        unit_price = float(latest.unit_price) if latest and latest.unit_price else None
        case_size = latest.case_size if latest else ""
        vendor = top_vendor['vendor__name'] if top_vendor else ""

        # Calculate IUP and P/#. Parser's stored price_per_pound wins over
        # reverse-engineering whenever the ILI carries it.
        stored_ppp = latest.price_per_pound if latest else None
        iup = calc_iup(unit_price, case_size) if unit_price and case_size else None
        pplb = (calc_price_per_lb(unit_price, case_size,
                                  stored_price_per_lb=stored_ppp)
                if unit_price and (case_size or stored_ppp) else None)

        result.append({
            "canonical_name": p.canonical_name,
            "category": p.category or "",
            "primary_descriptor": p.primary_descriptor or "",
            "secondary_descriptor": p.secondary_descriptor or "",
            "vendor": vendor,
            "unit_price": unit_price,
            "case_size": case_size or "",
            "iup": iup,
            "pplb": pplb,
        })

    return result


def restructure_sheet(tab_name=None, dry_run=False):
    tab = tab_name or ACTIVE_SHEET_TAB
    client = get_sheets_client()

    sheet_id, grid_props = _get_sheet_id(client, tab)
    if sheet_id is None:
        print(f"[!] Tab '{tab}' not found")
        return

    # ── 1. Load products from DB ─────────────────────────────────────
    print("  Loading products from database...")
    all_products = _load_products_from_db()
    print(f"  {len(all_products)} products loaded")

    # ── 2. Group by sheet category ───────────────────────────────────
    categories = []
    for sheet_cat, db_cats in SHEET_CATEGORIES:
        items = [p for p in all_products if p["category"] in db_cats]
        # Sort by primary_descriptor then canonical_name
        items.sort(key=lambda x: (x["primary_descriptor"].lower(), x["canonical_name"].lower()))
        categories.append({"name": sheet_cat, "items": items})
        print(f"  {sheet_cat}: {len(items)} items")

    # ── 3. Build new row layout ──────────────────────────────────────
    new_rows = []  # list of (row_values, row_type)

    import re
    from calendar import month_name
    title_text = tab.replace("Synergy ", "Synergy Inventory — ")
    for abbr, full in [(mn[:3], mn) for mn in month_name if mn]:
        title_text = title_text.replace(f"— {abbr} ", f"— {full} ")

    # Title row
    new_rows.append(([title_text] + [""] * 9, "title"))
    new_rows.append(([""] * 10, "blank"))

    summary_refs = []

    for cat in categories:
        # Category header
        new_rows.append(([cat["name"]] + [""] * 9, "cat_header"))

        # Column headers
        new_rows.append((HEADER_LABELS, "col_header"))

        # Data rows
        data_start = len(new_rows) + 1  # 1-indexed
        for item in cat["items"]:
            row = [
                item["primary_descriptor"],     # A: Sub Category
                item["canonical_name"],          # B: Product
                item["vendor"],                  # C: Vendor
                "",                              # D: Location
                item["unit_price"] if item["unit_price"] else "",  # E: Case Price
                item["case_size"],               # F: Case Size
                "",                              # G: Unit
                "",                              # H: On Hand
                item["iup"] if item["iup"] else "",  # I: IUP
                item["pplb"] if item["pplb"] else "", # J: P/#
            ]
            new_rows.append((row, "data"))
        data_end = len(new_rows)  # 1-indexed

        # Footer
        footer_row_1idx = len(new_rows) + 1
        footer = [""] * 10
        footer[1] = "Category Total"
        h_col = _col_letter(7)
        i_col = _col_letter(8)
        footer[7] = f"=SUM({h_col}{data_start}:{h_col}{data_end})"
        footer[8] = f"=SUMPRODUCT({h_col}{data_start}:{h_col}{data_end},{i_col}{data_start}:{i_col}{data_end})"
        new_rows.append((footer, "footer"))

        summary_refs.append({
            "name": cat["name"],
            "count": len(cat["items"]),
            "on_hand_cell": f"{h_col}{footer_row_1idx}",
            "inv_value_cell": f"{i_col}{footer_row_1idx}",
        })

        # Blank separator
        new_rows.append(([""] * 10, "blank"))

    total_rows = len(new_rows)
    print(f"\n  New layout: {total_rows} rows")

    if dry_run:
        print(f"\n  [DRY RUN] Would rewrite {total_rows} rows")
        for s in summary_refs:
            print(f"    {s['name']}: {s['count']} items")
        return

    # ── 4. Replace sheet (delete old, create fresh) ──────────────────
    print(f"\n  Replacing sheet...")

    # Add new blank sheet
    resp = client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": f"{tab}_new"}}}]}
    ).execute()
    new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Delete old sheet
    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]}
    ).execute()

    # Rename new sheet
    client.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": new_sheet_id, "title": tab},
                "fields": "title",
            }
        }]}
    ).execute()

    sheet_id = new_sheet_id
    print(f"  Fresh sheet created")

    # ── 5. Write data ────────────────────────────────────────────────
    print(f"  Writing {total_rows} rows...")
    all_values = [r[0] for r in new_rows]
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab}'!A1:J{total_rows}",
        valueInputOption="USER_ENTERED",
        body={"values": all_values},
    ).execute()

    # ── 6. Write summary panel (column M+) ───────────────────────────
    print(f"  Writing summary panel...")
    summary_start = 3
    summary_data = [["Category", "Items", "On Hand", "Inventory Value"]]
    for s in summary_refs:
        summary_data.append([
            s["name"], s["count"],
            f"={s['on_hand_cell']}", f"={s['inv_value_cell']}",
        ])
    n = len(summary_refs)
    summary_data.append([
        "TOTAL",
        f"=SUM(N{summary_start + 1}:N{summary_start + n})",
        f"=SUM(O{summary_start + 1}:O{summary_start + n})",
        f"=SUM(P{summary_start + 1}:P{summary_start + n})",
    ])
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab}'!M{summary_start}:P{summary_start + len(summary_data) - 1}",
        valueInputOption="USER_ENTERED",
        body={"values": summary_data},
    ).execute()

    # ── 7. Format ────────────────────────────────────────────────────
    print(f"  Applying formatting...")
    requests = []

    # Title row
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": 10},
            "cell": {"userEnteredFormat": {
                "backgroundColor": TITLE_BG,
                "textFormat": {"foregroundColor": WHITE, "fontSize": 16, "bold": True},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }
    })
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 48}, "fields": "pixelSize",
        }
    })

    # Column widths
    for col_idx, width in COL_WIDTHS.items():
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": col_idx, "endIndex": col_idx + 1},
                "properties": {"pixelSize": width}, "fields": "pixelSize",
            }
        })

    # Freeze title row
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Per-row formatting
    for i, (_, row_type) in enumerate(new_rows):
        if row_type == "cat_header":
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": 10},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": CAT_BG,
                        "textFormat": {"foregroundColor": WHITE, "fontSize": 12, "bold": True},
                        "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
                    }},
                    "fields": "userEnteredFormat",
                }
            })
            requests.append({
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS",
                               "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": 32}, "fields": "pixelSize",
                }
            })

        elif row_type == "col_header":
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": 10},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": HEADER_BG,
                        "textFormat": {"fontSize": 10, "bold": True},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    }},
                    "fields": "userEnteredFormat",
                }
            })
            requests.append({
                "updateBorders": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": 10},
                    "top": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
                    "bottom": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
                }
            })

        elif row_type == "footer":
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": 10},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": FOOTER_BG,
                        "textFormat": {"fontSize": 10, "bold": True},
                        "verticalAlignment": "MIDDLE",
                    }},
                    "fields": "userEnteredFormat",
                }
            })
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 8, "endColumnIndex": 9},
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
                        "horizontalAlignment": "RIGHT",
                    }},
                    "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment",
                }
            })
            requests.append({
                "updateBorders": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": 10},
                    "top": {"style": "SOLID_MEDIUM", "width": 2, "color": CAT_BG},
                    "bottom": {"style": "SOLID_MEDIUM", "width": 2, "color": CAT_BG},
                }
            })

    # Batch data rows formatting (currency + alignment) — do as ranges instead of per-row
    # Find contiguous data row ranges per category for efficient formatting
    data_ranges = []
    range_start = None
    for i, (_, row_type) in enumerate(new_rows):
        if row_type == "data":
            if range_start is None:
                range_start = i
        else:
            if range_start is not None:
                data_ranges.append((range_start, i))
                range_start = None
    if range_start is not None:
        data_ranges.append((range_start, len(new_rows)))

    for start, end in data_ranges:
        # Base font
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                           "startColumnIndex": 0, "endColumnIndex": 10},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"fontSize": 10}, "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat.textFormat.fontSize,userEnteredFormat.verticalAlignment",
            }
        })
        # Bold subcategory
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                           "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 10}}},
                "fields": "userEnteredFormat.textFormat",
            }
        })
        # Currency on E, I, J
        for col in [4, 8, 9]:
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                               "startColumnIndex": col, "endColumnIndex": col + 1},
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
                        "horizontalAlignment": "RIGHT",
                    }},
                    "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment",
                }
            })
        # Center D, F, G, H
        for col in [3, 5, 6, 7]:
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                               "startColumnIndex": col, "endColumnIndex": col + 1},
                    "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                    "fields": "userEnteredFormat.horizontalAlignment",
                }
            })

    # Summary panel formatting
    summary_hdr_idx = summary_start - 1
    summary_total_idx = summary_hdr_idx + len(summary_refs) + 1

    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": summary_hdr_idx, "endRowIndex": summary_hdr_idx + 1,
                       "startColumnIndex": 12, "endColumnIndex": 16},
            "cell": {"userEnteredFormat": {
                "backgroundColor": SUMMARY_HDR,
                "textFormat": {"foregroundColor": WHITE, "fontSize": 10, "bold": True},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }
    })
    # Summary data rows
    for j in range(len(summary_refs)):
        row_idx = summary_hdr_idx + 1 + j
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                           "startColumnIndex": 15, "endColumnIndex": 16},
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
                    "horizontalAlignment": "RIGHT",
                }},
                "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment",
            }
        })
    # Summary total row
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": summary_total_idx, "endRowIndex": summary_total_idx + 1,
                       "startColumnIndex": 12, "endColumnIndex": 16},
            "cell": {"userEnteredFormat": {
                "backgroundColor": FOOTER_BG,
                "textFormat": {"fontSize": 10, "bold": True},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": summary_total_idx, "endRowIndex": summary_total_idx + 1,
                       "startColumnIndex": 15, "endColumnIndex": 16},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
                "horizontalAlignment": "RIGHT",
            }},
            "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment",
        }
    })
    # Summary borders
    requests.append({
        "updateBorders": {
            "range": {"sheetId": sheet_id,
                       "startRowIndex": summary_hdr_idx, "endRowIndex": summary_total_idx + 1,
                       "startColumnIndex": 12, "endColumnIndex": 16},
            "top": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
            "bottom": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
            "left": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
            "right": {"style": "SOLID", "width": 1, "color": BORDER_CLR},
            "innerHorizontal": {"style": "SOLID", "width": 1, "color": {"red": 0.85, "green": 0.85, "blue": 0.85}},
            "innerVertical": {"style": "SOLID", "width": 1, "color": {"red": 0.85, "green": 0.85, "blue": 0.85}},
        }
    })

    # Send formatting
    print(f"  Sending {len(requests)} formatting requests...")
    BATCH_SIZE = 100
    for i in range(0, len(requests), BATCH_SIZE):
        batch = requests[i:i + BATCH_SIZE]
        client.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": batch},
        ).execute()
        if len(requests) > BATCH_SIZE:
            print(f"    Batch {i // BATCH_SIZE + 1} ({len(batch)} requests)")
        time.sleep(0.5)  # rate limit

    print(f"\n  [done] '{tab}' rebuilt — {len(categories)} tables, {sum(len(c['items']) for c in categories)} products")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restructure Synergy sheet into category tables")
    parser.add_argument("--tab", default=None, help="Tab name (default: active tab)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    restructure_sheet(tab_name=args.tab, dry_run=args.dry_run)
