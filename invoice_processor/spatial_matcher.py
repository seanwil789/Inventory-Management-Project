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


def _group_rows(tokens: list[dict]) -> list[list[dict]]:
    """Cluster tokens into rows by y-midpoint. Each row is a list of tokens
    sorted left-to-right by x_min.

    Row boundaries are determined by running mean: a token joins the
    current row if its y-center is within _ROW_Y_TOL of the row's mean
    y-center so far. This avoids anchoring to an outlier first token
    (e.g. a footer-wrap token with an unusual y) that would cause the
    row to truncate early and drop its description tail."""
    if not tokens:
        return []
    sorted_toks = sorted(tokens, key=_y_mid)
    rows: list[list[dict]] = []
    current: list[dict] = [sorted_toks[0]]
    current_sum = _y_mid(sorted_toks[0])
    for t in sorted_toks[1:]:
        y = _y_mid(t)
        mean = current_sum / len(current)
        if abs(y - mean) <= _ROW_Y_TOL:
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
    #   - drop standalone qty/marker tokens ("D", "1 CS", "2 CS", etc.)
    #   - drop bare integers 1-9 (quantity counts)
    #   - drop price-shaped and SUPC-shaped tokens
    desc_tokens = []
    for t in row:
        if t["x_min"] < _DESC_X_MIN or t["x_min"] >= anchor["x_min"]:
            continue
        tx = t["text"]
        if _PRICE_RE.fullmatch(tx) or _SUPC_RE.fullmatch(tx):
            continue
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
    m = re.search(
        r'\b(\d+(?:\.\d+)?)\s*(?:([O0]Z|LB|GAL|CT|EA|KG|ML|QT|GM|#\d+)|(L)\b)',
        description, re.IGNORECASE)
    if m:
        num = m.group(1)
        unit = (m.group(2) or m.group(3) or "").upper().replace("0Z", "OZ")
        raw = f"{num}{unit}"
        try:
            from parser import _normalize_pack_size
            case_size = _normalize_pack_size(raw)
        except Exception:
            case_size = raw
    # Fallback: slash-format like "4/50OZ", "24/12 OZ", "1/22LB"
    if not case_size:
        m = re.search(
            r'\b(\d+\s*/\s*\d+\s*(?:OZ|LB|GAL|CT|EA|KG|ML|L|#10|QT|GM|KT))\b',
            description, re.IGNORECASE)
        if m:
            case_size = re.sub(r'\s+', '', m.group(1)).upper()

    item = {
        "raw_description":  description or f"[Sysco #{anchor['text']}]",
        "sysco_item_code":  anchor["text"],
        "unit_price":       unit_price,
        "extended_amount":  unit_price,
        "case_size_raw":    case_size,
        "section":          section_name,
    }
    if price_per_unit is not None:
        item["unit_of_measure"] = "LB"
        item["price_per_unit"] = price_per_unit
    return item


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
                items.append(item)
    return items
