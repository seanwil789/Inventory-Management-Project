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


def parse_standard_page(page, book_page_num: int) -> list[ParsedRow]:
    # Note: this function strips BANNER_Y_MAX=100 (via page_words), so column
    # headers remain. Original parser used 170; rely on banner-line filter instead.
    all_words = page_words(page)
    # Extra skip-band for the top-of-page title area (Chapter X / section name logo)
    words = [w for w in all_words if w[1] >= 170]

    layout = _detect_layout(words)
    if layout == 'left':
        cols = STANDARD_COLS_LEFT
        name_boundary = NAME_BOUNDARY_LEFT
    else:
        cols = STANDARD_COLS_RIGHT
        name_boundary = NAME_BOUNDARY_RIGHT

    lines = group_lines(words)

    rows: list[ParsedRow] = []
    pending: list[str] = []

    for line in lines:
        if _is_banner_line(line):
            continue
        left_text = _line_left_text(line, name_boundary)
        if _line_has_data(line, name_boundary):
            name_parts = list(pending)
            if left_text:
                name_parts.append(left_text)
            full_name = ' '.join(name_parts).strip()
            cells = _line_right_cells(line, cols, name_boundary)

            ingredient, prep_state = _split_ingredient_and_prep(full_name)
            if not ingredient or not ingredient_name_is_plausible(ingredient):
                continue

            ap_w = to_decimal(cells.get('ap_weight_raw', ''))
            trimmed_raw = cells.get('trimmed_weight_raw', '') or ''
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
                ap_unit=cells.get('ap_unit', '').strip(),
                ap_weight_oz=ap_w,
                trimmed_unit=cells.get('trimmed_unit', '').strip(),
                trimmed_weight_oz=trimmed_w,
                trimmed_count=trimmed_count,
                yield_pct=parse_pct(cells.get('yield_pct_raw', '')),
                measures_per_ap=to_decimal(cells.get('measures_per_ap_raw', '')),
                ounce_weight_per_cup=to_decimal(cells.get('oz_per_cup_raw', '')),
                source_ref=f'p.{book_page_num}',
                extras={},
            ))
            pending = []
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
