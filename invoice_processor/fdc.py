"""
USDA FoodData Central (FDC) API client.

Source of the per-100g macro profile we store on Product (kcal / protein / fat
/ carb / fiber) for the provided-nutrition-from-orders pipeline. FDC reports
nutrients per 100 g of edible portion — exactly our storage basis.

Two endpoints:
  - search_food(query)  -> candidate foods (fdcId + description + dataType),
                           feeds the fuzzy matcher / review queue (step 3).
  - get_food(fdc_id)    -> full nutrient detail for a confirmed match.
  - extract_macros(detail) -> the {kcal,protein,fat,carb,fiber}_per_100g dict.

Energy is the fussy nutrient. SR Legacy foods carry number 208 ("Energy", kcal);
Foundation foods often carry only Atwater variants (957 general / 958 specific)
and occasionally only kilojoules (268). We resolve in that priority and convert
kJ->kcal as a last resort. Fiber (291) is frequently absent (e.g. raw meat) and
is returned as None — never silently 0.

Responses are cached under .fdc_cache/ (mirrors .ocr_cache) so re-runs and the
review loop never re-hit the rate-limited API. Set FDC_API_KEY in .env.
"""
import os
import re
import json
import hashlib
from pathlib import Path

import requests

from .config import FDC_API_KEY, _PROJECT_ROOT

_BASE = "https://api.nal.usda.gov/fdc/v1"
_CACHE_DIR = Path(_PROJECT_ROOT) / ".fdc_cache"
_TIMEOUT = 25
# Foundation + SR Legacy are the whole-food, lab-analyzed datasets we want;
# Branded/Survey are noisier and added only if a whole-food match isn't found.
_DEFAULT_DATA_TYPES = ("Foundation", "SR Legacy")

# Nutrient numbers (stable across the API; names/units vary, numbers don't).
_N_PROTEIN = "203"
_N_FAT     = "204"
_N_CARB    = "205"
_N_FIBER   = "291"
_N_ENERGY_KCAL          = "208"   # SR Legacy
_N_ENERGY_ATWATER_GEN   = "957"   # Foundation, general factors
_N_ENERGY_ATWATER_SPEC  = "958"   # Foundation, specific factors
_N_ENERGY_KJ            = "268"   # kilojoules — convert as last resort


class FDCError(RuntimeError):
    pass


def _require_key():
    if not FDC_API_KEY:
        raise FDCError("FDC_API_KEY is not set in .env")


def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.md5(f"{kind}:{key}".encode()).hexdigest()
    return _CACHE_DIR / f"{kind}_{h}.json"


def _cached_get(kind: str, key: str, url: str, params: dict) -> dict:
    """GET with a JSON file cache. Cache hit short-circuits the network."""
    path = _cache_path(kind, key)
    if path.exists():
        return json.loads(path.read_text())
    _require_key()
    resp = requests.get(url, params={**params, "api_key": FDC_API_KEY}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise FDCError(f"FDC {kind} {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    _CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def _clean_query(q: str) -> str:
    """FDC's gateway 400s on some characters (e.g. an unencoded '/').
    Keep a safe set (letters, digits, space, comma, period, hyphen)."""
    q = re.sub(r"[^A-Za-z0-9 ,.\-]", " ", q or "")
    return re.sub(r"\s+", " ", q).strip()


def search_food(query: str, page_size: int = 5, data_types=_DEFAULT_DATA_TYPES) -> list:
    """Return candidate foods for a query: [{fdc_id, description, data_type}]."""
    q = _clean_query(query)
    data = _cached_get(
        "search", f"{q}|{','.join(data_types)}|{page_size}",
        f"{_BASE}/foods/search",
        {"query": q, "pageSize": page_size, "dataType": ",".join(data_types)},
    )
    return [
        {"fdc_id": f.get("fdcId"), "description": f.get("description"), "data_type": f.get("dataType")}
        for f in data.get("foods", [])
    ]


def get_food(fdc_id) -> dict:
    """Full FDC detail record for one food, cached by id."""
    return _cached_get("food", str(fdc_id), f"{_BASE}/food/{fdc_id}", {})


def _amounts_by_number(detail: dict) -> dict:
    """Map nutrient-number -> amount from a detail record (first wins)."""
    out = {}
    for fn in detail.get("foodNutrients", []):
        num = (fn.get("nutrient") or {}).get("number")
        amt = fn.get("amount")
        if num is not None and amt is not None and num not in out:
            out[num] = amt
    return out


def extract_macros(detail: dict) -> dict:
    """Per-100g macros from an FDC detail record. Missing values -> None."""
    a = _amounts_by_number(detail)

    def first(*nums):
        for n in nums:
            if a.get(n) is not None:
                return a[n]
        return None

    kcal = first(_N_ENERGY_KCAL, _N_ENERGY_ATWATER_GEN, _N_ENERGY_ATWATER_SPEC)
    if kcal is None and a.get(_N_ENERGY_KJ) is not None:
        kcal = round(a[_N_ENERGY_KJ] / 4.184, 2)

    return {
        "kcal_per_100g":      kcal,
        "protein_g_per_100g": a.get(_N_PROTEIN),
        "fat_g_per_100g":     a.get(_N_FAT),
        "carb_g_per_100g":    a.get(_N_CARB),
        "fiber_g_per_100g":   a.get(_N_FIBER),
    }
