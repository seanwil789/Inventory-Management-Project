"""
Meats parser — 7-data-column layout (plus Item Name on left):
  Item Name | NAMP # | AP Weight lbs | Trim Loss lbs | Primary-Use Yield lbs | Yield % | Usable Oz/AP Pound | Trim Misc lbs

Verified against BoY pages 112-113 (PDF 135-136).
"""
from __future__ import annotations

from .base import (
    ParsedRow, page_words, group_lines,
    to_decimal, parse_pct,
    assign_words_to_columns, split_name_prep, filter_data_lines,
    shift_columns, detect_column_shift, looks_like_new_entry,
)


# Reference layout (verified against p.112 / PDF 135)
REF_NAME_COL_END = 165
REF_COLUMNS = [
    {'field': 'namp_number',           'x_min': 165, 'x_max': 200, 'kind': 'text'},
    {'field': 'ap_weight_lbs',         'x_min': 200, 'x_max': 240, 'kind': 'decimal'},
    {'field': 'trim_loss_lbs',         'x_min': 245, 'x_max': 285, 'kind': 'decimal'},
    {'field': 'primary_use_yield_lbs', 'x_min': 290, 'x_max': 330, 'kind': 'decimal'},
    {'field': 'yield_pct',             'x_min': 340, 'x_max': 385, 'kind': 'percent'},
    {'field': 'usable_oz_per_ap_pound','x_min': 390, 'x_max': 425, 'kind': 'decimal'},
    {'field': 'trim_misc_lbs',         'x_min': 440, 'x_max': 475, 'kind': 'decimal'},
]
REF_COL_CENTERS = [(c['x_min'] + c['x_max']) / 2 for c in REF_COLUMNS]


def _find_header_end_idx(lines):
    """Meats header spans 3 lines. Find the row containing 'Item' and 'Name' or 'NAMP'."""
    for i, line in enumerate(lines):
        texts = {w[4] for w in line}
        # 'Item' + 'Name' is always on the bottom of the 3-line header
        if 'Item' in texts and 'Name' in texts:
            return i
    return None


def parse_meats_page(page, book_page_num: int) -> list[ParsedRow]:
    words = page_words(page)
    lines = group_lines(words)

    header_end = _find_header_end_idx(lines)
    if header_end is None:
        return []

    data_lines = filter_data_lines(lines, header_end_idx=header_end)
    if not data_lines:
        return []

    shift = detect_column_shift(data_lines, REF_COL_CENTERS, right_of_x=140)
    columns = shift_columns(REF_COLUMNS, shift)
    name_col_end = REF_NAME_COL_END + shift

    rows: list[ParsedRow] = []
    pending_pre: list[str] = []   # name parts that appear BEFORE any data row

    for line in data_lines:
        name_text, cells = assign_words_to_columns(line, name_col_end, columns)

        numeric_fields = {'ap_weight_lbs', 'trim_loss_lbs', 'primary_use_yield_lbs',
                          'yield_pct', 'usable_oz_per_ap_pound', 'trim_misc_lbs'}
        has_data = any(cells.get(f) for f in numeric_fields)

        if has_data:
            full_name = ' '.join(pending_pre + [name_text]).strip()
            pending_pre = []
            if not full_name:
                continue
            ingredient, prep_state = split_name_prep(full_name)

            row = ParsedRow(
                ingredient=ingredient,
                prep_state=prep_state,
                yield_pct=parse_pct(cells.get('yield_pct')),
                source_ref=f'p.{book_page_num}',
                extras={},
            )
            for f in ['namp_number', 'ap_weight_lbs', 'trim_loss_lbs',
                      'primary_use_yield_lbs', 'usable_oz_per_ap_pound', 'trim_misc_lbs']:
                raw = cells.get(f, '').strip()
                if not raw:
                    continue
                if f == 'namp_number':
                    row.extras[f] = raw
                else:
                    v = to_decimal(raw)
                    if v is not None:
                        row.extras[f] = float(v)
            rows.append(row)
        else:
            if not name_text:
                continue
            if looks_like_new_entry(name_text):
                # Uppercase-leading → pre-name for the NEXT data row
                pending_pre.append(name_text)
            elif rows:
                # Lowercase/digit-leading → continuation of previous data row
                last = rows[-1]
                last.prep_state = (last.prep_state + ' ' + name_text).strip() if last.prep_state else name_text
            else:
                # No prior row yet; accumulate as pre-name
                pending_pre.append(name_text)

    return rows
