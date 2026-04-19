"""
Parse Book of Yields (8e) PDF into YieldReference rows.

Sections share a common layout: LEFT column is item name (multi-line), RIGHT columns are
data cells (AP unit, AP weight, trimmed unit, trimmed weight, yield%, measures/AP, oz/cup).

Run:
    python manage.py import_book_of_yields --pdf <path> --section vegetables --dry-run
    python manage.py import_book_of_yields --pdf <path> --section vegetables --apply
    python manage.py import_book_of_yields --pdf <path> --all --apply
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.models import YieldReference


# ---- Section → PDF page ranges (inclusive, 0-indexed) ----
# Verified by scanning header text at y=38 on each page (see probe output 2026-04-18).
# Each section has data on 2-page spreads; odd pages show "Chapter N Produce" running
# header but still carry data rows.
SECTION_PAGE_RANGES = {
    # Chapter 1
    'herbs_spices':  (33, 37),
    'fresh_herbs':   (38, 38),
    # Chapter 2 — Produce
    'vegetables':    (48, 61),
    'fruit':         (62, 69),
    'canned':        (70, 73),
    # Chapter 3 — Starchy
    'dry_legumes':   (80, 80),
    'grains':        (81, 82),
    'pasta':         (83, 84),
    # Chapter 4 — Baking (ranges approximate; refine after first dry run)
    'nuts_seeds':    (90, 91),
    'flour':         (92, 93),
    'sweeteners':    (94, 95),
    'baking':        (96, 97),
    # Chapter 5
    'fats_oils':     (103, 103),
    'condiments':    (104, 105),
    # Chapter 6
    'liquids':       (108, 108),
    # Chapter 7
    'dairy':         (116, 118),
    # Chapter 8
    'beverages':     (126, 126),
    # Chapter 9
    'meats':         (135, 136),
    # Chapter 10
    'seafood':       (144, 146),
    # Chapter 11
    'poultry':       (154, 156),
}


@dataclass
class ParsedRow:
    ingredient: str
    prep_state: str
    ap_unit: str
    ap_weight_oz: Decimal | None
    trimmed_unit: str
    trimmed_weight_oz: Decimal | None
    trimmed_count: int | None
    yield_pct: Decimal | None
    measures_per_ap: Decimal | None
    ounce_weight_per_cup: Decimal | None
    source_ref: str
    raw_right_cells: list[str]


# ---- Parsing helpers ----

_PCT_RE = re.compile(r'^(-?\d+(?:\.\d+)?)\s*%$')
_NUM_RE = re.compile(r'^-?\d+(?:\.\d+)?$')
_COUNT_RE = re.compile(r'^(\d+)\s*each$', re.I)


def _to_decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError):
        return None


def _parse_pct(cell: str) -> Decimal | None:
    if not cell:
        return None
    m = _PCT_RE.match(cell.strip())
    if m:
        return _to_decimal(m.group(1))
    if _NUM_RE.match(cell.strip()):
        return _to_decimal(cell.strip())
    return None


def _split_ingredient_and_prep(full_name: str) -> tuple[str, str]:
    """
    'Carrots,sliced about 1/4 in.' → ('Carrots', 'sliced about 1/4 in.')
    'Carrots,petite,6 in.long (slender), topped,scrubbed'
        → ('Carrots', 'petite,6 in.long (slender), topped,scrubbed')
    """
    if ',' not in full_name:
        return full_name.strip(), ''
    head, _, tail = full_name.partition(',')
    return head.strip(), tail.strip()


# ---- Page-level extraction ----

# Column x-ranges for the standard 7-col layout (Vegetables / Fruit).
# Two layouts: odd book pages are "right" (wider left margin), even pages are "left" (shifted ~36px).
# Name column ends at "left_x_boundary" which also shifts.
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

# Header/banner noise we strip before grouping.
BANNER_Y_MAX = 170   # column headers and page banner sit above this


def _detect_layout(words):
    """Odd (right) vs even (left) column layout based on leftmost data word."""
    xs = [w[0] for w in words]
    if not xs:
        return 'right'
    min_x = min(xs)
    return 'left' if min_x < 80 else 'right'

# Keywords that indicate a "banner" line (yield-formula boilerplate or volume ref).
# Applied at line level after y-clustering.
_BANNER_LINE_SUBSTRINGS = (
    'Y% means',
    'AS means',
    'Cost per',
    '1 gal.',
    '4 qt.',
    '16 c.',
    '128 fl.',
    '1 c.',
    '8 fl. oz.',
    '1 tbsp.',
    '1 lb.',
    '(Continued)',
    'last three columns',
)


def _page_words(page):
    """Return a flat list of (x, y, x1, y1, text) with top-of-page banner area stripped."""
    words = []
    for w in page.get_text('words'):
        x0, y0, x1, y1, text, *_ = w
        if y0 < BANNER_Y_MAX:
            continue
        words.append((x0, y0, x1, y1, text))
    return words


def _is_banner_line(line) -> bool:
    text = ' '.join(w[4] for w in line)
    return any(s in text for s in _BANNER_LINE_SUBSTRINGS)


def _group_lines(words, y_tol=3.0):
    """Cluster words into lines by y-coord."""
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
    # Sort each line left-to-right
    for line in lines:
        line.sort(key=lambda w: w[0])
    return lines


def _line_has_data(line, left_x_boundary):
    """True if the line contains at least one word to the right of the item-name column."""
    return any(w[0] >= left_x_boundary for w in line)


def _line_left_text(line, left_x_boundary):
    """Join words on the left (item name column) into a string."""
    parts = [w[4] for w in line if w[0] < left_x_boundary]
    return ' '.join(parts).strip()


def _line_right_cells(line, cols, left_x_boundary):
    """Assign right-side words to columns by x-range, return dict of raw strings."""
    cells = {name: [] for name, _, _ in cols}
    for w in line:
        if w[0] < left_x_boundary:
            continue
        for col_name, x_lo, x_hi in cols:
            if x_lo <= w[0] < x_hi:
                cells[col_name].append(w[4])
                break
    return {k: ' '.join(v).strip() for k, v in cells.items()}


def _parse_page(page, book_page_num: int) -> list[ParsedRow]:
    """Extract ParsedRow list from a single PDF page."""
    words = _page_words(page)
    layout = _detect_layout(words)
    if layout == 'left':
        cols = STANDARD_COLS_LEFT
        name_boundary = NAME_BOUNDARY_LEFT
    else:
        cols = STANDARD_COLS_RIGHT
        name_boundary = NAME_BOUNDARY_RIGHT

    lines = _group_lines(words)

    rows: list[ParsedRow] = []
    pending_name_parts: list[str] = []

    for line in lines:
        if _is_banner_line(line):
            continue
        left_text = _line_left_text(line, name_boundary)
        if _line_has_data(line, name_boundary):
            # This line carries data values; item name = accumulated parts + any left text on this line
            name_parts = list(pending_name_parts)
            if left_text:
                name_parts.append(left_text)
            # Lookahead: some multi-line names continue BELOW the data line.
            # Handled by not resetting pending_name_parts until we see the NEXT data row.
            full_name = ' '.join(name_parts).strip()
            cells = _line_right_cells(line, cols, name_boundary)

            ingredient, prep_state = _split_ingredient_and_prep(full_name)
            if not ingredient:
                # No name yet — skip (shouldn't happen on well-formed pages)
                continue

            ap_w = _to_decimal(cells.get('ap_weight_raw', ''))
            trimmed_raw = cells.get('trimmed_weight_raw', '') or ''
            trimmed_w: Decimal | None = None
            trimmed_count: int | None = None
            m = _COUNT_RE.match(trimmed_raw.strip())
            if m:
                trimmed_count = int(m.group(1))
            else:
                trimmed_w = _to_decimal(trimmed_raw.strip())

            rows.append(ParsedRow(
                ingredient=ingredient,
                prep_state=prep_state,
                ap_unit=cells.get('ap_unit', '').strip(),
                ap_weight_oz=ap_w,
                trimmed_unit=cells.get('trimmed_unit', '').strip(),
                trimmed_weight_oz=trimmed_w,
                trimmed_count=trimmed_count,
                yield_pct=_parse_pct(cells.get('yield_pct_raw', '')),
                measures_per_ap=_to_decimal(cells.get('measures_per_ap_raw', '').strip() or ''),
                ounce_weight_per_cup=_to_decimal(cells.get('oz_per_cup_raw', '').strip() or ''),
                source_ref=f'p.{book_page_num}',
                raw_right_cells=[
                    cells.get('ap_unit', ''),
                    cells.get('ap_weight_raw', ''),
                    cells.get('trimmed_unit', ''),
                    cells.get('trimmed_weight_raw', ''),
                    cells.get('yield_pct_raw', ''),
                    cells.get('measures_per_ap_raw', ''),
                    cells.get('oz_per_cup_raw', ''),
                ],
            ))
            pending_name_parts = []
        else:
            # Name-only line. Accumulate.
            if left_text:
                pending_name_parts.append(left_text)

    # Post-pass: attach any trailing name parts (below the last data row) to the previous row
    if pending_name_parts and rows:
        trailing = ' '.join(pending_name_parts).strip()
        if trailing and not trailing.lower().startswith('vegetables'):
            # Merge into last row's prep_state
            last = rows[-1]
            combined = (last.prep_state + ' ' + trailing).strip() if last.prep_state else trailing
            rows[-1] = ParsedRow(**{**asdict(last), 'prep_state': combined, 'raw_right_cells': last.raw_right_cells})

    return rows


class Command(BaseCommand):
    help = 'Parse Book of Yields PDF → YieldReference rows.'

    def add_arguments(self, parser):
        parser.add_argument('--pdf', required=True, type=str, help='Path to Book of Yields PDF')
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument('--section', choices=sorted(SECTION_PAGE_RANGES.keys()),
                       help='Single section to parse')
        g.add_argument('--all', action='store_true', help='Parse every section')
        parser.add_argument('--apply', action='store_true', help='Write to DB (default is dry-run)')
        parser.add_argument('--verbose-rows', action='store_true', help='Print every parsed row')

    def handle(self, *args, **opts):
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise CommandError('PyMuPDF not installed. `pip install pymupdf`.')

        pdf_path = Path(opts['pdf']).expanduser().resolve()
        if not pdf_path.exists():
            raise CommandError(f'PDF not found: {pdf_path}')

        sections = (
            list(SECTION_PAGE_RANGES.keys()) if opts['all'] else [opts['section']]
        )

        doc = fitz.open(str(pdf_path))
        total_rows = 0
        for sect in sections:
            start_pdf, end_pdf = SECTION_PAGE_RANGES[sect]
            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== Section: {sect}  (PDF pages {start_pdf}-{end_pdf}) ==='
            ))
            section_rows: list[ParsedRow] = []
            for pn in range(start_pdf, end_pdf + 1):
                book_page = pn - 23  # cover + frontmatter offset
                page = doc[pn]
                rows = _parse_page(page, book_page)
                section_rows.extend(rows)
                self.stdout.write(f'  PDF p{pn} (book p{book_page}): {len(rows)} rows')

            if opts['verbose_rows']:
                for r in section_rows:
                    self.stdout.write(
                        f'    {r.ingredient:<40} | {r.prep_state:<45} | '
                        f'{r.ap_unit:<7}={r.ap_weight_oz}  {r.trimmed_unit}={r.trimmed_weight_oz} '
                        f'y={r.yield_pct}%  cups/ap={r.measures_per_ap}  oz/c={r.ounce_weight_per_cup} '
                        f'({r.source_ref})'
                    )

            self.stdout.write(self.style.SUCCESS(f'  Total for {sect}: {len(section_rows)}'))
            total_rows += len(section_rows)

            if opts['apply']:
                self._apply(sect, section_rows)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Grand total parsed: {total_rows}'))
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('Dry run — no DB writes. Re-run with --apply to save.'))

    @transaction.atomic
    def _apply(self, section: str, rows: list[ParsedRow]):
        created, updated = 0, 0
        for r in rows:
            obj, was_created = YieldReference.objects.update_or_create(
                ingredient=r.ingredient,
                prep_state=r.prep_state,
                section=section,
                defaults=dict(
                    yield_pct=r.yield_pct,
                    ap_unit=r.ap_unit,
                    ap_weight_oz=r.ap_weight_oz,
                    trimmed_unit=r.trimmed_unit,
                    trimmed_weight_oz=r.trimmed_weight_oz,
                    trimmed_count=r.trimmed_count,
                    measures_per_ap=r.measures_per_ap,
                    ounce_weight_per_cup=r.ounce_weight_per_cup,
                    source='Book of Yields 8e',
                    source_ref=r.source_ref,
                ),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(f'  ✔ {section}: created={created}, updated={updated}'))
