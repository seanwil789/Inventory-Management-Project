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


def _find_sections(rows: list[list[dict]]) -> list[tuple[float, str]]:
    """Detect section headers (lines containing '****...****' or bracketed
    section names). Returns [(y_center, name)] in y-order."""
    sections: list[tuple[float, str]] = []
    for row in rows:
        texts = [t["text"] for t in row]
        joined = " ".join(texts)
        if _SECTION_HDR_RE.search(joined):
            # Strip the asterisks and keep the inner label
            label = re.sub(r'\*+', '', joined).strip()
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


# ── Line-item math validation helper (Sean 2026-05-03) ─────────────────────
#
# Per Sean's completeness LAW: validate every parsed line. qty × unit_price
# should ≈ extended_amount within tolerance. When math fails, surface the
# anomaly so it can be audited (real billing variance, parser bug, OCR
# misread). This helper is vendor-agnostic — call from each match_*_spatial
# function with the extracted (qty, unit_price, extended) triple.
#
# Returns dict with diagnostic info (also logs to stdout when anomaly found).

def _validate_line_math(vendor: str, description: str,
                         qty: float, unit_price: float, extended: float,
                         tolerance_pct: float = 5.0,
                         tolerance_abs: float = 2.0,
                         try_self_correct: bool = False) -> dict:
    """Check qty × unit_price ≈ extended. Log anomaly when both >tolerance_pct
    AND >tolerance_abs (avoids noise on rounding/discount under either bar).

    When try_self_correct=True and math fails, attempts to derive a corrected
    qty from extended / unit_price. If the ratio rounds to a clean small
    integer (1-50, within 0.10 of integer), returns it as 'corrected_qty'
    in the result. Caller decides whether to apply.

    Returns dict:
      ok: bool             — True when math passes within tolerance
      diff_pct: float      — percent discrepancy
      diff_abs: float      — absolute discrepancy in dollars
      expected: float      — qty × unit_price
      corrected_qty: float — set only when self-correction succeeds
    """
    out = {'ok': True, 'diff_pct': 0, 'diff_abs': 0, 'expected': 0}
    if not (qty and unit_price and extended) or qty <= 0 or unit_price <= 0:
        return out
    expected = qty * unit_price
    if expected <= 0:
        return out
    diff_abs = abs(extended - expected)
    diff_pct = diff_abs / expected * 100
    ok = not (diff_pct > tolerance_pct and diff_abs > tolerance_abs)
    out.update({'ok': ok, 'diff_pct': diff_pct, 'diff_abs': diff_abs,
                'expected': expected})
    if ok:
        return out

    # Math failed — try self-correction if requested
    if try_self_correct:
        derived_raw = extended / unit_price
        derived = round(derived_raw)
        if (1 <= derived <= 50
                and derived != qty
                and abs(derived_raw - derived) < 0.10):
            corrected_expected = derived * unit_price
            corrected_diff_pct = abs(extended - corrected_expected) / corrected_expected * 100
            if corrected_diff_pct < tolerance_pct:
                out['corrected_qty'] = float(derived)
                print(f"  [✓] {vendor} qty self-corrected: "
                      f"{description[:40]!r} qty {qty}→{derived} "
                      f"(ext=${extended:.2f} / U/P=${unit_price:.2f} "
                      f"= {derived_raw:.2f})")
                return out

    print(f"  [!] {vendor} line-math anomaly: {description[:40]!r} "
          f"qty={qty} × unit_price=${unit_price:.2f} = ${expected:.2f} "
          f"but extended=${extended:.2f} "
          f"(Δ={diff_pct:.0f}%, ${diff_abs:.2f})")
    return out


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
                # Validate qty × unit_price ≈ extended (vendor-agnostic check)
                _validate_line_math(
                    'Sysco', item.get('raw_description', ''),
                    item.get('quantity') or 0,
                    item.get('unit_price') or 0,
                    item.get('extended_amount') or 0,
                )
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

_PBM_ITEM_CODE_RE = re.compile(r'^[A-Z]?\d{3,5}$')
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
        # PBM rows are ~0.013 apart — tighter tol than Sysco so consecutive
        # line items don't get merged into one row.
        rows = _group_rows(tokens, tol=0.006)
        for row in rows:
            # Row must have an item-code-looking token in the far-left band
            code_toks = [t for t in row if _in_x(t, _PBM_CODE_X_RANGE)
                         and _PBM_ITEM_CODE_RE.fullmatch(t["text"])]
            if not code_toks:
                continue
            # Plus at least one price-shaped token in the ext-amount band
            ext_toks = [t for t in row if _in_x(t, _PBM_EXT_X_RANGE)
                        and _PBM_PRICE_RE.fullmatch(t["text"])]
            if not ext_toks:
                continue

            code = code_toks[0]["text"]
            extended = float(ext_toks[0]["text"].lstrip("$"))
            unit_toks = [t for t in row if _in_x(t, _PBM_UNIT_X_RANGE)
                         and _PBM_PRICE_RE.fullmatch(t["text"])]
            unit_price = float(unit_toks[0]["text"].lstrip("$")) if unit_toks else extended

            qty_toks = [t for t in row if _in_x(t, _PBM_QTY_X_RANGE)
                        and _PBM_PRICE_RE.fullmatch(t["text"])]
            qty = float(qty_toks[0]["text"]) if qty_toks else None

            um_toks = [t for t in row if _in_x(t, _PBM_UM_X_RANGE)
                       and _PBM_UM_RE.fullmatch(t["text"])]
            um = um_toks[0]["text"].upper() if um_toks else ""

            desc_toks = [t for t in row if _in_x(t, _PBM_DESC_X_RANGE)
                         and not _PBM_PRICE_RE.fullmatch(t["text"])
                         and not _PBM_UM_RE.fullmatch(t["text"])]
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
            check = _validate_line_math(
                'PBM', item.get('raw_description', ''),
                item.get('quantity') or 0,
                item.get('unit_price') or 0,
                item.get('extended_amount') or 0,
                try_self_correct=True,
            )
            if 'corrected_qty' in check:
                item['quantity'] = check['corrected_qty']
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

_EXC_CODE_X_RANGE    = (0.04, 0.14)
_EXC_QTY_X_RANGE     = (0.20, 0.26)
_EXC_UM_X_RANGE      = (0.26, 0.30)
_EXC_DESC_X_RANGE    = (0.28, 0.68)
_EXC_QTY_SHIP_X      = (0.68, 0.78)
_EXC_UNIT_X_RANGE    = (0.78, 0.85)
_EXC_PER_UM_X_RANGE  = (0.84, 0.90)
_EXC_EXT_X_RANGE     = (0.90, 0.98)


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
        # Exceptional items span ~0.010 of y-space (code sits below its row's
        # extended amount). 0.012 keeps them together without merging into
        # neighbor items (~0.029 spacing).
        rows = _group_rows(tokens, tol=0.012)
        for row in rows:
            code_toks = [t for t in row if _in_x(t, _EXC_CODE_X_RANGE)
                         and _EXC_ITEM_CODE_RE.fullmatch(t["text"])]
            if not code_toks:
                continue
            ext_toks = [t for t in row if _in_x(t, _EXC_EXT_X_RANGE)
                        and _EXC_PRICE_RE.fullmatch(t["text"])]
            if not ext_toks:
                continue

            code = code_toks[0]["text"]
            extended = float(ext_toks[0]["text"].lstrip("$"))
            unit_toks = [t for t in row if _in_x(t, _EXC_UNIT_X_RANGE)
                         and _EXC_PRICE_RE.fullmatch(t["text"])]
            unit_price = float(unit_toks[0]["text"].lstrip("$")) if unit_toks else extended

            per_um_toks = [t for t in row if _in_x(t, _EXC_PER_UM_X_RANGE)
                           and _EXC_UM_RE.fullmatch(t["text"])]
            per_um = per_um_toks[0]["text"].upper() if per_um_toks else ""

            qty_ship_toks = [t for t in row if _in_x(t, _EXC_QTY_SHIP_X)
                             and _EXC_PRICE_RE.fullmatch(t["text"])]
            qty_shipped = float(qty_ship_toks[0]["text"]) if qty_ship_toks else None

            um_toks = [t for t in row if _in_x(t, _EXC_UM_X_RANGE)
                       and _EXC_UM_RE.fullmatch(t["text"])]
            um = um_toks[0]["text"].upper() if um_toks else ""

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
            # Catch-weight: per-unit U/M is "LB" and unit_price is $/lb.
            # Promote it to price_per_unit; the line total stays as
            # unit_price/extended_amount so DB dollar totals balance.
            #
            # Phase 2b: emit structured fields. quantity = shipped lbs (matches
            # Sysco catch-weight convention). purchase_uom = "LB". Also write
            # case_total_weight_lb + the case_pack_* triple (count=1 for
            # single-shipment catch-weight).
            if per_um == "LB" and qty_shipped is not None:
                item["unit_of_measure"] = "LB"
                item["purchase_uom"] = "LB"
                item["price_per_unit"] = unit_price
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

            # Phase 3 #6: count-per-lb (BACON L/O 10/14, SHRIMP 21/25)
            try:
                from parser import _extract_count_per_lb
                cpl = _extract_count_per_lb(description)
                if cpl is not None:
                    item["count_per_lb_low"] = cpl[0]
                    item["count_per_lb_high"] = cpl[1]
            except Exception:
                pass
            # Validate qty × unit_price ≈ extended. Note: Exceptional sets
            # unit_price=extended for catch-weight rows (item totals match
            # by design); validation will pass trivially in those cases.
            # For non-catch-weight per-CASE rows it does the real check.
            _validate_line_math(
                'Exceptional', item.get('raw_description', ''),
                item.get('quantity') or 0,
                item.get('unit_price') or 0,
                item.get('extended_amount') or 0,
            )
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
            _validate_line_math(
                'Delaware', description, qty, unit_price, amount,
            )
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
            # Use the shared validation helper with try_self_correct=True.
            # When math fails AND ext/unit_price rounds to a clean small
            # integer, the helper returns corrected_qty — we apply it to
            # qty_shipped before constructing the item dict.
            check = _validate_line_math(
                'Farm Art', description, qty_shipped, unit_price, extended,
                try_self_correct=True,
            )
            if 'corrected_qty' in check:
                qty_shipped = check['corrected_qty']
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
            items.append(item)
    return items
