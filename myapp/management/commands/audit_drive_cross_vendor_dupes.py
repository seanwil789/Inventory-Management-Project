"""Drive archive audit: same-content files (same MD5) in different vendor folders.

This is the definitive mis-archive signal — file content is authoritative,
not metadata. When the same MD5 appears under two vendor folders, one of
them is wrong. Surfaced 2026-05-20 during the Drive-archive deep audit; one
real instance found (PBM invoice copied into Exceptional Foods folder).

Usage:
  python manage.py audit_drive_cross_vendor_dupes
  python manage.py audit_drive_cross_vendor_dupes --year 2026
  python manage.py audit_drive_cross_vendor_dupes --json    # machine-readable
"""
import json
import sys
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand


# Add invoice_processor to sys.path so we can import drive helpers
_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)


def _list_subfolders(drive, parent_id):
    """Page through all subfolders of parent_id."""
    out = []
    q = (f"'{parent_id}' in parents and "
         f"mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    page_token = None
    while True:
        kwargs = dict(q=q, fields='nextPageToken, files(id,name)', pageSize=200)
        if page_token:
            kwargs['pageToken'] = page_token
        res = drive.files().list(**kwargs).execute()
        out.extend(res.get('files', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return out


def _list_files(drive, parent_id):
    """Page through all non-folder files under parent_id, with md5Checksum."""
    out = []
    q = (f"'{parent_id}' in parents and "
         f"mimeType != 'application/vnd.google-apps.folder' and trashed = false")
    page_token = None
    while True:
        kwargs = dict(
            q=q,
            fields='nextPageToken, files(id,name,md5Checksum,size,modifiedTime)',
            pageSize=200,
        )
        if page_token:
            kwargs['pageToken'] = page_token
        res = drive.files().list(**kwargs).execute()
        out.extend(res.get('files', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return out


def _walk_archive(drive, root_id, year_filter=None):
    """Walk Drive archive, yielding dict per file with year/month/vendor/week."""
    years = _list_subfolders(drive, root_id)
    for y in years:
        if not y['name'].isdigit():
            continue
        if year_filter and y['name'] != str(year_filter):
            continue
        months = _list_subfolders(drive, y['id'])
        for m in months:
            vendors = _list_subfolders(drive, m['id'])
            for v in vendors:
                # Direct vendor-root files (no week subfolder)
                for f in _list_files(drive, v['id']):
                    yield {
                        'year': y['name'], 'month': m['name'],
                        'vendor': v['name'], 'week': '(direct)',
                        **f,
                    }
                # Files inside week subfolders
                for w in _list_subfolders(drive, v['id']):
                    for f in _list_files(drive, w['id']):
                        yield {
                            'year': y['name'], 'month': m['name'],
                            'vendor': v['name'], 'week': w['name'],
                            **f,
                        }


class Command(BaseCommand):
    help = ('Audit Drive archive for files with same MD5 in different vendor '
            'folders. This is the definitive mis-archive signal — content is '
            'authoritative, vendor folder is metadata.')

    def add_arguments(self, parser):
        parser.add_argument('--year', type=int, default=None,
                            help='Restrict scan to a single year folder (e.g. 2026)')
        parser.add_argument('--json', action='store_true',
                            help='Emit machine-readable JSON instead of formatted table')

    def handle(self, *args, **opts):
        try:
            from drive import get_drive_client, canonical_vendor
            from config import DRIVE_ROOT_FOLDER_ID
        except ImportError as e:
            self.stderr.write(self.style.ERROR(f'Drive client unavailable: {e}'))
            return

        year_filter = opts.get('year')
        drive = get_drive_client()

        if not opts['json']:
            self.stdout.write(f'Walking Drive archive '
                              f'({"year=" + str(year_filter) if year_filter else "all years"})...')

        # Group files by md5Checksum
        by_md5 = defaultdict(list)
        total_files = 0
        for f in _walk_archive(drive, DRIVE_ROOT_FOLDER_ID, year_filter=year_filter):
            total_files += 1
            md5 = f.get('md5Checksum')
            if not md5:
                continue
            by_md5[md5].append(f)

        # Find groups with multiple distinct CANONICAL vendor folders
        mis_archives = []
        for md5, files in by_md5.items():
            canon_vendors = {canonical_vendor(f['vendor']) for f in files}
            if len(canon_vendors) > 1:
                mis_archives.append({'md5': md5, 'files': files,
                                     'vendors': sorted(canon_vendors)})

        if opts['json']:
            out = {
                'total_files_scanned': total_files,
                'unique_md5s': len(by_md5),
                'cross_vendor_dupes': len(mis_archives),
                'detail': [
                    {
                        'md5': m['md5'],
                        'vendors': m['vendors'],
                        'files': [
                            {
                                'id': f['id'], 'name': f['name'],
                                'year': f['year'], 'month': f['month'],
                                'vendor': f['vendor'], 'week': f['week'],
                                'modified': f.get('modifiedTime'),
                            } for f in m['files']
                        ],
                    } for m in mis_archives
                ],
            }
            self.stdout.write(json.dumps(out, indent=2))
            return

        # Pretty-print summary
        self.stdout.write('')
        self.stdout.write(f'  Total Drive files scanned: {total_files}')
        self.stdout.write(f'  Unique MD5 hashes:         {len(by_md5)}')
        self.stdout.write(f'  Cross-vendor duplicates:   {len(mis_archives)}')
        self.stdout.write('')

        if not mis_archives:
            self.stdout.write(self.style.SUCCESS(
                '  ✓ No cross-vendor MD5 duplicates found — archive is consistent.'))
            return

        self.stdout.write(self.style.WARNING(
            f'  ⚠ {len(mis_archives)} mis-archive(s) detected:'))
        self.stdout.write('')
        for m in mis_archives:
            self.stdout.write(f"  MD5 {m['md5']}")
            self.stdout.write(f'    Vendors: {", ".join(m["vendors"])}')
            for f in m['files']:
                self.stdout.write(
                    f"      {f['year']}/{f['month']}/{f['vendor']}/{f['week']}/{f['name']}"
                )
                self.stdout.write(f"        id={f['id']}")
            self.stdout.write('')
