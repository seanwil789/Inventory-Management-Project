"""Report or trim entries in `.ocr_cache/`. Unbounded growth audit item
(DEP4). Cache entries are content-hashed so trimming older ones means
reprocessing those invoices would re-hit DocAI (paid) — use a generous
age threshold.

Usage:
    python manage.py clean_ocr_cache               # dry-run, shows what'd be removed
    python manage.py clean_ocr_cache --max-age 180 # different threshold (default 180d)
    python manage.py clean_ocr_cache --apply       # actually removes
"""
from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Report / trim .ocr_cache/ entries older than N days.'

    def add_arguments(self, parser):
        parser.add_argument('--max-age', type=int, default=180,
                            help='Max age in days before removal (default 180)')
        parser.add_argument('--apply', action='store_true',
                            help='Actually delete — without this flag, dry-run only')

    def handle(self, *args, **opts):
        cache_dir = Path(settings.BASE_DIR) / '.ocr_cache'
        if not cache_dir.exists():
            self.stdout.write(self.style.WARNING(f'{cache_dir} does not exist — nothing to do'))
            return

        max_age_secs = opts['max_age'] * 86400
        cutoff = time.time() - max_age_secs

        files = [f for f in cache_dir.iterdir() if f.is_file()]
        total_size = sum(f.stat().st_size for f in files)

        stale = [f for f in files if f.stat().st_mtime < cutoff]
        stale_size = sum(f.stat().st_size for f in stale)

        self.stdout.write(self.style.HTTP_INFO('=== .ocr_cache audit ==='))
        self.stdout.write(f'Total files: {len(files)}  ({total_size / 1024 / 1024:.1f} MB)')
        self.stdout.write(f'Threshold: {opts["max_age"]} days')
        self.stdout.write(
            f'Stale (older than threshold): {len(stale)} '
            f'({stale_size / 1024 / 1024:.1f} MB)')

        if not stale:
            self.stdout.write(self.style.SUCCESS('\nNothing stale. Cache healthy.'))
            return

        # Show oldest 5 stale entries
        self.stdout.write('\nOldest 5 stale:')
        stale.sort(key=lambda f: f.stat().st_mtime)
        for f in stale[:5]:
            age_days = (time.time() - f.stat().st_mtime) / 86400
            kb = f.stat().st_size / 1024
            self.stdout.write(f'  {f.name[:60]:<60} {age_days:.0f}d old  {kb:.0f} KB')

        if opts['apply']:
            for f in stale:
                f.unlink()
            self.stdout.write(self.style.SUCCESS(
                f'\nDeleted {len(stale)} file(s), freed {stale_size / 1024 / 1024:.1f} MB.'))
        else:
            self.stdout.write(self.style.WARNING(
                f'\nDry run — pass --apply to delete. '
                f'Caution: removed caches force DocAI re-charge on reprocess.'))
