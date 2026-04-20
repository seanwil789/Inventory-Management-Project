"""
Parse Book of Yields (8e) PDF → YieldReference rows.

Per-section parsers live in `myapp/yield_parsing/` — each section has its own
table layout (seafood=3-col, meats=7-col with NAMP, poultry=hierarchical,
vegetables/etc.=standard 7-col). Dispatch happens via `PARSER_FOR_SECTION`.

Run:
    python manage.py import_book_of_yields --pdf <path> --section seafood --dry-run
    python manage.py import_book_of_yields --pdf <path> --section meats --apply
    python manage.py import_book_of_yields --pdf <path> --all --apply
    python manage.py import_book_of_yields --pdf <path> --section poultry --apply --delete-existing
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.models import YieldReference
from myapp.yield_parsing import ParsedRow, PARSER_FOR_SECTION


# PDF pages are 0-indexed. book_page = pdf_index - 23 (cover + frontmatter offset).
SECTION_PAGE_RANGES = {
    # Chapter 1
    'herbs_spices':  (33, 37),
    'fresh_herbs':   (38, 38),
    # Chapter 2 — Produce.
    # Fruit chapter starts at book p.38 (PDF index 61); verified against the PDF
    # header "Chapter 2 Produce — Fruit". Previously vegetables overshot by 1
    # page, putting fruit rows (Apricots, Bananas, Berries) into the vegetables
    # section. Fixed 2026-04-19.
    'vegetables':    (48, 60),
    'fruit':         (61, 69),
    'canned':        (70, 73),
    # Chapter 3 — Starchy
    'dry_legumes':   (80, 80),
    'grains':        (81, 82),
    'pasta':         (83, 84),
    # Chapter 4 — Baking
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


class Command(BaseCommand):
    help = 'Parse Book of Yields PDF → YieldReference rows (per-section dispatch).'

    def add_arguments(self, parser):
        parser.add_argument('--pdf', required=True, type=str, help='Path to Book of Yields PDF')
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument('--section', choices=sorted(SECTION_PAGE_RANGES.keys()),
                       help='Single section to parse')
        g.add_argument('--all', action='store_true', help='Parse every section')
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB (default is dry-run)')
        parser.add_argument('--verbose-rows', action='store_true',
                            help='Print every parsed row')
        parser.add_argument('--delete-existing', action='store_true',
                            help='Delete all existing YieldReference rows for the selected '
                                 'sections before insert. Use when old data was misaligned.')

    def handle(self, *args, **opts):
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise CommandError('PyMuPDF not installed. `pip install pymupdf`.')

        pdf_path = Path(opts['pdf']).expanduser().resolve()
        if not pdf_path.exists():
            raise CommandError(f'PDF not found: {pdf_path}')

        sections = (list(SECTION_PAGE_RANGES.keys())
                    if opts['all'] else [opts['section']])

        doc = fitz.open(str(pdf_path))
        total_rows = 0

        for sect in sections:
            start_pdf, end_pdf = SECTION_PAGE_RANGES[sect]
            parser_fn = PARSER_FOR_SECTION.get(sect)
            if parser_fn is None:
                self.stdout.write(self.style.WARNING(
                    f'[skip] {sect}: no parser registered'))
                continue

            self.stdout.write(self.style.HTTP_INFO(
                f'\n=== Section: {sect}  (PDF pages {start_pdf}-{end_pdf}) ==='))

            section_rows: list[ParsedRow] = []

            # Poultry needs parent context across pages; everyone else is stateless.
            if sect == 'poultry':
                parent_carry = ''
                for pn in range(start_pdf, end_pdf + 1):
                    book_page = pn - 23
                    page = doc[pn]
                    rows, parent_carry = parser_fn(page, book_page,
                                                   initial_parent=parent_carry)
                    section_rows.extend(rows)
                    self.stdout.write(f'  PDF p{pn} (book p{book_page}): '
                                      f'{len(rows)} rows  (parent_carry={parent_carry!r})')
            else:
                for pn in range(start_pdf, end_pdf + 1):
                    book_page = pn - 23
                    page = doc[pn]
                    rows = parser_fn(page, book_page)
                    section_rows.extend(rows)
                    self.stdout.write(f'  PDF p{pn} (book p{book_page}): {len(rows)} rows')

            if opts['verbose_rows']:
                for r in section_rows:
                    extras_str = ' '.join(f'{k}={v}' for k, v in (r.extras or {}).items())
                    self.stdout.write(
                        f'    {r.ingredient:<40} | {r.prep_state[:35]:<35} | '
                        f'y={r.yield_pct}  oz={r.ap_weight_oz}  {extras_str}  ({r.source_ref})'
                    )

            self.stdout.write(self.style.SUCCESS(
                f'  Total for {sect}: {len(section_rows)}'))
            total_rows += len(section_rows)

            if opts['apply']:
                self._apply(sect, section_rows, delete_existing=opts['delete_existing'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Grand total parsed: {total_rows}'))
        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                'Dry run — no DB writes. Re-run with --apply to save.'))

    @transaction.atomic
    def _apply(self, section: str, rows: list[ParsedRow], delete_existing: bool = False):
        if delete_existing:
            deleted, _ = YieldReference.objects.filter(section=section).delete()
            self.stdout.write(self.style.WARNING(
                f'  [purge] deleted {deleted} existing {section} rows'))

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
                    extras=r.extras or None,
                    source='Book of Yields 8e',
                    source_ref=r.source_ref,
                ),
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f'  ✔ {section}: created={created}, updated={updated}'))
