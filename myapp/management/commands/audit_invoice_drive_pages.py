"""Diagnostic: per InvoiceValidationStatus, count Drive pages in the
archive folder vs OCR cache files we have. Distinguishes missing-pages
(Drive has more files than we OCR'd) from missing-fees (we have all
pages, gap is unparsed non-item charges).

Usage:
  python manage.py audit_invoice_drive_pages              # all non-pass
  python manage.py audit_invoice_drive_pages --status fail
  python manage.py audit_invoice_drive_pages --vendor Sysco
  python manage.py audit_invoice_drive_pages --year 2025

Walks Drive once per (year, month, vendor, week) — caches folder
contents to minimize API calls when multiple invoices share a week.
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models import Q

from myapp.models import InvoiceValidationStatus


_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)


def _week_folder_for_date(d: datetime) -> tuple[int, int, str]:
    """Return (year, month, week_folder_name) for an invoice_date.

    Sysco-archive convention from drive.py: 'Week N MM.DD - MM.DD' where
    the week starts on Monday. We compute the Monday-of-week and the
    Sunday-end, then format. N is the week-of-month index (1-5).
    """
    # Find the Monday of this week
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    # Week number = ceil(monday.day / 7)
    week_n = ((monday.day - 1) // 7) + 1
    # Drive convention (verified from actual archive paths in image_cache
    # index): 'Week 1 04.06-04.12' — no spaces around the dash
    name = f'Week {week_n} {monday.month:02d}.{monday.day:02d}-{sunday.month:02d}.{sunday.day:02d}'
    return monday.year, monday.month, name


_MONTH_NAMES = {
    1: 'January', 2: 'February', 3: 'March', 4: 'April',
    5: 'May', 6: 'June', 7: 'July', 8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}


class Command(BaseCommand):
    help = ('Compare Drive archive page count to OCR cache count for '
            'each non-PASS invoice — distinguishes missing-pages from '
            'missing-fees / parser-bug cases.')

    def add_arguments(self, parser):
        parser.add_argument('--status', default='',
                            help='Filter by status: fail / review / partial')
        parser.add_argument('--vendor', default='',
                            help='Filter by vendor name')
        parser.add_argument('--year', type=int, default=None,
                            help='Filter by year')

    def handle(self, *args, **opts):
        try:
            from drive import get_drive_client, canonical_vendor
            from config import DRIVE_ROOT_FOLDER_ID
            from reprocess_archive import list_subfolders, list_files
        except ImportError as e:
            self.stdout.write(self.style.ERROR(f'Drive client unavailable: {e}'))
            return

        # Build queryset
        qs = InvoiceValidationStatus.objects.exclude(status='pass')
        if opts.get('status'):
            qs = qs.filter(status=opts['status'])
        if opts.get('vendor'):
            qs = qs.filter(vendor__name__icontains=opts['vendor'])
        if opts.get('year'):
            qs = qs.filter(invoice_date__year=opts['year'])

        targets = list(qs.select_related('vendor').order_by('-invoice_date'))
        if not targets:
            self.stdout.write('No matching invoices.')
            return

        self.stdout.write(f'Auditing {len(targets)} invoice(s)...')
        self.stdout.write('')

        drive = get_drive_client()

        # Cache: (year, month, vendor) → vendor_folder_id
        # Cache: (year, month, vendor, week_name) → list of files
        vendor_folder_cache: dict = {}
        week_files_cache: dict = {}

        # Build OCR cache lookup once
        ocr_dir = settings.BASE_DIR / '.ocr_cache'
        ocr_shas = set()
        if ocr_dir.exists():
            for p in ocr_dir.iterdir():
                if '_docai_' in p.name:
                    ocr_shas.add(p.name.split('_')[0])

        results = []
        for ivs in targets:
            d = ivs.invoice_date
            if d is None:
                continue
            year, month, week_name = _week_folder_for_date(datetime.combine(d, datetime.min.time()))
            month_str = f'{month:02d} {_MONTH_NAMES[month]} {year}'
            vendor_canon = canonical_vendor(ivs.vendor.name)

            # Walk to vendor folder, cache the ID
            vc_key = (year, month, vendor_canon)
            if vc_key not in vendor_folder_cache:
                year_folders = list_subfolders(drive, DRIVE_ROOT_FOLDER_ID)
                year_id = next((f['id'] for f in year_folders
                                if f['name'] == str(year)), None)
                if not year_id:
                    vendor_folder_cache[vc_key] = None
                    results.append({'ivs': ivs, 'drive_files': [],
                                    'cached_count': len(ivs.cache_hashes or []),
                                    'missing_count': 0,
                                    'note': f'no year folder {year}'})
                    continue
                month_folders = list_subfolders(drive, year_id)
                month_id = next((f['id'] for f in month_folders
                                 if f['name'] == month_str), None)
                if not month_id:
                    vendor_folder_cache[vc_key] = None
                    results.append({'ivs': ivs, 'drive_files': [],
                                    'cached_count': len(ivs.cache_hashes or []),
                                    'missing_count': 0,
                                    'note': f'no month folder {month_str}'})
                    continue
                vendor_folders = list_subfolders(drive, month_id)
                vendor_id = next((f['id'] for f in vendor_folders
                                  if f['name'] == vendor_canon), None)
                vendor_folder_cache[vc_key] = vendor_id

            vendor_id = vendor_folder_cache[vc_key]
            if not vendor_id:
                results.append({'ivs': ivs, 'drive_files': [],
                                'cached_count': len(ivs.cache_hashes or []),
                                'missing_count': 0,
                                'note': f'no vendor folder {vendor_canon}'})
                continue

            # Walk to week folder
            wk_key = (year, month, vendor_canon, week_name)
            if wk_key not in week_files_cache:
                week_folders = list_subfolders(drive, vendor_id)
                week_id = next((f['id'] for f in week_folders
                                if f['name'] == week_name), None)
                if week_id:
                    week_files_cache[wk_key] = list_files(drive, week_id)
                else:
                    week_files_cache[wk_key] = None

            files = week_files_cache[wk_key]
            if files is None:
                # Try to find the week heuristically — week names sometimes
                # vary in folder convention
                week_folders = list_subfolders(drive, vendor_id)
                fallback_id = None
                for w in week_folders:
                    if w['name'].startswith('Week') and (
                        f'{d.month:02d}.{d.day:02d}' in w['name']
                        or _date_in_week_range(d, w['name'])
                    ):
                        fallback_id = w['id']
                        week_name = w['name']
                        break
                if fallback_id:
                    files = list_files(drive, fallback_id)
                    week_files_cache[wk_key] = files
                else:
                    results.append({'ivs': ivs, 'drive_files': [],
                                    'cached_count': len(ivs.cache_hashes or []),
                                    'missing_count': 0,
                                    'note': f'no week folder for {d}'})
                    continue

            # Now we have a list of Drive files for this week.
            # We can't easily attribute each file to a specific invoice
            # without downloading + hashing. But we can show the count
            # and flag the gap.
            cached_count = len(ivs.cache_hashes or [])
            drive_count = len(files)
            results.append({
                'ivs': ivs,
                'drive_files': [f['name'] for f in files],
                'cached_count': cached_count,
                'drive_count': drive_count,
                'week_folder': week_name,
                'note': '',
            })

        # Render
        self.stdout.write('')
        self.stdout.write(
            f'  {"date":<12} {"vendor":<12} {"inv#":<11} {"status":<8} '
            f'{"cached":>7} {"drive":>6}  delta  files'
        )
        self.stdout.write('  ' + '-' * 90)
        for r in results:
            ivs = r['ivs']
            cached = r['cached_count']
            drive = r.get('drive_count', '?')
            if drive == '?':
                delta = '?'
            else:
                delta = drive - cached
            files_str = ', '.join(r['drive_files'][:5])
            if len(r['drive_files']) > 5:
                files_str += f' (+{len(r["drive_files"])-5} more)'
            self.stdout.write(
                f'  {str(ivs.invoice_date):<12} {ivs.vendor.name[:12]:<12} '
                f'{ivs.invoice_number:<11} {ivs.status:<8} '
                f'{cached:>7} {drive:>6}  {delta:>5}  {files_str}'
            )
            if r.get('note'):
                self.stdout.write(f'    [!] {r["note"]}')

        # Summary
        self.stdout.write('')
        with_drive = [r for r in results if isinstance(r.get('drive_count'), int)]
        more_drive = [r for r in with_drive if r['drive_count'] > r['cached_count']]
        same_count = [r for r in with_drive if r['drive_count'] == r['cached_count']]
        self.stdout.write('Summary:')
        self.stdout.write(f'  Drive has MORE files than OCR cache (likely missing pages): {len(more_drive)}')
        self.stdout.write(f'  Drive matches OCR cache (gap is fees/parser, not pages):     {len(same_count)}')


def _date_in_week_range(d, week_name: str) -> bool:
    """Check if date d falls within the range encoded in 'Week N MM.DD - MM.DD'."""
    import re
    m = re.search(r'(\d{2})\.(\d{2})\s*-\s*(\d{2})\.(\d{2})', week_name)
    if not m:
        return False
    s_mo, s_da, e_mo, e_da = (int(g) for g in m.groups())
    s = datetime(d.year, s_mo, s_da).date()
    e = datetime(d.year, e_mo, e_da).date()
    if s > e:  # week crosses year boundary
        e = datetime(d.year + 1, e_mo, e_da).date()
    return s <= d <= e
