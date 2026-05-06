"""Rank-pair v2 extraction.

Vendor-agnostic invoice line extractor that survives sub-degree photo tilt.
Each row is anchored on a triplet of column tokens (qty, unit_price, ext) at
known x-bands. Rows are identified by RANK (sort by y, pair index N to index N)
rather than by absolute y-cluster — so tilt-induced y-shifts between columns
preserve row identity instead of binding to the wrong row.

See `project_spatial_drift_finding.md` for the empirical validation behind this
algorithm:
  - 86% ACH-math pass rate across 48 cached Farm Art invoices
  - 14% drift-cascade rate on prior y-cluster extraction confirmed
  - All 15 ILIs on Sean photo-verified 3/6 invoice were cascade-drifted; rank-pair
    v2 produces the correct truth

Public entry: extract_farmart_rank(pages) -> list[dict] with the same item shape
as spatial_matcher.match_farmart_spatial. Other vendors come later.
"""
from __future__ import annotations
import re
from statistics import median


_QTY_RE = re.compile(r"^\d+\.\d{3}$")
_PRICE_RE = re.compile(r"^\$?\d+\.\d{2,4}\*?$")

# Description-y interpolation tolerance. Tightened from 0.014 (initial) to 0.008
# after observing extraction-stage mash-ups on tightly-spaced rows. Rows whose
# description tokens span >1.5x this tolerance get an ambiguity flag.
_DESC_Y_TOL = 0.008
_AMBIGUITY_RATIO = 1.5

# Ext-pick tolerance: ext token must lie within this y-distance of the unit-price
# token to pair. Catches the savings/discount column without false-pairing
# because the savings column's $0.00 tokens are usually on a different y sub-line.
_EXT_Y_TOL = 0.010


def _y_mid(t: dict) -> float:
    return (t["y_min"] + t["y_max"]) / 2


def _x_mid(t: dict) -> float:
    return (t["x_min"] + t["x_max"]) / 2


def _flatten_tokens(pages: list[dict]) -> list[dict]:
    out = []
    for p in pages or []:
        out.extend(p.get("tokens") or [])
    return out


def detect_layout_farmart(tokens: list[dict]) -> dict | None:
    """Auto-detect column x-bands from token x-distribution.

    Farm Art has at least two invoice templates: prices land at x≈0.74/0.85 in
    one layout and x≈0.86/0.95 in another. Detection picks the layout from data
    rather than hard-coding per-vendor bands.

    Returns:
        Config dict with x-bands for qty, unit, ext, desc — or None if the
        token distribution doesn't have enough signal (e.g., fragmentary cache).
    """
    qtys = sorted(_x_mid(t) for t in tokens
                  if _QTY_RE.fullmatch(t.get("text") or ""))
    prices = sorted(_x_mid(t) for t in tokens
                    if _PRICE_RE.fullmatch(t.get("text") or ""))
    if len(qtys) < 3 or len(prices) < 4:
        return None
    qty_ord = qtys[0]
    right = sorted(p for p in prices if p > 0.6)
    if len(right) < 2:
        return None
    ext_max = right[-1]
    return {
        "qty_x":   (max(0.0, qty_ord - 0.025), qty_ord + 0.025),
        "unit_x":  (ext_max - 0.13, ext_max - 0.07),
        "ext_x":   (ext_max - 0.04, ext_max + 0.04),
        "desc_x":  (qty_ord + 0.10, ext_max - 0.15),
    }


def _ach_ok(qty: float, unit: float, ext: float | None,
            ach_pct: float = 0.01, tol: float = 0.05) -> bool:
    """Predicate: does qty * unit * (1-ach) ≈ ext within tol?

    ACH 1% discount is Farm Art's wholesale-payment-method adjustment. Rows that
    pass this check are internally consistent. Rows that fail despite being
    extracted indicate either (a) a non-ACH line, (b) extraction error, or
    (c) genuine math anomaly worth flagging.
    """
    if ext is None:
        return False
    expected = qty * unit * (1.0 - ach_pct)
    return abs(expected - ext) < tol or (ext == 0 and qty * unit < 5)


def extract_farmart_rank(pages: list[dict]) -> list[dict]:
    """Rank-pair v2 extraction for Farm Art invoices.

    Output shape matches spatial_matcher.match_farmart_spatial:
        {
            "qty": float,
            "purchase_uom": str | None,
            "unit_price": float,
            "extended_amount": float | None,
            "raw_description": str,
            "section_hint": str | None,
            "ambiguous": bool,  # description tokens span > expected row height
        }

    Empty list when layout can't be detected (caller should fall back to the
    legacy y-cluster matcher in those cases — typically thin OCR caches).
    """
    tokens = _flatten_tokens(pages)
    cfg = detect_layout_farmart(tokens)
    if cfg is None:
        return []

    qtys = sorted(
        [t for t in tokens
         if cfg["qty_x"][0] <= _x_mid(t) <= cfg["qty_x"][1]
         and _QTY_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    units = sorted(
        [t for t in tokens
         if cfg["unit_x"][0] <= _x_mid(t) <= cfg["unit_x"][1]
         and _PRICE_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    ext_pool = [
        t for t in tokens
        if cfg["ext_x"][0] <= _x_mid(t) <= cfg["ext_x"][1]
        and _PRICE_RE.fullmatch(t.get("text") or "")
    ]

    n = min(len(qtys), len(units))
    if n == 0:
        return []
    pairs = list(zip(qtys[:n], units[:n]))

    rows: list[dict] = []
    for q, u in pairs:
        yU = _y_mid(u)

        # Pick ext token closest in y to unit, within tolerance — robust against
        # the savings/discount column producing $0.00 tokens on a parallel sub-line.
        ext = None
        if ext_pool:
            cand = min(ext_pool, key=lambda t: abs(_y_mid(t) - yU))
            if abs(_y_mid(cand) - yU) <= _EXT_Y_TOL:
                ext = cand

        # Description tokens via y-line interpolation between qty (left) and unit
        # (right). Tilt is encoded in the (yL, yU) endpoints so a row's diagonal
        # is the natural baseline for its description tokens.
        yL, xL = _y_mid(q), _x_mid(q)
        xR = _x_mid(u)
        dx = xR - xL if xR != xL else 1.0
        desc_toks = []
        desc_ys = []
        for t in tokens:
            x = _x_mid(t)
            if not (cfg["desc_x"][0] <= x <= cfg["desc_x"][1]):
                continue
            text = t.get("text") or ""
            if _PRICE_RE.fullmatch(text) or _QTY_RE.fullmatch(text):
                continue
            interp_y = yL + (yU - yL) * (x - xL) / dx
            dy = abs(_y_mid(t) - interp_y)
            if dy < _DESC_Y_TOL:
                desc_toks.append(t)
                desc_ys.append(_y_mid(t) - interp_y)

        # Ambiguity flag: if description tokens are spread wider than expected,
        # the row may have absorbed neighboring-row content. Caller can route
        # ambiguous rows to mapping-review instead of trusting the description.
        ambiguous = False
        if desc_ys:
            spread = max(desc_ys) - min(desc_ys)
            if spread > _DESC_Y_TOL * _AMBIGUITY_RATIO:
                ambiguous = True

        desc = " ".join(t["text"] for t in sorted(desc_toks, key=_x_mid))

        try:
            qty_f = float(q["text"])
            unit_f = float(u["text"].lstrip("$").rstrip("*"))
        except (ValueError, KeyError):
            continue
        ext_f = None
        if ext is not None:
            try:
                ext_f = float(ext["text"].lstrip("$").rstrip("*"))
            except ValueError:
                ext_f = None

        rows.append({
            "qty": qty_f,
            "purchase_uom": None,  # Farm Art doesn't expose a U/M column reliably
            "unit_price": unit_f,
            "extended_amount": ext_f,
            "raw_description": desc,
            "section_hint": None,  # section detection is downstream of extraction
            "ambiguous": ambiguous,
        })

    return rows


def diagnostic_summary(rows: list[dict]) -> dict:
    """Produce per-extraction summary stats for shadow-mode comparison.

    Returns:
        {
            "row_count": int,
            "ach_pass": int,
            "ach_fail": int,
            "ach_no_ext": int,
            "ambiguous": int,
            "median_tilt": float | None,  # placeholder — caller computes
        }
    """
    summary = {
        "row_count": len(rows),
        "ach_pass": 0,
        "ach_fail": 0,
        "ach_no_ext": 0,
        "ambiguous": 0,
    }
    for r in rows:
        if r.get("ambiguous"):
            summary["ambiguous"] += 1
        ext = r.get("extended_amount")
        if ext is None:
            summary["ach_no_ext"] += 1
        elif _ach_ok(r["qty"], r["unit_price"], ext):
            summary["ach_pass"] += 1
        else:
            summary["ach_fail"] += 1
    return summary
