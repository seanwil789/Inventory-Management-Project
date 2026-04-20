"""
Poultry parser — hierarchical 5-data-column layout:
  Item Name | Part Type | Ounce Weight | % of Original Weight | Ounce Weight of Each | Each: % of Original Weight

Parent rows (bold, no data): "Chicken, Large Fryer", "Turkey, hen (10 pounds)",
"Turkey, hen (14 pounds)", "Turkey, tom (22 pounds)", "Ducklings".

Child rows inherit the parent context — stored as 'Parent - Child' in ingredient.

Weight modes:
  - Batch mode:  Ounce Weight + % of Original populated, Each columns blank
  - Each mode:   Each columns populated, batch columns blank (e.g., Gizzard = 0.60oz each = 1.0% of bird)
  - Both:        rare; store both

Verified against BoY pages 131-133 (PDF 154-156).
"""
from __future__ import annotations

from .base import (
    ParsedRow, page_words, group_lines,
    to_decimal, parse_pct,
    assign_words_to_columns, filter_data_lines,
    shift_columns, detect_column_shift, looks_like_new_entry,
)


# Reference layout (verified against p.131 / PDF 154)
REF_NAME_COL_END = 215
REF_COLUMNS = [
    {'field': 'part_type',                'x_min': 215, 'x_max': 260, 'kind': 'text'},
    {'field': 'ounce_weight',             'x_min': 275, 'x_max': 315, 'kind': 'decimal'},
    {'field': 'pct_of_original',          'x_min': 340, 'x_max': 385, 'kind': 'percent'},
    {'field': 'oz_weight_each',           'x_min': 405, 'x_max': 445, 'kind': 'decimal'},
    {'field': 'each_pct_of_original',     'x_min': 475, 'x_max': 520, 'kind': 'percent'},
]
REF_COL_CENTERS = [(c['x_min'] + c['x_max']) / 2 for c in REF_COLUMNS]


def _find_header_end_idx(lines):
    """Poultry header spans 2 lines: 'Percent of ...' above, 'Item Name Part Type ...' below."""
    for i, line in enumerate(lines):
        texts = {w[4] for w in line}
        if 'Item' in texts and 'Name' in texts and 'Part' in texts:
            return i
    return None


def parse_poultry_page(page, book_page_num: int,
                      initial_parent: str = '') -> tuple[list[ParsedRow], str]:
    """Returns (rows, last_parent). last_parent passes across page boundaries so a
    continuation page knows which parent to inherit."""
    words = page_words(page)
    lines = group_lines(words)

    header_end = _find_header_end_idx(lines)
    if header_end is None:
        return [], initial_parent

    data_lines = filter_data_lines(lines, header_end_idx=header_end)
    if not data_lines:
        return [], initial_parent

    shift = detect_column_shift(data_lines, REF_COL_CENTERS, right_of_x=150)
    columns = shift_columns(REF_COLUMNS, shift)
    name_col_end = REF_NAME_COL_END + shift

    rows: list[ParsedRow] = []
    parent = initial_parent

    for line in data_lines:
        name_text, cells = assign_words_to_columns(line, name_col_end, columns)

        numeric_fields = {'ounce_weight', 'pct_of_original', 'oz_weight_each', 'each_pct_of_original'}
        has_numeric = any(cells.get(f) for f in numeric_fields)
        has_part_type = bool(cells.get('part_type'))

        # Parent detection: line has a name but NO numeric data and NO part-type.
        # These are section headers like "Chicken, Large Fryer", "Turkey, hen (10 pounds)".
        if name_text and not has_numeric and not has_part_type:
            parent = name_text
            continue

        if has_numeric or has_part_type:
            full_name = name_text.strip()
            if not full_name:
                # Name column empty; skip
                continue
            # Build ingredient with parent context (if present)
            if parent:
                ingredient = f'{parent} - {full_name}'
            else:
                ingredient = full_name

            row = ParsedRow(
                ingredient=ingredient,
                prep_state=cells.get('part_type', '').strip(),
                ap_weight_oz=to_decimal(cells.get('ounce_weight', '')),
                yield_pct=parse_pct(cells.get('pct_of_original', '')),
                source_ref=f'p.{book_page_num}',
                extras={},
            )
            # Store "each mode" columns in extras
            oz_each = to_decimal(cells.get('oz_weight_each', ''))
            each_pct = parse_pct(cells.get('each_pct_of_original', ''))
            if oz_each is not None:
                row.extras['oz_weight_each'] = float(oz_each)
            if each_pct is not None:
                row.extras['each_pct_of_original'] = float(each_pct)
            if parent:
                row.extras['parent_section'] = parent
            rows.append(row)

    return rows, parent
