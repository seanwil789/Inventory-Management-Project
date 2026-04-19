"""
Local file cache for OCR API results.

Caches output from ocr_with_docai(), parse_with_docai(), and extract_text()
so reprocessing the same invoice file never re-calls the paid API.

Cache key = SHA256 of file content + function name.
Storage = JSON files in .ocr_cache/ directory.
"""

import hashlib
import json
import os

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ocr_cache")


def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _file_hash(file_path: str) -> str:
    """SHA256 hash of file content."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path(file_hash: str, func_name: str) -> str:
    return os.path.join(_CACHE_DIR, f"{file_hash}_{func_name}.json")


def get(file_path: str, func_name: str):
    """
    Look up cached result for a file + function.
    Returns the cached dict/str, or None if not cached.
    """
    try:
        fh = _file_hash(file_path)
        cp = _cache_path(fh, func_name)
        if os.path.exists(cp):
            with open(cp, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def put(file_path: str, func_name: str, result):
    """
    Store a result in the cache.
    result can be a dict, str, or None.
    """
    try:
        _ensure_cache_dir()
        fh = _file_hash(file_path)
        cp = _cache_path(fh, func_name)
        with open(cp, "w") as f:
            json.dump(result, f)
    except Exception as e:
        print(f"  [cache] Warning: failed to write cache — {e}")


def stats():
    """Return cache statistics."""
    if not os.path.exists(_CACHE_DIR):
        return {"files": 0, "size_mb": 0}
    files = os.listdir(_CACHE_DIR)
    total_size = sum(os.path.getsize(os.path.join(_CACHE_DIR, f)) for f in files)
    return {"files": len(files), "size_mb": round(total_size / 1024 / 1024, 2)}
