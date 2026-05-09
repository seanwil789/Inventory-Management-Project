"""Re-OCR missing pages for a specific InvoiceValidationStatus.

Targeted version of reprocess_archive — only processes the Drive files
that aren't yet in `.ocr_cache/` for the invoice's week folder. No DB
writes, no archive sweep; just adds missing OCR results so
validate_all_invoices --apply can regroup the invoice with its full
page set.

Usage:
  python manage.py reocr_for_invoice <ivs_id>
  python manage.py reocr_for_invoice 105 --apply

Default is dry-run (lists what would be OCR'd). --apply does the OCR.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from myapp.models import InvoiceValidationStatus

_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)


SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf'}


class Command(BaseCommand):
    help = ('Re-OCR pages for a non-PASS invoice. Walks the Drive week '
            'folder for the invoice, OCRs files not already in cache.')

    def add_arguments(self, parser):
        parser.add_argument('ivs_id', type=int, help='InvoiceValidationStatus.id')
        parser.add_argument('--apply', action='store_true',
                            help='Actually run DocAI. Default is dry-run.')

    def handle(self, *args, **opts):
        from drive import get_drive_client, canonical_vendor
        from config import DRIVE_ROOT_FOLDER_ID, DOCAI_PROCESSOR_ID
        from reprocess_archive import list_subfolders, list_files
        from googleapiclient.http import MediaIoBaseDownload
        from myapp.management.commands.audit_invoice_drive_pages import _folder_covers_date
        import hashlib

        if not DOCAI_PROCESSOR_ID:
            self.stdout.write(self.style.ERROR('DOCAI_PROCESSOR_ID not set'))
            return

        try:
            ivs = InvoiceValidationStatus.objects.select_related('vendor').get(
                pk=opts['ivs_id'])
        except InvoiceValidationStatus.DoesNotExist:
            self.stdout.write(self.style.ERROR(
                f'No InvoiceValidationStatus with id={opts["ivs_id"]}'))
            return

        d = ivs.invoice_date
        vendor_canon = canonical_vendor(ivs.vendor.name)
        self.stdout.write(f'Target: ivs_id={ivs.id}  inv#={ivs.invoice_number}  '
                          f'vendor={vendor_canon}  date={d}  status={ivs.status}')
        self.stdout.write(f'Currently cached: {len(ivs.cache_hashes or [])} '
                          f'page(s) — {ivs.cache_hashes}')

        # Walk to the right week folder
        drive = get_drive_client()
        year_folders = list_subfolders(drive, DRIVE_ROOT_FOLDER_ID)
        year_id = next((f['id'] for f in year_folders
                        if f['name'] == str(d.year)), None)
        if not year_id:
            self.stdout.write(self.style.ERROR(f'No Drive year folder {d.year}'))
            return
        month_folders = list_subfolders(drive, year_id)
        # Match month flexibly — e.g. "06 June 2025" or "09 September 2025"
        month_id = None
        for m in month_folders:
            if str(d.month).zfill(2) in m['name'][:3] or str(d.month) in m['name'][:2]:
                month_id = m['id']
                break
        if not month_id:
            self.stdout.write(self.style.ERROR(
                f'No Drive month folder for {d.year}-{d.month}'))
            return
        vendor_folders = list_subfolders(drive, month_id)
        vendor_id = next((f['id'] for f in vendor_folders
                          if f['name'] == vendor_canon), None)
        if not vendor_id:
            self.stdout.write(self.style.ERROR(
                f'No Drive vendor folder for {vendor_canon}'))
            return
        all_weeks = list_subfolders(drive, vendor_id)
        week_folder = None
        for w in all_weeks:
            if _folder_covers_date(w['name'], d):
                week_folder = w
                break
        if not week_folder:
            self.stdout.write(self.style.ERROR(
                f'No week folder covering {d}'))
            self.stdout.write('  Available weeks: ' +
                              ', '.join(w['name'] for w in all_weeks))
            return
        self.stdout.write(f'Week folder: {week_folder["name"]}')
        files = list_files(drive, week_folder['id'])
        self.stdout.write(f'Drive files in week: {len(files)}')

        # Build OCR cache lookup
        ocr_dir = settings.BASE_DIR / '.ocr_cache'
        ocr_shas = set()
        if ocr_dir.exists():
            for p in ocr_dir.iterdir():
                if '_docai_' in p.name:
                    ocr_shas.add(p.name.split('_')[0])

        # For each Drive file: download bytes, compute SHA, check if cached
        plan = []  # files we need to OCR
        for f in files:
            ext = os.path.splitext(f['name'])[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                self.stdout.write(f'  skip (unsupported ext): {f["name"]}')
                continue
            # Download bytes (small files; quick)
            request = drive.files().get_media(fileId=f['id'])
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            image_bytes = buf.getvalue()
            sha = hashlib.sha256(image_bytes).hexdigest()
            if sha in ocr_shas:
                self.stdout.write(f'  ✓ already cached: {f["name"]}  ({sha[:16]})')
            else:
                self.stdout.write(f'  ✗ MISSING OCR: {f["name"]}  ({sha[:16]})')
                plan.append({'file': f, 'sha': sha, 'bytes': image_bytes,
                             'ext': ext})

        self.stdout.write('')
        self.stdout.write(f'Plan: re-OCR {len(plan)} file(s)')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to OCR via DocAI.'))
            return

        if not plan:
            self.stdout.write('Nothing to do.')
            return

        # Apply: write each missing file to a temp path, OCR via DocAI,
        # save to ocr_cache.
        from docai import ocr_with_docai
        import json

        for entry in plan:
            f = entry['file']
            self.stdout.write(f'  OCR\'ing {f["name"]}...')
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix=entry['ext']) as tmp:
                tmp.write(entry['bytes'])
                tmp_path = tmp.name
            try:
                docai_result = ocr_with_docai(tmp_path)
                if docai_result is None:
                    self.stdout.write(self.style.WARNING(
                        f'    DocAI returned None for {f["name"]}'))
                    continue
                # Save to .ocr_cache/<sha>_docai_ocr.json
                cache_path = ocr_dir / f'{entry["sha"]}_docai_ocr.json'
                with open(cache_path, 'w') as cf:
                    json.dump(docai_result, cf)
                self.stdout.write(self.style.SUCCESS(
                    f'    cached → {cache_path.name}'))
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. Now run: python manage.py validate_all_invoices --apply'
        ))
