"""Ingest Drive vendor-root files into DB + move them into the correct week subfolder.

The Class B mis-archive pattern: files placed directly into a vendor folder
(no week subfolder), bypassing the inbox → archive flow that batch.py owns.
`batch.py` polls the inbox; these files were never queued. Result: image
exists in Drive, no ILI in DB, parser/mapper never ran.

Surfaced by the 2026-05-20 archive audit — 14 such files (8 Delaware Linen
2025-09 through 2026-01, 5 Exceptional/PBM Feb 2026 bulk upload, 2 Aramark).

Pipeline per file:
  1. Walk Drive archive for files where the parent folder is a vendor
     folder (not a week subfolder).
  2. Download bytes → OCR (uses DocAI; reuses .ocr_cache if SHA already
     seen) → parse → map.
  3. Write to DB via write_invoice_to_db.
  4. With --move, files().update() to move from vendor-root to its
     proper week subfolder per drive._week_label(invoice_date).

Usage:
  python manage.py ingest_drive_direct                  # dry-run report
  python manage.py ingest_drive_direct --apply          # OCR + DB write, no Drive move
  python manage.py ingest_drive_direct --apply --move   # full path (DB write + Drive move)
  python manage.py ingest_drive_direct --vendor 'Delaware County Linen'
  python manage.py ingest_drive_direct --year 2025
"""
import io
import os
import sys
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand


_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)


def _list_subfolders(drive, parent_id):
    out, page_token = [], None
    q = (f"'{parent_id}' in parents and "
         f"mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    while True:
        kw = dict(q=q, fields='nextPageToken, files(id,name)', pageSize=200)
        if page_token:
            kw['pageToken'] = page_token
        res = drive.files().list(**kw).execute()
        out.extend(res.get('files', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return out


def _list_files(drive, parent_id):
    out, page_token = [], None
    q = (f"'{parent_id}' in parents and "
         f"mimeType != 'application/vnd.google-apps.folder' and trashed = false")
    while True:
        kw = dict(q=q, fields='nextPageToken, files(id,name,md5Checksum,mimeType,size,parents)',
                  pageSize=200)
        if page_token:
            kw['pageToken'] = page_token
        res = drive.files().list(**kw).execute()
        out.extend(res.get('files', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return out


def _download_bytes(drive, file_id):
    from googleapiclient.http import MediaIoBaseDownload
    req = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    d = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = d.next_chunk()
    return buf.getvalue()


class Command(BaseCommand):
    help = ('Ingest Drive vendor-root files (no week subfolder) into DB. '
            'Class B mis-archive on-ramp.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write to DB. Without this, only reports what would happen.')
        parser.add_argument('--move', action='store_true',
                            help='Also move each file from vendor-root to its proper week '
                                 'subfolder. Requires --apply.')
        parser.add_argument('--vendor', default='',
                            help='Restrict to a single vendor folder name')
        parser.add_argument('--year', type=int, default=None,
                            help='Restrict to a single year folder')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after processing N files (0 = no limit)')

    def handle(self, *args, **opts):
        try:
            from drive import get_drive_client, canonical_vendor, MONTH_NAMES, _week_label, _find_or_create_folder
            from config import DRIVE_ROOT_FOLDER_ID
            from docai import ocr_with_docai
            from parser import parse_invoice
            from mapper import load_mappings, map_items
            from db_write import write_invoice_to_db
        except ImportError as e:
            self.stderr.write(self.style.ERROR(f'Pipeline import failed: {e}'))
            return

        apply_flag = opts['apply']
        move_flag = opts['move']
        vendor_filter = opts['vendor']
        year_filter = opts['year']
        limit = opts['limit']

        if move_flag and not apply_flag:
            self.stderr.write(self.style.ERROR(
                '--move requires --apply (can\'t move a file you haven\'t ingested)'))
            return

        drive = get_drive_client()

        # Walk archive, find vendor-root files
        targets = []
        years = _list_subfolders(drive, DRIVE_ROOT_FOLDER_ID)
        for y in years:
            if not y['name'].isdigit():
                continue
            if year_filter and y['name'] != str(year_filter):
                continue
            for m in _list_subfolders(drive, y['id']):
                for v in _list_subfolders(drive, m['id']):
                    if vendor_filter and v['name'] != vendor_filter:
                        continue
                    for f in _list_files(drive, v['id']):
                        targets.append({
                            'year': y['name'], 'month': m['name'],
                            'vendor': v['name'], 'vendor_folder_id': v['id'],
                            **f,
                        })

        if not targets:
            self.stdout.write('No vendor-root files found.')
            return

        self.stdout.write(f'Vendor-root files to process: {len(targets)}')
        if limit and limit < len(targets):
            self.stdout.write(f'Limiting to first {limit}.')
            targets = targets[:limit]
        self.stdout.write('')

        # Cache mappings once
        mappings = load_mappings() if apply_flag else None

        ingested = 0
        moved = 0
        skipped = 0
        errored = 0

        for t in targets:
            label = f"{t['year']}/{t['month']}/{t['vendor']}/{t['name']}"
            self.stdout.write(f'→ {label}')

            try:
                # Download
                b = _download_bytes(drive, t['id'])
                ext = os.path.splitext(t['name'])[1].lower() or '.jpg'
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
                    tf.write(b)
                    tmp_path = tf.name
                try:
                    ocr = ocr_with_docai(tmp_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                if not ocr or not ocr.get('raw_text'):
                    self.stdout.write(f'   [skip] OCR returned empty')
                    skipped += 1
                    continue

                detected_vendor = ocr.get('vendor', 'Unknown')
                detected_date = ocr.get('invoice_date')

                # Parse
                parsed = parse_invoice(ocr['raw_text'], vendor=detected_vendor,
                                       pages=ocr.get('pages'))
                if detected_vendor and detected_vendor != 'Unknown':
                    parsed['vendor'] = detected_vendor
                if detected_date:
                    parsed['invoice_date'] = detected_date

                items = parsed.get('items', [])
                self.stdout.write(
                    f'   vendor={parsed["vendor"]} date={parsed["invoice_date"] or "?"} '
                    f'items={len(items)}')

                if not parsed['invoice_date']:
                    self.stdout.write(f'   [skip] No invoice_date — needs manual review')
                    skipped += 1
                    continue
                if not items:
                    self.stdout.write(f'   [skip] No line items')
                    skipped += 1
                    continue

                # Folder-vendor sanity check — warn if parser disagrees with folder
                if canonical_vendor(parsed['vendor']) != canonical_vendor(t['vendor']):
                    self.stdout.write(self.style.WARNING(
                        f'   ⚠ vendor mismatch: folder={t["vendor"]} vs '
                        f'parsed={parsed["vendor"]}'))

                if not apply_flag:
                    self.stdout.write(f'   [dry-run] would write to DB and {"move" if move_flag else "leave in place"}')
                    continue

                # Map
                mapped = map_items(items, mappings=mappings, vendor=parsed['vendor'])

                # DB write
                n_rows = write_invoice_to_db(
                    parsed['vendor'], parsed['invoice_date'], mapped,
                    source_file=t['name'],
                    invoice_number=parsed.get('invoice_number') or '',
                )
                self.stdout.write(f'   [✓] {n_rows} ILI rows written')
                ingested += 1

                # Move to week subfolder
                if move_flag:
                    from datetime import datetime
                    d = datetime.strptime(parsed['invoice_date'], '%Y-%m-%d')
                    canon_v = canonical_vendor(parsed['vendor'])
                    year_folder = str(d.year)
                    month_folder = f"{d.month:02d} {MONTH_NAMES[d.month]} {d.year}"
                    week_folder = _week_label(d)
                    year_id = _find_or_create_folder(drive, year_folder, DRIVE_ROOT_FOLDER_ID)
                    month_id = _find_or_create_folder(drive, month_folder, year_id)
                    vendor_id = _find_or_create_folder(drive, canon_v, month_id)
                    week_id = _find_or_create_folder(drive, week_folder, vendor_id)
                    drive.files().update(
                        fileId=t['id'],
                        addParents=week_id,
                        removeParents=t['vendor_folder_id'],
                        fields='id, parents',
                    ).execute()
                    self.stdout.write(
                        f'   [✓] moved to {year_folder}/{month_folder}/{canon_v}/{week_folder}')
                    moved += 1

            except Exception as e:
                self.stderr.write(self.style.ERROR(f'   [error] {e}'))
                errored += 1

        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write(f'Vendor-root files scanned:  {len(targets)}')
        self.stdout.write(f'Ingested to DB:             {ingested}')
        self.stdout.write(f'Moved to week subfolder:    {moved}')
        self.stdout.write(f'Skipped (no date/items):    {skipped}')
        self.stdout.write(f'Errors:                     {errored}')
