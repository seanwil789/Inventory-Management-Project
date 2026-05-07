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

    Vendor-agnostic — passes when EITHER:
      - qty * unit * (1 - ach_pct) ≈ ext (Farm Art ACH discount path)
      - qty * unit ≈ ext (Sysco / no discount applied at line level)

    ACH 1% discount is Farm Art's wholesale-payment-method adjustment. Sysco
    line totals don't carry that adjustment. Either form means the line math
    is internally consistent.
    """
    if ext is None:
        return False
    expected_ach = qty * unit * (1.0 - ach_pct)
    expected_no_ach = qty * unit
    return (abs(expected_ach - ext) < tol
            or abs(expected_no_ach - ext) < tol
            or (ext == 0 and qty * unit < 5))


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

    Multi-page handling: each page has independent y∈[0,1]. Flattening pages
    cross-contaminates descriptions across cache boundaries (a row on page 1
    at y=0.4 picks up description tokens that belong to page 2's row at
    y=0.4). Per-page extraction prevents this. Surfaced 2026-05-07 during
    Farm Art 2026-05-05 merged validation — rows 3, 4, 5 had mashed
    descriptions until per-page handling was added.
    """
    all_rows: list[dict] = []
    for page in pages or []:
        page_tokens = page.get("tokens") or []
        if not page_tokens:
            continue
        all_rows.extend(_extract_farmart_rank_one_page(page_tokens))
    return all_rows


def _extract_farmart_rank_one_page(tokens: list[dict]) -> list[dict]:
    """Single-page rank-pair extraction. Caller (extract_farmart_rank)
    iterates pages and concatenates results."""
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


# ---------------------------------------------------------------------------
# Sysco rank-pair extraction
# ---------------------------------------------------------------------------
#
# Sysco invoices anchor each line on a 7-digit SUPC code at x≈0.40-0.68.
# Description tokens sit LEFT of the SUPC at the same row y; price tokens
# sit RIGHT. Catch-weight rows have a 3-decimal per-lb price right of the
# SUPC.
#
# Drift cascade observed 2026-05-07: y-cluster row binding (legacy spatial
# matcher) under photo tilt cross-bound descriptions and prices between
# adjacent rows. audit_spatial_drift_suspects exposed 8+ confirmed swap
# pairs (CHOBANI YOGURT desc with EGG SHELL price, etc).
#
# Rank-pair fix: rank SUPCs by y; rank right-of-SUPC unit-price tokens by y;
# pair them by RANK (rank N to rank N) instead of y-tolerance window. Tilt-
# induced y-shifts no longer cross-bind. Description tokens belong to the
# rank-N row when their y is closest to SUPC_N's interpolated y.

# Sysco SUPC anchor regex
_SUPC_RE = re.compile(r'^\d{7}$')

# Sysco price tokens: dollars-and-cents OR three-decimal (catch-weight per-lb)
_SYSCO_PRICE_RE = re.compile(r'^\$?\d+\.\d{2,3}\*?$')

# Description token band — LEFT of SUPC, but right of the qty/marker column.
_SYSCO_DESC_X_MIN = 0.06
_SYSCO_QTY_DROP_X_MAX = 0.17  # tokens at x<0.17 are qty/marker — drop standalones

# y-tolerance for "this token belongs to row K" (relative to interpolated y).
# Looser than Farm Art's 0.008 because Sysco descriptions are wider, so a
# given token can sit further from the row's anchor y under tilt without
# being a different row.
_SYSCO_DESC_Y_TOL = 0.012


def detect_layout_sysco(tokens: list[dict]) -> dict | None:
    """Detect Sysco column layout from token x-distribution.

    Returns config with x-bands for SUPC anchor + price column, or None
    when token distribution doesn't have enough signal (thin OCR cache,
    header-only page, etc.).

    Initial SUPC filter [0.40, 0.78] is intentionally wider than the
    typical x≈0.57 SUPC band — captures variant invoices where the
    column landed further right (e.g. cache 3b25a37a61d531 2026-03-30:
    SUPCs at x=0.69 instead of 0.57). Excludes invoice-number tokens
    at x≈0.84 in the header.
    """
    supc_xs = sorted(
        _x_mid(t) for t in tokens
        if _SUPC_RE.fullmatch(t.get("text") or "")
        and 0.40 <= _x_mid(t) <= 0.78
    )
    if len(supc_xs) < 3:
        return None
    # Robust median of SUPC x positions (resists outlier header tokens)
    supc_x = median(supc_xs)
    return {
        "supc_x":  (supc_x - 0.04, supc_x + 0.04),
        "price_x": (supc_x + 0.02, 1.0),  # right of SUPC
        "desc_x":  (_SYSCO_DESC_X_MIN, supc_x - 0.01),
    }


def extract_sysco_rank(pages: list[dict]) -> list[dict]:
    """Rank-pair v2 extraction for Sysco invoices.

    Output shape matches spatial_matcher.match_sysco_spatial — same fields
    + same units — so downstream pipeline doesn't differentiate.

    Multi-page handling: each page has independent y∈[0,1], so flattening
    across pages produces false "two SUPCs at same y" collisions that break
    competitive-y assignment. We extract per-page and concatenate.
    """
    all_rows: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        all_rows.extend(_extract_sysco_rank_one_page(tokens))
    return all_rows


def _extract_sysco_rank_one_page(tokens: list[dict]) -> list[dict]:
    """Single-page rank-pair extraction. Caller (extract_sysco_rank) iterates
    pages and concatenates results."""
    cfg = detect_layout_sysco(tokens)
    if cfg is None:
        return []

    # Rank SUPCs by y
    supcs = sorted(
        [t for t in tokens
         if cfg["supc_x"][0] <= _x_mid(t) <= cfg["supc_x"][1]
         and _SUPC_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    if not supcs:
        return []

    # Build description band tokens (excluding price/SUPC/qty noise)
    desc_pool = []
    for t in tokens:
        x = _x_mid(t)
        if not (cfg["desc_x"][0] <= x <= cfg["desc_x"][1]):
            continue
        text = t.get("text") or ""
        if _SUPC_RE.fullmatch(text) or _SYSCO_PRICE_RE.fullmatch(text):
            continue
        # Drop standalone qty/marker tokens in the qty column
        if x < _SYSCO_QTY_DROP_X_MAX:
            if text in ("D", "S", "A", "CS", "EA", "LB", "T/WT=", "T/WT"):
                continue
            if re.fullmatch(r'\d{1,2}', text):
                continue
        desc_pool.append(t)

    # Build price-token pool (right of SUPC band)
    price_pool = [
        t for t in tokens
        if cfg["price_x"][0] <= _x_mid(t)
        and _SYSCO_PRICE_RE.fullmatch(t.get("text") or "")
    ]

    # Pool of digit-only tokens in the qty column (x < 0.17). Used to find
    # qty per row by competitive-y (same rank-pair principle as descriptions).
    # Sysco prints "1 CS" / "2 CS" / "3 EA" — we want the leading integer.
    qty_pool = [
        t for t in tokens
        if _x_mid(t) < _SYSCO_QTY_DROP_X_MAX
        and re.fullmatch(r"\d{1,2}", t.get("text") or "")
    ]

    rows: list[dict] = []
    for k, supc in enumerate(supcs):
        y_supc = _y_mid(supc)

        # Find the unit_price for this row: closest 2-decimal price to y_supc,
        # right of SUPC, that's also closer to THIS rank than to neighbor ranks.
        candidates_2dec = [
            t for t in price_pool
            if "." in t["text"]
            and len(t["text"].rstrip("*").lstrip("$").split(".")[1]) == 2
            and _x_mid(t) > _x_mid(supc)
        ]
        # Choose by smallest |y - y_supc|; require it's strictly closest to
        # THIS supc rank vs any other supc to prevent cross-rank drift.
        unit_t = None
        best_dy = float("inf")
        for t in candidates_2dec:
            dy_self = abs(_y_mid(t) - y_supc)
            # Check competitors — distance from this token to other SUPCs
            min_other = min(
                (abs(_y_mid(t) - _y_mid(other_supc))
                 for j, other_supc in enumerate(supcs) if j != k),
                default=float("inf"),
            )
            if dy_self < min_other and dy_self < best_dy:
                best_dy = dy_self
                unit_t = t

        if unit_t is None:
            continue
        try:
            unit_f = float(unit_t["text"].lstrip("$").rstrip("*"))
        except ValueError:
            continue

        # Per-lb (catch-weight) — 3-decimal token closest to y_supc, right of SUPC
        candidates_3dec = [
            t for t in price_pool
            if "." in t["text"]
            and len(t["text"].rstrip("*").lstrip("$").split(".")[1]) == 3
            and _x_mid(t) > _x_mid(supc)
        ]
        per_lb_f = None
        for t in candidates_3dec:
            if abs(_y_mid(t) - y_supc) < _SYSCO_DESC_Y_TOL * 1.5:
                try:
                    per_lb_f = float(t["text"].lstrip("$").rstrip("*"))
                    break
                except ValueError:
                    pass

        # NEW: Extract qty from the left column. Sysco prints "1 CS" / "2 CS"
        # at x<0.17. Pick the digit token closest in y to THIS supc (same
        # rank-pair competitive-y rule). Default 1 when not found.
        # Surfaced 2026-05-07 by Sean: PURLIFE WATER row had qty=2 on paper
        # but extraction hardcoded qty=1 (extended_amount $8.99 vs paper $17.98).
        qty_int = 1
        for t in qty_pool:
            dy_self = abs(_y_mid(t) - y_supc)
            min_other = min(
                (abs(_y_mid(t) - _y_mid(other_supc))
                 for j, other_supc in enumerate(supcs) if j != k),
                default=float("inf"),
            )
            if dy_self < min_other and dy_self < _SYSCO_DESC_Y_TOL:
                try:
                    qty_int = int(t["text"])
                    break
                except ValueError:
                    pass

        # NEW: Find extended_amount. When qty>1, ext is a separate 2-decimal
        # token RIGHT of unit_price (Sysco's "EXTENDED" column). Validated
        # by qty × unit_price ≈ ext within 5% / $2 tolerance.
        ext_f = unit_f * qty_int  # default fallback (compute from qty × unit)
        if qty_int > 1:
            best_ext_dy = float("inf")
            for t in candidates_2dec:
                if _x_mid(t) <= _x_mid(unit_t) + 0.04:
                    continue  # must be RIGHT of unit_price
                dy_self = abs(_y_mid(t) - y_supc)
                min_other = min(
                    (abs(_y_mid(t) - _y_mid(other_supc))
                     for j, other_supc in enumerate(supcs) if j != k),
                    default=float("inf"),
                )
                if dy_self >= min_other or dy_self >= _SYSCO_DESC_Y_TOL:
                    continue
                try:
                    cand_ext = float(t["text"].lstrip("$").rstrip("*"))
                except ValueError:
                    continue
                # Validate: matches qty × unit_price within tolerance
                expected = qty_int * unit_f
                if expected > 0:
                    diff_pct = abs(cand_ext - expected) / expected
                    if diff_pct < 0.05 or abs(cand_ext - expected) < 2.0:
                        if dy_self < best_ext_dy:
                            best_ext_dy = dy_self
                            ext_f = cand_ext

        # Description tokens for this row: left-of-SUPC tokens whose y is
        # closer to THIS supc's y than to any other supc's y.
        desc_toks = []
        for t in desc_pool:
            y_t = _y_mid(t)
            # Distance to this supc rank
            dy_self = abs(y_t - y_supc)
            # Distance to nearest other supc rank
            min_other = min(
                (abs(y_t - _y_mid(other_supc))
                 for j, other_supc in enumerate(supcs) if j != k),
                default=float("inf"),
            )
            if dy_self <= min_other and dy_self < _SYSCO_DESC_Y_TOL:
                desc_toks.append(t)

        desc = " ".join(t["text"] for t in sorted(desc_toks, key=_x_mid))

        # Ambiguity flag — wide y spread on description tokens
        ambiguous = False
        if desc_toks:
            ys = [_y_mid(t) for t in desc_toks]
            spread = max(ys) - min(ys)
            if spread > _SYSCO_DESC_Y_TOL * _AMBIGUITY_RATIO:
                ambiguous = True

        item = {
            "raw_description":  desc or f"[Sysco #{supc['text']}]",
            "sysco_item_code":  supc["text"],
            "unit_price":       unit_f,
            "extended_amount":  ext_f,
            "case_size_raw":    "",
            "section":          "",      # caller assigns from section headers
            "quantity":         qty_int,
            "unit_of_measure":  "CASE",
            "ambiguous":        ambiguous,
        }
        if per_lb_f is not None:
            item["unit_of_measure"] = "LB"
            item["price_per_unit"] = per_lb_f

        rows.append(item)

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
        # Farm Art rows use 'qty'; Sysco rows use 'quantity'
        qty = r.get("qty") if "qty" in r else r.get("quantity")
        if ext is None:
            summary["ach_no_ext"] += 1
        elif _ach_ok(qty or 0, r["unit_price"], ext):
            summary["ach_pass"] += 1
        else:
            summary["ach_fail"] += 1
    return summary
