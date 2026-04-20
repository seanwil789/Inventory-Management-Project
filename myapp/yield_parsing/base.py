"""Shared helpers: word extraction, line grouping, value coercion."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class ParsedRow:
    ingredient: str
    prep_state: str = ''
    ap_unit: str = ''
    ap_weight_oz: Decimal | None = None
    trimmed_unit: str = ''
    trimmed_weight_oz: Decimal | None = None
    trimmed_count: int | None = None
    yield_pct: Decimal | None = None
    measures_per_ap: Decimal | None = None
    ounce_weight_per_cup: Decimal | None = None
    source_ref: str = ''
    extras: dict = field(default_factory=dict)


# Strip only the page title band; keep table headers in view.
BANNER_Y_MAX = 100

# Footer boilerplate lines: "Y% means yield percentage..." and "1 gal. = 4 qt. = ..."
FOOTER_MARKERS = ('Y%', 'means', 'yield', 'percentage')
FOOTER_UNIT_MARKERS = ('gal.', 'qt.', 'pt.', 'c.', 'fl.', 'tbsp.', 'tsp.', 'lb.', 'oz.')


_PCT_RE = re.compile(r'^(-?\d+(?:\.\d+)?)\s*%$')
_NUM_RE = re.compile(r'^-?\d+(?:\.\d+)?$')


def page_words(page):
    out = []
    for w in page.get_text('words'):
        x0, y0, x1, y1, text, *_ = w
        if y0 < BANNER_Y_MAX:
            continue
        out.append((x0, y0, x1, y1, text))
    return out


def group_lines(words, y_tol=3.0):
    if not words:
        return []
    sw = sorted(words, key=lambda w: (w[1], w[0]))
    lines = [[sw[0]]]
    for w in sw[1:]:
        if abs(w[1] - lines[-1][-1][1]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w[0])
    return lines


def is_footer_line(line) -> bool:
    """Match 'Y% means yield...' legend AND the '1 gal. = 4 qt. = ...' volume key."""
    texts = {w[4] for w in line}
    if all(m in texts for m in FOOTER_MARKERS[:2]):
        return True
    # Volume/weight-conversion footer: multiple unit abbreviations on one line
    matched_units = sum(1 for u in FOOTER_UNIT_MARKERS if u in texts)
    return matched_units >= 3


def is_notes_line(line) -> bool:
    if not line:
        return False
    first_token = line[0][4]
    return first_token.startswith(('*', 'Notes')) or first_token.lower() == 'notes'


def to_decimal(s: str | None) -> Decimal | None:
    if not s:
        return None
    try:
        return Decimal(s.strip())
    except (InvalidOperation, TypeError, AttributeError):
        return None


def parse_pct(cell: str | None) -> Decimal | None:
    """Parse '60%' → Decimal(60). Also accept bare numbers like '60.5' as already-parsed pct."""
    if not cell:
        return None
    s = cell.strip()
    m = _PCT_RE.match(s)
    if m:
        return to_decimal(m.group(1))
    if _NUM_RE.match(s):
        v = to_decimal(s)
        # Heuristic: bare number ≤ 100 is likely already a percent; >100 is probably
        # oz/count/other — leave for caller to sort out
        return v if v is not None and v <= Decimal('100') else None
    return None


def filter_data_lines(lines, header_end_idx=None):
    """Return lines after header_end_idx, stopping at footer or notes."""
    start = (header_end_idx + 1) if header_end_idx is not None else 0
    out = []
    for line in lines[start:]:
        if is_footer_line(line):
            break
        if is_notes_line(line):
            break
        out.append(line)
    return out


def assign_words_to_columns(words, name_col_end, columns):
    """Given a list of words, partition into (name_parts, cell_values_by_field).
    columns is a list of dicts: {'field', 'x_min', 'x_max'}.
    Words at x_start < name_col_end join name_parts.
    Words at x_center within a column range join that column's value list.
    Everything else is ignored."""
    name_parts = []
    cell_lists = {c['field']: [] for c in columns}
    for w in words:
        x0, _, x1, _, text = w
        xc = (x0 + x1) / 2
        if x0 < name_col_end:
            name_parts.append(text)
            continue
        for c in columns:
            if c['x_min'] <= xc <= c['x_max']:
                cell_lists[c['field']].append(text)
                break
    cells = {f: ' '.join(vs).strip() for f, vs in cell_lists.items()}
    return ' '.join(name_parts).strip(), cells


def split_name_prep(full_name: str) -> tuple[str, str]:
    """'Ahi Tuna,H&G,center cut' → ('Ahi Tuna', 'H&G,center cut')"""
    if ',' not in full_name:
        return full_name.strip(), ''
    head, _, tail = full_name.partition(',')
    return head.strip(), tail.strip()


def looks_like_new_entry(text: str) -> bool:
    """Heuristic: no-data lines that start with an uppercase letter or a proper noun
    indicate the start of a new entry (pre-name for the next data row).
    Lowercase or digit-leading lines are continuations of the previous data row.

    Examples:
      'Lamb Leg,defatted; leg'           → True (new entry)
      'Pork Tenderloins'                  → True (new entry)
      'and shank bones intact'            → False (continuation)
      'trimmed to french'                 → False (continuation)
      '2 in.tail; trimmed to loin'        → False (continuation, digit-leading)
    """
    s = (text or '').strip()
    if not s:
        return False
    first = s[0]
    return first.isupper()


def detect_page_layout(words) -> str:
    """Even book pages shift left ~35px vs odd pages.
    Returns 'left' if leftmost data word is near x=40, else 'right'."""
    xs = [w[0] for w in words]
    if not xs:
        return 'right'
    return 'left' if min(xs) < 80 else 'right'


def shift_columns(columns: list[dict], delta_x: float) -> list[dict]:
    """Return a new columns list with x_min/x_max shifted by delta_x."""
    return [{**c, 'x_min': c['x_min'] + delta_x, 'x_max': c['x_max'] + delta_x}
            for c in columns]


def detect_column_shift(data_lines, reference_column_centers: list[float],
                        right_of_x: float = 80) -> float:
    """Find the x-shift (added to reference centers) that best aligns with
    the actual data-row column peaks on this page.

    Robust to per-page layout variation (BoY shifts columns by 0-55px between
    adjacent pages depending on section and row density)."""
    from collections import Counter

    all_xs = []
    for line in data_lines:
        for w in line:
            if w[0] > right_of_x:
                all_xs.append((w[0] + w[2]) / 2)
    if not all_xs or not reference_column_centers:
        return 0.0

    # Bin to 4px buckets; peaks are bins with density ≥ 25% of data rows
    bins = Counter(round(x / 4) * 4 for x in all_xs)
    min_density = max(3, len(data_lines) // 4)
    peaks = sorted(b for b, n in bins.items() if n >= min_density)
    if not peaks:
        return 0.0

    # Try shifts in 2px steps; pick the one with most column matches, tiebreak small-|shift|
    best_shift, best_score = 0.0, -1
    for test_shift in range(-60, 61, 2):
        score = sum(1 for exp in reference_column_centers
                    if any(abs(p - (exp + test_shift)) <= 6 for p in peaks))
        if score > best_score or (score == best_score and abs(test_shift) < abs(best_shift)):
            best_score, best_shift = score, test_shift
    return float(best_shift)
