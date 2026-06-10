"""
Match canonical Products to USDA FoodData Central foods.

Pairs with fdc.py (the API client). Given a Product.canonical_name like
"Beef, Shoulder Butt" or "Mushroom, Dried Shiitake", search FDC (whole-food
datasets only), score the candidates, and return the best — with its per-100g
macros — plus a confidence score.

Design choice: conservative. We search only Foundation + SR Legacy (lab-analyzed
whole foods), so condiments/branded items (Gochujang, a branded cocoa mix) simply
return no match and fall to the human-review queue rather than getting a
plausible-but-wrong macro profile. Better a gap than bad data
(feedback_methodologies #11 — confident-wrong bulk write is the failure mode).
"""
import re
import unicodedata
import difflib

from . import fdc


def _normalize(s: str) -> str:
    """Lowercase, strip accents + punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


# FDC often prefixes a CATEGORY ("Nuts, almonds" / "Cheese, cheddar") — those
# extra tokens are fine. But these tokens mark a DIFFERENT food when they appear
# in the candidate and not the canonical (avocado vs avocado OIL, bagel vs bagel
# CHIPS) — hard-penalize, or the auto-tier writes wrong macros. (Stemmed forms.)
_DISQUALIFY = {"oil", "juice", "chip", "imitation", "kernel", "meatless",
               "powder", "substitute", "drink", "beverage", "snack", "concentrate",
               "tofu", "vegan", "plant",   # substitute/alt foods: dairy yogurt != "tofu yogurt"
               "flour", "sauce", "blood", "blend", "mix"}
# form/type mismatch: rice != rice FLOUR, horseradish != horseradish SAUCE,
# sausage != BLOOD sausage, juice != juice BLEND, muffin != muffin MIX. Safe —
# a real flour/sauce product carries the word in its OWN canonical too, so the
# penalty only fires on a mismatch. ("canned" was deliberately NOT added: it
# wrongly demoted legit canned-veg matches like Capers and Stewed Tomatoes;
# the lone "canned" error — Chicken Broth -> canned chicken — is fixed in the
# review UI instead.)


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def score(canonical_norm: str, candidate_norm: str) -> float:
    """0..1 similarity. Rewards canonical-token coverage + string ratio;
    penalizes a missing head noun and disqualifying extra tokens (oil/chips/
    imitation/etc.) that mark the candidate as a different food."""
    ct = {_stem(t) for t in canonical_norm.split()}
    dt = {_stem(t) for t in candidate_norm.split()}
    if not ct or not dt:
        return 0.0
    covered = len(ct & dt) / len(ct)
    ratio = difflib.SequenceMatcher(None, canonical_norm, candidate_norm).ratio()
    head = _stem(canonical_norm.split()[0])
    head_penalty = 0.0 if head in dt else 0.35
    disq_penalty = 0.5 * len(_DISQUALIFY & (dt - ct))
    return max(0.0, 0.6 * covered + 0.4 * ratio - head_penalty - disq_penalty)


def match_product(canonical_name: str, page_size: int = 8) -> dict | None:
    """Best FDC match for a canonical name, or None if FDC returns nothing.

    Returns {fdc_id, description, data_type, score, macros}. The caller decides
    whether `score` clears the auto-apply bar or needs review.
    """
    candidates = fdc.search_food(canonical_name, page_size=page_size)
    if not candidates:
        return None
    cn = _normalize(canonical_name)
    best = None
    for c in candidates:
        s = score(cn, _normalize(c.get("description", "")))
        if best is None or s > best["score"]:
            best = {**c, "score": round(s, 3)}
    if best and best.get("fdc_id"):
        best["macros"] = fdc.extract_macros(fdc.get_food(best["fdc_id"]))
    return best


def candidates_for(canonical_name: str, n: int = 5) -> list:
    """Top-N scored FDC candidates (with macros) for the review UI."""
    cands = fdc.search_food(canonical_name, page_size=max(n, 8))
    cn = _normalize(canonical_name)
    scored = []
    for c in cands:
        if not c.get("fdc_id"):
            continue
        scored.append({**c, "score": round(score(cn, _normalize(c.get("description", ""))), 3)})
    scored.sort(key=lambda x: -x["score"])
    top = scored[:n]
    for c in top:
        try:
            c["macros"] = fdc.extract_macros(fdc.get_food(c["fdc_id"]))
        except Exception:
            c["macros"] = {}          # a candidate whose detail 404s shouldn't kill the batch
    return top
