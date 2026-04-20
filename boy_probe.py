"""
Diagnostic: dump word-level structure of a BoY PDF page.
Identifies header row by looking for known column-name tokens,
emits column x-ranges for use in the main parser.

Usage:
    python boy_probe.py <pdf_page_index>
"""
import sys
from pathlib import Path

import fitz

PDF = Path(__file__).parent / "The-Book-of-Yields-Accuracy-in-Food-Costing-and-Purchasing.pdf"

# Strong header tokens — unique to table headers (not the page footer formula key).
HEADER_TOKENS = {
    "Item",         # universal column 1
    "Name",
    "Fillet",       # seafood
    "Edible",       # seafood
    "NAMP",         # meats
    "Primary-Use",  # meats
    "Usable",       # meats
    "Miscellaneous",# meats
    "Part",         # poultry
    "Original",     # poultry
    "Measures",     # vegetables
}

# Footer banners to skip (the Y%/AS/AP formula key appears at page bottom)
FOOTER_TOKEN_SETS = [
    {"Y%", "means", "yield", "percentage"},
]

BANNER_Y_MAX = 100   # strip page title only; keep table header


def page_words(page):
    out = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text, *_ = w
        if y0 < BANNER_Y_MAX:
            continue
        out.append((x0, y0, x1, y1, text))
    return out


def group_lines(words, y_tol=3.0):
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w[1], w[0]))
    lines = [[sorted_words[0]]]
    for w in sorted_words[1:]:
        prev_y = lines[-1][-1][1]
        if abs(w[1] - prev_y) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w[0])
    return lines


def is_footer_line(line):
    text_set = {w[4] for w in line}
    return any(ftset.issubset(text_set) for ftset in FOOTER_TOKEN_SETS)


def find_header_block(lines):
    """Headers often span 2-3 lines (stacked column titles). Return contiguous
    header lines as a group, and return the union of their words keyed by x-range."""
    header_line_idxs = []
    for i, line in enumerate(lines):
        if is_footer_line(line):
            continue
        texts = {w[4] for w in line}
        if texts & HEADER_TOKENS and len(line) >= 2:
            header_line_idxs.append(i)
    if not header_line_idxs:
        return None

    # Take the contiguous block starting at the first header line
    first = header_line_idxs[0]
    block = [lines[first]]
    for j in range(first + 1, len(lines)):
        # stop once there's a gap (non-header line)
        if j in header_line_idxs:
            block.append(lines[j])
        else:
            # allow up to 1 intervening non-header line if the NEXT line is header
            if (j + 1) in header_line_idxs:
                block.append(lines[j])
            else:
                break
    return block


def detect_data_columns(data_lines, name_col_exclude_xmax=70):
    """Histogram-based peak detection. Bins x-centers to 2px buckets, identifies
    density peaks (actual data columns), merges neighboring high-density bins.

    name_col_exclude_xmax: x-range BELOW this is guaranteed to be item-name area
    (skipped). Data columns are auto-detected; name column is everything left of
    the leftmost detected column."""
    from collections import Counter

    all_xs = []
    for line in data_lines:
        for w in line:
            # Exclude only deep-left tokens (clearly name area)
            if w[0] >= name_col_exclude_xmax:
                xc = (w[0] + w[2]) / 2
                all_xs.append(xc)

    if not all_xs:
        return []

    # Bin to 2px buckets
    bins = Counter(round(x / 2) * 2 for x in all_xs)
    # Minimum-density threshold: a real column has tokens in ~half of data rows
    min_density = max(3, len(data_lines) // 4)

    # Find bins exceeding threshold
    hot_bins = sorted(b for b, n in bins.items() if n >= min_density)
    if not hot_bins:
        return []

    # Merge adjacent hot bins (within 8px → same column peak)
    peaks = [[hot_bins[0]]]
    for b in hot_bins[1:]:
        if b - peaks[-1][-1] <= 8:
            peaks[-1].append(b)
        else:
            peaks.append([b])

    # For each peak, compute x_min/x_max from the original token x-centers within 10px of peak
    out = []
    for peak_bins in peaks:
        peak_lo = min(peak_bins) - 3
        peak_hi = max(peak_bins) + 3
        in_peak = [x for x in all_xs if peak_lo <= x <= peak_hi]
        if len(in_peak) < min_density:
            continue
        out.append((min(in_peak), max(in_peak), sum(in_peak) / len(in_peak), len(in_peak)))

    return out


def label_columns_from_header(data_cols, header_block, name_col_end=200):
    """For each data column x-range, find the header words near it and concatenate."""
    # Collect all non-left-column header words
    header_words = []
    for line in header_block:
        for w in line:
            if w[0] >= name_col_end - 20:  # slight leeway
                header_words.append(w)
    labels = []
    for x_min, x_max, x_center, count in data_cols:
        # Match header words whose x-center falls within or near this column
        matched = []
        for hw in header_words:
            hc = (hw[0] + hw[2]) / 2
            # Column x-range expanded for header matching (column header is usually
            # to the left of the data — right-aligned numbers vs left/center-aligned headers)
            if x_min - 30 <= hc <= x_max + 30:
                matched.append(hw)
        matched.sort(key=lambda w: (w[1], w[0]))
        label = " ".join(w[4] for w in matched) or "(unlabeled)"
        labels.append({
            "label": label,
            "x_min": x_min,
            "x_max": x_max,
            "x_center": x_center,
            "data_count": count,
        })
    return labels


def main():
    if len(sys.argv) != 2:
        print("Usage: python boy_probe.py <pdf_page_index>")
        sys.exit(1)
    pdf_idx = int(sys.argv[1])
    doc = fitz.open(str(PDF))
    page = doc[pdf_idx]
    words = page_words(page)
    lines = group_lines(words)

    print(f"=== PDF page {pdf_idx} ({len(words)} words, {len(lines)} lines) ===\n")

    print("--- First 30 lines (y, [x]: text): ---")
    for i, line in enumerate(lines[:30]):
        y = line[0][1]
        parts = [f"[x={w[0]:>5.1f}]{w[4]}" for w in line]
        print(f"  line{i:02d} y={y:6.1f}  {'  '.join(parts[:8])}{' ...' if len(parts) > 8 else ''}")

    hb = find_header_block(lines)
    if not hb:
        print("\n!! No header block detected — may not be a data page.")
        return

    print(f"\n--- Header block: {len(hb)} line(s) ---")
    for i, line in enumerate(hb):
        print(f"  header-line{i}: {' '.join(w[4] for w in line)}")

    # Find data lines (below header, above footer)
    header_end_idx = lines.index(hb[-1])
    data_lines = []
    for line in lines[header_end_idx + 1:]:
        if is_footer_line(line):
            break
        text = " ".join(w[4] for w in line)
        if text.startswith(("*", "Notes")):
            break
        data_lines.append(line)

    data_cols = detect_data_columns(data_lines)
    # Name column is everything to the left of the leftmost detected column.
    name_col_end = data_cols[0][0] - 5 if data_cols else 200
    print(f"\n--- Detected data columns ({len(data_cols)}) via data-row clustering: ---")
    for xmin, xmax, xc, n in data_cols:
        print(f"  [{xmin:>6.1f} - {xmax:>6.1f}]  center={xc:>6.1f}  | {n} data-row hits")

    labeled = label_columns_from_header(data_cols, hb, name_col_end=name_col_end)
    print(f"\n--- Labeled columns: ---")
    for c in labeled:
        print(f"  col x=[{c['x_min']:>6.1f}-{c['x_max']:>6.1f}] ({c['data_count']} hits)  → {c['label']!r}")

    # Preview: split first 8 data rows by detected columns
    print(f"\n--- First 8 data rows split by detected columns: ---")
    col_ranges = [(c["x_min"] - 5, c["x_max"] + 5) for c in labeled]
    for line in data_lines[:8]:
        # Item name = words before first data column
        name_parts = []
        col_vals = [[] for _ in col_ranges]
        first_col_start = col_ranges[0][0] if col_ranges else 1000
        for w in line:
            xc = (w[0] + w[2]) / 2
            if xc < first_col_start:
                name_parts.append(w[4])
                continue
            placed = False
            for i, (lo, hi) in enumerate(col_ranges):
                if lo <= xc <= hi:
                    col_vals[i].append(w[4])
                    placed = True
                    break
        name = " ".join(name_parts)
        cells = [" ".join(cv).strip() for cv in col_vals]
        print(f"  NAME={name[:40]:<40}  | {' | '.join(f'{c[:16]:<16}' for c in cells)}")


if __name__ == "__main__":
    main()
