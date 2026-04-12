"""
Cleans up the Item Mapping tab in Google Sheets.

Automated removals (no judgment required):
  1. Duplicate rows — same (canonical, raw_description) pair more than once
  2. "Unmatched" canonical rows — items tagged Unmatched should stay unmatched
  3. Empty raw_description rows — col A has OCR junk, col B is blank; useless for matching
  4. Conflicting Bar Mops row — "Bar Mops" maps to both Towels and Mop Heads; drop Towels

Usage:
  python cleanup_mappings.py          # preview (no changes written)
  python cleanup_mappings.py --apply  # write cleaned table back to sheet
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from sheets import get_sheets_client, get_sheet_values
from config import SPREADSHEET_ID, MAPPING_TAB


def load_rows():
    rows = get_sheet_values(SPREADSHEET_ID, f"{MAPPING_TAB}!A:D")
    header = rows[0] if rows else []
    data   = rows[1:] if len(rows) > 1 else []
    # Normalise to 4 columns
    for row in data:
        while len(row) < 4:
            row.append("")
    return header, data


def clean(data: list) -> tuple[list, dict]:
    """
    Apply all automated cleanup rules.
    Returns (cleaned_rows, stats).
    """
    kept   = []
    seen   = set()           # (canonical_upper, desc_upper) pairs already added
    stats  = {
        "duplicates":    [],
        "unmatched":     [],
        "empty_desc":    [],
        "conflicts":     [],
    }

    for row in data:
        canonical = row[0].strip()
        raw_desc  = row[1].strip()
        status    = row[2].strip()
        item_code = row[3].strip()

        # Rule 1 — skip if canonical is blank
        if not canonical:
            continue

        # Rule 2 — remove "Unmatched" canonical rows
        if canonical.lower() == "unmatched":
            stats["unmatched"].append(row)
            continue

        # Rule 3 — remove rows where raw_description is empty
        if not raw_desc:
            stats["empty_desc"].append(row)
            continue

        # Rule 4 — resolve Bar Mops conflict: keep Mop Heads, drop Towels
        if raw_desc.upper() == "BAR MOPS" and canonical.lower() == "towels":
            stats["conflicts"].append(row)
            continue

        # Rule 5 — deduplicate (canonical + raw_desc pair)
        key = (canonical.upper(), raw_desc.upper())
        if key in seen:
            stats["duplicates"].append(row)
            continue

        seen.add(key)
        kept.append(row)

    return kept, stats


def print_preview(header, original, cleaned, stats):
    orig_count    = len(original)
    cleaned_count = len(cleaned)

    print("=" * 65)
    print("ITEM MAPPING CLEANUP — PREVIEW")
    print("=" * 65)
    print(f"  Rows before : {orig_count}")
    print(f"  Rows after  : {cleaned_count}  ({orig_count - cleaned_count} removed)")
    print()

    print(f"  Duplicates removed      : {len(stats['duplicates'])}")
    print(f"  'Unmatched' rows removed: {len(stats['unmatched'])}")
    print(f"  Empty-description rows  : {len(stats['empty_desc'])}")
    print(f"  Conflict rows removed   : {len(stats['conflicts'])}")
    print()

    if stats["unmatched"]:
        print("  Unmatched rows (will be removed):")
        for r in stats["unmatched"]:
            print(f"    {r[1]}")
        print()

    if stats["conflicts"]:
        print("  Conflict rows (will be removed):")
        for r in stats["conflicts"]:
            print(f"    '{r[0]}' → '{r[1]}'  (keeping Mop Heads mapping instead)")
        print()

    print("-" * 65)
    print("WRONG MAPPINGS — REQUIRE MANUAL FIXES IN GOOGLE SHEETS")
    print("-" * 65)
    print("  These will NOT be auto-removed. Fix them directly in the")
    print("  Item Mapping tab after running --apply.")
    print()

    # Known wrong mappings grouped by canonical
    wrong = {
        "Eggs": [
            "EGGPLANT, FANCY, 1-1/9 BUSHEL",
            "EGGPLANT, SICILIAN 22 LB",
        ],
        "Red Bliss": [
            "MSVICKI CHIP POTATO CUBE VARI",
            "PRINGLE CHIP POTATO ORIG GRAB&GO",
            "SYS PRM POTATO FRY 3/8 ULTIMATE C",
            "SWEET POTATO, YAMS, MEDIUM, 40 LB",
        ],
        "Bell Pepper, Green": [
            "DRIED, GUAJILLO CHILI PEPPERS, 25 LB CASE",
            "DRIED, PEPPERS, ANCHOES, 5 LB box \"NO SPLITS 2",
            "DRIED, PEPPERS, D'ARBOL, 5 LB box \"NO SPLITS",
            "IMP/MCC SPICE PEPPER BLK WHL",
            "PEPPERS, RED, 11# X FANCY",
            "PEPPERS, RED, 11#X FANCY",
            "PEPPERS, RED, 150 X FANCY",
            "PEPPERS, JALAPENO, BUSHEL 1-1/9",
        ],
        "Ground Pork": [
            "Pork Belly Boneless RIND ON CVP LEID",
            "Pork Loin Boneless **BONELESS LOIN**",
            "Pork Sakura Smoked Boneless Ham 72223",
        ],
        "Ham": [
            "Pepperoni Sandwich",
            "Salami Genoa",
        ],
        "Swiss": [
            "CHEESE AMERICAN SLICED, WHITE, 160 SLICE, 4/5 LB, CS",
            "CHEESE CHEDDAR JACK FNCY SHRD",
            "CHEESE CHEDDAR MONT JACK, 50/50 MIX FANCY SHRED 4/5 LB",
            "CHEESE MOZZARELLA SHRD MIL",
        ],
        "Heavy Cream": [
            "COFMATE CREAMER FRCH VAN LIQ",
        ],
        "Mustard, Dijon": [
            "GREENS, MUSTARD, 24 BU",
        ],
        "Fresh Lemon Juice": [
            "HERB, LEMON GRASS, 40 LB CASE",
            "HERB, LEMON GRASS, 40LB CASE",
        ],
        "Grapes, Red Seedless": [
            "UNCRUST SANDWICH PEANUT STR&GRAPE",
        ],
        "Mop Heads": [
            "Bib Aprons - White",
        ],
        "Cinnamon Toast Crunch": [
            "QUAKER CEREAL CRUNCH BERRY",
        ],
        "Pita": [
            "STACYS CHIP PITA LSS PRMSN GRLIC HERB",
        ],
        "Red Onion": [
            "ONIONS, SPANISH JUMBO, 50 LB \"NO SPLIT\"",
        ],
        "Trail Mix": [
            "NUT, ALMONDS, SLICE/BLANCH NO SALT, 3 LB BAG \"NO SPLITS",
            "NUT, WALNUT, HALVES AND PIECES, 3 LB BAG \"NO SPLITS",
        ],
    }

    for canonical, bad_descs in wrong.items():
        print(f"  {canonical}")
        for d in bad_descs:
            print(f"    ✗  {d}")
        print()


def apply_cleanup(header, cleaned):
    client = get_sheets_client()

    # 1. Clear everything below the header
    client.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MAPPING_TAB}!A2:D",
    ).execute()

    if not cleaned:
        print("  Sheet cleared (no rows to write back).")
        return

    # 2. Write cleaned rows
    client.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MAPPING_TAB}!A2",
        valueInputOption="RAW",
        body={"values": cleaned},
    ).execute()

    print(f"  [✓] Wrote {len(cleaned)} clean rows back to '{MAPPING_TAB}'")


def main():
    apply = "--apply" in sys.argv

    print("Reading Item Mapping tab...")
    header, data = load_rows()
    cleaned, stats = clean(data)
    print_preview(header, data, cleaned, stats)

    if apply:
        print("=" * 65)
        print("Applying cleanup...")
        apply_cleanup(header, cleaned)
        print("Done. Fix the wrong mappings listed above directly in")
        print("Google Sheets, then run:  python batch.py --refresh-mappings")
    else:
        print("=" * 65)
        print("This was a preview. Run with --apply to write changes.")


if __name__ == "__main__":
    main()
