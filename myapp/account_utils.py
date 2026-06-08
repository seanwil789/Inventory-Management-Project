"""Department/account auto-suggestion (Sean 2026-06-08).

Suggests which Account an invoice belongs to from its line content — SUGGEST
only, never silent-write (avoids the confident-wrong-bulk-write trap; a regular
food order with a few cleaning items must not be auto-filed to Operations).

Rules (PACKAGE_department_accounts.md §6):
  - Operations          ← non-fee lines dominated by chem/paper sections.
  - Coffee/Concessions  ← non-fee lines dominated by Coffee/Concessions products.
  - Food/Kitchen        ← default / anything mixed.
  - mixed flag          ← lines span more than one candidate bucket.

A non-food account is only suggested when it's effectively the WHOLE invoice
(>= DOMINANCE of non-fee lines), matching the separate-invoice-per-department
workflow. Fees/tax (match_confidence='non_product') are ignored.
"""
from __future__ import annotations

OPS_SECTION_TOKENS = ('PAPER & DISP', 'CHEMICAL', 'JANITORIAL', 'SUPPLY & EQUIP')
COFFEE_CATEGORY = 'coffee/concessions'
DOMINANCE = 0.9

_BUCKET_TO_ACCOUNT = {
    'ops': 'Operations',
    'coffee': 'Coffee/Concessions',
    'food': 'Food/Kitchen',
}


def _bucket(section_hint: str | None, product_category: str | None) -> str:
    if (product_category or '').strip().lower() == COFFEE_CATEGORY:
        return 'coffee'
    sec = (section_hint or '').upper()
    if any(tok in sec for tok in OPS_SECTION_TOKENS):
        return 'ops'
    return 'food'


def suggest_account(lines) -> dict:
    """`lines`: iterable of dicts with keys section_hint, product_category, is_fee.
    Returns {'suggested': <account name>, 'mixed': bool, 'buckets': {bucket: n}}.
    """
    buckets: dict[str, int] = {}
    nonfee = 0
    for ln in lines:
        if ln.get('is_fee'):
            continue
        nonfee += 1
        b = _bucket(ln.get('section_hint'), ln.get('product_category'))
        buckets[b] = buckets.get(b, 0) + 1

    if nonfee == 0:
        return {'suggested': 'Food/Kitchen', 'mixed': False, 'buckets': buckets}

    mixed = len(buckets) > 1
    top = max(buckets, key=buckets.get)
    if top != 'food' and buckets[top] / nonfee >= DOMINANCE:
        return {'suggested': _BUCKET_TO_ACCOUNT[top], 'mixed': mixed, 'buckets': buckets}
    return {'suggested': 'Food/Kitchen', 'mixed': mixed, 'buckets': buckets}


def suggest_account_for_ili(ili_iterable) -> dict:
    """Adapter: build line dicts from InvoiceLineItem instances, then suggest."""
    lines = []
    for i in ili_iterable:
        lines.append({
            'section_hint': i.section_hint,
            'product_category': (i.product.category if i.product_id else ''),
            'is_fee': i.match_confidence == 'non_product',
        })
    return suggest_account(lines)
