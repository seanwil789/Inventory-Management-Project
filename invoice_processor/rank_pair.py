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

from line_math import validate_line_math


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
    # 2026-05-07 fix (Farm Art INV 1631546): the ext column is right-aligned,
    # so short-dollar tokens like `9.50` and `15.84` have x_mid further LEFT
    # than long-dollar tokens like `189.87` (which sets ext_max). Widen the
    # left side of ext_x from 0.04 to 0.06 so short-dollar items aren't
    # silently dropped from extraction. Right side unchanged.
    return {
        "qty_x":   (max(0.0, qty_ord - 0.025), qty_ord + 0.025),
        "unit_x":  (ext_max - 0.13, ext_max - 0.07),
        "ext_x":   (ext_max - 0.06, ext_max + 0.04),
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

        row = {
            "qty": qty_f,
            "purchase_uom": None,  # Farm Art doesn't expose a U/M column reliably
            "unit_price": unit_f,
            "extended_amount": ext_f,
            "raw_description": desc,
            "section_hint": None,  # section detection is downstream of extraction
            "ambiguous": ambiguous,
        }
        # Catch-weight aware math validation. Self-correct enabled because
        # Farm Art rank-pair occasionally pulls qty=2 when actual qty=1
        # (ext rounds clean to ext/unit). Sets math_flagged on real anomalies.
        validate_line_math(row, vendor='Farm Art', try_self_correct=True)
        rows.append(row)

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
    # B-SingleSupcLayout fix (2026-05-12): accept >=1 SUPC + cluster by
    # x-proximity to handle pages with mixed product+header SUPCs.
    #
    # Sysco LAST PAGE / totals pages naturally carry 1-2 product SUPCs
    # (trailing items in BEVERAGE/MISC sections) + 1-2 header SUPCs
    # (doc numbers at y≈0.15 that happen to fall in the x-band).
    # Pre-fix #1 (threshold >=3): pages dropped wholesale.
    # Pre-fix #2 (naive median of all SUPCs): with 2 SUPCs at distant x
    # positions, median lands BETWEEN them, excluding both from the
    # ±0.04 band.
    #
    # Fix: cluster SUPCs by x-proximity (0.04 = typical column width),
    # pick densest cluster as the product band. Ties broken leftmost
    # (header noise drifts right of product columns at x≈0.57-0.69).
    # Reference: INV 775404605 cache D — clusters=[[0.567],[0.699]] tied,
    # leftmost wins → supc_x=0.567 → band captures Coffee Beans.
    if not supc_xs:
        return None
    clusters: list[list[float]] = []
    for x in supc_xs:
        placed = False
        for cluster in clusters:
            # Within 0.04 of any member → same cluster
            if any(abs(x - cx) < 0.04 for cx in cluster):
                cluster.append(x)
                placed = True
                break
        if not placed:
            clusters.append([x])
    # Densest cluster wins; tie → leftmost cluster
    clusters.sort(key=lambda c: (-len(c), c[0]))
    supc_x = median(clusters[0])
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

    Section carry across pages (B2b fix 2026-05-07): when page N starts
    mid-section (its first SUPC is above all section headers detected on
    page N), items at the top inherit the section from the END of page N-1.
    Without this, those items get section="" and break section-level
    reconciliation.

    Caller responsibility: pass `pages` ordered by physical page sequence
    WITHIN a single invoice. Cross-invoice interleaving will produce wrong
    section carry. validate_all_invoices + reprocess_ocr_cache group caches
    by (vendor, invoice_number) and order by `section_validator.
    cache_page_order_key` before passing here (Sean 2026-05-11). An earlier
    cache_sha reset was added to mitigate filesystem-order interleaving
    that no longer occurs at this layer — removing it unblocks correct
    carry on multi-photo invoices (INV 775292014/775451714/775238251).
    """
    all_rows: list[dict] = []
    carry_section: str = ""
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        rows, last_section = _extract_sysco_rank_one_page(
            tokens, carry_section=carry_section)
        all_rows.extend(rows)
        # Carry the LAST section detected on this page (or the carry-in
        # if this page had no headers at all) forward to the next page.
        if last_section:
            carry_section = last_section
    return all_rows


def _extract_sysco_rank_one_page(
    tokens: list[dict],
    carry_section: str = "",
) -> tuple[list[dict], str]:
    """Single-page rank-pair extraction. Caller (extract_sysco_rank) iterates
    pages and concatenates results.

    Args:
        tokens: page tokens with bounding boxes
        carry_section: section name carried over from previous page's bottom.
            Used as the section for items above the first detected section
            header on this page (B2b cross-page continuity fix).

    Returns:
        (rows, last_section): extracted item rows + the last section header
        detected on this page (or carry_section if no headers detected).
    """
    cfg = detect_layout_sysco(tokens)
    if cfg is None:
        return [], carry_section

    # Section detection — reuse spatial_matcher's row-clustering + section
    # finder so each extracted item carries its **** SECTION **** tag. Without
    # this, downstream section-level reconciliation (parser items grouped by
    # section vs printed GROUP TOTAL) has no signal. Failure is non-fatal:
    # if the import fails or section detection yields nothing, items get
    # section="" and reconciliation falls back to invoice-level only.
    sections: list[tuple[float, str]] = []
    try:
        from spatial_matcher import _group_rows, _find_sections
        sections = _find_sections(_group_rows(tokens))
    except Exception:
        pass

    # Rank SUPCs by y
    supcs = sorted(
        [t for t in tokens
         if cfg["supc_x"][0] <= _x_mid(t) <= cfg["supc_x"][1]
         and _SUPC_RE.fullmatch(t.get("text") or "")],
        key=_y_mid,
    )
    if not supcs:
        # No SUPCs but sections may still exist on this page — carry forward
        # the last section if any.
        last_sec = carry_section
        if sections:
            try:
                from spatial_matcher import canonicalize_sysco_section
                sorted_secs = sorted(sections, key=lambda s: s[0])
                last_sec = canonicalize_sysco_section(sorted_secs[-1][1])
            except Exception:
                last_sec = sorted(sections, key=lambda s: s[0])[-1][1]
        return [], last_sec

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

    # B-GroupTotalLeak fix (Sean 2026-05-11): exclude price tokens on
    # GROUP TOTAL rows. Sysco's mid-page section totals print as
    # "GROUP TOTAL****  $<value>" on the right margin in the same x-band
    # as item exts. Without filtering, rank-pair pairs the bottom-most
    # SUPC with the GROUP TOTAL value — INV 775292014 page 2 had
    # LACROIX LMN SUPC 15021239 wrongly paired with $749.33 (CANNED & DRY
    # GROUP TOTAL), inflating items_sum by $730+ and making rank_pair
    # lose the picker to spatial. spatial filters footer-only SUB/TAX
    # TOTAL via `_find_footer_y`; GROUP TOTAL is mid-page, needs its
    # own filter here.
    #
    # Detection: a row is a GROUP TOTAL row if it contains both a
    # `GROUP` token and a `TOTAL` token at the same y (within tight
    # tolerance).
    _GT_Y_TOL = 0.005
    group_label_ys: list[float] = []
    for t in tokens:
        if (t.get("text") or "").upper() == "GROUP":
            yg = _y_mid(t)
            for t2 in tokens:
                if ((t2.get("text") or "").upper() == "TOTAL"
                        and abs(_y_mid(t2) - yg) < _GT_Y_TOL):
                    group_label_ys.append(yg)
                    break
    if group_label_ys:
        price_pool = [
            t for t in price_pool
            if not any(abs(_y_mid(t) - gy) < _GT_Y_TOL
                        for gy in group_label_ys)
        ]

    # Pool of digit-only tokens in the qty column (x < 0.17). Used to find
    # qty per row by competitive-y (same rank-pair principle as descriptions).
    # Sysco prints "1 CS" / "2 CS" / "3 EA" — we want the leading integer.
    qty_pool = [
        t for t in tokens
        if _x_mid(t) < _SYSCO_QTY_DROP_X_MAX
        and re.fullmatch(r"\d{1,2}", t.get("text") or "")
    ]

    # B-NEW (2026-05-07): REMOTE-STOCK marker pool. Sysco prints `REMOTE-STOCK`
    # immediately below items that were on the order but NOT delivered (the
    # vendor substituted, ran out of stock, etc.). The row's unit_price is
    # printed but the row's ext IS NOT — Sean confirmed: this is the same
    # class as Farm Art's `zz` items. Without this filter, the parser uses
    # the catalog unit_price as ext (because qty=1 default + no ext token in
    # right column = synthesize ext = unit × 1), inflating items_sum by the
    # catalog price of every undelivered row.
    #
    # Confirmed cases: INV 775619701 CAMBRO LID ($56.53 over), INV 775645370
    # (2 items), INV 775632629 (3 items), INV 775662001 (2 items).
    #
    # Each REMOTE-STOCK marker associates with the SINGLE closest SUPC above
    # it (the undelivered row's anchor) — not all SUPCs in a wide window.
    # First implementation used a wide y-band and accidentally skipped
    # ADJACENT delivered rows too (gloves at y_g + CAMBRO at y_c, marker at
    # y_rs > y_c, both SUPCs within wide band → both skipped wrongly).
    remote_stock_tokens = [
        t for t in tokens
        if (t.get("text") or "").upper() in ("REMOTE-STOCK", "REMOTE")
    ]
    remote_stock_supcs: set[str] = set()
    for rs in remote_stock_tokens:
        rs_y = _y_mid(rs)
        # Find SUPC closest ABOVE this marker, within reasonable distance.
        above = [s for s in supcs if _y_mid(s) < rs_y]
        if not above:
            continue
        closest = max(above, key=_y_mid)
        if (rs_y - _y_mid(closest)) < 0.04:
            remote_stock_supcs.add(closest["text"])

    rows: list[dict] = []
    for k, supc in enumerate(supcs):
        y_supc = _y_mid(supc)

        # Find the unit_price for this row: closest 2-decimal price to y_supc,
        # right of SUPC, that's also closer to THIS rank than to neighbor ranks.
        # Sort by x_mid (leftmost first) — Sysco prints unit_price LEFT of
        # tax/ext on the same row. Picking the leftmost equally-close-y
        # candidate gives the unit_price column rather than tax/ext.
        # B-NEW (2026-05-07): without this, INV 775726055 SUPC 4458646
        # picked TAX $3.72 as unit_price, then ext-derived qty $93.10/$3.72
        # = 25 (internally math-validated but semantically wrong).
        candidates_2dec = sorted(
            [t for t in price_pool
             if "." in t["text"]
             and len(t["text"].rstrip("*").lstrip("$").split(".")[1]) == 2
             and _x_mid(t) > _x_mid(supc)],
            key=lambda t: _x_mid(t),
        )
        # Choose by smallest |y - y_supc|; require it's strictly closest to
        # THIS supc rank vs any other supc to prevent cross-rank drift.
        # Tie-break: when multiple candidates land on the same OCR row
        # (dy within 0.005 of best), prefer LEFTMOST. The Sysco template
        # prints unit_price LEFT of tax/ext, so leftmost-on-the-row is the
        # unit_price column; rightward tokens are tax or ext.
        # B-NEW (2026-05-07): without strict same-row tie-break,
        # INV 775726055 SUPC 7136165 picked $139.90 (ext column,
        # dy=0.0000) over $69.95 (unit column, dy=0.0003) and inflated
        # ext from $139.90 to $279.80.
        unit_t = None
        best_dy = float("inf")
        SAME_ROW_DY = 0.005
        for t in candidates_2dec:  # already sorted leftmost-first
            dy_self = abs(_y_mid(t) - y_supc)
            # Check competitors — distance from this token to other SUPCs
            min_other = min(
                (abs(_y_mid(t) - _y_mid(other_supc))
                 for j, other_supc in enumerate(supcs) if j != k),
                default=float("inf"),
            )
            if dy_self >= min_other:
                continue
            if unit_t is None or dy_self + SAME_ROW_DY < best_dy:
                # Strictly better row OR first acceptable candidate.
                best_dy = dy_self
                unit_t = t
            # else: same row as current best; keep current (leftmost wins
            # because candidates_2dec is sorted leftmost-first).

        if unit_t is None:
            continue
        try:
            unit_f = float(unit_t["text"].lstrip("$").rstrip("*"))
        except ValueError:
            continue

        # B-NEW: skip REMOTE-STOCK rows. The remote_stock_supcs set was
        # pre-computed as: for each REMOTE-STOCK marker, the SINGLE SUPC
        # closest above it within 0.04 y-distance. Skipping the row drops
        # the catalog unit_price from items_sum — matches the actual
        # invoice (undelivered rows print unit but ext=0).
        if supc["text"] in remote_stock_supcs:
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

        # B1+B4 fix (2026-05-07): derive qty from the extended-amount column,
        # not the left-column qty token. Per `project_parser_accuracy_goal.md`
        # — the Sysco invoice template puts the qty/CS/pack-size tokens
        # ~0.011-0.015 BELOW the SUPC y; with `_SYSCO_DESC_Y_TOL=0.012` the
        # filter cuts off real qty tokens for 11 of 13 SUPCs on a typical
        # invoice. Result: silent qty=1 fallback. INV 775170714 lost $53 on
        # MANDARIN; INV 775619701 lost $116 across 4 DAIRY rows.
        #
        # New approach: ext / unit_price = qty (when both are extracted).
        # If that ratio rounds to a clean integer 1-50 within rounding
        # tolerance 0.05, accept it. This:
        #   - Is layout-independent (no y-tolerance dependency)
        #   - Math-validates by construction (the qty IS what makes math work)
        #   - Handles B4 (OCR token-merging like "1 5" → "15") because
        #     a wrong qty wouldn't divide cleanly into ext.
        #
        # Fall back to left-column qty extraction (with widened y-tolerance)
        # only when no right-column ext token is present — for rows where
        # ext = unit (qty=1) the result is identical.
        qty_int = 1
        ext_f = unit_f  # default: qty=1, ext=unit_price

        # Step 1 — find rightmost 2-decimal token to the right of unit_t.
        # That's the EXTENDED PRICE column (x≈0.78-0.85 on Sysco). Skip
        # tax tokens at x≈0.71 by requiring a 0.04 x-buffer past unit_t.
        ext_t = None
        for t in sorted(candidates_2dec, key=lambda x: -_x_mid(x)):
            if _x_mid(t) <= _x_mid(unit_t) + 0.04:
                continue
            dy_self = abs(_y_mid(t) - y_supc)
            min_other = min(
                (abs(_y_mid(t) - _y_mid(other_supc))
                 for j, other_supc in enumerate(supcs) if j != k),
                default=float("inf"),
            )
            # Slightly wider y-tol for ext (1.5x) — printer baseline can
            # differ between SUPC token and right-column ext token.
            if dy_self < min_other and dy_self < _SYSCO_DESC_Y_TOL * 1.5:
                ext_t = t
                break

        # Step 2 — derive qty from ext / unit when ext is found.
        if ext_t is not None and unit_f > 0:
            try:
                cand_ext = float(ext_t["text"].lstrip("$").rstrip("*"))
            except ValueError:
                cand_ext = None
            if cand_ext is not None and cand_ext > 0:
                derived = cand_ext / unit_f
                rounded = round(derived)
                # Accept when (a) rounds to a small integer (1-50), AND
                # (b) the rounding error is small (< 0.05). The latter
                # rejects coincidental near-divisions, e.g. tax-only rows
                # where ext = unit + tax wouldn't divide cleanly.
                if 1 <= rounded <= 50 and abs(derived - rounded) < 0.05:
                    qty_int = rounded
                    ext_f = cand_ext

        # Step 3 — fallback: left-column qty extraction. Only useful when
        # Step 2 didn't produce qty>1 (typical case: row has unit but no
        # distinct ext token — single-qty row where ext=unit). Wider
        # y-tolerance (0.018) here since the Sysco template structurally
        # offsets qty below SUPC.
        if qty_int == 1:
            unit_codes = {"CS", "EA", "DZ", "LB", "GA", "PK", "BG", "RL", "PT", "QT"}
            QTY_Y_TOL = 0.018
            unit_anchor = next(
                (t for t in tokens
                 if (t.get("text") or "").upper() in unit_codes
                 and _x_mid(t) < 0.20
                 and abs(_y_mid(t) - y_supc) < QTY_Y_TOL),
                None,
            )
            if unit_anchor is not None:
                anchor_x = _x_mid(unit_anchor)
                qty_candidates = []
                for t in qty_pool:
                    dy_self = abs(_y_mid(t) - y_supc)
                    min_other = min(
                        (abs(_y_mid(t) - _y_mid(other_supc))
                         for j, other_supc in enumerate(supcs) if j != k),
                        default=float("inf"),
                    )
                    if dy_self >= min_other or dy_self >= QTY_Y_TOL:
                        continue
                    t_x = _x_mid(t)
                    if t_x >= anchor_x or anchor_x - t_x > 0.04:
                        continue
                    try:
                        qty_candidates.append((t, int(t["text"])))
                    except ValueError:
                        pass
                if qty_candidates:
                    qty_candidates.sort(key=lambda c: anchor_x - _x_mid(c[0]))
                    candidate_qty = qty_candidates[0][1]
                    # B4 guard: validate against ext token before accepting
                    # a qty>1 from left-column extraction. If ext exists and
                    # candidate_qty × unit doesn't match it within 5%, the
                    # qty token is likely OCR-merged garbage (the "1 5"→"15"
                    # case on the Carrot row). Keep qty=1 in that case.
                    if candidate_qty > 1 and ext_t is not None:
                        try:
                            cand_ext = float(ext_t["text"].lstrip("$").rstrip("*"))
                            expected = candidate_qty * unit_f
                            if expected > 0 and abs(cand_ext - expected) / expected < 0.05:
                                qty_int = candidate_qty
                                ext_f = cand_ext
                        except ValueError:
                            pass
                    else:
                        qty_int = candidate_qty
                        # B-CatchWeightDoubling fix (2026-05-12): for catch-
                        # weight rows (per_lb_f set), unit_f IS the printed
                        # line ext (T/WT × ppp), already totaling all cases.
                        # Multiplying by candidate_qty would double the ext.
                        # Reference: INV 775404605 BEEF STEAK STRIP — 2 CS,
                        # T/WT=24 lbs, ppp=$12.75, paper ext=$306. Pre-fix
                        # stored ext=$612, then B-Salmon derived qty=48.
                        # 3 corpus rows affected (~$600 total inflation).
                        if per_lb_f is None:
                            ext_f = unit_f * candidate_qty
                        # else: catch-weight — leave ext_f at default unit_f
                        # so B-Salmon below derives correct T/WT = ext/ppp.

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

        # Section tag — most-recent **** SECTION **** header above this SUPC.
        # Sort sections by y (find_sections returns them in row-iteration
        # order, which isn't always y-sorted on multi-column invoices).
        # B2b: when no section header on this page sits above the SUPC's y,
        # use carry_section (the section from the previous page's bottom) —
        # critical for items at top of continuation pages.
        # B2c: only use sections that canonicalize to a known Sysco section.
        # Junk labels like "26.14" (price tokens that passed _find_sections
        # via stray asterisks) get filtered out here so items aren't tagged
        # with non-section labels.
        try:
            from spatial_matcher import (canonicalize_sysco_section,
                                          _CANONICAL_SYSCO_SECTIONS)
            _canon_set = _CANONICAL_SYSCO_SECTIONS
            _canon_fn = canonicalize_sysco_section
        except Exception:
            _canon_set = []
            _canon_fn = lambda x: x
        sec_name = carry_section  # default to inherited section from prior page
        for sec_y, sec_label in sorted(sections, key=lambda s: s[0]):
            if sec_y <= y_supc:
                canon = _canon_fn(sec_label)
                if canon in _canon_set:
                    sec_name = canon
            else:
                break

        # B-OrphanSection fix (2026-05-10): when no canonical section is at-
        # or-above the SUPC AND carry_section is empty (item appears before
        # ANY section header), fall back to the NEAREST canonical section
        # below the SUPC by y-distance. Without this, items get section=""
        # and surface as an orphan entry in section_reconciliation with
        # parser_sum > 0 + printed_total=None, polluting REVIEW classification.
        # Reference: INV 775837983 (2026-04-27) had 5 items orphan-tagged
        # ($299.10) when they belonged to the section starting just below
        # them on the page. Conservative — only fires when sec_name is
        # truly empty (not when carry_section already provided a value).
        #
        # Refinement (2026-05-11): require y-distance < 0.10 (half a typical
        # page section). Items further than that are likely truly orphan
        # (page-spanning, missing header) and shouldn't be force-tagged
        # into a far-away section — that creates false section_with_gap
        # entries when the real section's reconciliation breaks.
        if not sec_name:
            _ORPHAN_FALLBACK_Y_TOL = 0.10
            below_canonicals = [
                (sec_y, _canon_fn(sec_label))
                for sec_y, sec_label in sections
                if sec_y > y_supc
                and (sec_y - y_supc) < _ORPHAN_FALLBACK_Y_TOL
                and _canon_fn(sec_label) in _canon_set
            ]
            if below_canonicals:
                # Nearest section below by y-distance (smallest sec_y - y_supc)
                below_canonicals.sort(key=lambda s: s[0] - y_supc)
                sec_name = below_canonicals[0][1]

        item = {
            "raw_description":  desc or f"[Sysco #{supc['text']}]",
            "sysco_item_code":  supc["text"],
            "unit_price":       unit_f,
            "extended_amount":  ext_f,
            "case_size_raw":    "",
            "section":          sec_name,
            "quantity":         qty_int,
            "unit_of_measure":  "CASE",
            "ambiguous":        ambiguous,
        }
        if per_lb_f is not None:
            item["unit_of_measure"] = "LB"
            item["price_per_unit"] = per_lb_f
            # B-Salmon fix (2026-05-10): catch-weight rows ship by actual
            # weight (T/WT), not by case count. Without this fix, quantity
            # stays at qty_int (case count, typically 1) while ppp holds
            # the per-lb price — `validate_line_math` then computes
            # qty(1) × ppp($9.06) = $9.06 ≠ ext($105.08) → false-positive
            # math_flag on EVERY Sysco MEATS/POULTRY/SEAFOOD line.
            #
            # Derive shipped weight from ext / per_lb. Mirrors what
            # spatial_matcher already does for Exceptional catch-weight
            # (see spatial_matcher.py:951-971). Reference: INV 775856655
            # Salmon — ext=$105.08, ppp=$9.059, derived qty=11.600 = paper
            # truth T/WT.
            #
            # Also populates case_total_weight_lb + case_pack_* structured
            # fields for downstream consumers (cost_utils, synergy_sync's
            # $/lb math, inventory) to read the canonical weight shape.
            if per_lb_f > 0 and ext_f > 0:
                derived_weight = round(ext_f / per_lb_f, 3)
                # Sanity: derived weight must be positive and within a
                # reasonable range (no million-lb shipments). Skip when
                # implausible — leave qty alone so math_flag can surface.
                if 0.1 < derived_weight < 1000:
                    item["quantity"] = derived_weight
                    item["case_total_weight_lb"] = derived_weight
                    item["case_pack_count"] = 1
                    item["case_pack_unit_size"] = str(derived_weight)
                    item["case_pack_unit_uom"] = "LB"
                    item["purchase_uom"] = "LB"

        # Catch-weight aware math validation. ppp (via price_per_unit alias)
        # wins for MEATS/POULTRY/SEAFOOD. No self-correct here — Sysco
        # rank-pair already derives qty from ext/unit deterministically
        # (B1 fix); a second self-correct layer could mask real anomalies.
        validate_line_math(item, vendor='Sysco')
        rows.append(item)

    # B2b: compute the LAST canonical section detected on this page so
    # the caller can carry it forward to the next page. If no canonical
    # sections detected here, the carry_section we received remains the
    # carry-out. B2c: only canonical sections are considered (junk labels
    # like "26.14" that survive _find_sections must not be carried forward).
    last_section = carry_section
    try:
        from spatial_matcher import (canonicalize_sysco_section,
                                      _CANONICAL_SYSCO_SECTIONS)
        for sec_y, sec_label in sorted(sections, key=lambda s: s[0]):
            canon = canonicalize_sysco_section(sec_label)
            if canon in _CANONICAL_SYSCO_SECTIONS:
                last_section = canon  # last-write wins → final canonical section
    except Exception:
        pass
    return rows, last_section


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
