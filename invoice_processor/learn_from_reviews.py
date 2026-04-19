"""
Learn from Mapping Review Y/N decisions to improve matching accuracy.

Analyzes rejection patterns and approval patterns to:
1. Auto-discover known mismatches (add to _KNOWN_MISMATCHES in discover_unmapped.py)
2. Identify misleading words that cause wrong fuzzy matches
3. Generate a matching quality report

Usage:
  python learn_from_reviews.py              # analyze and report
  python learn_from_reviews.py --apply      # apply learned rules to known mismatches
"""
import os
import sys
import json
import re
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import django
django.setup()

from sheets import get_sheet_values
from config import SPREADSHEET_ID
from discover_unmapped import (
    REVIEW_TAB, _load_negative_matches, clean_description,
    _KNOWN_MISMATCHES, NEGATIVE_MATCH_PATH,
)

LEARNED_RULES_PATH = os.path.join(os.path.dirname(__file__), "mappings", "learned_rules.json")


def load_review_history() -> tuple[list[dict], list[dict]]:
    """Load all Y and N decisions from the Mapping Review tab."""
    raw = get_sheet_values(SPREADSHEET_ID, f"'{REVIEW_TAB}'!A:E")

    approvals = []
    rejections = []

    for row in raw[1:]:
        while len(row) < 5:
            row.append("")
        status = row[0].strip().upper()
        vendor = row[1].strip()
        desc = row[3].strip()
        canonical = row[4].strip()

        if not desc or not canonical:
            continue

        entry = {
            "vendor": vendor,
            "raw_description": desc,
            "canonical": canonical,
            "cleaned": clean_description(desc),
        }

        if status in ("Y", "DONE", "APPROVE"):
            approvals.append(entry)
        elif status in ("N", "REJECT"):
            rejections.append(entry)

    return approvals, rejections


def extract_misleading_words(rejections: list[dict]) -> dict[str, list]:
    """
    Find words that appear in both the description and the wrong canonical,
    causing the fuzzy matcher to produce a false match.
    """
    word_misleads = defaultdict(list)

    for r in rejections:
        desc_words = set(re.findall(r'[A-Z]{3,}', r["cleaned"].upper()))
        canon_words = set(re.findall(r'[A-Z]{3,}', r["canonical"].upper()))
        overlap = desc_words & canon_words

        for word in overlap:
            word_misleads[word].append({
                "desc": r["raw_description"][:60],
                "wrong_canonical": r["canonical"],
            })

    # Only keep words that caused 2+ wrong matches
    return {word: pairs for word, pairs in word_misleads.items() if len(pairs) >= 2}


def extract_mismatch_rules(rejections: list[dict]) -> list[tuple[str, str]]:
    """
    Extract (description_substring, wrong_canonical) rules from rejections.

    Strategy: for each wrong canonical that was rejected 2+ times,
    find the common distinguishing words in the rejected descriptions
    that differentiate them from the canonical.
    """
    # Group rejections by wrong canonical
    by_canonical = defaultdict(list)
    for r in rejections:
        by_canonical[r["canonical"]].append(r)

    rules = []

    for canonical, rejects in by_canonical.items():
        if len(rejects) < 2:
            continue

        # Find distinctive words in rejected descriptions
        # These are words that appear in most rejected descriptions but NOT in the canonical
        canon_words = set(re.findall(r'[a-z]{3,}', canonical.lower()))
        word_freq = Counter()

        for r in rejects:
            desc_words = set(re.findall(r'[a-z]{3,}', r["cleaned"].lower()))
            distinctive = desc_words - canon_words
            word_freq.update(distinctive)

        # Words that appear in most rejections for this canonical
        threshold = max(2, len(rejects) * 0.5)
        common_distinctive = [word for word, count in word_freq.items()
                              if count >= threshold and len(word) >= 4]

        for word in common_distinctive:
            rules.append((word, canonical))

    return rules


def extract_correction_pairs(approvals: list[dict], rejections: list[dict]) -> list[dict]:
    """
    Find cases where the same description was rejected for one canonical
    but approved for a different one. These are the most valuable corrections.
    """
    # Index rejections by description
    rejected_for = defaultdict(set)
    for r in rejections:
        key = r["raw_description"]
        rejected_for[key].add(r["canonical"])

    corrections = []
    for a in approvals:
        key = a["raw_description"]
        if key in rejected_for:
            for wrong in rejected_for[key]:
                corrections.append({
                    "description": key[:60],
                    "wrong": wrong,
                    "correct": a["canonical"],
                })

    return corrections


def save_learned_rules(rules: list[tuple[str, str]], corrections: list[dict]):
    """Save learned rules to disk for reference and application."""
    data = {
        "mismatch_rules": [{"substring": s, "wrong_canonical": c} for s, c in rules],
        "corrections": corrections,
    }
    os.makedirs(os.path.dirname(LEARNED_RULES_PATH), exist_ok=True)
    with open(LEARNED_RULES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Learn from Mapping Review decisions to improve matching"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Apply learned rules to the known mismatches list")
    args = parser.parse_args()

    print("Loading review history...")
    approvals, rejections = load_review_history()
    print(f"  {len(approvals)} approvals, {len(rejections)} rejections")

    if not rejections:
        print("\nNo rejections to learn from yet.")
        return

    # 1. Misleading words
    print(f"\n{'='*60}")
    print("MISLEADING WORDS")
    print(f"{'='*60}")
    print("Words that appear in descriptions AND wrong canonicals, causing false matches:\n")

    misleading = extract_misleading_words(rejections)
    if misleading:
        for word in sorted(misleading, key=lambda w: -len(misleading[w])):
            pairs = misleading[word]
            print(f'  "{word}" — {len(pairs)} false matches:')
            for p in pairs[:3]:
                print(f'    "{p["desc"][:45]}" → wrongly matched to "{p["wrong_canonical"]}"')
            if len(pairs) > 3:
                print(f'    ... and {len(pairs)-3} more')
    else:
        print("  None found (need more rejection data)")

    # 2. Mismatch rules
    print(f"\n{'='*60}")
    print("LEARNED MISMATCH RULES")
    print(f"{'='*60}")
    print("When description contains [word], it should NOT match [canonical]:\n")

    rules = extract_mismatch_rules(rejections)
    existing_rules = set(_KNOWN_MISMATCHES)
    new_rules = [(s, c) for s, c in rules if (s, c) not in existing_rules]

    if rules:
        for substring, canonical in sorted(rules):
            status = "NEW" if (substring, canonical) not in existing_rules else "existing"
            print(f'  "{substring}" → NOT "{canonical}"  [{status}]')
    else:
        print("  None extracted (need more rejection data)")

    # 3. Correction pairs
    print(f"\n{'='*60}")
    print("CORRECTION PAIRS")
    print(f"{'='*60}")
    print("Same description: rejected for one canonical, approved for another:\n")

    corrections = extract_correction_pairs(approvals, rejections)
    if corrections:
        for c in corrections:
            print(f'  "{c["description"][:45]}"')
            print(f'    Wrong: "{c["wrong"]}"  →  Correct: "{c["correct"]}"')
    else:
        print("  None found (happens when you reject and approve the same description)")

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  Misleading words found:  {len(misleading)}")
    print(f"  Mismatch rules:          {len(rules)} ({len(new_rules)} new)")
    print(f"  Correction pairs:        {len(corrections)}")

    # Save rules
    save_learned_rules(rules, corrections)
    print(f"\n  Learned rules saved to: {LEARNED_RULES_PATH}")

    if new_rules and not args.apply:
        print(f"\n  Run with --apply to add {len(new_rules)} new rules to the known mismatch filter.")

    # Apply if requested
    if args.apply and new_rules:
        print(f"\nApplying {len(new_rules)} new mismatch rules...")

        # Read current discover_unmapped.py and add new rules to _KNOWN_MISMATCHES
        discover_path = os.path.join(os.path.dirname(__file__), "discover_unmapped.py")
        with open(discover_path) as f:
            content = f.read()

        # Build new entries to add
        new_entries = []
        for substring, canonical in new_rules:
            entry = f'    ("{substring}", "{canonical}"),'
            if entry not in content:
                new_entries.append(entry)

        if new_entries:
            # Find the end of _KNOWN_MISMATCHES set and insert before the closing brace
            marker = "}\n\nAUTO_APPROVE_THRESHOLD"
            insert_text = "\n".join(["    # Learned from review rejections"] + new_entries) + "\n"
            content = content.replace(marker, insert_text + marker)

            with open(discover_path, "w") as f:
                f.write(content)

            print(f"  [OK] Added {len(new_entries)} new rules to _KNOWN_MISMATCHES in discover_unmapped.py")
        else:
            print("  All new rules already present in code.")


if __name__ == "__main__":
    main()
