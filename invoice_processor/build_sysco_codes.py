"""
Build the Sysco SUPC code library by cross-referencing Vision+regex and DocAI.

Vision+regex extracts (item_code, price) pairs from Sysco invoices via regex.
DocAI extracts (description, price) pairs via structured table parsing.
By matching on price, we can pair SUPC codes with their full descriptions.

Walks Sysco invoices in the Drive archive, runs both pipelines, and outputs
a deduplicated code-to-description mapping that can be written to the
Item Mapping sheet.

Usage:
  python build_sysco_codes.py                # preview mode (print matches)
  python build_sysco_codes.py --write        # write new codes to Item Mapping sheet
  python build_sysco_codes.py --limit 10     # process only 10 invoices
"""
import os
import sys
import json
import re
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

from reprocess_archive import walk_archive, download_file, get_drive_client
from config import DRIVE_ROOT_FOLDER_ID, SPREADSHEET_ID
from ocr import extract_text
from docai import parse_with_docai
from sheets import get_sheet_values, get_sheets_client
from mapper import load_mappings

# Sysco price anchor: 6-7 digit code followed by price
_PRICE_ANCHOR = re.compile(r'(\d{6,7})\s+(\d+\.\d{2})\s*$')


def extract_code_price_pairs(raw_text: str) -> list[tuple[str, float]]:
    """
    Extract all (item_code, price) pairs from Vision OCR text using
    the same regex the Sysco parser uses.
    """
    pairs = []
    for line in raw_text.splitlines():
        m = _PRICE_ANCHOR.search(line.strip())
        if m:
            code = m.group(1)
            price = float(m.group(2))
            pairs.append((code, price))
    return pairs


def match_by_price(code_pairs: list[tuple[str, float]],
                   docai_items: list[dict]) -> list[dict]:
    """
    Match Vision's (code, price) pairs to DocAI's (description, price) items.

    Strategy:
    1. Build a price→items index from DocAI results.
    2. For each Vision (code, price), look up matching DocAI item(s) by price.
    3. If exactly one match at that price, it's a confident pairing.
    4. If multiple DocAI items share a price, use position order as tiebreaker.

    Returns list of {code, description, price, confidence} dicts.
    """
    # Index DocAI items by price (rounded to 2 decimals)
    price_index = defaultdict(list)
    for item in docai_items:
        p = item.get("unit_price")
        if p is not None:
            price_index[round(p, 2)].append(item)

    matches = []
    used_descriptions = set()

    for code, price in code_pairs:
        candidates = price_index.get(round(price, 2), [])

        # Filter out already-used descriptions
        available = [c for c in candidates
                     if c["raw_description"] not in used_descriptions]

        if len(available) == 1:
            desc = available[0]["raw_description"]
            case_size = available[0].get("case_size_raw", "")
            used_descriptions.add(desc)
            matches.append({
                "code": code,
                "description": desc,
                "case_size": case_size,
                "price": price,
                "confidence": "exact",
            })
        elif len(available) > 1:
            # Multiple items at same price — take the first unused one
            # (positional order from DocAI tends to match invoice order)
            desc = available[0]["raw_description"]
            case_size = available[0].get("case_size_raw", "")
            used_descriptions.add(desc)
            matches.append({
                "code": code,
                "description": desc,
                "case_size": case_size,
                "price": price,
                "confidence": "positional",
            })
        # else: no price match — skip this code for this invoice

    return matches


def load_existing_codes(mappings: dict) -> set[str]:
    """Return set of SUPC codes already in the Item Mapping sheet."""
    existing = set()
    code_map = mappings.get("code_map", {})
    for code in code_map:
        existing.add(str(code))
    return existing


def write_codes_to_sheet(new_mappings: dict[str, dict]):
    """
    Write new SUPC code mappings to the Item Mapping sheet.
    Appends rows with: Vendor | Raw Description | Category | Primary | Secondary | Canonical | SUPC Code
    For now, sets Vendor=Sysco, Raw Description=description, SUPC Code=code,
    and leaves Category/Primary/Secondary/Canonical blank for manual review.
    """
    client = get_sheets_client()
    rows = []
    for code, info in sorted(new_mappings.items()):
        rows.append([
            "Sysco",                    # A: Vendor
            info["description"],        # B: Raw Description
            "",                         # C: Category (manual)
            "",                         # D: Primary descriptor (manual)
            "",                         # E: Secondary descriptor (manual)
            "",                         # F: Canonical name (manual)
            code,                       # G: SUPC Code
        ])

    if not rows:
        print("No new codes to write.")
        return

    client.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Item Mapping!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

    print(f"Wrote {len(rows)} new code mappings to Item Mapping sheet.")


def main():
    parser = argparse.ArgumentParser(
        description="Build Sysco SUPC code library from Vision+DocAI cross-reference"
    )
    parser.add_argument("--write", action="store_true",
                        help="Write new codes to the Item Mapping sheet")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only N Sysco invoices")
    args = parser.parse_args()

    if not DRIVE_ROOT_FOLDER_ID:
        print("[!] DRIVE_ROOT_FOLDER_ID not set")
        return

    # Load existing mappings to know which codes we already have
    print("Loading existing item mappings...")
    mappings = load_mappings(force_refresh=True)
    existing_codes = load_existing_codes(mappings)
    print(f"  {len(existing_codes)} SUPC codes already mapped.\n")

    drive = get_drive_client()

    # Collect all code→description pairings across all invoices
    # Key = SUPC code, Value = {description, case_size, price, count, confidence}
    all_codes = {}
    processed = 0
    total_matches = 0

    print("Scanning Sysco invoices in archive...\n")

    for drive_file, vendor_name, folder_path in walk_archive(drive, DRIVE_ROOT_FOLDER_ID, "Sysco"):
        if args.limit and processed >= args.limit:
            print(f"\n[Limit reached: {args.limit}]")
            break

        file_name = drive_file["name"]
        print(f"  {folder_path}/{file_name}")

        tmp_path = None
        try:
            tmp_path = download_file(drive, drive_file["id"], file_name)

            # Run Vision OCR to get raw text with item codes
            raw_text = extract_text(tmp_path)
            code_pairs = extract_code_price_pairs(raw_text)

            if not code_pairs:
                print(f"    Vision: 0 code/price pairs — skipping")
                processed += 1
                continue

            # Run DocAI to get descriptions with prices
            docai_result = parse_with_docai(tmp_path)
            if not docai_result or not docai_result.get("items"):
                print(f"    DocAI: no items — skipping")
                processed += 1
                continue

            # Match by price
            matches = match_by_price(code_pairs, docai_result["items"])

            new_this_invoice = 0
            for m in matches:
                code = m["code"]
                if code in existing_codes:
                    continue

                total_matches += 1

                if code not in all_codes:
                    all_codes[code] = {
                        "description": m["description"],
                        "case_size": m["case_size"],
                        "price": m["price"],
                        "confidence": m["confidence"],
                        "count": 1,
                    }
                    new_this_invoice += 1
                else:
                    all_codes[code]["count"] += 1
                    # Keep the description from the most confident match
                    if (m["confidence"] == "exact"
                            and all_codes[code]["confidence"] != "exact"):
                        all_codes[code]["description"] = m["description"]
                        all_codes[code]["confidence"] = m["confidence"]

            print(f"    Vision: {len(code_pairs)} codes | "
                  f"DocAI: {len(docai_result['items'])} items | "
                  f"Matched: {len(matches)} | "
                  f"New codes: {new_this_invoice}")

            processed += 1

        except Exception as e:
            print(f"    [ERROR] {e}")

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Sysco invoices processed: {processed}")
    print(f"Total new code matches:   {total_matches}")
    print(f"Unique new SUPC codes:    {len(all_codes)}")
    print(f"Already mapped (skipped): {len(existing_codes)}")

    # Show confidence breakdown
    exact = sum(1 for c in all_codes.values() if c["confidence"] == "exact")
    positional = len(all_codes) - exact
    print(f"Confidence: {exact} exact price match, {positional} positional")

    # Show codes seen multiple times (higher confidence)
    multi = {k: v for k, v in all_codes.items() if v["count"] >= 2}
    print(f"Codes confirmed across 2+ invoices: {len(multi)}")

    if all_codes:
        print(f"\n{'='*60}")
        print(f"NEW SUPC CODE MAPPINGS")
        print(f"{'='*60}")
        print(f"{'Code':<10} {'Conf':<12} {'Seen':<5} {'Price':>8}  Description")
        print(f"{'-'*10} {'-'*12} {'-'*5} {'-'*8}  {'-'*40}")
        for code in sorted(all_codes, key=lambda c: all_codes[c]["count"], reverse=True):
            info = all_codes[code]
            print(f"{code:<10} {info['confidence']:<12} {info['count']:<5} "
                  f"${info['price']:>7.2f}  {info['description'][:60]}")

    # Save to JSON for reference
    report_path = os.path.join(os.path.dirname(__file__), "sysco_code_report.json")
    with open(report_path, "w") as f:
        json.dump(all_codes, f, indent=2)
    print(f"\nFull report saved to: {report_path}")

    # Write to sheet if requested
    if args.write and all_codes:
        print(f"\nWriting {len(all_codes)} new codes to Item Mapping sheet...")
        write_codes_to_sheet(all_codes)
    elif all_codes and not args.write:
        print(f"\nRun with --write to add these codes to the Item Mapping sheet.")


if __name__ == "__main__":
    main()
