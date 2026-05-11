"""Spatial anchor/description matcher.

Uses DocAI per-token bounding boxes (pages[].tokens[]) to pair SUPC anchors
with their descriptions and prices by PHYSICAL ROW — tokens whose y-centers
fall within a tight window. Bypasses the 1D line-ordering ambiguity that
the raw_text-based parser has to work around with heuristics (Step A0/A/
A2, B.1/B.2, orphan-code pairing, column-dump fallbacks).

Validated against invoice 2025-09-16 Sysco: a naive 20-line row-clustering
matcher extracted 20/21 anchors correctly with matching descriptions and
prices; the 21st was the invoice-number token (wrong x-column, trivial
filter).

Public entry: match_sysco_spatial(pages) -> list[dict] with the same item
shape as parser._parse_sysco() output.
"""
from __future__ import annotations
import re

from line_math import validate_line_math

# ── Tunable geometry constants (all in DocAI normalized [0,1] space) ────────

# Row grouping tolerance — tokens within this y-center delta are "same row".
# 0.012 tuned on Sysco portrait-format: captures minor DocAI y-jitter from
# subscript/superscript tokens while still splitting adjacent rows (typical
# row spacing is ~0.029).
_ROW_Y_TOL = 0.012

# SUPC codes land around x≈0.57 in Sysco; tighten to avoid picking up
# header/footer numbers at far-right (invoice number at x≈0.72).
_SUPC_X_MIN = 0.40
_SUPC_X_MAX = 0.68

# Description tokens sit LEFT of the SUPC column. Pack size, brand, and
# product text share the left-of-anchor space. We keep the LHS cutoff
# loose (0.06) so pack tokens like "241.5 OZSTACYS" are captured; the
# delivery marker column ("D") at x<0.05 and the raw qty column ("1 CS",
# "2 CS") are filtered by token-content heuristics inside the extractor.
_DESC_X_MIN = 0.06
# (_DESC_X_MAX is derived per-row from the anchor's own x_min.)
_QTY_TOKENS = {"D", "S", "A", "CS", "EA", "LB", "1s", "1S", "T/WT=", "T/WT"}

# Price tokens: dollars-and-cents, with optional third decimal for per-lb
# (e.g. "3.299"). They sit RIGHT of the SUPC column.
_PRICE_RE = re.compile(r'^\$?\d+\.\d{2,3}\*?$')
_SUPC_RE  = re.compile(r'^\d{7}$')
_SECTION_HDR_RE = re.compile(r'\*{2,}')


def _y_mid(tok: dict) -> float:
    return (tok["y_min"] + tok["y_max"]) / 2


def _group_rows(tokens: list[dict], tol: float | None = None) -> list[list[dict]]:
    """Cluster tokens into rows by y-midpoint. Each row is a list of tokens
    sorted left-to-right by x_min.

    A token joins the current row if its y-center is within `tol` of the
    row's running-mean y-center. Mean-based clustering avoids truncating
    a row early when its first token has an outlier y (e.g. subscript).

    `tol` defaults to _ROW_Y_TOL (0.012, tuned for Sysco portrait layout).
    Denser vendors may need a tighter tol — PBM squeezes consecutive
    items only ~0.013 apart, so its matcher passes tol=0.006."""
    if tol is None:
        tol = _ROW_Y_TOL
    if not tokens:
        return []
    sorted_toks = sorted(tokens, key=_y_mid)
    rows: list[list[dict]] = []
    current: list[dict] = [sorted_toks[0]]
    current_sum = _y_mid(sorted_toks[0])
    for t in sorted_toks[1:]:
        y = _y_mid(t)
        mean = current_sum / len(current)
        if abs(y - mean) <= tol:
            current.append(t)
            current_sum += y
        else:
            rows.append(current)
            current = [t]
            current_sum = y
    rows.append(current)
    for row in rows:
        row.sort(key=lambda t: t["x_min"])
    return rows


# Canonical Sysco section names — the only valid section labels. Order
# matters: longer names checked first so "PAPER & DISP" wins over "PAPER".
# Maps any extracted label that CONTAINS one of these to the canonical
# form, dropping trailing junk like "PAPER & DISP GROUP" → "PAPER & DISP".
_CANONICAL_SYSCO_SECTIONS = [
    'CHEMICAL & JANITORIAL',
    'SUPPLY & EQUIPMENT',
    'PAPER & DISPOSABLE',
    'PAPER & DISP',
    'CANNED & DRY',
    'MISC CHARGES',
    'POULTRY', 'SEAFOOD', 'PRODUCE', 'BAKERY', 'BEVERAGE',
    'GROCERY', 'SPICES', 'FROZEN', 'DAIRY', 'MEATS', 'MEAT', 'DELI',
]


def canonicalize_sysco_section(label: str) -> str:
    """Map a Sysco section header label (possibly polluted with adjacent
    OCR tokens) to its canonical name.

    Returns the canonical name (e.g. 'PAPER & DISP') when found as a
    substring of the input. Returns the original label unchanged when no
    canonical match is found (defensive — preserves anything we don't
    recognize so it surfaces in audit). Empty input returns empty.

    B2 fix (2026-05-07): used by rank_pair section assignment and
    section_validator to normalize labels across pages of the same invoice.
    Without this, INV 775823034's PAPER section was tagged 'PAPER & DISP'
    on one page and 'PAPER & DISP GROUP' on another — the section_validator
    treated them as different sections.
    """
    if not label:
        return label
    upper = label.upper()
    for canonical in _CANONICAL_SYSCO_SECTIONS:
        if canonical in upper:
            return canonical
    return label


def _find_sections(rows: list[list[dict]]) -> list[tuple[float, str]]:
    """Detect section headers (lines containing '****...****' or bracketed
    section names). Returns [(y_center, name)] in y-order.

    B2 fix (2026-05-07): when the section-header row's y-cluster picks up
    adjacent line-item tokens (e.g. `**** FROZEN **** PUFF PASTRY SLAB ...`),
    the legacy approach `re.sub('\\*+', '', joined)` retained ALL tokens,
    leaking line content into the section label. Resulted in inconsistent
    section names across pages of the same invoice (`FROZEN` vs `FROZEN PUFF
    PASTRY SLAB`), which broke `section_validator.extract_section_totals_by_max`
    cross-page merging on INV 775823034 (residual +27% gap).

    Fix: extract just the text BETWEEN the first two asterisk runs. The
    Sysco section-header pattern is `**** SECTION ****` with both runs
    captured by OCR; the section name lives strictly between them. Falls
    back to legacy strip-and-keep when only one asterisk run is present
    (rare OCR cases where one run was clipped).
    """
    sections: list[tuple[float, str]] = []
    for row in rows:
        texts = [t["text"] for t in row]
        joined = " ".join(texts)
        if not _SECTION_HDR_RE.search(joined):
            continue
        asterisk_runs = list(re.finditer(r'\*{2,}', joined))
        label = None
        if len(asterisk_runs) >= 2:
            # Standard `**** NAME ****` — extract between the runs.
            between = joined[asterisk_runs[0].end():asterisk_runs[1].start()].strip()
            if 4 <= len(between) <= 30:
                label = between
        elif len(asterisk_runs) == 1:
            # Single-run header: `**** NAME [item tokens]` — the closing
            # `****` got OCR'd into another y-row. Take first 4 tokens
            # after the run and accept ONLY if they map to a canonical
            # section name. Stops phantom labels like "FILET BLSL IQF"
            # from being treated as sections.
            after = joined[asterisk_runs[0].end():].strip()
            candidate = ' '.join(after.split()[:4])
            canon = canonicalize_sysco_section(candidate)
            if canon in _CANONICAL_SYSCO_SECTIONS:
                label = canon
        if label:
            y = _y_mid(row[0])
            sections.append((y, label))
    return sections


def _section_for_y(y: float, sections: list[tuple[float, str]]) -> str:
    """The most-recent section header whose y is above `y`. Empty string
    if no section header comes before this row."""
    last = ""
    for sec_y, name in sections:
        if sec_y <= y:
            last = name
        else:
            break
    return last


def _extract_row_item(row: list[dict], anchor: dict,
                     section_name: str) -> dict | None:
    """Pull (desc, price, price_per_unit, case_size) from a row given its
    anchor token. Returns an item dict in parser._parse_sysco's shape, or
    None if the row is structurally invalid (no price, garbage desc)."""
    # Description = text tokens left of the anchor, right of the qty column.
    # Filter:
    #   - drop price-shaped and SUPC-shaped tokens
    #   - drop standalone qty/marker tokens ("D", "1 CS", "2 CS") ONLY when
    #     they sit in the quantity x-band (x < _PACK_X_MIN). Further right
    #     they are part of the pack column or product name ("1 CS" brand
    #     prefix in a merged column) and must be preserved for pack-size
    #     extraction (e.g. "6 1 GAL" for a 6×1-gallon olive oil pack).
    _PACK_X_MIN = 0.17  # tokens at x>=0.17 belong to pack/desc columns
    desc_tokens = []
    for t in row:
        if t["x_min"] < _DESC_X_MIN or t["x_min"] >= anchor["x_min"]:
            continue
        tx = t["text"]
        if _PRICE_RE.fullmatch(tx) or _SUPC_RE.fullmatch(tx):
            continue
        if t["x_min"] < _PACK_X_MIN:
            # Qty column — drop marker/qty tokens so they don't pollute desc
            if tx in _QTY_TOKENS:
                continue
            if re.fullmatch(r'\d{1,2}', tx):
                continue
        desc_tokens.append(t)
    description = " ".join(t["text"] for t in desc_tokens).strip()

    # Price tokens right of the anchor. First price = unit_price, any
    # third-decimal token = price_per_lb (catch-weight).
    right_tokens = [t for t in row if t["x_min"] > anchor["x_min"]]
    unit_price = None
    price_per_unit = None
    for t in right_tokens:
        txt = t["text"].lstrip("$").rstrip("*")
        if _PRICE_RE.fullmatch(t["text"]):
            try:
                val = float(txt)
            except ValueError:
                continue
            if "." in txt and len(txt.split(".", 1)[1]) == 3:
                # Three-decimal token — catch-weight per-lb price
                if price_per_unit is None:
                    price_per_unit = val
                continue
            if unit_price is None:
                unit_price = val

    if unit_price is None:
        return None

    # Pack size extraction. Parser.py's _extract_pack_size requires the
    # pack to be at the start of line (re.match anchor) which doesn't fit
    # spatial's mid-desc pack location. We use a regex SEARCH over the
    # row text that handles the common Sysco OCR variants:
    #   "241.5 OZ STACYS" (decimal + space + unit)
    #   "241.50Z STACYS"  (decimal + merged unit + 'O' OCR'd as '0')
    #   "52LB SYS"        (int + merged unit)
    #   "617 OZ PAM"      (int + space + unit)
    #   "1216 OZ LYON"    (merged qty+size, e.g. "12/16 OZ")
    # Then pass the raw hit through parser's _normalize_pack_size which
    # knows how to split "1216OZ" → "12/16OZ", "124OZ" → "12/4OZ", etc.
    case_size = ""
    # Pre-normalize OCR artifacts and format variants before regex match:
    #   Ω / Ο / Ρ prefix (DocAI misreads "D" or similar glyphs) — strip
    #   "ONLY5 LB"    → "5 LB"        (bare ONLY prefix)
    #   "ONLY1 # TIN" → "1 # TIN"     (ONLY + hash — keep the count)
    #   "KILO"        → "KG"
    #   "6 # 10 CAN"  → "6/10CAN"
    #   "PTPACKER"    → "PT PACKER"   (Sysco brand fused to unit)
    #   "LBIMPFRSH"   → "LB IMPFRSH"  (ditto)
    norm_desc = description
    norm_desc = re.sub(r'^[ΩΟΡ]\s*', '', norm_desc)
    norm_desc = re.sub(r'\bONLY(\d+)\s*#', r'\1 # ', norm_desc, flags=re.IGNORECASE)
    norm_desc = re.sub(r'\bONLY\s*(\d+(?:\.\d+)?)\s*', r'\1 ', norm_desc, flags=re.IGNORECASE)
    norm_desc = re.sub(r'\bKILO\b', 'KG', norm_desc, flags=re.IGNORECASE)
    norm_desc = re.sub(r'\b(\d+)\s*#\s*(\d+)\b', r'\1/\2CAN', norm_desc)
    # Split fused unit+brand: Sysco brands are 3+ uppercase letters, and
    # no real English word starts with PT/LB/OZ/GAL/DZ/CT/PC followed by
    # 3+ uppercase letters. Restricting the right side to uppercase
    # avoids matching lowercase words like "ozone". Also handles the
    # OCR variant "0Z" (letter O read as zero — common DocAI artifact).
    norm_desc = re.sub(r'\b(PT|LB|OZ|GAL|DZ|CT|PC)([A-Z]{3,})\b', r'\1 \2', norm_desc)
    norm_desc = re.sub(r'(\d)(0Z)([A-Z]{3,})\b', r'\1\2 \3', norm_desc)
    # "C LB IMPFRSH", "F LB SYS CLS", "T LB JTM" — the leading single
    # letter is a Sysco container/marker token (C=case, F=freight-bill,
    # T=tare-weight); it replaces the pack-count that would normally
    # occupy that position. Treat as implicit "1 <unit>" so
    # case_size populates (single-pack items).
    norm_desc = re.sub(
        r'^([A-Z])\s+(GAL|LB|OZ|PT|DZ|CT|EA|GM|QT|BAG|BCH|PC)\b',
        r'1 \2', norm_desc)

    # Try slash-format FIRST so we don't accidentally pick up the trailing
    # number of a pack like "6 1/2 PT" (OCR'd as "61/2 PT") as if it were a
    # standalone "2 PT". Slash-format also covers standard Sysco packs
    # "4/50OZ", "24/12 OZ", "1/22LB", "6/10CAN".
    #
    # Mixed-number heuristic: "61/2 PT" is really "6 1/2 PT" (6 cases x
    # 0.5 pt each) per Sean's Sysco ordering convention. When the first
    # digits look too large for a realistic pack count AND the divisor is
    # a common food-service fraction denominator (2/3/4/8), reinterpret
    # as "pack × fraction × unit":
    #   "61/2 PT"   → "6/.5PT"   (6 × half-pint — Sysco berry packs)
    #   "121/2 PT"  → "12/.5PT"  (12 × half-pint)
    #   "61/4 LB"   → "6/.25LB"  (6 × quarter-lb)
    m = re.search(
        r'\b(\d+)\s*/\s*(\d+)\s*(OZ|LB|GAL|CT|EA|KG|ML|L|#10|QT|GM|KT|CAN|PT|DZ|PC)\b',
        norm_desc, re.IGNORECASE)
    if m:
        num1, num2, unit = m.group(1), m.group(2), m.group(3).upper()
        # Try to reinterpret as mixed number if divisor is a common
        # food-service fraction denominator. For multi-digit num1, try
        # each possible split point: pack=num1[:k], frac=num1[k:].
        # Pick the FIRST split where pack is 1-12 AND frac/denom < 1.
        if len(num1) >= 2 and num2 in ("2", "3", "4", "8"):
            denom = int(num2)
            for split_k in (1, 2):
                if split_k >= len(num1):
                    break
                pack_str = num1[:split_k]
                frac_str = num1[split_k:]
                if not (pack_str and frac_str):
                    continue
                pack = int(pack_str)
                frac_num = int(frac_str)
                if 1 <= pack <= 12 and 1 <= frac_num < denom:
                    size = frac_num / denom
                    size_str = f"{size:g}".lstrip("0") or "0"
                    case_size = f"{pack}/{size_str}{unit}"
                    break
        if not case_size:
            case_size = f"{num1}/{num2}{unit}".upper().replace(" ", "")

    # Primary pack-size match (no slash). Unit list is INTENTIONALLY broader
    # than parser._extract_pack_size's — spatial row text includes Sysco's
    # full pack-column tokens (PT pints, DZ dozen, PC pieces, BU bushel,
    # etc.) that the 1D parser doesn't always see because it anchors on
    # start-of-line patterns.
    if not case_size:
        m = re.search(
            r'\b(\d+(?:\.\d+)?)\s*(?:'
            r'([O0]Z|LB|GAL|CT|EA|KG|ML|QT|GM|DZ|PT|BAG|BU|BCH|BTL|PC|ROLL|#\d+)'
            r'|(L)\b)',
            norm_desc, re.IGNORECASE)
        if m:
            num = m.group(1)
            unit = (m.group(2) or m.group(3) or "").upper().replace("0Z", "OZ")
            raw = f"{num}{unit}"
            try:
                from parser import _normalize_pack_size
                case_size = _normalize_pack_size(raw)
            except Exception:
                case_size = raw

    item = {
        "raw_description":  description or f"[Sysco #{anchor['text']}]",
        "sysco_item_code":  anchor["text"],
        "unit_price":       unit_price,
        "extended_amount":  unit_price,
        "case_size_raw":    case_size,
        "section":          section_name,
        # Sysco lines are always 1 case per anchor — same convention as _parse_sysco
        "quantity":         1,
        "unit_of_measure":  "CASE",
    }
    if price_per_unit is not None:
        item["unit_of_measure"] = "LB"
        item["price_per_unit"] = price_per_unit
        # B-Salmon fix (2026-05-10): mirror the rank_pair.py fix — when
        # catch-weight detected (3-decimal per-lb token), quantity must
        # be the actual shipped weight (T/WT), not case count. Without
        # this, validate_line_math fires qty(1) × ppp ≠ ext (line total)
        # → false-positive math_flag on every Sysco MEATS/POULTRY/SEAFOOD
        # line. Derive weight from ext/ppp; populate structured catch-
        # weight fields for downstream cost/inventory consumers.
        # Sanity guard: skip implausible weights (≤0.1 or ≥1000 lbs).
        if price_per_unit > 0 and unit_price > 0:
            derived_weight = round(unit_price / price_per_unit, 3)
            if 0.1 < derived_weight < 1000:
                item["quantity"] = derived_weight
                item["case_total_weight_lb"] = derived_weight
                item["case_pack_count"] = 1
                item["case_pack_unit_size"] = str(derived_weight)
                item["case_pack_unit_uom"] = "LB"
                item["purchase_uom"] = "LB"
    # Phase 2a (2026-05-02): structured pack-size fields. Reuse the parser
    # helper so spatial + text paths produce identical structured output.
    try:
        from parser import _structured_pack_from_case_size, _extract_count_per_lb
        item.update(_structured_pack_from_case_size(case_size))
        # Phase 3 #6: count-per-lb extraction for protein items
        cpl = _extract_count_per_lb(description)
        if cpl is not None:
            item['count_per_lb_low'] = cpl[0]
            item['count_per_lb_high'] = cpl[1]
    except Exception:
        pass
    return item


# Per-line math validation lives in `line_math.py` — catch-weight aware
# (uses price_per_pound when populated). Imported at top of this module
# as `validate_line_math`.


def match_sysco_spatial(pages: list[dict]) -> list[dict]:
    """Extract Sysco invoice items from per-page bounding-box layout.

    Each item carries the same keys as parser._parse_sysco output so the
    downstream pipeline (mapper → db_write → synergy_sync) doesn't need
    to know which path produced it.

    Returns empty list if pages have no tokens (caller should then fall
    back to the raw_text-based parser)."""
    items: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        rows = _group_rows(tokens)
        sections = _find_sections(rows)
        for row in rows:
            anchors = [t for t in row
                       if _SUPC_RE.fullmatch(t["text"])
                       and _SUPC_X_MIN <= t["x_min"] <= _SUPC_X_MAX]
            if not anchors:
                continue
            # Multiple anchors on one row = rare OCR artifact; take first.
            anchor = anchors[0]
            sec = _section_for_y(_y_mid(anchor), sections)
            sec_clean = re.sub(r'[*\s]+', ' ', sec).strip()
            item = _extract_row_item(row, anchor, sec_clean)
            if item:
                # Catch-weight aware math validation (qty × ppp or qty × unit
                # depending on which is populated). Mutates item with
                # math_flagged on anomaly.
                validate_line_math(item, vendor='Sysco')
                items.append(item)
    return items


# ═══════════════════════════════════════════════════════════════════════════
# PBM (Philadelphia Bakery Merchants) — Format 2 (digital invoices)
# ═══════════════════════════════════════════════════════════════════════════

# Observed layout from 2026-04-02 invoice:
#   x=0.08  item code (H106, L7408, R1012, or 4-digit like "0290")
#   x=0.24  qty (decimal, e.g. "2.00")
#   x=0.41  U/M (DZ, EA)
#   x=0.46-0.65  description tokens (Wheat / Brioche / Pita / Buns)
#   x=0.78  unit price (decimal)
#   x=0.85  extended amount (decimal)
# Item rows sit at y=0.38-0.75 typically; headers above, totals below.

# PBM item codes observed in real invoices:
#   - Letter + 2-5 digits: G105, H097, L07, L118, R1012
#   - Leading-zero 4-digit: 0258, 0290, 0389
# Excludes plain 3-digit numerics like "P.O. Box 723" and 5-digit zip codes.
_PBM_ITEM_CODE_RE = re.compile(r'^(?:[A-Z]\d{2,5}|0\d{3})$')
_PBM_UM_RE = re.compile(r'^(DZ|EA|LB|CS|OZ|PK|BG|CTN)$', re.IGNORECASE)
_PBM_PRICE_RE = re.compile(r'^\$?\d+\.\d{2}$')

_PBM_CODE_X_RANGE  = (0.05, 0.15)
_PBM_QTY_X_RANGE   = (0.20, 0.32)
_PBM_UM_X_RANGE    = (0.38, 0.44)
_PBM_DESC_X_RANGE  = (0.44, 0.72)
_PBM_UNIT_X_RANGE  = (0.72, 0.82)
_PBM_EXT_X_RANGE   = (0.82, 0.95)


def _in_x(tok: dict, band: tuple[float, float]) -> bool:
    return band[0] <= tok["x_min"] < band[1]


def match_pbm_spatial(pages: list[dict]) -> list[dict]:
    """Extract PBM invoice items from per-token bounding-box layout.

    PBM's 1D parser struggles with wrap/split rows that interleave codes,
    qtys, U/M, and descriptions across multiple raw_text lines. The
    spatial matcher reconstructs the true row by y-cluster and partitions
    tokens into the fixed column grid.

    Each item carries the parser output schema (raw_description, unit_price,
    extended_amount, case_size_raw, sysco_item_code). sysco_item_code is
    empty for PBM — PBM has its own item codes which we keep in the
    raw_description prefix until the mapper learns them."""
    items: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        # B-NEW (2026-05-07): code-column-anchored row construction.
        # Old approach used `_group_rows(tokens, tol=0.006)` to cluster
        # tokens by y, then partitioned each row into columns. That fails
        # on PBM invoices with column y-skew (OCR reads each column with
        # a slight independent y-shift): unit-price token y can be ~0.011
        # ABOVE its row's code y, ext token y ~0.012 above. With ~0.012
        # row-spacing and ~0.011 column skew, sequential token y's form
        # a continuous gap-less chain that collapses into one giant
        # cluster — INV 5764 produced 1 spatial item instead of 2.
        #
        # New approach: anchor on item-code tokens (one per row, well
        # separated). For each code, find the closest qty/UM/desc/unit/ext
        # token in its respective x-band by y-proximity, allowing up to
        # ±half-row-spacing y-window to absorb column skew.
        def _ymid(t):
            return (t["y_min"] + t["y_max"]) / 2

        # Pre-filter: only code tokens that have a price-shaped token in
        # the ext band within a generous y-window count as real item rows.
        # The PBM item-code regex `^[A-Z]?\d{3,5}$` also matches header
        # numbers like "P.O. Box 723" — without this filter, those bogus
        # codes pollute row_spacing and the desc-band y-window.
        candidate_codes = sorted(
            [t for t in tokens
             if _in_x(t, _PBM_CODE_X_RANGE)
             and _PBM_ITEM_CODE_RE.fullmatch(t["text"])],
            key=_ymid,
        )
        if not candidate_codes:
            continue
        # Sanity-filter: keep only codes that have at least one price-shaped
        # token in the ext band within ±0.025 y (covers any reasonable
        # column skew). Real item rows always have an extended-amount value.
        code_toks = [t for t in candidate_codes
                     if any(
                         _in_x(p, _PBM_EXT_X_RANGE)
                         and _PBM_PRICE_RE.fullmatch(p["text"])
                         and abs(_ymid(p) - _ymid(t)) <= 0.025
                         for p in tokens)]
        if not code_toks:
            continue
        # Estimate row spacing from MEDIAN of consecutive code-token y gaps.
        if len(code_toks) >= 2:
            ys = [_ymid(t) for t in code_toks]
            gaps = sorted(ys[i+1] - ys[i] for i in range(len(ys)-1))
            row_spacing = gaps[len(gaps)//2] if gaps else 0.014
        else:
            row_spacing = 0.014
        # Search window = ~half the row spacing — wide enough to catch
        # column-skewed tokens but narrow enough not to bleed adjacent
        # rows.
        y_win = max(row_spacing * 0.55, 0.008)

        def _nearest_in_band(code_y, band, regex):
            best = None
            best_dy = float("inf")
            for t in tokens:
                if not _in_x(t, band):
                    continue
                if not regex.fullmatch(t["text"]):
                    continue
                dy = abs(_ymid(t) - code_y)
                if dy < best_dy and dy <= y_win:
                    best_dy = dy
                    best = t
            return best

        # Ordinal-position pairing for unit and ext columns. PBM has
        # consistent column y-skew (prices read slightly higher on page
        # than their row's code). With nearest-y matching, row 1's code
        # (y=0.404) gets matched to row 2's price (y=0.407) instead of
        # its own (y=0.394). Sorting both columns by y and pairing by
        # ordinal index gives the correct row-to-price assignment.
        #
        # Filter out tokens outside the items range — Subtotal/Invoice
        # Total values appear in the ext-column band but in the footer
        # (well below the last code). Items range = code y-range padded
        # by half a row.
        code_y_min = min(_ymid(t) for t in code_toks)
        code_y_max = max(_ymid(t) for t in code_toks)

        def _in_items_range(t):
            ty = _ymid(t)
            return (code_y_min - 0.020) <= ty <= (code_y_max + 0.020)

        _unit_toks_sorted = sorted(
            [t for t in tokens
             if _in_x(t, _PBM_UNIT_X_RANGE)
             and _PBM_PRICE_RE.fullmatch(t["text"])
             and _in_items_range(t)],
            key=_ymid,
        )
        _ext_toks_sorted = sorted(
            [t for t in tokens
             if _in_x(t, _PBM_EXT_X_RANGE)
             and _PBM_PRICE_RE.fullmatch(t["text"])
             and _in_items_range(t)],
            key=_ymid,
        )
        _qty_toks_sorted = sorted(
            [t for t in tokens
             if _in_x(t, _PBM_QTY_X_RANGE)
             and _PBM_PRICE_RE.fullmatch(t["text"])
             and _in_items_range(t)],
            key=_ymid,
        )
        _um_toks_sorted = sorted(
            [t for t in tokens
             if _in_x(t, _PBM_UM_X_RANGE)
             and _PBM_UM_RE.fullmatch(t["text"])
             and _in_items_range(t)],
            key=_ymid,
        )

        def _by_ordinal(idx, sorted_list):
            return sorted_list[idx] if idx < len(sorted_list) else None

        def _all_in_band(code_y, band, exclude_regexes=()):
            out = []
            for t in tokens:
                if not _in_x(t, band):
                    continue
                if any(r.fullmatch(t["text"]) for r in exclude_regexes):
                    continue
                if abs(_ymid(t) - code_y) <= y_win:
                    out.append(t)
            return out

        # Use ordinal pairing when counts match across columns; fall back
        # to nearest-y when they don't (e.g., a code missing its ext).
        ordinal_ok = (
            len(_unit_toks_sorted) == len(code_toks)
            and len(_ext_toks_sorted) == len(code_toks)
        )
        for idx, code_t in enumerate(code_toks):
            code_y = _ymid(code_t)
            if ordinal_ok:
                ext_t = _by_ordinal(idx, _ext_toks_sorted)
                unit_t = _by_ordinal(idx, _unit_toks_sorted)
            else:
                ext_t = _nearest_in_band(code_y, _PBM_EXT_X_RANGE, _PBM_PRICE_RE)
                unit_t = _nearest_in_band(code_y, _PBM_UNIT_X_RANGE, _PBM_PRICE_RE)
            if ext_t is None:
                continue
            code = code_t["text"]
            extended = float(ext_t["text"].lstrip("$"))
            unit_price = float(unit_t["text"].lstrip("$")) if unit_t else extended

            # Qty and UM use ordinal when counts match, else nearest-y.
            qty_ordinal_ok = len(_qty_toks_sorted) == len(code_toks)
            um_ordinal_ok = len(_um_toks_sorted) == len(code_toks)
            qty_t = (_by_ordinal(idx, _qty_toks_sorted) if qty_ordinal_ok
                     else _nearest_in_band(code_y, _PBM_QTY_X_RANGE, _PBM_PRICE_RE))
            qty = float(qty_t["text"]) if qty_t else None
            um_t = (_by_ordinal(idx, _um_toks_sorted) if um_ordinal_ok
                    else _nearest_in_band(code_y, _PBM_UM_X_RANGE, _PBM_UM_RE))
            um = um_t["text"].upper() if um_t else ""

            # B-NEW (2026-05-08): description owner = first code below
            # token. PBM (both phone and scanner) prints item codes BELOW
            # their description: scanner has desc y≈0.435, code y≈0.443
            # (code 0.008 below); phone has desc y≈0.411, code y≈0.404
            # (code visually at top but bbox y_min ≈ desc y_min — the
            # next-row's code is the one geometrically below). For each
            # desc-band token, the owning row's code is the first code
            # whose y >= desc_y. Closest-anchor fails at boundaries
            # because midway tokens can be geometrically nearer to the
            # wrong row — e.g., on INV 2055 'Rolls' (y=0.4222, row 2) is
            # 0.0046 from row-1 code G105 (0.4176) but 0.0078 from row-2
            # code L07 (0.4300). First-code-below correctly says L07.
            sorted_codes = sorted(code_toks, key=_ymid)
            desc_band_toks = [
                t for t in tokens
                if _in_x(t, _PBM_DESC_X_RANGE)
                and not _PBM_PRICE_RE.fullmatch(t["text"])
                and not _PBM_UM_RE.fullmatch(t["text"])
            ]
            # PRIMARY: first-code-below assignment. Works for invoices where
            # description y < code y (most PBM phone + scanner formats).
            desc_max = row_spacing - 0.002
            primary_toks = []
            for t in desc_band_toks:
                ty = _ymid(t)
                owner = next((c for c in sorted_codes
                              if _ymid(c) >= ty - 0.0005), None)
                if owner is None:
                    continue
                if owner is code_t and abs(_ymid(owner) - ty) <= desc_max:
                    primary_toks.append(t)

            if primary_toks:
                desc_toks = sorted(primary_toks, key=lambda t: t["x_min"])
            else:
                # FALLBACK: some PBM invoices have description y > code y
                # (irregular OCR layout, e.g., e4d0bcf4 2026-02-10). When
                # primary yields nothing for a code, fall back to nearest-
                # anchor by y-distance — better to have a mashed description
                # than a [PBM #code] placeholder.
                fallback_toks = []
                for t in desc_band_toks:
                    ty = _ymid(t)
                    closest_dy = min(abs(_ymid(c) - ty) for c in code_toks)
                    if closest_dy > desc_max:
                        continue
                    if abs(ty - code_y) == closest_dy:
                        fallback_toks.append(t)
                desc_toks = sorted(fallback_toks, key=lambda t: t["x_min"])
            description = " ".join(t["text"] for t in desc_toks).strip()

            if not description:
                description = f"[PBM #{code}]"

            # Phase 2 polish (2026-05-02): UM (DZ/EA) is the purchase unit,
            # not case-size info. Same fix as Farm Art Phase 2c.
            item = {
                "raw_description": description,
                "sysco_item_code": "",   # PBM doesn't use SUPC
                "unit_price": unit_price,
                "extended_amount": extended,
                "case_size_raw": "",
                "section": "",
                "purchase_uom": um,
                "unit_of_measure": um,
            }
            if qty is not None:
                item["quantity"] = qty
            # PBM has the same qty self-correction pattern as Farm Art —
            # 5 of 6 anomalies in test sample had ext/up rounding cleanly
            # to a small integer (Brioche Buns qty 2→1, White Pita qty 3→1,
            # Plain Bagels qty 2→3, Club White qty 5→3, etc.).
            # validate_line_math mutates item['quantity'] in place on
            # successful self-correction; sets math_flagged on real anomaly.
            validate_line_math(item, vendor='PBM', try_self_correct=True)
            items.append(item)
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Exceptional Foods
# ═══════════════════════════════════════════════════════════════════════════

# Observed layout from 2026-04-16 invoice:
#   x=0.06  item code (32425, c1215, 61565, 0150, p1768)
#   x=0.22  qty ordered (decimal)
#   x=0.27  U/M (EA, CS, LB)
#   x=0.30-0.62  description tokens
#   x=0.70  qty shipped (decimal — for catch-weight rows this is the weight)
#   x=0.79  unit price (decimal) — may be $/lb for catch-weight
#   x=0.85  per-unit U/M (LB for catch-weight)
#   x=0.92  extended amount (decimal)

_EXC_ITEM_CODE_RE = re.compile(r'^[a-zA-Z]?\d{4,5}$')
_EXC_UM_RE = re.compile(r'^(EA|CS|LB|DZ|OZ|PK|BG|CTN)$', re.IGNORECASE)
_EXC_PRICE_RE = re.compile(r'^\$?\d+\.\d{2,3}$')

# Lower bound widened from 0.04 to 0.03 — INV 330577 (PDF scan vs phone
# photo: scanner's left margin is tighter) has codes at x_min≈0.037,
# below the original threshold. Code regex (`^[a-zA-Z]?\d{4,5}$`) plus
# the row-level requirement of an ext-band match prevents address/zip
# tokens (which match the regex shape) from polluting items.
# Lower bound widened from 0.04 to 0.03 — INV 330577 (PDF scan vs phone
# photo: scanner's left margin is tighter) has codes at x_min≈0.037,
# below the original threshold. Code regex (`^[a-zA-Z]?\d{4,5}$`) plus
# the row-level requirement of an ext-band match prevents address/zip
# tokens (which match the regex shape) from polluting items.
# qty_ship/unit/per_um/ext bands also widened ±0.02 for scanner shifts:
# scanner output has columns at x_min ≈ 0.67/0.77/0.83/0.91 vs phone
# photo's 0.69/0.79/0.85/0.92.
_EXC_CODE_X_RANGE    = (0.03, 0.14)
_EXC_QTY_X_RANGE     = (0.18, 0.26)
_EXC_UM_X_RANGE      = (0.23, 0.30)
_EXC_DESC_X_RANGE    = (0.26, 0.66)
# Non-overlapping bands — phone-photo and scanner-PDF column x_min values
# both fit. Phone: qty_ship≈0.69, unit≈0.79, per_um≈0.85, ext≈0.92.
# Scanner: qty_ship≈0.67, unit≈0.77, per_um≈0.83, ext≈0.91.
_EXC_QTY_SHIP_X      = (0.66, 0.74)
_EXC_UNIT_X_RANGE    = (0.74, 0.82)
_EXC_PER_UM_X_RANGE  = (0.82, 0.88)
_EXC_EXT_X_RANGE     = (0.88, 0.98)


def match_exceptional_spatial(pages: list[dict]) -> list[dict]:
    """Extract Exceptional Foods items from bounding-box layout.

    Exceptional has the cleanest printed invoice structure of all vendors
    — single-row items, well-separated columns. Spatial matching mainly
    helps catch-weight rows where weight/per-lb/extended sit in three
    distinct x-bands that 1D text flattens together."""
    items: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        # B-NEW (2026-05-07): code-anchored row construction. Old approach
        # used `_group_rows(tokens, tol=0.012)` which chain-merges via
        # intermediate tokens. On scanned-PDF Exceptional invoices,
        # within-row spread (~0.010) plus between-row gap (~0.005)
        # produces a continuous y-chain where adjacent items get
        # bridged into one row — INV 330577 had Butter+Beef merged
        # into a single "row" with two codes, breaking extraction.
        #
        # New approach: each code-column token is a row anchor. Tokens
        # within ±row_spacing/2 of the code's y belong to that row.
        # Phone-photographed invoices (~0.029 row spacing) and
        # scanner-PDF invoices (~0.015 row spacing) both work because
        # row_spacing is computed from observed code-y gaps.
        def _ymid(t):
            return (t["y_min"] + t["y_max"]) / 2

        code_anchors = sorted(
            [t for t in tokens
             if _in_x(t, _EXC_CODE_X_RANGE)
             and _EXC_ITEM_CODE_RE.fullmatch(t["text"])
             # Sanity-filter: real items have a price-shaped token in the
             # ext band within ±0.025 y. Excludes header/zip/address codes.
             and any(
                 _in_x(p, _EXC_EXT_X_RANGE)
                 and _EXC_PRICE_RE.fullmatch(p["text"])
                 and abs(_ymid(p) - _ymid(t)) <= 0.025
                 for p in tokens)],
            key=_ymid,
        )
        if code_anchors:
            ys = [_ymid(t) for t in code_anchors]
            if len(ys) >= 2:
                gaps = sorted(ys[i+1] - ys[i] for i in range(len(ys)-1))
                row_spacing = gaps[len(gaps)//2] if gaps else 0.029
            else:
                row_spacing = 0.029
            # half_win must be: (a) wide enough to catch within-row tokens
            # (Exceptional rows span ~0.010 of y), (b) narrow enough not
            # to bleed into neighboring rows. The challenge: scanner-PDF
            # invoices have row_spacing as small as ~0.015 (similar to
            # within-row spread), so half_win can't be much less than 0.010.
            # Pick floor at 0.010 to capture within-row, then bleed
            # mitigation comes from the ORDINAL PAIRING below: sort each
            # column's tokens by y and pair by index, which is robust to
            # the column y-skew (ext tokens ~0.008 ABOVE their row's code).
            half_win = max(row_spacing * 0.55, 0.010)
            rows = []
            for anchor in code_anchors:
                ay = _ymid(anchor)
                row = [t for t in tokens
                       if abs(_ymid(t) - ay) <= half_win]
                rows.append(row)
        else:
            # No codes detected — fall back to legacy row grouping (which
            # would also produce no useful items, but keeps existing tests
            # of non-item pages passing).
            rows = _group_rows(tokens, tol=0.012)
            code_anchors = []

        # B-NEW (2026-05-07): pre-compute ordinal column lookups for
        # bleed-resistant column extraction. Each column's tokens are
        # sorted by y and paired with code_anchors by index. This is
        # robust to column y-skew (ext at y≈code_y-0.008, qty at
        # y≈code_y+0.002, etc.) because relative ordering within each
        # column matches the code ordering.
        def _ymid_t(t):
            return (t["y_min"] + t["y_max"]) / 2

        # Restrict each column's tokens to the items y-range so footer
        # tokens (subtotal/total/tax) don't pollute ordinal pairing.
        if code_anchors:
            code_y_min = min(_ymid_t(t) for t in code_anchors)
            code_y_max = max(_ymid_t(t) for t in code_anchors)
        else:
            code_y_min, code_y_max = 0, 1

        def _items_range_filter(t, band, regex):
            ty = _ymid_t(t)
            return (_in_x(t, band)
                    and regex.fullmatch(t["text"])
                    and (code_y_min - 0.020) <= ty <= (code_y_max + 0.020))

        ext_col = sorted(
            [t for t in tokens if _items_range_filter(t, _EXC_EXT_X_RANGE, _EXC_PRICE_RE)],
            key=_ymid_t,
        )
        unit_col = sorted(
            [t for t in tokens if _items_range_filter(t, _EXC_UNIT_X_RANGE, _EXC_PRICE_RE)],
            key=_ymid_t,
        )
        qty_ship_col = sorted(
            [t for t in tokens if _items_range_filter(t, _EXC_QTY_SHIP_X, _EXC_PRICE_RE)],
            key=_ymid_t,
        )
        per_um_col = sorted(
            [t for t in tokens if _items_range_filter(t, _EXC_PER_UM_X_RANGE, _EXC_UM_RE)],
            key=_ymid_t,
        )
        um_col = sorted(
            [t for t in tokens if _items_range_filter(t, _EXC_UM_X_RANGE, _EXC_UM_RE)],
            key=_ymid_t,
        )
        # Ordinal pairing only used when all columns have the same length
        # as code_anchors. Otherwise fall back to per-row search below.
        ordinal_ok = (
            len(code_anchors) > 0
            and len(ext_col) == len(code_anchors)
            and len(unit_col) == len(code_anchors)
        )
        for ri, row in enumerate(rows):
            code_toks = [t for t in row if _in_x(t, _EXC_CODE_X_RANGE)
                         and _EXC_ITEM_CODE_RE.fullmatch(t["text"])]
            if not code_toks:
                continue
            anchor_y = _ymid(code_toks[0])

            def _nearest_in_band(in_row, band, regex):
                cands = [t for t in in_row
                         if _in_x(t, band) and regex.fullmatch(t["text"])]
                if not cands:
                    return None
                return min(cands, key=lambda t: abs(_ymid(t) - anchor_y))

            # Use ordinal pairing when all key columns have the same count
            # as code anchors — robust to column y-skew. Otherwise per-row
            # nearest-y picker (legacy behavior for phone-photo invoices
            # with cleaner row separation).
            if ordinal_ok and ri < len(ext_col):
                ext_t = ext_col[ri]
                unit_t = unit_col[ri] if ri < len(unit_col) else None
                qty_ship_t = qty_ship_col[ri] if ri < len(qty_ship_col) else None
                per_um_t = per_um_col[ri] if ri < len(per_um_col) else None
                um_t = um_col[ri] if ri < len(um_col) else None
            else:
                ext_t = _nearest_in_band(row, _EXC_EXT_X_RANGE, _EXC_PRICE_RE)
                unit_t = _nearest_in_band(row, _EXC_UNIT_X_RANGE, _EXC_PRICE_RE)
                qty_ship_t = _nearest_in_band(row, _EXC_QTY_SHIP_X, _EXC_PRICE_RE)
                per_um_t = _nearest_in_band(row, _EXC_PER_UM_X_RANGE, _EXC_UM_RE)
                um_t = _nearest_in_band(row, _EXC_UM_X_RANGE, _EXC_UM_RE)

            if ext_t is None:
                continue

            code = code_toks[0]["text"]
            extended = float(ext_t["text"].lstrip("$"))
            unit_price = float(unit_t["text"].lstrip("$")) if unit_t else extended
            per_um = per_um_t["text"].upper() if per_um_t else ""
            qty_shipped = float(qty_ship_t["text"]) if qty_ship_t else None
            um = um_t["text"].upper() if um_t else ""

            desc_toks = [t for t in row if _in_x(t, _EXC_DESC_X_RANGE)
                         and not _EXC_PRICE_RE.fullmatch(t["text"])
                         and not _EXC_UM_RE.fullmatch(t["text"])
                         and not _EXC_ITEM_CODE_RE.fullmatch(t["text"])]
            description = " ".join(t["text"] for t in desc_toks).strip()
            if not description:
                description = f"[Exceptional #{code}]"

            # Sean 2026-05-03: same bug pattern as Farm Art — was setting
            # unit_price = extended (line total) when local `unit_price` is
            # the actual per-unit value from the U/P column. For catch-weight
            # this is $/lb; for non-catch-weight CASE/EA it's per-case/per-EA.
            # Either way, ILI.unit_price should be per-unit and ext is the
            # line total. Fixed so line-math validation works + downstream
            # consumers (synergy_sync) get consistent semantics with Farm Art.
            item = {
                "raw_description": description,
                "sysco_item_code": "",
                "unit_price": unit_price,
                "extended_amount": extended,
                "case_size_raw": um or "",
                "section": "",
            }
            # Catch-weight: per-unit U/M is "LB" and per-lb price.
            #
            # Sean 2026-05-03: when spatial can't find the U/P column token
            # (band 0.78-0.85 misses it for some catch-weight layouts) the
            # unit_price defaults to extended — then both fields hold the
            # line total, NOT the per-lb price. Beef 42.7 Chuck Flap stored
            # unit_price=$197.53 (= ext) instead of $/lb=$10.98 (= 197.53/17.99).
            #
            # Self-correction: for catch-weight rows, derive per-lb from
            # ext / qty_shipped. This is always reliable when both are present
            # (qty is the shipped weight, ext is what Sean paid). Overrides the
            # potentially-wrong U/P column extraction for catch-weight only —
            # non-catch-weight rows still use the extracted U/P value.
            #
            # Phase 2b: emit structured fields. quantity = shipped lbs (matches
            # Sysco catch-weight convention). purchase_uom = "LB". Also write
            # case_total_weight_lb + the case_pack_* triple (count=1 for
            # single-shipment catch-weight).
            if per_um == "LB" and qty_shipped is not None and qty_shipped > 0:
                # If unit_price was extracted cleanly from U/P column it
                # differs from extended (line total); trust it. If it's
                # suspiciously equal to extended (means U/P column wasn't
                # extracted — the local var defaulted to extended at line
                # 581), derive per-lb from ext÷qty as the corrective.
                final_per_lb = unit_price
                if abs(unit_price - extended) < 0.01:
                    derived_per_lb = round(extended / qty_shipped, 4)
                    if derived_per_lb > 0:
                        final_per_lb = derived_per_lb
                item["unit_price"] = final_per_lb
                item["unit_of_measure"] = "LB"
                item["purchase_uom"] = "LB"
                item["price_per_unit"] = final_per_lb
                item["case_size_raw"] = f"{qty_shipped}LB"
                item["quantity"] = qty_shipped
                item["case_total_weight_lb"] = round(qty_shipped, 3)
                item["case_pack_count"] = 1
                item["case_pack_unit_size"] = str(round(qty_shipped, 3))
                item["case_pack_unit_uom"] = "LB"
            else:
                # Non-catch-weight: order-unit qty (1.00 EA / 1.00 CS).
                # qty_shipped column may carry the case count for non-LB rows;
                # fall back to qty_shipped→qty when present, else leave NULL.
                if qty_shipped is not None:
                    item["quantity"] = qty_shipped
                if um:
                    item["purchase_uom"] = um

                # Sean 2026-05-03 phase B: catch-weight rows where per_um
                # extraction missed "LB" still get the same unit_price=ext
                # bug signature. When qty_shipped > 1 AND unit_price ≈
                # extended, derive per-unit from ext÷qty_shipped. Catches
                # Beef Philly, Bacon Applewood, Ground Turkey Sage Pattie
                # — all catch-weight with per_um not detected.
                #
                # Gate qty_shipped > 1 (not > 0): when qty_shipped=1 and
                # unit_price=ext, that's a legitimate single-unit case
                # where ext IS the per-unit price (no division needed).
                if (qty_shipped is not None and qty_shipped > 1
                        and abs(unit_price - extended) < 0.01):
                    derived_per_unit = round(extended / qty_shipped, 4)
                    if derived_per_unit > 0 and derived_per_unit != unit_price:
                        item["unit_price"] = derived_per_unit

            # Phase 3 #6: count-per-lb (BACON L/O 10/14, SHRIMP 21/25)
            try:
                from parser import _extract_count_per_lb
                cpl = _extract_count_per_lb(description)
                if cpl is not None:
                    item["count_per_lb_low"] = cpl[0]
                    item["count_per_lb_high"] = cpl[1]
            except Exception:
                pass
            # Catch-weight aware math validation. Exceptional stores line-total
            # in unit_price for catch-weight rows; validate_line_math uses ppp
            # when populated so those rows reconcile via qty × ppp ≈ ext rather
            # than the false-positive qty × unit_price.
            validate_line_math(item, vendor='Exceptional Foods')
            items.append(item)
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Delaware County Linen
# ═══════════════════════════════════════════════════════════════════════════

# Observed layout from 2026-04-15 invoice:
#   x=0.11  qty (3-digit integer)
#   x=0.16  item code (MOPS, BAPSWT — 3-6 letters)
#   x=0.24-0.40  description tokens
#   x=0.66  unit price (decimal)
#   x=0.76  amount (decimal, possibly with 'T' suffix for taxable)

_DEL_ITEM_CODE_RE = re.compile(r'^[A-Z]{3,8}$')
_DEL_QTY_RE = re.compile(r'^\d{1,4}$')
_DEL_PRICE_RE = re.compile(r'^\$?\d+\.\d{2}T?$')  # T suffix = taxable

_DEL_QTY_X_RANGE  = (0.08, 0.15)
_DEL_CODE_X_RANGE = (0.14, 0.22)
_DEL_DESC_X_RANGE = (0.20, 0.55)
_DEL_UNIT_X_RANGE = (0.60, 0.72)
_DEL_AMT_X_RANGE  = (0.72, 0.90)


def match_delaware_spatial(pages: list[dict]) -> list[dict]:
    """Extract Delaware County Linen items from bounding-box layout.

    Small-volume vendor (~5-10 items per invoice). Layout is simple but
    1D parsing sometimes misses when OCR splits rows unpredictably."""
    items: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        rows = _group_rows(tokens, tol=0.008)
        for row in rows:
            # Anchor: integer qty in left band
            qty_toks = [t for t in row if _in_x(t, _DEL_QTY_X_RANGE)
                        and _DEL_QTY_RE.fullmatch(t["text"])]
            if not qty_toks:
                continue
            # And a price-shaped token in the amount band
            amt_toks = [t for t in row if _in_x(t, _DEL_AMT_X_RANGE)
                        and _DEL_PRICE_RE.fullmatch(t["text"])]
            if not amt_toks:
                continue

            qty = int(qty_toks[0]["text"])
            amt_text = amt_toks[0]["text"].rstrip("T").lstrip("$")
            amount = float(amt_text)

            code_toks = [t for t in row if _in_x(t, _DEL_CODE_X_RANGE)
                         and _DEL_ITEM_CODE_RE.fullmatch(t["text"])]
            code = code_toks[0]["text"] if code_toks else ""

            unit_toks = [t for t in row if _in_x(t, _DEL_UNIT_X_RANGE)
                         and _DEL_PRICE_RE.fullmatch(t["text"])]
            unit_price = float(unit_toks[0]["text"].rstrip("T").lstrip("$")) \
                         if unit_toks else (amount / qty if qty else amount)

            desc_toks = [t for t in row if _in_x(t, _DEL_DESC_X_RANGE)
                         and not _DEL_PRICE_RE.fullmatch(t["text"])
                         and not _DEL_ITEM_CODE_RE.fullmatch(t["text"])
                         and not _DEL_QTY_RE.fullmatch(t["text"])]
            description = " ".join(t["text"] for t in desc_toks).strip()
            if not description and code:
                description = code
            if not description:
                continue  # Row with no desc and no code — probably fee row

            # Sean 2026-05-03: skip surcharge / delivery-fee rows.
            # Delaware spatial extracts these with default unit_price=$1.00
            # but actual ext varies ($0.76 for fuel surcharge), so they
            # always fail line-math validation. They're not real product
            # lines — mapper would tag as non_product anyway.
            desc_lower = description.lower()
            if any(p in desc_lower for p in
                   ('fuel surcharge', 'surcharge', 'delivery fee',
                    'delivery cha', 'fuelsurcharge')):
                continue

            # Phase 2 polish: Delaware items are sold per-piece (towels,
            # mops, aprons billed by count). Default purchase_uom='EA'.
            del_item = {
                "raw_description": description,
                "sysco_item_code": "",
                "unit_price": unit_price,
                "extended_amount": amount,
                "case_size_raw": "",
                "section": "",
                "quantity": qty,
                "purchase_uom": "EA",
                "unit_of_measure": "EA",
            }
            validate_line_math(del_item, vendor='Delaware County Linen')
            items.append(del_item)
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Farm Art
# ═══════════════════════════════════════════════════════════════════════════

# Observed layout from 2026-04-02 invoice:
#   x=0.07  qty ordered (decimal "1.000")
#   x=0.12  qty shipped (decimal "1.000")
#   x=0.16  U/M (EACH, CASE, LB)
#   x=0.20  item code (CRESC, EGG, GRR, JUIOJCG — 3-8 letters)
#   x=0.27-0.55  description + pack tokens
#   x=0.70  COOL (country of origin, e.g. "United States") — skip for desc
#   x=0.83  unit price
#   x=0.90  extended amount

_FARM_ITEM_CODE_RE = re.compile(r'^[A-Z]{2,10}\d?$')
# U/M extraction regex — captures the actual U/M column value (in the U/M
# x-band). Sean 2026-05-03 extended to include GAL/QT/PT/CT/BU/DOZ which
# appear on real Farm Art invoices (Shallot=GAL, Heavy Cream at gallon
# pack=CASE, etc.).
_FARM_UM_RE = re.compile(
    r'^(EACH|CASE|LB|EA|DZ|OZ|PK|BG|CTN|GAL|QT|PT|CT|BU|DOZ)$',
    re.IGNORECASE,
)
# Description-token noise filter — backward-compat with the original
# spatial extraction. These tokens get stripped from desc when they
# appear in the desc x-band. MUST match the original _FARM_UM_RE
# (pre-2026-05-03 expansion) so retroactive spatial re-extracts produce
# raw_description identical to what was originally stored.
#
# Critically does NOT include GAL/QT/PT/CT/BU/DOZ — those appear as
# legitimate size indicators in desc text ("4 / 1 - GAL" Shallot,
# "12/1 QT" Heavy Cream) and the original spatial code preserved them
# (because the original U/M regex didn't include them either).
_FARM_DESC_NOISE_RE = re.compile(
    r'^(EACH|CASE|LB|EA|DZ|OZ|PK|BG|CTN)$',
    re.IGNORECASE,
)
_FARM_PRICE_RE = re.compile(r'^\$?\d+\.\d{2,4}$')
_FARM_DEC_RE = re.compile(r'^\d+\.\d{3}$')  # qty format "1.000"

_FARM_QTY_ORD_X   = (0.04, 0.11)
_FARM_QTY_SHP_X   = (0.10, 0.16)
# U/M column observed at x=0.133 on the 2026-05-01 invoice (just LEFT of the
# original 0.14-0.20 band). Loosened lower bound to 0.12 so tokens at x=0.13
# get captured. Overlaps with qty_shp band but disambiguated by regex
# (qty_shp matches '1.000', U/M matches alpha tokens).
_FARM_UM_X        = (0.12, 0.20)
_FARM_CODE_X      = (0.18, 0.28)
_FARM_DESC_X      = (0.26, 0.68)
_FARM_COOL_X      = (0.68, 0.80)
_FARM_UNIT_X      = (0.80, 0.88)
_FARM_EXT_X       = (0.88, 0.96)


def match_farmart_spatial(pages: list[dict]) -> list[dict]:
    """Extract Farm Art (FarmArt) invoice items from bounding-box layout.

    Per Sean: Farm Art invoices are fully PRINTED, not handwritten — parse
    gaps are layout-structural (broken-case items, unusual multi-line
    wraps) rather than OCR-quality. Spatial should bring Farm Art to
    parity with Sysco on parse accuracy.

    Farm Art has no SUPC code; its per-product identifiers are short
    2-8 letter codes (CRESC, EGG, GRR, JUIOJCG). We keep these as
    raw_description prefix — the mapper fuzzy-matches against existing
    desc_map entries."""
    items: list[dict] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        rows = _group_rows(tokens, tol=0.010)
        for row in rows:
            # Farm Art items have qty ORDERED at far-left and a price at
            # far-right. Both are required to qualify as an item row.
            qty_ord_toks = [t for t in row if _in_x(t, _FARM_QTY_ORD_X)
                            and _FARM_DEC_RE.fullmatch(t["text"])]
            if not qty_ord_toks:
                continue
            ext_toks = [t for t in row if _in_x(t, _FARM_EXT_X)
                        and _FARM_PRICE_RE.fullmatch(t["text"])]
            if not ext_toks:
                continue

            extended = float(ext_toks[0]["text"].lstrip("$"))
            unit_toks = [t for t in row if _in_x(t, _FARM_UNIT_X)
                         and _FARM_PRICE_RE.fullmatch(t["text"])]
            unit_price = float(unit_toks[0]["text"].lstrip("$")) \
                         if unit_toks else extended

            qty_ord = float(qty_ord_toks[0]["text"])
            qty_shp_toks = [t for t in row if _in_x(t, _FARM_QTY_SHP_X)
                            and _FARM_DEC_RE.fullmatch(t["text"])
                            and t != qty_ord_toks[0]]
            qty_shipped = float(qty_shp_toks[0]["text"]) if qty_shp_toks else qty_ord
            # Sean 2026-05-02: skip rows where extended (billed amount) is 0.
            # Farm Art uses zz prefix for out-of-stock items; the row appears
            # on invoice paperwork but with no money paid. ext=0 means the
            # line wasn't billed regardless of qty value (some "ordered but
            # not delivered" rows have qty>0 but ext=0 — zz BAKING YEAST,
            # zz SPICE CUMIN, etc.). No money paid = no ILI row needed.
            # Note: zz alone isn't disqualifying — fulfilled-substitution
            # zz items have real qty + ext and should generate ILI rows.
            if extended == 0:
                continue

            um_toks = [t for t in row if _in_x(t, _FARM_UM_X)
                       and _FARM_UM_RE.fullmatch(t["text"])]
            um = um_toks[0]["text"].upper() if um_toks else ""

            # Description: everything in the desc x-band that isn't COOL
            # (country name) or a recognizable numeric/noise token.
            # Uses _FARM_DESC_NOISE_RE (narrower than _FARM_UM_RE) so size
            # tokens like GAL/QT in description text stay (e.g. "4 / 1 - GAL"
            # for Shallot). Only business-note words like CASE/EACH get
            # filtered as noise.
            desc_toks = [t for t in row if _in_x(t, _FARM_DESC_X)
                         and not _FARM_PRICE_RE.fullmatch(t["text"])
                         and not _FARM_DEC_RE.fullmatch(t["text"])
                         and not _FARM_DESC_NOISE_RE.fullmatch(t["text"])]
            description = " ".join(t["text"] for t in desc_toks).strip()
            # Strip leading comma if desc starts with one (OCR noise)
            description = re.sub(r'^,\s*', '', description)
            if not description:
                continue

            # Phase 2c (2026-05-02): structured Farm Art emit.
            # U/M (EACH/CASE/LB) lands in purchase_uom — it's the order unit,
            # NOT the case size.
            #
            # Phase 3 followup (2026-05-02): pack-size from raw_description.
            # `_extract_farmart_pack` parses "4/1GAL", "9CT", "15DOZ", "5#"
            # tokens out of the description into structured fields — closes
            # the 0% case_pack_count gap for Farm Art's 556 ILI rows.
            # Sean 2026-05-03: unit_price MUST be the per-unit price from the
            # U/P column, not the line amount. For qty=1 rows the two are
            # equal (modulo rounding) so the bug was invisible. For qty>1
            # rows (Romaine 5 heads × $3.46 = $17.33; Cilantro 2 bunches ×
            # $0.99 = $1.98), using `extended` as unit_price overstated
            # per-unit price by qty× and broke calc_iup.
            #
            # Build item dict, then run shared validation with
            # try_self_correct=True. The helper mutates item['quantity']
            # on successful self-correction; sets math_flagged on real
            # anomaly. Catch-weight aware (uses ppp when populated).
            item = {
                "raw_description": description,
                "sysco_item_code": "",
                "unit_price": unit_price,
                "extended_amount": extended,
                "case_size_raw": "",
                "section": "",
                "quantity": qty_shipped,
                "purchase_uom": um,
                "unit_of_measure": um,
            }
            try:
                from parser import _extract_farmart_pack
                item.update(_extract_farmart_pack(description))
            except Exception:
                pass
            validate_line_math(item, vendor='Farm Art', try_self_correct=True)
            items.append(item)
    return items
