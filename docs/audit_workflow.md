# Audit Workflow — Truth-Seeking Inventory Audit

**Status: probationary methodology, shipped 2026-05-26.**

Multi-tier audit of the Product catalog that surfaces discrepancies between stored DB
state and what's true on paper. Solves two operational problems:

1. **Searching for bugs in the dark** — without the workflow, bugs surface accidentally
   (manual sheet inspection, dashboard anomalies, your eye). With the workflow, every
   bug class encoded in the pattern library is detected systematically.
2. **Guessing whether fixes work** — re-running the same audit before and after a fix
   gives a measurable before/after diff. The audit is the measurement instrument.

## The seven tiers

Each tier costs more to run than the previous one and catches a different class of
bug. The tooling automates Tiers 1-3; Tiers 4 and 7 are human-in-the-loop. Tiers 5,
6, 8, 9 are tracked separately as the methodology matures.

| Tier | What it checks | Cost | Catches |
|------|---------------|------|---------|
| 1 | Schema completeness — is the field populated? | SQL, ~5 sec | Missing data |
| 2 | Semantic correctness — does the value make operational sense? | SQL + rules, ~10 sec | Populated but wrong values |
| 3 | Paper truth — does the value match the OCR cache? | Full corpus, ~3 min | Parser-storage divergence from invoice |
| 4 | Paper image inspection — does the OCR match the paper? | Per-product, 5-10 min | OCR errors DocAI itself made |
| 5 | Longitudinal consistency — is the value stable across invoices? | Heavy SQL, ~1 session | Parser regressions over time |
| 6 | Cross-source verification — does it match the vendor's own catalog? | Depends on data access | Independent ground truth |
| 7 | Code-path trace — where in the code did this divergence happen? | 60-90 min per bug class | Architectural root cause |

## Running the audit

```bash
# Full audit (all three automated tiers, runs against current DB + .ocr_cache)
python manage.py audit_inventory

# Fast version (Tier 1+2 only, skips OCR sweep)
python manage.py audit_inventory --no-tier3

# Single category
python manage.py audit_inventory --category Proteins

# Additional JSON output to a named path
python manage.py audit_inventory --json /tmp/audit.json
```

Every run writes a timestamped JSON snapshot to `.audits/audit_YYYYMMDD_HHMMSS.json`
(gitignored — durable runtime artifact, not source). The JSON drives future
`/audit/` Django surface; the stdout markdown summary is the operator-readable view.

## Reading the output

The markdown summary leads with:

- **Coverage** — total products, verified clean, discrepancies, unverifiable
- **Severity distribution** — count by severity 2/3/4
- **By bug pattern** — which pattern fired how many times
- **Severity 4 products by category** — the highest-impact list to drill into
- **Tier 1 schema completeness by category** — field-fill rates per category

The JSON contains everything the markdown summary shows plus per-discrepancy detail
(OCR snippet, stored values, suggested fix, cache SHA for image lookup).

## Severity levels

| Severity | Meaning | Examples |
|----------|---------|----------|
| 4 | Critical — count off by factor, operationally wrong | Pringles SOS stored as 121.3OZ single container when it's 12 × 1.3oz cans |
| 3 | High — descriptor/class mismatch, qty error, K on non-weighed | Bottled Water with K=$0.35/lb of fluid in plastic; LaCroix qty=1 when invoice shows 10 CS |
| 2 | Medium — audit-noise-prone, may be false positive | case_size string vs OCR text mismatch where my token search picked wrong line |

## Bug patterns currently encoded

| Pattern | Tier | What it catches |
|---------|------|-----------------|
| `K_on_non_weighed_category` | 2 | P/# computed on Coffee/Concessions / Chemicals / Smallwares / Beverages |
| `K_on_counted_container` | 2 | P/# computed on counted_* items with container-word unit_descriptor |
| `descriptor_implausible_size` | 2 | inventory_unit_descriptor with implausibly large OZ for category |
| `normalization_bypassed` | 2 | stored case_size differs from what `_normalize_pack_size` would produce — the flagship pattern for Sysco DocAI fusion class |
| `number_token_fusion` | 3 | cpc=1 + cps>50 + OZ — the Sysco fusion signature with OCR snippet for evidence |
| `case_size_token_fusion` | 3 | case_size regex matches "pack count + size fused" pattern |
| `fluid_oz_as_weight` | 3 | case_total_weight_lb computed from fluid-OZ container |
| `qty_mismatch` | 3 | OCR shows N CS but stored quantity ≠ N |

## Extending the pattern library

When you spot a new bug class:

1. Add the detection logic to `audit_inventory.py` — Tier 2 if it's a pure DB check,
   Tier 3 if it needs OCR cross-reference.
2. Add a test in `myapp/tests.py` under `AuditInventoryTier2Tests` or
   `AuditInventoryTier3PatternTests` covering the new pattern.
3. Run `python manage.py audit_inventory` to see it surface against current data.
4. Re-run after fixes ship to verify they closed the pattern.

The pattern library grows with each new bug class. The workflow scales with discipline,
not with code volume — extend it as new classes surface, retire patterns whose bug
class has been architecturally closed.

## Recommended cadence

- **Tier 1 + 2 monthly** — cheap drift detection. Run on the 1st of each month.
- **Tier 3 quarterly** — corpus-wide paper-truth sweep. End of quarter, before
  inventory close. Pairs with the quarterly archival artifact.
- **Tier 4 + 7 ad-hoc** — when a new bug class surfaces or a fix needs verification.

## Connection to existing LAWs

This is the schema-layer expression of two existing LAWs:

- **`feedback_trust_as_primary_requirement` (Trust LAW)** — "if I don't trust your
  number, I do it myself." The audit operationalizes trust as a measurable surface
  rather than a personal judgment.
- **`feedback_completeness` (Completeness LAW)** — "100% or don't ship." The audit
  output shows the gap; the discipline is closing it.

Per `project_encoded_craft_tapestry`: the audit workflow makes the truth-seeking
discipline visible to inheritors. Albert or a future operator sees the surface and
immediately understands the system has known imperfections + where to look.

## Limitations

Honest:

- **Coverage isn't 100%.** Pi has ~73% Tier 3 coverage; the remaining 27% are
  products with no recent ILI or whose OCR cache rotated out.
- **Patterns are hand-coded.** A new bug class not in the library stays invisible
  until you add the detector.
- **Tier 3 false positives.** My OCR-line-match is coarse (token-based). Severity 2
  has known false-positive prone patterns that need human triage.
- **The audit can't catch what it doesn't know to look for.** Trust the workflow as
  a measurement instrument, not an omniscient detector.

## Promotion path

Probationary methodology 2026-05-26. Promotes to LAW after:

- Exercised on 2+ domains beyond inventory (e.g. recipes/menus, mappings)
- Caught a bug class your eye missed
- Used to verify a fix's effectiveness against the corpus

See `feedback_methodologies.md`.
