"""Backfill the local image cache from the Drive archive.

For each file in `Kitchen Invoices/YYYY/Month/Vendor/Week N/`, downloads
the bytes, computes SHA256, and saves to `.image_cache/<sha>.<ext>`.
Builds the SHA→Drive metadata index so future cache misses can re-fetch.

One-time backfill. Going forward, batch.py caches images at OCR time
(see image_cache.cache_image_bytes integration in process_invoice).

Usage:
  python manage.py backfill_image_cache              # dry-run summary
  python manage.py backfill_image_cache --apply
  python manage.py backfill_image_cache --apply --year 2026
  python manage.py backfill_image_cache --apply --vendor Sysco

Network-bound — full archive walk takes ~10-30 minutes depending on
archive size + Pi network.
"""
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings


_IP_PATH = str(settings.BASE_DIR / 'invoice_processor')
if _IP_PATH not in sys.path:
    sys.path.insert(0, _IP_PATH)

from image_cache import (  # noqa: E402
    cache_image_bytes, is_cached, compute_sha256, cache_stats,
)


SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf'}


class Command(BaseCommand):
    help = ('Backfill .image_cache/ by walking the Drive archive. '
            'Default is dry-run; pass --apply to download + save.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Download + save. Without this, dry-run only.')
        parser.add_argument('--year', help='Limit to a year folder (e.g. 2026)')
        parser.add_argument('--vendor', help='Limit to a vendor folder (e.g. Sysco)')
        parser.add_argument('--limit', type=int,
                            help='Cap number of files (testing/cost control)')

    def handle(self, *args, **opts):
        apply_writes = opts.get('apply')
        year_filter = opts.get('year')
        vendor_filter = opts.get('vendor')
        limit = opts.get('limit')

        try:
            from drive import get_drive_client  # noqa: E402
            from config import DRIVE_ROOT_FOLDER_ID  # noqa: E402
            from reprocess_archive import walk_archive  # noqa: E402
            from googleapiclient.http import MediaIoBaseDownload  # noqa: E402
        except ImportError as e:
            self.stdout.write(self.style.ERROR(
                f'Drive client unavailable: {e}'))
            return

        if not DRIVE_ROOT_FOLDER_ID:
            self.stdout.write(self.style.ERROR(
                'DRIVE_ROOT_FOLDER_ID not set in .env'))
            return

        before = cache_stats()
        self.stdout.write(f'Cache before: {before}')

        drive = get_drive_client()
        n_seen = 0
        n_skipped_year = 0
        n_skipped_already_cached = 0
        n_skipped_unsupported = 0
        n_downloaded = 0
        n_errors = 0
        bytes_downloaded = 0
        started = time.time()

        for file_dict, vendor_name, folder_path in walk_archive(drive, DRIVE_ROOT_FOLDER_ID, vendor_filter):
            n_seen += 1
            if year_filter and not folder_path.startswith(year_filter):
                n_skipped_year += 1
                continue
            if limit is not None and n_downloaded >= limit:
                self.stdout.write(f'Hit --limit {limit}; stopping.')
                break
            ext = os.path.splitext(file_dict['name'])[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                n_skipped_unsupported += 1
                continue

            try:
                if apply_writes:
                    # Download bytes into memory
                    request = drive.files().get_media(fileId=file_dict['id'])
                    import io
                    buf = io.BytesIO()
                    downloader = MediaIoBaseDownload(buf, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    image_bytes = buf.getvalue()
                    sha = compute_sha256(image_bytes)

                    if is_cached(sha):
                        n_skipped_already_cached += 1
                        # Still update index in case it's missing metadata
                        from image_cache import update_index
                        update_index(sha, {
                            'drive_file_id': file_dict['id'],
                            'drive_name': file_dict['name'],
                            'drive_path': folder_path,
                            'drive_mime_type': file_dict.get('mimeType', ''),
                            'ext': ext,
                            'size_bytes': len(image_bytes),
                            'cached_at': datetime.now(timezone.utc).isoformat(),
                            'vendor': vendor_name,
                        })
                        continue

                    cache_image_bytes(
                        sha, image_bytes, ext=ext,
                        drive_metadata={
                            'drive_file_id': file_dict['id'],
                            'drive_name': file_dict['name'],
                            'drive_path': folder_path,
                            'drive_mime_type': file_dict.get('mimeType', ''),
                            'ext': ext,
                            'size_bytes': len(image_bytes),
                            'cached_at': datetime.now(timezone.utc).isoformat(),
                            'vendor': vendor_name,
                        },
                    )
                    n_downloaded += 1
                    bytes_downloaded += len(image_bytes)

                    if n_downloaded % 10 == 0:
                        elapsed = time.time() - started
                        mb = bytes_downloaded / 1024 / 1024
                        self.stdout.write(
                            f'  ... {n_downloaded} cached '
                            f'({mb:.1f} MB, {elapsed:.0f}s elapsed)')
                else:
                    # Dry-run: just count
                    n_downloaded += 1
            except Exception as e:
                n_errors += 1
                self.stdout.write(self.style.WARNING(
                    f'  [error] {folder_path}/{file_dict["name"]}: {e}'))

        elapsed = time.time() - started
        self.stdout.write('')
        self.stdout.write('Summary:')
        self.stdout.write(f'  Files seen:               {n_seen}')
        self.stdout.write(f'  Skipped (year filter):    {n_skipped_year}')
        self.stdout.write(f'  Skipped (unsupported):    {n_skipped_unsupported}')
        self.stdout.write(f'  Skipped (already cached): {n_skipped_already_cached}')
        if apply_writes:
            self.stdout.write(f'  Newly cached:             {n_downloaded}')
            self.stdout.write(f'  Bytes downloaded:         '
                              f'{bytes_downloaded/1024/1024:.1f} MB')
        else:
            self.stdout.write(f'  Would cache:              {n_downloaded}')
        self.stdout.write(f'  Errors:                   {n_errors}')
        self.stdout.write(f'  Elapsed:                  {elapsed:.0f}s')

        after = cache_stats()
        self.stdout.write(f'Cache after: {after}')

        if not apply_writes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Dry-run only. Re-run with --apply to download + cache.'))
