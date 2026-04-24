"""Microsoft Graph / OneDrive integration.

Provides auth + minimal file ops against a user's OneDrive via the
"Synergy Budget Sync" Azure AD app registration. Auth is the
client-credentials grant (application permissions, no interactive
login) so this works in cron.

Required environment variables (set in .env):
    AZURE_CLIENT_ID       — App registration's Application (client) ID
    AZURE_TENANT_ID       — Directory (tenant) ID
    AZURE_CLIENT_SECRET   — Client secret value (one-time-displayed in Azure)
    AZURE_REPORTS_FOLDER_ID — drive item ID of the Kitchen Reports/ folder
    AZURE_USER_ID         — user principal name (UPN) of the OneDrive owner
                            whose drive we're accessing (e.g. sean@wentworth.org)

Required scopes on the app registration (granted by tenant admin):
    Files.Read.All              — read any file/folder in the tenant
    Files.ReadWrite.Selected    — write to specifically-shared folders only
    User.Read                   — SSO identity

Module exposes:
    get_graph_token()                — bearer token (cached until expiry)
    download_file_by_path(path)      — bytes from /drive/root:/<path>:/content
    download_file_by_id(item_id)     — bytes from /drive/items/<id>/content
    upload_to_folder(folder_id, ...) — PUT bytes to a folder, returns dict
    list_folder(folder_id)           — list children of a folder

All HTTP errors raise OneDriveError with response body context.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

# msal is the Microsoft-published auth library.
# Imported lazily inside get_graph_token() so test environments without
# the package can still import this module to inspect helpers.
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, Any] = {"value": None, "expires_at": 0.0}


class OneDriveError(RuntimeError):
    """Raised when a Graph API call returns non-2xx, with body context."""


def _config() -> dict[str, str]:
    """Pull Azure config from env. Raises OneDriveError if any are missing."""
    keys = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET",
            "AZURE_USER_ID")
    cfg = {k: os.environ.get(k, "") for k in keys}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise OneDriveError(
            f"OneDrive not configured — missing env vars: {', '.join(missing)}. "
            "See invoice_processor/onedrive.py docstring for setup."
        )
    return cfg


def _drive_root_for_user(user_id: str) -> str:
    """Graph URL prefix for the OneDrive owned by a specific user.
    Application-permission calls always need to specify which user's drive."""
    return f"{_GRAPH_BASE}/users/{user_id}/drive"


def get_graph_token(force_refresh: bool = False) -> str:
    """Acquire a Graph API bearer token via client-credentials grant.

    Cached in-process until 60s before expiry. Thread-safe. Pass
    `force_refresh=True` to bypass the cache (e.g. on 401 retries).
    """
    now = time.time()
    if (not force_refresh and _TOKEN_CACHE["value"]
            and _TOKEN_CACHE["expires_at"] - now > 60):
        return _TOKEN_CACHE["value"]

    with _TOKEN_LOCK:
        # Re-check inside lock — another thread may have refreshed
        if (not force_refresh and _TOKEN_CACHE["value"]
                and _TOKEN_CACHE["expires_at"] - time.time() > 60):
            return _TOKEN_CACHE["value"]

        cfg = _config()
        try:
            import msal
        except ImportError as e:
            raise OneDriveError(
                "msal package not installed. Add `msal` to requirements.txt."
            ) from e

        app = msal.ConfidentialClientApplication(
            client_id=cfg["AZURE_CLIENT_ID"],
            authority=f"https://login.microsoftonline.com/{cfg['AZURE_TENANT_ID']}",
            client_credential=cfg["AZURE_CLIENT_SECRET"],
        )
        # Application permissions use the .default scope per Microsoft docs
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise OneDriveError(
                f"Token acquisition failed: {result.get('error')} — "
                f"{result.get('error_description', 'no description')}"
            )
        _TOKEN_CACHE["value"] = result["access_token"]
        _TOKEN_CACHE["expires_at"] = time.time() + int(result.get("expires_in", 3600))
        return _TOKEN_CACHE["value"]


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_graph_token()}"}


def _check(resp: requests.Response, op: str) -> requests.Response:
    """Raise OneDriveError on non-2xx with body context for debugging."""
    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else "(empty body)"
        raise OneDriveError(
            f"{op} failed: HTTP {resp.status_code} — {body}"
        )
    return resp


def download_file_by_path(path: str, *, user_id: str | None = None) -> bytes:
    """Download a file from OneDrive by its drive-relative path.

    `path` is the path WITHIN the user's OneDrive root, with NO leading
    slash, e.g. 'Kitchen Operations/Wentworth Budget 2026.xlsx'.
    """
    cfg = _config()
    uid = user_id or cfg["AZURE_USER_ID"]
    safe_path = path.lstrip("/")
    url = f"{_drive_root_for_user(uid)}/root:/{safe_path}:/content"
    resp = _check(requests.get(url, headers=_auth_headers(), timeout=60),
                  f"download_file_by_path({path!r})")
    return resp.content


def download_file_by_id(item_id: str, *, user_id: str | None = None) -> bytes:
    """Download a file by its drive item ID (from list_folder or Graph search)."""
    cfg = _config()
    uid = user_id or cfg["AZURE_USER_ID"]
    url = f"{_drive_root_for_user(uid)}/items/{item_id}/content"
    resp = _check(requests.get(url, headers=_auth_headers(), timeout=60),
                  f"download_file_by_id({item_id!r})")
    return resp.content


def upload_to_folder(folder_id: str, filename: str, content: bytes,
                     *, user_id: str | None = None) -> dict[str, Any]:
    """PUT `content` as `filename` into a OneDrive folder.

    Replaces the file if it already exists (Graph default is conflict='replace').
    Returns the new/updated drive item metadata as a dict.

    Uses the simple upload endpoint (works for files <4MB). For larger
    files, switch to upload sessions per Microsoft docs.
    """
    if len(content) > 4 * 1024 * 1024:
        raise OneDriveError(
            f"upload_to_folder: file size {len(content)} bytes exceeds 4MB simple-upload limit. "
            "Use upload sessions for larger files."
        )
    cfg = _config()
    uid = user_id or cfg["AZURE_USER_ID"]
    safe_name = filename.replace("/", "_")
    url = (f"{_drive_root_for_user(uid)}/items/{folder_id}:/{safe_name}:/content")
    headers = _auth_headers()
    headers["Content-Type"] = "application/octet-stream"
    resp = _check(
        requests.put(url, headers=headers, data=content, timeout=120),
        f"upload_to_folder({folder_id!r}, {filename!r})",
    )
    return resp.json()


def list_folder(folder_id: str, *, user_id: str | None = None) -> list[dict[str, Any]]:
    """List immediate children of a folder. Returns list of drive-item dicts.

    Useful for discovering folder IDs of subfolders, or finding a file's
    ID before downloading. Each item dict has keys including 'id', 'name',
    'folder' (if it's a folder), 'file' (if it's a file), 'size', etc.
    """
    cfg = _config()
    uid = user_id or cfg["AZURE_USER_ID"]
    url = f"{_drive_root_for_user(uid)}/items/{folder_id}/children"
    resp = _check(requests.get(url, headers=_auth_headers(), timeout=60),
                  f"list_folder({folder_id!r})")
    return resp.json().get("value", [])


def _reset_cache_for_tests() -> None:
    """Test helper — clears the token cache between test runs."""
    _TOKEN_CACHE["value"] = None
    _TOKEN_CACHE["expires_at"] = 0.0
