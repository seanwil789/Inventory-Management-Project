"""
Seafood parser — 3-column layout:
  Item | Fillet Yield Percent | Edible Ounces per AP Pound

Verified against BoY pages 121-123 (PDF 144-146).

Column x-ranges (verified via probe):
  name:     x_start < 225
  yield%:   x_center in [225, 275]
  edible oz/AP: x_center in [285, 335]
"""
from __future__ import annotations

from .base import (
    ParsedRow, page_words, group_lines,
    to_decimal, parse_pct,
    assign_words_to_columns, split_name_prep, filter_data_lines,
    shift_columns, detect_column_shift, looks_like_new_entry,
)


# Reference layout (verified against p.121 / PDF 144)
REF_NAME_COL_END = 225
REF_COLUMNS = [
    {'field': 'yield_pct',                 'x_min': 225, 'x_max': 275, 'kind': 'percent'},
    {'field': 'edible_oz_per_ap_pound',    'x_min': 285, 'x_max': 335, 'kind': 'decimal'},
]
REF_COL_CENTERS = [(c['x_min'] + c['x_max']) / 2 for c in REF_COLUMNS]


def _find_header_end_idx(lines):
    """Header row has 'Item' and one of 'Fillet'/'Edible'/'Percent*'."""
    for i, line in enumerate(lines):
        texts = {w[4] for w in line}
        if 'Item' in texts and (texts & {'Fillet', 'Edible', 'Percent*', 'Yield', 'per'}):
            # Header may span 2 lines; return index of last header line
            # Peek ahead: if next line looks like header continuation, include it
            for j in range(i + 1, min(i + 3, len(lines))):
                nt = {w[4] for w in lines[j]}
                if nt & {'Fillet', 'Edible', 'Percent*', 'Yield', 'Ounces', 'Pound', 'Item', 'per', 'AP'}:
                    i = j
                else:
                    break
            return i
    return None


def parse_seafood_page(page, book_page_num: int) -> list[ParsedRow]:
    words = page_words(page)
    lines = group_lines(words)

    header_end = _find_header_end_idx(lines)
    if header_end is None:
        return []

    data_lines = filter_data_lines(lines, header_end_idx=header_end)
    if not data_lines:
        return []

    # Adapt to per-page column shift
    shift = detect_column_shift(data_lines, REF_COL_CENTERS, right_of_x=80)
    columns = shift_columns(REF_COLUMNS, shift)
    name_col_end = REF_NAME_COL_END + shift

    rows: list[ParsedRow] = []
    pending_pre: list[str] = []

    for line in data_lines:
        name_text, cells = assign_words_to_columns(line, name_col_end, columns)

        has_data = any(v for v in cells.values())
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
            edible = to_decimal(cells.get('edible_oz_per_ap_pound', ''))
            if edible is not None:
                row.extras['edible_oz_per_ap_pound'] = float(edible)
            rows.append(row)
        else:
            if not name_text:
                continue
            if looks_like_new_entry(name_text):
                pending_pre.append(name_text)
            elif rows:
                last = rows[-1]
                last.prep_state = (last.prep_state + ' ' + name_text).strip() if last.prep_state else name_text
            else:
                pending_pre.append(name_text)

    return rows
