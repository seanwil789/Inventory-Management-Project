"""Section-level reconciliation for Sysco invoices.

Each Sysco invoice carries its own ground truth: every section ends with a
printed `GROUP TOTAL` line whose value equals the sum of that section's
items. Comparing the parser's section sums against the printed GROUP TOTALs
identifies extraction failures at section granularity — without needing
external paper-comparison.

Pipeline:
  1. extract_sections(pages)      → [(y, section_name)]    via spatial_matcher
  2. extract_group_totals(pages)  → [(y, value)]           token-based, this module
  3. pair_sections_to_totals(sections, totals) → {section_name: printed_total}
  4. reconcile(parsed_items, printed_totals)   → per-section diffs

Token layout observed on real Sysco invoices (cache 6fe30512488f, INV
775619701, 2026-01-13):
  GROUP@x≈0.41 | TOTAL@x≈0.45 | ****@x≈0.48 | <VALUE>@x≈0.78
All four tokens share a y-coordinate — same row.
"""
from __future__ import annotations
import re
from collections import defaultdict


# Decimal token shape: "326.82", "$1,234.56", "105.30*". Allow optional
# dollar sign, comma thousands, and trailing '*' (Sysco uses '*' to flag
# special values like discounts).
_DECIMAL_RE = re.compile(r"^\$?[\d,]+\.\d{2}\*?$")

# Right-column x-band — Extended Price column on Sysco. Values for both
# GROUP TOTALs and individual line ext amounts land here; we filter to
# GROUP TOTAL rows by requiring the row to also contain "GROUP" + "TOTAL"
# tokens, not by x-position alone.
_RIGHT_COL_X_MIN = 0.70


def _y_mid(t: dict) -> float:
    return (t["y_min"] + t["y_max"]) / 2


def _x_mid(t: dict) -> float:
    return (t["x_min"] + t["x_max"]) / 2


def _parse_decimal(text: str) -> float | None:
    s = text.lstrip("$").rstrip("*").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_group_totals(pages: list[dict]) -> list[tuple[float, float]]:
    """Find every GROUP TOTAL row and its printed value.

    Returns [(y_mid, value), ...] in y-order. The y is the row's y-mid;
    callers pair to sections by "most-recent section header above this y".
    Empty list when no GROUP TOTAL rows are found (header-only pages,
    pages that don't have a section ending on them).
    """
    try:
        from spatial_matcher import _group_rows
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for page in pages or []:
        tokens = page.get("tokens") or []
        if not tokens:
            continue
        rows = _group_rows(tokens)
        for row in rows:
            texts_upper = [t["text"].upper() for t in row]
            if "GROUP" not in texts_upper or "TOTAL" not in texts_upper:
                continue
            # Find the right-column decimal token in this row.
            value = None
            for t in row:
                if _x_mid(t) >= _RIGHT_COL_X_MIN and _DECIMAL_RE.match(t["text"]):
                    v = _parse_decimal(t["text"])
                    if v is not None:
                        value = v
                        break
            if value is None:
                continue
            y = _y_mid(row[0])
            out.append((y, value))
    out.sort(key=lambda r: r[0])
    return out


def _find_footer_y(tokens: list[dict]) -> float:
    """B2c-2 (2026-05-07): find the y where the totals footer begins.

    Sysco invoices print a "SUB TOTAL", "TAX TOTAL", or "INVOICE TOTAL"
    label below the last section's items. The right-column decimals
    BELOW this y are the printed totals — NOT items belonging to any
    section. Without this boundary, `extract_section_totals_by_max` for
    the LAST section on a page picks up the INVOICE TOTAL (~$1000+) as
    its max, producing huge spurious section diffs.

    Detection requires BOTH a "SUB"/"TAX" label token AND a "TOTAL" token
    at similar y, in the BOTTOM half of the page (y > 0.5). Restricts to
    the bottom half so the page-header "INVOICE NUMBER" tokens at top of
    page don't get picked up as the footer marker (the original bug —
    that swallowed every section's range).

    Returns the y of the earliest detected footer-marker pair, or
    float('inf') when no footer markers found.
    """
    # Find candidate label tokens in bottom half
    label_tokens = []
    total_tokens = []
    for t in tokens:
        ty = _y_mid(t)
        if ty < 0.5:  # only look in bottom half
            continue
        text = (t.get("text") or "").upper()
        if text in ("SUB", "TAX"):
            label_tokens.append(t)
        elif text == "TOTAL":
            total_tokens.append(t)
    # Require both a label and a TOTAL token at compatible y (within 0.05)
    footer_y = float("inf")
    for label in label_tokens:
        ly = _y_mid(label)
        for total in total_tokens:
            ty = _y_mid(total)
            if abs(ly - ty) < 0.05:
                # Found a "SUB/TAX TOTAL" pair — use the earlier y
                pair_y = min(ly, ty)
                if pair_y < footer_y:
                    footer_y = pair_y
                break
    return footer_y


# ── Non-item charges extraction (FUEL / CC / TAX) ───────────────────────────
# Sysco invoices print MISC CHARGES (fuel surcharge, CC processing fee) and
# TAX TOTAL as labeled rows in the bottom-right totals block of the LAST
# PAGE. parse_invoice uses these to populate parsed['non_item_charges'],
# closing the gap between items_sum and invoice_total without relying on
# the gap-derivation 8% cap (which suppresses for invoices with > 8%
# real underextraction, leaving real fees unaccounted).

_FEE_PRICE_RE = re.compile(r'^\d+\.\d{2}$')


def _value_for_label(
    tokens: list[dict],
    label_tokens: list[dict],
    max_dy: float = 0.005,
    min_x: float = 0.5,
) -> float | None:
    """Find the dollar amount on the same row as the label, right of it.

    Prices in the totals block stack vertically with ~0.014 row pitch, so
    max_dy=0.005 keeps each label bound to its own row. Ties broken by
    closest y, then leftmost x.
    """
    if not label_tokens:
        return None
    y_target = sum(_y_mid(t) for t in label_tokens) / len(label_tokens)
    x_max_label = max(_x_mid(t) for t in label_tokens)
    candidates = [
        (abs(_y_mid(t) - y_target), _x_mid(t), float(t['text']))
        for t in tokens
        if _FEE_PRICE_RE.fullmatch(t.get('text') or '')
        and abs(_y_mid(t) - y_target) < max_dy
        and _x_mid(t) > x_max_label
        and _x_mid(t) > min_x
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def _label_row(
    tokens: list[dict],
    words: set[str],
    anchor: str,
    max_dy: float = 0.005,
) -> list[dict]:
    """Return label tokens forming a row whose anchor word is present."""
    matches = [t for t in tokens
               if (t.get('text') or '').upper() in words]
    if not any((t.get('text') or '').upper() == anchor for t in matches):
        return []
    anchor_y = next(
        (_y_mid(t) for t in matches if (t.get('text') or '').upper() == anchor),
        None,
    )
    if anchor_y is None:
        return []
    return [t for t in matches if abs(_y_mid(t) - anchor_y) < max_dy]


def extract_sysco_fees(pages: list[dict]) -> dict:
    """Extract fuel surcharge / CC processing / tax from Sysco totals block.

    Returns dict with keys 'fuel_surcharge', 'cc_processing', 'tax' (only
    those found). Sysco prints these as labeled rows in the totals block at
    the bottom-right of the LAST PAGE cache.

    For multi-cache invoices (Sysco can have 2-4 photo caches per invoice),
    finds the page that contains the `LAST PAGE` marker token and uses
    that page's tokens for label-anchored extraction. Falls back to
    pages[-1] when no LAST PAGE marker is present (e.g. single-page
    invoices, or caller has already isolated the totals cache).

    Used by parse_invoice for Sysco to populate parsed['non_item_charges']
    directly from invoice labels, replacing the gap-derivation heuristic
    that's blocked by the 8% cap on invoices with real underextraction
    (e.g. INV 775687424 had $56.48 in real fees but gap-derivation
    suppressed because 13.6% > 8% cap).
    """
    if not pages:
        return {}

    # Find the cache page that contains the LAST PAGE marker — that's the
    # totals page. Without this, multi-photo invoices may have non-totals
    # pages at the end of `pages` by filesystem order; the wrong page would
    # miss the FUEL/MISC/TAX labels entirely.
    totals_page = None
    for p in pages:
        text = ' '.join((t.get('text') or '') for t in p.get('tokens') or [])
        if 'LAST PAGE' in text.upper():
            totals_page = p
            break
    if totals_page is None:
        totals_page = pages[-1]

    tokens = totals_page.get('tokens') or []
    if not tokens:
        return {}

    fees: dict = {}

    fuel_row = _label_row(tokens, {'FUEL', 'SURCHARGE'}, 'FUEL')
    if fuel_row:
        amt = _value_for_label(tokens, fuel_row)
        if amt is not None:
            fees['fuel_surcharge'] = amt

    cc_row = _label_row(tokens, {'CREDIT', 'CARD'}, 'CREDIT')
    if cc_row:
        amt = _value_for_label(tokens, cc_row)
        if amt is not None:
            fees['cc_processing'] = amt

    # TAX: bottom half only (column-header "TAX" tokens sit in upper-half).
    tax_tokens = [t for t in tokens
                  if (t.get('text') or '').upper() == 'TAX'
                  and _y_mid(t) > 0.5]
    if tax_tokens:
        # Lowest-y TAX is closest to INVOICE TOTAL block (the real fee).
        tax_tokens.sort(key=_y_mid, reverse=True)
        amt = _value_for_label(tokens, [tax_tokens[0]], max_dy=0.02)
        if amt is not None:
            fees['tax'] = amt

    return fees


def extract_section_totals_by_max(
    page: dict,
    sections: list[tuple[float, str]],
) -> dict[str, float]:
    """Robust per-page section totals extraction.

    Approach: for each section, the printed GROUP TOTAL is the LARGEST
    right-column decimal between this section's y and the next section's y.
    This works because:
      - Right-column decimals (x ≥ 0.70) are extended-amount values in
        Sysco's "Extended Price" column, which holds both per-line ext
        amounts AND the printed GROUP TOTAL.
      - GROUP TOTAL = sum of section's items, so it is always >= the
        largest single line ext in that section.
      - When OCR flattens the GROUP TOTAL row's value into an adjacent
        line item's row (the cache 2 case), this approach still picks
        the correct value because that flattened value is the section's
        max — by definition of GROUP TOTAL.

    B2c-2 (2026-05-07): the LAST section on a page would otherwise extend
    its search range to page-bottom, picking up SUB TOTAL / INVOICE TOTAL
    as its "max." `_find_footer_y` detects where the totals area begins
    and caps each section's search range at that y.

    Caller filters to "real" sections (excluding misclassified GROUP
    TOTAL rows) before invocation. Returns {section_name: total}.
    """
    tokens = page.get("tokens") or []
    if not tokens or not sections:
        return {}
    real_sections = [(y, name) for y, name in sections
                     if _is_real_section_label(name)]
    if not real_sections:
        return {}
    real_sections.sort(key=lambda s: s[0])

    footer_y = _find_footer_y(tokens)

    right_decimals: list[tuple[float, float]] = []
    for t in tokens:
        if _x_mid(t) < _RIGHT_COL_X_MIN:
            continue
        if not _DECIMAL_RE.match(t["text"]):
            continue
        v = _parse_decimal(t["text"])
        if v is None:
            continue
        right_decimals.append((_y_mid(t), v))

    out: dict[str, float] = {}
    for i, (sec_y, sec_name) in enumerate(real_sections):
        next_section_y = (real_sections[i + 1][0]
                          if i + 1 < len(real_sections)
                          else float("inf"))
        # Cap at footer marker — totals below this y belong to invoice
        # footer, not to this section.
        next_y = min(next_section_y, footer_y)
        in_range = [v for y, v in right_decimals if sec_y < y < next_y]
        if in_range:
            out[sec_name] = max(in_range)
    return out


# `_find_sections` matches any row containing `**` — but the printed
# `GROUP TOTAL ****  326.82` row also contains asterisks, so it gets
# emitted as a fake "section" with label like "GROUP TOTAL 326.82".
# Filter those out before pairing — a real section name doesn't contain
# the words GROUP or TOTAL.
def _is_real_section_label(label: str) -> bool:
    upper = (label or "").upper()
    if "GROUP" in upper or "TOTAL" in upper:
        return False
    return bool(label and label.strip())


def pair_sections_to_totals(
    sections: list[tuple[float, str]],
    group_totals: list[tuple[float, float]],
) -> dict[str, float]:
    """Each section's printed total = the first GROUP TOTAL appearing
    after the section header and before the next section header.

    Returns {section_name: printed_total}. Sections without a matching
    GROUP TOTAL are omitted (incomplete page, last section ran off the
    page, etc.). When a section header repeats (shouldn't happen, but
    defensive), the last occurrence wins — matches what the parser sees.
    """
    if not sections or not group_totals:
        return {}
    real_sections = [(y, name) for y, name in sections
                     if _is_real_section_label(name)]
    if not real_sections:
        return {}
    out: dict[str, float] = {}
    sections_sorted = sorted(real_sections, key=lambda s: s[0])
    for i, (sec_y, sec_name) in enumerate(sections_sorted):
        next_sec_y = (sections_sorted[i + 1][0]
                      if i + 1 < len(sections_sorted)
                      else float("inf"))
        for gt_y, gt_val in group_totals:
            if sec_y < gt_y < next_sec_y:
                out[sec_name] = gt_val
                break
    return out


def _is_plausible_group_total(printed: float, parser_sum: float) -> bool:
    """A real GROUP TOTAL is the SUM of items in the section, so it must
    be >= max(items_sum). If the "max in right column" we extracted is
    LESS than the parser's items_sum for the section, we haven't found
    the real GROUP TOTAL — the OCR likely didn't capture it (the value
    is at an x position outside the right-column band, or simply absent).

    Returns True when the printed value is plausibly the GROUP TOTAL,
    False when the validator should skip reconciliation for this section.
    """
    if printed is None or parser_sum is None:
        return False
    # Allow small floor tolerance (±$0.50) for rounding/discount artifacts.
    return printed >= parser_sum - 0.50


def compute_invoice_section_reconciliation(
    parsed_items: list[dict],
    pages: list[dict] | None,
    vendor: str = 'Sysco',
) -> list[dict]:
    """End-to-end section reconciliation for one invoice.

    Combines section detection, GROUP TOTAL extraction, and per-section
    diff into one call. Used by `parse_invoice` to surface section-level
    accuracy on every Sysco invoice (B5 — `project_parser_accuracy_goal.md`).

    Returns a list of per-section reconciliation dicts:
      [{section, parser_sum, printed_total, diff_abs, diff_pct, item_count}]

    Empty list when:
      - vendor isn't Sysco (other vendors don't have GROUP TOTAL structure)
      - pages is empty / no spatial data
      - no section headers detectable on the pages
    """
    if vendor != 'Sysco' or not pages:
        return []
    try:
        from spatial_matcher import (_group_rows, _find_sections,
                                      canonicalize_sysco_section,
                                      _CANONICAL_SYSCO_SECTIONS)
    except Exception:
        return []
    # Collect labeled and max-in-range totals separately across all pages.
    # Labeled = anchored to actual "GROUP TOTAL" row → high confidence.
    # Max-in-range = max decimal between section header and next header →
    # heuristic, can pick up adjacent section's GROUP TOTAL value when
    # OCR layout puts it at an unexpected y.
    #
    # Priority: any LABELED value across pages wins over max-in-range.
    # Real example: INV 775719979 — cache 825853eca2ef page 1 LABELED
    # PAPER & DISP=$44.75 (correct), cache 89f9568fadef page 0 max-in-range
    # PAPER & DISP=$416.37 (CANNED & DRY's value bleeding into PAPER's
    # range due to OCR layout). MAX-across-all gave $416.37; correct
    # priority gives $44.75.
    labeled_across_pages: dict[str, float] = {}
    max_in_range_across_pages: dict[str, float] = {}

    # B-Section-MultiPage fix (2026-05-10): collect ALL sections and ALL
    # group_totals across pages with page-adjusted y-coordinates, then pair
    # globally. Without this, a section header on page N and its GROUP TOTAL
    # on page N+1 never pair (per-page pairing finds neither side alone).
    # INV 775856655 reference: CANNED & DRY section starts on page 1 with
    # GROUP TOTAL printed on page 2 → pre-fix `printed_total=None`.
    # Page-adjusted y = page_idx + y_normalized (each page's y is in [0,1],
    # so adding the page index shifts ascendingly across the document).
    all_sections_global: list[tuple[float, str]] = []
    all_group_totals_global: list[tuple[float, float]] = []

    for page_idx, page in enumerate(pages):
        tokens = page.get('tokens') or []
        if not tokens:
            continue
        rows = _group_rows(tokens)
        secs = [(y, canon)
                for y, label in _find_sections(rows)
                for canon in [canonicalize_sysco_section(label)]
                if canon in _CANONICAL_SYSCO_SECTIONS]

        group_total_rows = extract_group_totals([page])
        labeled_totals = pair_sections_to_totals(secs, group_total_rows)
        max_totals = extract_section_totals_by_max(page, secs)

        for sec, val in labeled_totals.items():
            # Take MAX across pages of LABELED values — when a section
            # spans pages, the complete GROUP TOTAL is on the page that
            # completes it.
            labeled_across_pages[sec] = max(
                labeled_across_pages.get(sec, 0), val)
        for sec, val in max_totals.items():
            max_in_range_across_pages[sec] = max(
                max_in_range_across_pages.get(sec, 0), val)

        # B-Section-MultiPage: also collect with page-adjusted y for the
        # global cross-page pairing pass below. This rescues sections whose
        # GROUP TOTAL prints on a later page than the section header.
        for y, name in secs:
            all_sections_global.append((page_idx + y, name))
        for y, val in group_total_rows:
            all_group_totals_global.append((page_idx + y, val))

    # B-Section-MultiPage: global cross-page pairing. Only fills sections
    # NOT already labeled per-page — preserves the existing labeled-priority
    # over max-in-range, while adding cross-page coverage. Matches the
    # documented priority: labeled-anywhere > max-in-range.
    if len(pages) > 1:
        cross_page_labeled = pair_sections_to_totals(
            all_sections_global, all_group_totals_global)
        for sec, val in cross_page_labeled.items():
            if sec not in labeled_across_pages:
                labeled_across_pages[sec] = val

    all_printed: dict[str, float] = dict(labeled_across_pages)
    for sec, val in max_in_range_across_pages.items():
        if sec not in all_printed:
            all_printed[sec] = val
    if not all_printed:
        return []

    # B-NEW (2026-05-07): drop printed_totals that are LESS than parser_sum
    # for the section. A real GROUP TOTAL must equal the sum of items in
    # the section; if our "max-in-section" is less than parser_sum, we
    # haven't captured the real GROUP TOTAL (OCR likely missed it). Better
    # to leave the section un-reconciled than report a fake diff that
    # masquerades as an extraction bug.
    # B-CorruptSection IVS-side fix (2026-05-11): normalize parser-emitted
    # section labels through canonicalize_sysco_section before grouping.
    # Some extractor paths emit corrupt labels ("CANNED & DRY GROUP",
    # "DISPENSER BEVERAGE", "HAZARD") that pollute the section graph.
    # The db_write fix (commit 94d1813) handles this for stored ILI rows,
    # but validate_all_invoices re-parses from cache + computes section_recon
    # from parser items directly — bypassing db_write. Apply the same
    # normalization here so IVS classification reflects the fix.
    def _norm_sec(s: str) -> str:
        if not s:
            return ''
        try:
            from spatial_matcher import (canonicalize_sysco_section,
                                          _CANONICAL_SYSCO_SECTIONS as _CSS)
        except Exception:
            return s
        canon = canonicalize_sysco_section(s)
        if canon in _CSS:
            return canon
        upper = s.upper()
        if 'GROUP TOTAL' in upper or upper.startswith('TOTAL'):
            return ''
        return ''

    parser_by_section: dict = {}
    for it in parsed_items:
        sec = _norm_sec(it.get('section') or '')
        parser_by_section[sec] = parser_by_section.get(sec, 0) + (it.get('extended_amount') or 0)
    plausible_printed = {}
    for sec, val in all_printed.items():
        if _is_plausible_group_total(val, parser_by_section.get(sec, 0)):
            plausible_printed[sec] = val
    return reconcile(parsed_items, plausible_printed,
                      section_normalizer=_norm_sec)


def reconcile(
    parsed_items: list[dict],
    printed_totals: dict[str, float],
    *,
    section_normalizer=None,
) -> list[dict]:
    """Compare parser items grouped by section against printed totals.

    Returns a list of per-section diff dicts:
      {section, parser_sum, printed_total, diff_abs, diff_pct, item_count}

    `section_normalizer` is an optional callable applied to both the
    parser's `item['section']` and the printed_totals keys before lookup —
    use to bridge minor formatting differences (asterisks, casing) when
    different paths emit slightly different labels for the same section.
    """
    norm = section_normalizer or (lambda s: s)
    parser_by_section: dict[str, list[dict]] = defaultdict(list)
    for it in parsed_items:
        parser_by_section[norm(it.get("section") or "")].append(it)
    printed_norm = {norm(k): v for k, v in printed_totals.items()}

    out: list[dict] = []
    seen_sections = set()
    for sec_name, items in parser_by_section.items():
        seen_sections.add(sec_name)
        parser_sum = round(sum((it.get("extended_amount") or 0)
                                for it in items), 2)
        printed = printed_norm.get(sec_name)
        diff_abs = round(parser_sum - printed, 2) if printed is not None else None
        diff_pct = (round(diff_abs / printed * 100, 2)
                    if printed and printed != 0 else None)
        out.append({
            "section": sec_name,
            "parser_sum": parser_sum,
            "printed_total": printed,
            "diff_abs": diff_abs,
            "diff_pct": diff_pct,
            "item_count": len(items),
        })
    # Sections in printed_totals but not in parser output — pure misses.
    for sec_name, printed in printed_norm.items():
        if sec_name in seen_sections:
            continue
        out.append({
            "section": sec_name,
            "parser_sum": 0.0,
            "printed_total": printed,
            "diff_abs": -printed,
            "diff_pct": -100.0,
            "item_count": 0,
        })
    out.sort(key=lambda r: -abs(r.get("diff_abs") or 0))
    return out
