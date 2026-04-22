"""Backfill InvoiceLineItem.section_hint for existing rows by re-reading
OCR caches.

For each unmapped `[Sysco #NNN]` row, locate the OCR cache (by content hash
of the source image), find the Sysco item code in raw_text, walk backward
to the nearest `***SECTION***` header, and stamp the normalized section name
onto the row.

Also stamps section_hint for ALL Sysco rows if --all is passed — useful
after the initial schema bump, before the next reprocess cycle.

Usage:
    python manage.py backfill_section_hints               # unknown-code rows only
    python manage.py backfill_section_hints --all         # every Sysco row
    python manage.py backfill_section_hints --dry-run     # report, don't write
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


# Match lines that LOOK LIKE section headers: start with asterisks, or are
# wrapped in asterisks. Reject GROUP TOTAL, PRICE, EXTENDED lines that happen
# to have trailing asterisks.
_SECTION_HEADER_RE = re.compile(r'^\s*\*{2,}\s*[A-Z][A-Z &]+[A-Z]\s*\*{0,}\s*$')
_SECTION_EXCLUDE_RE = re.compile(
    r'\b(?:GROUP\s*TOTAL|PRICE|EXTENDED|TOTAL|AMOUNT|QTY|SUBTOTAL)\b',
    re.IGNORECASE,
)


def _section_before(raw_text: str, target_pos: int) -> str:
    """Walk backward from `target_pos` in raw_text to find the nearest
    section-header line. Returns normalized section name ('DAIRY') or ''."""
    lines = raw_text[:target_pos].splitlines()
    for line in reversed(lines):
        if not _SECTION_HEADER_RE.match(line):
            continue
        if _SECTION_EXCLUDE_RE.search(line):
            continue
        name = re.sub(r'[*\s]+', ' ', line).strip()
        if len(name) < 3:
            continue
        return name[:60]
    return ''


def _build_cache_index(cache_dir: Path) -> dict[str, Path]:
    """Return {source_filename: cache_path} by inspecting each cache's
    'vendor'/'source' metadata. Our OCR cache is keyed by content hash
    which we can't reverse without the image — so index by any source
    filename the cache records. Falls back to scanning all caches."""
    # The cache doesn't store the source filename directly; we'll return
    # the list of all caches and match by presence of a probe code.
    return {}


class Command(BaseCommand):
    help = 'Stamp section_hint on InvoiceLineItem rows from OCR cache.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='Backfill ALL Sysco rows (not just unknown-code). '
                                 'Slower but gives every row a category hint.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing.')

    def handle(self, *args, **opts):
        cache_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not cache_dir.exists():
            self.stdout.write(self.style.ERROR(f'OCR cache dir missing: {cache_dir}'))
            return

        # Pick target rows
        if opts['all']:
            qs = (InvoiceLineItem.objects
                  .filter(vendor__name__icontains='sysco', section_hint=''))
            label = 'all empty-section Sysco rows'
        else:
            qs = (InvoiceLineItem.objects
                  .filter(vendor__name__icontains='sysco',
                          section_hint='',
                          raw_description__regex=r'^\[Sysco\s*#\d+\]'))
            label = 'unknown-code Sysco rows'

        total = qs.count()
        self.stdout.write(f'Targeting {total} {label}')
        if not total:
            return

        # Load all Sysco OCR caches once
        caches = []
        for cf in cache_dir.glob('*_docai_ocr.json'):
            try:
                data = json.loads(cf.read_text())
            except Exception:
                continue
            if 'sysco' not in (data.get('vendor') or '').lower():
                continue
            caches.append(data.get('raw_text', ''))
        self.stdout.write(f'Loaded {len(caches)} Sysco OCR caches')

        # Process each row: extract code from raw_description, find in caches,
        # walk back to section header
        updated = 0
        not_found = 0
        no_section = 0
        code_re = re.compile(r'\[Sysco\s*#(\d+)\]')

        for row in qs.iterator():
            # Try two probes in order: (1) [Sysco #NNN] code, (2) raw_description
            # text itself. Mapped rows have real descriptions; unmapped rows have
            # the code placeholder.
            probe = None
            m = code_re.search(row.raw_description or '')
            if m:
                probe = m.group(1)
            elif row.raw_description and len(row.raw_description) >= 10:
                # Use the raw_description as probe — long enough to be
                # distinctive in the OCR text.
                probe = row.raw_description[:40]
            else:
                not_found += 1
                continue

            # Find the probe in the OCR caches
            section_found = ''
            for raw_text in caches:
                pos = raw_text.find(probe)
                if pos < 0:
                    continue
                section = _section_before(raw_text, pos)
                if section:
                    section_found = section
                    break

            if not section_found:
                no_section += 1
                continue

            if opts['dry_run']:
                self.stdout.write(f'  [dry] row {row.id} {row.raw_description}: '
                                  f'{section_found!r}')
            else:
                row.section_hint = section_found
                row.save(update_fields=['section_hint'])
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Updated: {updated}  No section found: {no_section}  '
            f'No code on row: {not_found}'))
