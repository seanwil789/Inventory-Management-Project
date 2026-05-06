"""Shared fuzzy-match helpers for VendorPriceList canonical resolution.

Both backfill_canonical_vpl_fk (one-time historical) and db_write
(per-ingestion) use this module to compute `canonical_vendor_pricelist_id`
from a raw_description.

Design:
  - Token normalization collapses whitespace inside compound numeric tokens
    ("4 / 1 - GAL" -> "4/1-GAL") so parser-variant whitespace doesn't break
    matches. Validated empirically on Pi 2026-05-06: 38+ of 82 unmatched
    Farm Art ILIs were this exact pattern.
  - Jaccard over normalized token sets at threshold 0.65 — sweet spot
    discovered via stratified sampling of 504 backfill matches:
      0.85-1.00: ~99% accurate
      0.65-0.85: ~85% accurate
      0.55-0.65: ~60% accurate (size/format discriminator failures)
    0.65 is the auto-attach threshold; 0.55-0.65 surfaces as review queue.

Pricing-as-event-driven LAW (`feedback_event_driven_pricing.md`):
  This module ONLY computes identity (which catalog SKU). It does not
  modify any price field. Callers must preserve ILI.unit_price / ext etc.
"""
from __future__ import annotations
import re


_OPERATOR_SPACE_RE = re.compile(
    r"(?<=\w)\s+([/-])\s*(?=\w)|(?<=\w)\s*([/-])\s+(?=\w)"
)
_PERCENT_SPACE_RE = re.compile(r"(\d+)\s+%")
_TOKEN_RE = re.compile(r"[A-Z][A-Z]+|\d+(?:[/.,-]\d+)*%?")


# Default thresholds — see module docstring for empirical basis.
ATTACH_THRESHOLD = 0.65
REVIEW_THRESHOLD = 0.55


def normalize_tokens(s: str) -> str:
    """Collapse whitespace inside numeric/compound tokens for stable matching.

    Lookaround pattern means adjacent matches don't share consumed characters,
    so chained patterns ("1-1 / 9 - LB") collapse in a single pass.
    """
    if not s:
        return ""
    out = _OPERATOR_SPACE_RE.sub(lambda m: m.group(1) or m.group(2), s)
    out = _PERCENT_SPACE_RE.sub(r"\1%", out)
    return out


def tokenize(s: str) -> frozenset:
    """Return a frozen set of canonical tokens from a raw_description."""
    if not s:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(normalize_tokens(s).upper()))


def jaccard(a: frozenset, b: frozenset) -> float:
    """Symmetric set similarity in [0.0, 1.0]."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_candidate_index(vendor) -> list[tuple]:
    """Pre-tokenize a vendor's VendorPriceList rows for repeated matching.

    Call once per ingestion run (not per row) to amortize the token-extract
    cost. Returns a list of (vpl_row, token_set) tuples.
    """
    # Lazy import — this module is shared between management commands and
    # db_write (which already bootstraps Django). Avoid circular import at
    # module load time.
    from myapp.models import VendorPriceList

    return [
        (vpl, tokenize(vpl.raw_description))
        for vpl in VendorPriceList.objects.filter(vendor=vendor)
    ]


def find_canonical_match(raw_description: str,
                         candidates: list[tuple],
                         threshold: float = ATTACH_THRESHOLD) -> tuple:
    """Find the best VendorPriceList match for a raw_description.

    Args:
        raw_description: incoming ILI raw_description text.
        candidates: pre-tokenized index from `build_candidate_index`.
        threshold: minimum Jaccard for a valid match.

    Returns:
        (vpl_row | None, best_score). vpl_row is None if no candidate
        scored at or above the threshold; best_score is the highest
        observed score regardless (useful for review-queue logic).
    """
    if not raw_description or not candidates:
        return (None, 0.0)
    toks = tokenize(raw_description)
    if len(toks) < 2:
        return (None, 0.0)

    best_vpl, best_s = None, 0.0
    for vpl, vpl_tokens in candidates:
        s = jaccard(toks, vpl_tokens)
        if s > best_s:
            best_s = s
            best_vpl = vpl
    if best_s < threshold:
        return (None, best_s)
    return (best_vpl, best_s)
