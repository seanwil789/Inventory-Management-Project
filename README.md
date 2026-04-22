# my-saas — Kitchen Operations Pipeline

A Django application that runs food operations for a sober-living program: invoice ingestion (OCR → parse → map → store), recipe/menu authoring, order-guide generation, and cost tracking. Built around a central processing hub with thin push/pull adapters to Google Sheets, a kitchen-display surface, and a planned OneDrive integration.

Single-operator today (Wentworth kitchen, ~45 residents, 4 meals/day). Architecture targets future productization across similar small institutional kitchens.

---

## Stack

- **Python 3.11**, **Django 5.2**
- **SQLite** in dev (2.5 MB), **PostgreSQL-ready** via `dj-database-url`
- **HTMX + Tailwind (CDN)** on the frontend — no SPA, no bundler
- **Google Document AI** for invoice OCR; **Vision API** fallback
- **Google Sheets API** for the operational surface the kitchen manager already uses (Synergy monthly sheet)
- **Google Drive API** for the invoice inbox + archive
- **WhiteNoise + Gunicorn** for production serving
- **cron** for pipeline scheduling (hourly batch, daily refresh, weekly sync)

316 tests, ~43s suite runtime. Test discipline notes in `.claude/memory` (shared separately).

---

## Architecture

**One-sentence:** a central Django hub ingests invoices and reference docs, stores structured data, and serves multiple consumer surfaces through thin adapters.

```
          INPUTS                        CENTRAL HUB                     OUTPUTS
  ──────────────────────           ─────────────────────           ──────────────────────

  Google Drive inbox                                              ┌─▶ Google Sheets
  (invoice .jpg/.pdf) ─▶ drive ──┐                                │   (Synergy monthly
                                 │                                │    tab, Item Mapping,
  Sysco CSV portal ─▶ csv_ingest ┤                                │    Mapping Review)
                                 ▼                                │      via synergy_sync
                          ┌─────────────┐                         │
                          │  OCR Cache  │  .ocr_cache/ SHA256-keyed│
                          │  + DocAI    │                         ├─▶ Kitchen display
                          └──────┬──────┘                         │   (/display/ HTMX,
                                 ▼                                │    60s auto-refresh)
                          ┌─────────────┐                         │
                          │   parser    │  6 vendor dialects      ├─▶ Web dashboards
                          └──────┬──────┘                         │   /cost-coverage/
                                 ▼                                │   /cogs/
                          ┌─────────────┐                         │   /order-guide/
                          │   mapper    │  7-tier: code → exact → │   /calendar/biweekly/
                          │             │  vendor_fuzzy → ...     │   /pipeline-health/
                          └──────┬──────┘                         │
                                 ▼                                ├─▶ Budget xlsx push
                          ┌─────────────┐                         │   (OneDrive, via
                          │  db_write   │                         │    Microsoft Graph —
                          └──────┬──────┘                         │    blocked on admin
                                 ▼                                │    consent)
    Word/PDF/CSV         ┌──────────────────┐                     │
   ────────────────      │                  │                     └─▶ Invoice totals
  Kitchen Coord  ─▶      │   SQLite DB      │                         monthly cache
  Recipe Book    ─▶      │   (Django ORM)   │                         (.invoice_totals/*.json)
  Menu Guide     ─▶      │                  │
  Book of Yields ─▶      │  15 core models  │
  Budget CSV     ─▶      │                  │
                         └────────┬─────────┘
                                  │
                                  ▼ cost_utils.py
                          Django views + templates
```

### Layer decomposition

```
┌──────────────────────────────────────────────────────────────────┐
│ PRESENTATION   views.py (69 views, 3007 lines) + 35 templates    │
│                LoginRequiredMiddleware on all views except        │
│                /display/ (kiosk mode for wall-mounted panel)      │
├──────────────────────────────────────────────────────────────────┤
│ BUSINESS       cost_utils.py    — case-size parsing, unit        │
│ LOGIC                            conversion, ingredient cost      │
│                                  dispatch (weight↔weight, vol↔   │
│                                  vol, cross-domain via density)  │
│                calendar_utils   — biweekly anchor (2026-01-05)   │
│                signals.py       — Menu save → PrepTask auto-     │
│                                   derivation; MealService save   │
│                                   → learned-popularity recompute │
├──────────────────────────────────────────────────────────────────┤
│ DATA           15 models · 23 migrations · Django ORM            │
│                SQLite file (Postgres-ready via env var)          │
├──────────────────────────────────────────────────────────────────┤
│ INGESTION      invoice_processor/  — 23 modules, ~10.7K LOC      │
│                Pipeline:  drive → docai → parser → mapper →      │
│                           db_write                                │
│                Siblings:  synergy_sync, budget_sync, csv_ingest  │
│                Utilities: discover_unmapped, learn_from_reviews, │
│                           reprocess_{archive,jpgs}, audit_*      │
│                                                                   │
│                myapp/management/commands/ — 29 commands for      │
│                imports, audits, backfills, and derivations       │
└──────────────────────────────────────────────────────────────────┘
```

### Key architectural properties

- **Hub model, strictly enforced.** No per-output business logic leaks back into the pipeline. Adding a new output surface (next target: OneDrive Excel) is a thin adapter reading from the DB, not a pipeline change.
- **Idempotent ingestion.** `db_write.write_invoice_to_db` upserts on `(vendor, product, date)`. `reprocess_*` commands can replay the OCR cache without duplicating rows. Manual reprocess is cheap.
- **Caching at every paid-API boundary.** `.ocr_cache/` means each invoice is OCR'd exactly once regardless of how many times the parser is re-tuned. `invoice_processor/mappings/` caches sheet reads with 1h TTL.
- **Human-in-the-loop mapping as first-class.** Low-confidence fuzzy matches route to a "Mapping Review" Google Sheets tab; the operator approves/rejects inline; `learn_from_reviews.py` trains `_KNOWN_MISMATCHES` + `negative_matches.json` from the decisions. The matcher learns; the operator stays in the loop.
- **Signals decouple side effects.** Menu saves auto-derive PrepTasks; MealService saves auto-recompute per-recipe learned popularity. No view has to remember to call these.
- **Every offline op is a management command.** Imports, audits, backfills, derivations — all `manage.py <cmd>` with `--dry-run` conventions. Scriptable, idempotent, cron-able.

---

## Data model

```
Vendor ──┐
         │
         └──▶ InvoiceLineItem ◀── Product ◀─────── ProductMapping
                 │                  │                    │
                 │                  │                    │
                 │                  ▼                    │
                 │            RecipeIngredient ──▶ Recipe
                 │                  │              │  (level: recipe /
                 │                  ▼              │   composed_dish /
                 │            YieldReference       │   meal)
                 │                                 │
                 │                                 │  (parent_recipe FK
                 │                                 │   for V1→V2→V3)
                 │                                 ▼
                 │                                Menu ──▶ MealService
                 │                                 │
                 │                                 ▼
                 │                            PrepTask   (auto-derived
                 │                                        via signal)
                 │
                 └──── unit_price, extended_amount, case_size, section_hint,
                       match_confidence, match_score, invoice_date
```

**Key fields worth knowing:**

- **`InvoiceLineItem.match_confidence`** — enum capturing how the line was mapped: `code` (Sysco SUPC code match, most reliable) → `vendor_exact` → `vendor_fuzzy` → `fuzzy` → `stripped_fuzzy` → `keyword_batch` (human bulk label) → `manual_review` (human single label) → `unmatched`. Drives both cost-calc confidence and review-queue filtering.
- **`Product.default_case_size`** — fallback case size when the invoice line's case_size is missing or OCR-mangled. Populated by `infer_product_default_case_sizes` from historical mode; curated further via migration 0023 for high-value products.
- **`Recipe.level`** (recipe / composed_dish / meal) — solves a real matcher bug: "Shrimp Pesto Pasta" was fuzzy-matching to "Pesto" (a sub-recipe). The matcher now only considers `composed_dish` and `meal` rows as menu link candidates.
- **`Recipe.parent_recipe` + `version_number`** — the kitchen's actual Word Doc convention (`Biscuits V2 4 13 2026.docx`) formalized as immutable version history. Past versions stay queryable; menu links pin to a specific version.
- **`YieldReference`** — 1,103 rows from *Book of Yields 8e* (Lynch, Wiley). Per-section parsers in `myapp/yield_parsing/` handle the 20 sections' different column layouts. Feeds the yield% step of the cost calc: 1 lb raw chicken breast ≠ 1 lb edible portion.

---

## Current state

### Volume
- 1,757 InvoiceLineItems across 2025-06 → 2026-04, 7 vendors
- 511 Products, 291 ProductMappings
- 83 Recipes, 562 RecipeIngredients (95% linked to Products, 74% have quantities)
- 1,103 YieldReferences, 96 StandardPortionReferences
- 50 Menus across the active biweekly cycles
- 316 tests passing in 43s

### What's in flight
Active workstream is a bottom-up refinement of recipe cost accuracy. The cost calculator is the load-bearing metric: it exercises every layer (parser → ILI → Product → RecipeIngredient → cost_utils), and its coverage drives order-guide accuracy, sheet correctness, and COGs dashboard trust.

Recent progress: recipe-cost coverage moved from 19.3% to 57.0% in a single session via five shipped phases (weight extraction from description, curated product defaults, density expansion for spices/produce, `doz`/`#10 can` unit support, multi-candidate case-size dispatch). Next phase wires `RecipeIngredient.yield_ref` linking to unlock the remaining ~80 piece-unit RIs ('medium', 'each', 'cloves', 'large').

### Known gaps worth discussing with an architect
1. **No transaction boundaries in `db_write.py`** — a batch that crashes midway can leave half an invoice written. Idempotent upsert mitigates but doesn't eliminate.
2. **View tests are smoke-only.** 316 tests concentrate on parser/mapper/cost_utils/integrations. Views get one GET-returns-200 check each.
3. **No real-time push layer.** Kitchen display polls `/display/` every 60s. Fine for single-kitchen; future multi-tenant product needs SSE or WebSocket.
4. **Single-tenant schema.** Menu/Census/InvoiceLineItem have no property/tenant FK. Documented scaling posture: SQLite holds until ~20 customers, then Postgres; job queue (Celery+Redis) at ~50; load balancer at ~200.
5. **Budget sync cron is scheduled but producing no logs** — likely blocked on an upstream OneDrive Graph API admin consent that hasn't been granted yet.
6. **Mapper regression check surfaces current drift** (1.1%, two real regressions) — safeguards any mapper change with a ground-truth replay.

---

## Running it

Requires Python 3.11, `poppler-utils` for PDF OCR, and a Google Cloud service account with Document AI + Vision + Drive + Sheets scopes (or none of those, for DB-only development).

```bash
# Setup
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create .env with at minimum:
#   SECRET_KEY=...
#   DEBUG=True
#   ALLOWED_HOSTS=localhost,127.0.0.1
# (plus Google service account paths for the pipeline)

# Migrate + run
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver

# Tests
python manage.py test myapp                    # full suite, ~43s
python manage.py test myapp.tests.IngredientCostTests   # narrow to a class

# Pipeline commands (dry-run conventions throughout)
python manage.py reprocess_invoices --month 2026 4 --dry-run
python manage.py infer_product_default_case_sizes --apply
python manage.py audit_orphan_products
python manage.py mapper_regression_check
```

Cron schedule (production; not needed for review):
```
0 * * * *   run_invoice_batch.sh          # hourly Drive inbox poll
0 6 * * *   run_refresh_invoice_totals.sh # daily totals cache rebuild
0 7 * * *   run_mapping_review_discover.sh
0 */6 * * * run_mapping_review_apply.sh
0 8 * * 5   run_budget_sync.sh            # Fri 8am budget push
```

---

## Code map

```
myproject/                Django project scaffold
  settings.py             SECRET_KEY, TIME_ZONE, dj_database_url, middleware
  urls.py                 Root routing (admin, auth, myapp)

myapp/                    Main application
  models.py               15 models end-to-end (~600 lines)
  cost_utils.py           Cost calculation dispatch + density tables (~580 lines)
  calendar_utils.py       Biweekly anchor math (~20 lines)
  signals.py              3 receivers (menu, m2m, mealservice)
  views.py                69 view functions (~3000 lines)
  urls.py                 44 URL routes
  admin.py                Admin registration for all models
  tests.py                316 tests (~3800 lines)
  yield_parsing/          Per-section parsers for Book of Yields PDF
  templates/myapp/        35 HTMX + Tailwind templates
  management/commands/    29 commands (imports, audits, backfills)
  migrations/             23 migrations

invoice_processor/        Pipeline modules (non-Django)
  batch.py                Cron entry — Drive inbox → full pipeline (~500 lines)
  parser.py               6 vendor dialects (~2200 lines, the crown-jewel file)
  mapper.py               7-tier match cascade (~600 lines)
  docai.py                Document AI wrapper (~840 lines)
  db_write.py             Upsert layer — the boundary between pipeline and ORM
  synergy_sync.py         Google Sheets push for the operator's monthly tab
  budget_sync.py          OneDrive xlsx push (scheduled but blocked on consent)
  discover_unmapped.py    Fuzzy-suggestion generator for the review workflow
  learn_from_reviews.py   Self-modifying rule learner from operator decisions
  reprocess_archive.py    Full-archive replay for parser-change validation

run_*.sh                  Cron wrappers (flock, log rotation, venv activation)

.claude/                  Harness + session-snapshot convention
  session_start_scour.py  Hook — injects project scour directive each session
  session_snapshot.md     Rewritten each session with verified state
  settings.local.json     Permission allow-rules + SessionStart hook config
```

---

## Things I'd specifically want an architect's eye on

1. **Is the hub-and-adapters decomposition right for where this is heading** (multi-tenant productization across institutional kitchens), or is there a point at which an event bus / service split earns its keep?
2. **The `invoice_processor/` vs `myapp/management/commands/` split** — both are "offline Python that touches the DB." Pipeline ingestion lives in the former, derivations/audits in the latter. The line is historical more than principled. Consolidate? Keep separate? Criteria?
3. **Transaction discipline in `db_write.py`.** Adding `transaction.atomic` around the per-invoice write loop is a small change; are there downsides I'm not seeing (partial-retry semantics, etc.)?
4. **Schema boundary for the multi-tenant future.** Adding a `tenant_id` FK proliferates; what's a cleaner pattern for this codebase specifically?
5. **Signal overuse.** The two signal receivers work today. If more cascading behavior shows up (recipe cost change → menu re-cost → order guide invalidation), do we outgrow signals before we feel it?
6. **Cost-calc dispatch design.** `cost_utils.ingredient_cost` is a big `if/elif` over unit-kind combinations. It's readable at current size but growing. Strategy pattern? Or keep linear while it stays simple?
