"""
Standard (vegetables/fruit/canned/grains/etc.) parser — 7-column layout:
  AP Unit | AP Weight | Trimmed Unit | Trimmed Weight | Yield % | Measures/AP | Oz/Cup

Two page-variant layouts: odd-page "right" and even-page "left" (shifted ~36px).
Preserved from original implementation with minor cleanup.
"""
from __future__ import annotations

from decimal import Decimal
import re

from .base import (
    ParsedRow, page_words, group_lines, is_footer_line, is_notes_line,
    to_decimal, parse_pct, filter_data_lines, ingredient_name_is_plausible,
)


# --- Per-page column peak detection ---
#
# BoY's Part I tables nominally share a 7-data-col layout but column positions
# and semantics drift between pages (dried-fruit pages shift Yield from x~420
# to x~345; herbs have a different schema entirely). The old approach used
# fixed x-ranges and produced garbage on drifted pages.
#
# New approach: per page, find N density peaks in right-side data-row x-centers.
# Map them ordinally to standard columns (peak 1 = ap_unit, peak 2 = ap_weight,
# ..., peak 5 = yield_pct, etc.). Tolerant of x-shifts and minor schema
# reorderings — the 5th data column is yield_pct regardless of where it sits.

def _detect_data_peaks(data_lines, right_of_x=150, min_density_frac=0.20):
    """Find x-positions where data values consistently land across rows.
    Returns a sorted list of peak x-centers, each wide enough to capture
    typical decimal-number widths (~35px window centered on peak).

    Filters: bin to 4px, require ≥ min_density_frac × len(data_lines) rows
    contributing. Merges adjacent high-density bins into single peaks."""
    from collections import Counter
    all_xs = []
    for line in data_lines:
        for w in line:
            if w[0] > right_of_x:
                all_xs.append((w[0] + w[2]) / 2)
    if not all_xs:
        return []
    bins = Counter(round(x / 4) * 4 for x in all_xs)
    min_density = max(3, int(len(data_lines) * min_density_frac))
    hot = sorted(b for b, n in bins.items() if n >= min_density)
    if not hot:
        return []
    peaks = [[hot[0]]]
    for b in hot[1:]:
        if b - peaks[-1][-1] <= 10:
            peaks[-1].append(b)
        else:
            peaks.append([b])
    return [sum(p) / len(p) for p in peaks]


def _assign_to_peak(w_center, peaks, window=20):
    """Return the peak index whose center is within `window` of w_center.
    Returns None if no peak matches."""
    best_idx, best_dist = None, window + 1
    for i, p in enumerate(peaks):
        d = abs(w_center - p)
        if d < best_dist:
            best_idx, best_dist = i, d
    return best_idx

STANDARD_COLS_RIGHT = [
    ('ap_unit',              195, 250),
    ('ap_weight_raw',        250, 300),
    ('trimmed_unit',         300, 350),
    ('trimmed_weight_raw',   350, 400),
    ('yield_pct_raw',        400, 445),
    ('measures_per_ap_raw',  445, 495),
    ('oz_per_cup_raw',       495, 555),
]
NAME_BOUNDARY_RIGHT = 190

STANDARD_COLS_LEFT = [
    ('ap_unit',              158, 215),
    ('ap_weight_raw',        215, 260),
    ('trimmed_unit',         260, 310),
    ('trimmed_weight_raw',   310, 360),
    ('yield_pct_raw',        360, 410),
    ('measures_per_ap_raw',  410, 460),
    ('oz_per_cup_raw',       460, 520),
]
NAME_BOUNDARY_LEFT = 155

BANNER_LINE_SUBSTRINGS = (
    'Y% means', 'AS means', 'Cost per', '1 gal.', '4 qt.', '16 c.',
    '128 fl.', '1 c.', '8 fl. oz.', '1 tbsp.', '1 lb.', '(Continued)',
    'last three columns',
)

_COUNT_RE = re.compile(r'^(\d+)\s*each$', re.I)


def _detect_layout(words):
    xs = [w[0] for w in words]
    if not xs:
        return 'right'
    return 'left' if min(xs) < 80 else 'right'


def _is_banner_line(line) -> bool:
    text = ' '.join(w[4] for w in line)
    return any(s in text for s in BANNER_LINE_SUBSTRINGS)


def _line_has_data(line, left_x_boundary):
    return any(w[0] >= left_x_boundary for w in line)


def _line_left_text(line, left_x_boundary):
    return ' '.join(w[4] for w in line if w[0] < left_x_boundary).strip()


def _line_right_cells(line, cols, left_x_boundary):
    cells = {name: [] for name, _, _ in cols}
    for w in line:
        if w[0] < left_x_boundary:
            continue
        for col_name, x_lo, x_hi in cols:
            if x_lo <= w[0] < x_hi:
                cells[col_name].append(w[4])
                break
    return {k: ' '.join(v).strip() for k, v in cells.items()}


def _split_ingredient_and_prep(full_name: str) -> tuple[str, str]:
    if ',' not in full_name:
        return full_name.strip(), ''
    head, _, tail = full_name.partition(',')
    return head.strip(), tail.strip()


# Ordinal field mapping for peak-based column detection.
# 7 peaks → standard 7-col semantics.
PEAK_TO_FIELD_7 = ['ap_unit', 'ap_weight_raw', 'trimmed_unit',
                   'trimmed_weight_raw', 'yield_pct_raw',
                   'measures_per_ap_raw', 'oz_per_cup_raw']


def parse_standard_page(page, book_page_num: int) -> list[ParsedRow]:
    """Parse a standard BoY data page via per-page peak detection.

    Strategy:
      1. Group words into lines; filter banner/notes/footer lines.
      2. Detect the name-column boundary from the leftmost token x.
      3. Find data-column peaks across data rows (density histogram).
      4. Map peaks ordinally to the 7 standard fields. (Semantics mostly
         consistent across BoY Part I tables; occasional drift handled by
         falling through to whatever the 5th peak contains.)
      5. For each data row, assign words to peaks by nearest-center match,
         then parse values into the right fields.
      6. Fallback to fixed x-ranges if peak detection produces < 3 peaks
         (very sparse pages can trip the density threshold)."""
    all_words = page_words(page)
    # Strip top-of-page title band (Chapter logo etc.)
    words = [w for w in all_words if w[1] >= 170]

    # Rough name-column boundary: find the leftmost data word on lines with
    # numeric content. Most BoY pages have item name starting at x<100 and
    # first data column starting at x>150. Use x=150 as a pragmatic floor.
    layout = _detect_layout(words)
    if layout == 'left':
        name_boundary = NAME_BOUNDARY_LEFT
    else:
        name_boundary = NAME_BOUNDARY_RIGHT

    lines = group_lines(words)

    # Candidate data lines (skip banners/notes/footer)
    candidate_data_lines = []
    for line in lines:
        if _is_banner_line(line):
            continue
        if is_footer_line(line):
            break
        if is_notes_line(line):
            break
        if _line_has_data(line, name_boundary):
            candidate_data_lines.append(line)

    # Detect peaks
    peaks = _detect_data_peaks(candidate_data_lines, right_of_x=name_boundary - 10)
    # Limit to 7 peaks max (truncate extras left-to-right by density)
    peaks = peaks[:7]
    use_peaks = len(peaks) >= 3

    # Fallback column definition (used when peaks fail)
    if layout == 'left':
        fallback_cols = STANDARD_COLS_LEFT
    else:
        fallback_cols = STANDARD_COLS_RIGHT

    rows: list[ParsedRow] = []
    pending: list[str] = []

    for line in lines:
        if _is_banner_line(line):
            continue
        if is_footer_line(line) or is_notes_line(line):
            break
        left_text = _line_left_text(line, name_boundary)
        has_right = _line_has_data(line, name_boundary)
        if has_right:
            name_parts = list(pending)
            if left_text:
                name_parts.append(left_text)
            full_name = ' '.join(name_parts).strip()
            pending = []
            ingredient, prep_state = _split_ingredient_and_prep(full_name)
            if not ingredient or not ingredient_name_is_plausible(ingredient):
                continue

            # Assign right-side words to columns
            cells: dict[str, list[str]] = {}
            if use_peaks:
                for w in line:
                    if w[0] < name_boundary:
                        continue
                    wc = (w[0] + w[2]) / 2
                    idx = _assign_to_peak(wc, peaks)
                    if idx is None or idx >= len(PEAK_TO_FIELD_7):
                        continue
                    field = PEAK_TO_FIELD_7[idx]
                    cells.setdefault(field, []).append(w[4])
            else:
                cells_raw = _line_right_cells(line, fallback_cols, name_boundary)
                for k, v in cells_raw.items():
                    cells.setdefault(k, []).append(v)

            cell_text = {k: ' '.join(v).strip() for k, v in cells.items()}

            # Type-based validation on yield_pct: reject non-percent-looking
            # values in the yield slot (e.g., "ounce", "pound"). Parse stricter.
            yield_raw = cell_text.get('yield_pct_raw', '').strip()
            yield_val = None
            if yield_raw:
                # Prefer a %-suffixed value, but accept bare numbers between 0-105.
                if '%' in yield_raw:
                    yield_val = parse_pct(yield_raw.replace(' ', ''))
                else:
                    v = to_decimal(yield_raw)
                    if v is not None and 0 < v <= 105:
                        yield_val = v

            ap_w = to_decimal(cell_text.get('ap_weight_raw', ''))
            trimmed_raw = cell_text.get('trimmed_weight_raw', '') or ''
            trimmed_w: Decimal | None = None
            trimmed_count: int | None = None
            m = _COUNT_RE.match(trimmed_raw.strip())
            if m:
                trimmed_count = int(m.group(1))
            else:
                trimmed_w = to_decimal(trimmed_raw.strip())

            rows.append(ParsedRow(
                ingredient=ingredient,
                prep_state=prep_state,
                ap_unit=cell_text.get('ap_unit', '').strip(),
                ap_weight_oz=ap_w,
                trimmed_unit=cell_text.get('trimmed_unit', '').strip(),
                trimmed_weight_oz=trimmed_w,
                trimmed_count=trimmed_count,
                yield_pct=yield_val,
                measures_per_ap=to_decimal(cell_text.get('measures_per_ap_raw', '')),
                ounce_weight_per_cup=to_decimal(cell_text.get('oz_per_cup_raw', '')),
                source_ref=f'p.{book_page_num}',
                extras={},
            ))
        else:
            if left_text:
                pending.append(left_text)

    # Trailing name parts → append to last row's prep_state
    if pending and rows:
        trailing = ' '.join(pending).strip()
        if trailing and not trailing.lower().startswith(('vegetables', 'fruit')):
            last = rows[-1]
            last.prep_state = (last.prep_state + ' ' + trailing).strip() if last.prep_state else trailing

    return rows
