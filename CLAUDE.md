# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`my-saas` is a Django 5.2 application running food operations for a sober-living kitchen (Wentworth, ~45 residents, 4 meals/day). It's a **mature mid-prototype**, not a skeleton — see `README.md` for the architecture diagram and current volumes (2,821 ILI, 553 Products, 88 Recipes, 282 Menus, 704 tests as of 2026-05-02).

The system covers: invoice OCR pipeline (DocAI + 6 vendor parsers + spatial matcher), product/canonical mapping with human-in-the-loop review, recipe/menu authoring + version history, perpetual inventory + variance reporting, kitchen-display and dashboard surfaces, and Google Sheets/OneDrive integration adapters.

## Production vs dev

- **Pi (`tailscale ssh sean@kitchen-pi-1`) = PRODUCTION.** Authoritative DB, 7 cron jobs firing, `django.service` systemd unit. See `~/.claude/projects/-home-seanwil789/memory/project_pi_access.md`.
- **Chromebook (`/home/seanwil789/my-saas/`) = DEV.** Code lives here; sync to Pi via `git push` at session end. Local DB drifts from Pi during the day.

## Active priorities

Always check `MEMORY.md` first — it's auto-loaded and indexes everything load-bearing. Top items as of 2026-04-29:

- **`project_thursday_inventory_ready.md`** — current week's punch list, Thursday 2026-04-30 inventory count
- **`project_six_month_roadmap.md`** — canonical 6-phase plan through 2026-10-28
- **`feedback_methodologies.md`** — design rules with promotion/shedding mechanics
- **`feedback_verification_law.md`**, **`feedback_scour_depth.md`**, **`feedback_upstream_downstream_planning.md`** — binding LAWs

## Session start

The `SessionStart` hook in `.claude/settings.local.json` injects the project scour directive + last session's snapshot. **First substantive turn is always a thorough scour** — see `feedback_project_scour.md` and `feedback_scour_depth.md`. No subagent delegation for scours.

`session_snapshot.md` is rewritten at the end of each scour with verified state. It becomes next session's baseline.

## Common commands

```bash
# Activate virtualenv
source .venv/bin/activate

# Tests (~110s, expect 704 passing)
python manage.py test myapp

# Pipeline operations (dry-run conventions throughout)
python manage.py reprocess_invoices --month 2026 4 --dry-run
python manage.py infer_product_default_case_sizes --apply
python manage.py audit_real_suspects
python manage.py mapper_regression_check

# Pi state queries (read-only over Tailscale)
tailscale ssh sean@kitchen-pi-1 "cd ~/my-saas && .venv/bin/python -c \"...\""
```

## Architecture

- `myproject/` — Django scaffold (settings, root URL conf, WSGI/ASGI)
- `myapp/` — Main application (15 models, ~80 views, 58 management commands, 63 migrations, 44 templates, 704 tests)
- `invoice_processor/` — Non-Django pipeline modules (28 files: parser, mapper, spatial_matcher, docai, db_write, synergy_sync, budget_sync, etc.)
- `myapp/yield_parsing/` — Per-section parsers for Book of Yields PDF
- `docs/` — Pi migration runbook, IT access request, deployment notes
- `.claude/` — Harness configuration, session_snapshot, time_log

## Environment

`.env` (gitignored) holds:
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`
- `DATABASE_URL` (Postgres-ready via `dj-database-url`; SQLite locally)
- Google service account paths (Drive, Document AI, Vision, Sheets — all on Sean's personal GCP)
- `MSGRAPH_*` (when OneDrive credentials land)

## Operating constraints

- **No project work Mon-Fri 8a-5p ET** unless Sean explicitly overrides for a session. See `feedback_no_work_during_hours.md`.
- **Time log required** at `~/my-saas/.claude/time_log.md`. See `feedback_track_session_time.md`.
- **No subagent delegation for scours.** Read AND test in-thread.
- **Memory edits warrant check-in** even in auto mode. See `feedback_auto_mode_scope.md`.
