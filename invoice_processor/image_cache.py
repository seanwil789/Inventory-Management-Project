"""Local cache of invoice images for the rectification UI.

The original paper-invoice images live in the Google Drive archive
(`Kitchen Invoices/YYYY/Month/Vendor/Week N/...`). For the L1 dual-view
to render them quickly, this module caches the bytes locally at
`.image_cache/<sha>.<ext>` so the Django view can serve via FileResponse
without per-request Drive API calls.

Cache key: SHA256 of file content. Matches the OCR cache key in
`.ocr_cache/<sha>_docai_ocr.json`. Lookups can use either the full
64-char SHA256 OR the 16-char prefix that
`InvoiceValidationStatus.cache_hashes` stores.

Index: `.image_cache/_index.json` is a `{sha: {...drive_metadata}}` map
that lets us re-fetch from Drive if the local file is missing (e.g.
cache wiped, fresh Pi). Index isn't load-bearing — image lookup falls
back to filesystem glob — but speeds up future operations and lets a
catch-up mgmt cmd resume gracefully.

Built 2026-05-09 for L1 Phase 1.1b. See project_quickbooks_roadmap.md.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional


_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_DIR = _PROJECT_ROOT / '.image_cache'
_INDEX_PATH = _CACHE_DIR / '_index.json'


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_dir() -> Path:
    """Path to the cache directory (created on demand)."""
    _ensure_cache_dir()
    return _CACHE_DIR


def cache_path_for_hash(sha: str) -> Optional[Path]:
    """Return the local file path for a cached image, or None if not cached.

    Accepts the full 64-char SHA256 OR a prefix (≥8 chars). The 16-char
    prefix used in `InvoiceValidationStatus.cache_hashes` works directly.
    Returns the first match; when multiple files share the prefix
    (extremely unlikely with a 16-char SHA prefix) the lexicographically
    first one wins.
    """
    if not sha or len(sha) < 8:
        return None
    if not _CACHE_DIR.exists():
        return None
    matches = sorted(_CACHE_DIR.glob(f'{sha}*'))
    # Filter out the index file
    matches = [p for p in matches if p.name != '_index.json']
    return matches[0] if matches else None


def is_cached(sha: str) -> bool:
    """True when an image with this SHA (or prefix) is locally cached."""
    return cache_path_for_hash(sha) is not None


def cache_image_bytes(sha: str, image_bytes: bytes,
                     ext: str = '.jpg',
                     drive_metadata: Optional[dict] = None) -> Path:
    """Save image bytes to cache + optionally update the index.

    Args:
        sha: full 64-char SHA256 of the file content
        image_bytes: raw bytes
        ext: file extension including leading dot ('.jpg', '.pdf', etc.)
        drive_metadata: optional dict with at least 'drive_file_id';
                        also typically: 'drive_name', 'drive_path',
                        'size_bytes', 'cached_at'

    Returns the cache path. Idempotent: re-writing the same SHA replaces
    the file content (size may differ if Drive file was replaced).
    """
    _ensure_cache_dir()
    if not ext.startswith('.'):
        ext = '.' + ext
    path = _CACHE_DIR / f'{sha}{ext}'
    path.write_bytes(image_bytes)
    if drive_metadata:
        update_index(sha, drive_metadata)
    return path


def compute_sha256(image_bytes: bytes) -> str:
    """SHA256 hex digest — matches the OCR cache key derivation."""
    return hashlib.sha256(image_bytes).hexdigest()


def read_index() -> dict:
    """Load the SHA→drive metadata index. Empty dict when missing."""
    if not _INDEX_PATH.exists():
        return {}
    try:
        return json.loads(_INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def update_index(sha: str, metadata: dict) -> None:
    """Merge metadata into the index for this SHA. Atomic-write."""
    _ensure_cache_dir()
    index = read_index()
    existing = index.get(sha) or {}
    existing.update(metadata)
    index[sha] = existing
    # Write atomically — write to temp, rename
    tmp = _INDEX_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
    tmp.replace(_INDEX_PATH)


def get_drive_metadata(sha: str) -> Optional[dict]:
    """Look up Drive metadata (file_id, name, path, etc.) for a SHA.

    Accepts full SHA or prefix. Falls back to scanning the index for
    any key starting with the prefix when an exact match isn't found.
    """
    if not sha:
        return None
    index = read_index()
    if sha in index:
        return index[sha]
    # Prefix scan
    for k, v in index.items():
        if k.startswith(sha):
            return v
    return None


def cache_stats() -> dict:
    """Quick stats for ops/health surfaces."""
    if not _CACHE_DIR.exists():
        return {'exists': False, 'files': 0, 'size_mb': 0, 'index_entries': 0}
    files = [p for p in _CACHE_DIR.iterdir()
             if p.is_file() and p.name != '_index.json']
    size_bytes = sum(p.stat().st_size for p in files)
    return {
        'exists': True,
        'files': len(files),
        'size_mb': round(size_bytes / 1024 / 1024, 2),
        'index_entries': len(read_index()),
    }
