"""
Herbs and spices parser — 5-column weight/volume-conversion layout:
  Item | Tablespoons per Ounce | Ounces per Tablespoon | Ounces per Cup |
       Each per Ounce | Each per Tablespoon

Herbs don't have a "trim yield" — they're used as-is. This section is purely
about weight/volume conversions.

Schema mapping:
  Ounces per Cup       → ounce_weight_per_cup  (primary value)
  Tablespoons per Ounce → extras.tbsp_per_oz
  Ounces per Tablespoon → extras.oz_per_tbsp
  Each per Ounce       → extras.each_per_oz
  Each per Tablespoon  → extras.each_per_tbsp

Verified against BoY pages 10-14 (PDF 33-37).
"""
from __future__ import annotations

from decimal import Decimal

from .base import (
    ParsedRow, page_words, group_lines,
    to_decimal, filter_data_lines,
    assign_words_to_columns, split_name_prep,
    shift_columns, detect_column_shift,
    looks_like_new_entry, ingredient_name_is_plausible,
)


# Reference layout verified against p.10 (PDF 33).
REF_NAME_COL_END = 180
REF_COLUMNS = [
    {'field': 'tbsp_per_oz',     'x_min': 180, 'x_max': 240, 'kind': 'decimal'},
    {'field': 'oz_per_tbsp',     'x_min': 255, 'x_max': 305, 'kind': 'decimal'},
    {'field': 'oz_per_cup',      'x_min': 320, 'x_max': 370, 'kind': 'decimal'},
    {'field': 'each_per_oz',     'x_min': 390, 'x_max': 440, 'kind': 'decimal'},
    {'field': 'each_per_tbsp',   'x_min': 450, 'x_max': 500, 'kind': 'decimal'},
]
REF_COL_CENTERS = [(c['x_min'] + c['x_max']) / 2 for c in REF_COLUMNS]


def _find_header_end_idx(lines):
    """Herbs header spans 3 lines. Locate the 'Item Name' row."""
    for i, line in enumerate(lines):
        texts = {w[4] for w in line}
        if 'Item' in texts and 'Name' in texts:
            return i
    return None


def parse_herbs_page(page, book_page_num: int) -> list[ParsedRow]:
    words = page_words(page)
    lines = group_lines(words)

    header_end = _find_header_end_idx(lines)
    if header_end is None:
        return []

    data_lines = filter_data_lines(lines, header_end_idx=header_end)
    if not data_lines:
        return []

    shift = detect_column_shift(data_lines, REF_COL_CENTERS, right_of_x=100)
    columns = shift_columns(REF_COLUMNS, shift)
    name_col_end = REF_NAME_COL_END + shift

    rows: list[ParsedRow] = []
    pending_pre: list[str] = []

    for line in data_lines:
        name_text, cells = assign_words_to_columns(line, name_col_end, columns)

        has_numeric = any(cells.get(f) for f in ('tbsp_per_oz', 'oz_per_tbsp',
                                                  'oz_per_cup', 'each_per_oz', 'each_per_tbsp'))

        if has_numeric:
            full_name = ' '.join(pending_pre + [name_text]).strip()
            pending_pre = []
            if not ingredient_name_is_plausible(full_name):
                continue

            ingredient, prep_state = split_name_prep(full_name)

            row = ParsedRow(
                ingredient=ingredient,
                prep_state=prep_state,
                ounce_weight_per_cup=to_decimal(cells.get('oz_per_cup', '')),
                source_ref=f'p.{book_page_num}',
                extras={},
            )
            for f in ('tbsp_per_oz', 'oz_per_tbsp', 'each_per_oz', 'each_per_tbsp'):
                v = to_decimal(cells.get(f, ''))
                if v is not None:
                    row.extras[f] = float(v)
            rows.append(row)
        else:
            if not name_text:
                continue
            # Reject footnote-like lines outright
            if not ingredient_name_is_plausible(name_text):
                continue
            if looks_like_new_entry(name_text):
                pending_pre.append(name_text)
            elif rows:
                last = rows[-1]
                last.prep_state = (last.prep_state + ' ' + name_text).strip() if last.prep_state else name_text
            else:
                pending_pre.append(name_text)

    return rows
