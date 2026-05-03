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

704 tests, ~110s suite runtime. Test discipline notes in `.claude/memory` (shared separately).

Production runs on a Raspberry Pi 4 (Trixie 64-bit, Python 3.13) reachable via Tailscale; the Chromebook is the dev host. The Pi has been the authoritative cron + DB host since the 2026-04-26-28 cutover.

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
│ PRESENTATION   views.py (~80 views, 3836 lines) + 44 templates   │
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
│ DATA           15 models · 63 migrations · Django ORM            │
│                SQLite file (Postgres-ready via env var)          │
├──────────────────────────────────────────────────────────────────┤
│ INGESTION      invoice_processor/  — 28 modules                  │
│                Pipeline:  drive → docai → parser → mapper →      │
│                           db_write                                │
│                Siblings:  synergy_sync, budget_sync, csv_ingest  │
│                Utilities: discover_unmapped, learn_from_reviews, │
│                           reprocess_{archive,jpgs}, audit_*      │
│                                                                   │
│                myapp/management/commands/ — 44 commands for      │
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
- **`YieldReference`** — 1,119 rows from *Book of Yields 8e* (Lynch, Wiley). Per-section parsers in `myapp/yield_parsing/` handle the 20 sections' different column layouts. Feeds the yield% step of the cost calc: 1 lb raw chicken breast ≠ 1 lb edible portion.

---

## Current state

### Volume (Pi production, 2026-05-02)
- 2,821 InvoiceLineItems across 2025-06 → today, 7 vendors
- 553 Products, 2,033 ProductMappings (~86% mapping coverage post-quarantine guards)
- 88 Recipes, 587 RecipeIngredients
- 1,119 YieldReferences, 96 StandardPortionReferences
- 282 Menus across the active biweekly cycles
- 37 MenuFreetextComponents, 28 Census rows
- 622 ProductMappingProposals (528 approved / 7 rejected / 87 pending) — drift_audit unification active
- 704 tests passing in ~110s (local)

### What's in flight
Active workstream is bottom-up refinement of the data foundations: parser → DB schema → cost calc → consumers, in that order. The cost calculator is the load-bearing metric (it exercises every layer); its coverage drives order-guide accuracy, sheet correctness, and COGs dashboard trust.

Recent progress: recipe-cost coverage moved from 19.3% to 57.0% via the cost-accuracy push (Apr 22), then convention migration + Pi cutover (Apr 25-26) lifted mapping coverage from 87% to 95%. Mapper regression today reads 0% drift against the 782-row ground-truth replay.

The next benchmark is the **May 31 perpetual-inventory reconciliation**: Sean authors May menus in-app (Apr 30 = baseline count; May 1-31 = real authoring + consumption tracking; May 31 = first real variance report).

### Known gaps worth discussing with an architect
1. **No transaction boundaries in `db_write.py`** — a batch that crashes midway can leave half an invoice written. Idempotent upsert mitigates but doesn't eliminate.
2. **View tests are smoke-only.** 704 tests concentrate on parser/mapper/cost_utils/integrations. Views get one GET-returns-200 check each.
3. **No real-time push layer.** Kitchen display polls `/display/` every 60s. Fine for single-kitchen; future multi-tenant product needs SSE or WebSocket.
4. **Single-tenant schema.** Menu/Census/InvoiceLineItem have no property/tenant FK. Documented scaling posture: SQLite holds until ~20 customers, then Postgres; job queue (Celery+Redis) at ~50; load balancer at ~200.
5. **Budget sync cron is scheduled but producing no logs** — blocked on OneDrive Graph API credential drop. Admin consent was granted 2026-04-24; awaiting Client/Tenant ID + Secret from IT.
6. **Sheet retirement progress (2026-05-02):** Mapping Review tab fully retired (no code path); Data Sheets tab retired (db_write replaced append_to_data_sheet); Item Mapping tab semi-retired (DB primary, sheet fallback only when ProductMapping empty + audit surface for Sean per locked memory). 2,033 ProductMappings (up from 1,790). Synergy monthly tab remains load-bearing.
7. **Two memory roots, only one indexed.** User-scope `~/.claude/projects/-home-seanwil789/memory/` (40 files, fully indexed in `MEMORY.md`). Project-scope `~/.claude/projects/-home-seanwil789-my-saas/memory/` (62 files, minimal index). Decision pending.

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
python manage.py test myapp                    # full suite, ~96s
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
  models.py               15 models end-to-end (~806 lines)
  cost_utils.py           Cost calculation dispatch + density tables (~817 lines)
  taxonomy.py             Locked naming + descriptor convention (~907 lines)
  calendar_utils.py       Biweekly anchor math (~17 lines)
  signals.py              3 receivers (menu, m2m, mealservice) — 138 lines
  views.py                ~70 view functions (~3,740 lines)
  urls.py                 60 URL routes
  forms.py                Custom forms (~208 lines)
  consumption_utils.py    Per-Product date-range consumption engine (~232 lines)
  admin.py                Admin registration for all models (~141 lines)
  tests.py                704 tests (~10,542 lines)
  yield_parsing/          Per-section parsers for Book of Yields PDF
  templates/myapp/        44 HTMX + Tailwind templates
  management/commands/    58 commands (imports, audits, backfills)
  migrations/             63 migrations (latest: 0064)

invoice_processor/        Pipeline modules (non-Django, 28 files)
  batch.py                Cron entry — Drive inbox → full pipeline (~530 lines)
  parser.py               6 vendor dialects (~2,574 lines, the crown-jewel file)
  mapper.py               7-tier match cascade (~1,002 lines)
  spatial_matcher.py      2D bbox matching for Sysco + 4 other vendors (~691 lines)
  docai.py                Document AI wrapper (~906 lines)
  db_write.py             Upsert + quarantine layer — pipeline/ORM boundary (~292 lines)
  synergy_sync.py         Google Sheets push for the operator's monthly tab (~1,506 lines)
  budget_sync.py          OneDrive xlsx push (scheduled; blocked on credential drop)
  discover_unmapped.py    Fuzzy-suggestion generator for the review workflow (~1,054 lines)
  learn_from_reviews.py   Self-modifying rule learner from operator decisions
  reprocess_archive.py    Full-archive replay for parser-change validation
  abbreviations.py        Vendor shorthand expansion (~187 lines, ~75 entries)

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
