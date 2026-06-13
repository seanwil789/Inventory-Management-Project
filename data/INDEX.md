# data/ — File-Drop Manifest

Staging area for loose data files that used to dump into the repo root.
Established **2026-06-13** (file-drop reorg). This file is the **only** tracked
thing under `data/`; everything else is gitignored (`data/*` + `!data/INDEX.md`)
and synced externally, not via git.

## Workflow

1. Drop any new file into **`data/inbox/`** — single known path.
2. Claude triages: moves it to the right subfolder, renames to
   `YYYY-MM-DD_source_type.ext` where practical, and logs it here.
3. Reference-check before moving anything that already lived at root — some
   files are read by code/cron (see "Code bindings" below).

## Layout

| Folder | Holds |
|---|---|
| `inbox/` | New drops, untriaged |
| `vendor_exports/` | Vendor catalog / order-guide / velocity exports |
| `budget/` | Wentworth food-budget workbooks + CSVs |
| `invoices_misc/` | One-off / sample invoice PDFs not in the OCR pipeline |
| `reference/` | Published reference PDFs (Book of Yields, Flavor Matrix) |
| `generated/` | Artifacts our own commands write (cadence reports, etc.) |
| `archive/` | Cold storage (large one-shot zips) |

## Manifest

| File | Folder | Source | Date | Status |
|---|---|---|---|---|
| Concessions_Performance Foodservice New Jersey-56927427_06-13-2026.csv | vendor_exports | PFG CustomerFirst export (acct 56927427); 70 products, no prices | 2026-06-13 | active — PFG vendor eval |
| Synergy - Default Order Guide (1).csv | vendor_exports | Synergy/Farm Art order guide; read by `import_vendor_price_list` | 2026-05-05 | active |
| pbm_2025_09.csv | vendor_exports | PBM menu parsed to CSV (via `.pbm_pdf_to_csv.py`) | 2025-09 | reference |
| PBM MENU 9-2025.pdf | vendor_exports | PBM source menu PDF | 2025-09 | reference |
| Velocity Report Detail.xlsx | vendor_exports | Sysco velocity report; candidate source for Amazon/concession spend | 2026-05-04 | active — concession spend analysis |
| Men's Wentworth Food Budget 2026 (1).xlsx | budget | Employer budget workbook, download v1 | 2026-05-21 | superseded by (3) |
| Men's Wentworth Food Budget 2026 (3).xlsx | budget | Employer budget workbook, download v3 (latest) | 2026-06-02 | active |
| Men's Wentworth Food Budget 2026(Mar).csv | budget | March budget CSV; read by `import_budget_csv` | 2026-04-15 | imported |
| aramark-sample.pdf | invoices_misc | Aramark sample invoice; in `backup.sh` include list | 2026-04-24 | reference |
| EnterpriseInvoice-775856655.pdf | invoices_misc | One-off Enterprise invoice PDF | 2026-05-10 | reference |
| Flavor Matrix Virtual Copy.pdf | reference | Flavor Matrix (published reference, 57 MB) | 2025-07-02 | reference |
| The-Book-of-Yields-Accuracy-in-Food-Costing-and-Purchasing.pdf | reference | Book of Yields 8e; parsed by `myapp/yield_parsing/`; in `backup.sh` | 2026-04-18 | reference (load-bearing) |
| sysco_cadence.csv | generated | Output of `sysco_ordering_cadence` (all items) | 2026-06-03 | generated |
| sysco_cadence_by_category.csv | generated | Output of `sysco_ordering_cadence` | 2026-06-03 | generated |
| sysco_cadence_concessions.csv | generated | Output of `sysco_ordering_cadence` (concessions subset) | 2026-06-03 | generated |
| OneDrive_2026-04-17.zip | archive | 1.4 GB OneDrive snapshot, one-shot | 2026-04-17 | cold archive |

## Code bindings (repoint these if files move again)

- **`scripts/backup.sh`** — includes `data/reference/The-Book-of-Yields-…pdf`,
  `data/invoices_misc/aramark-sample.pdf`, and globs
  `data/budget/Men's Wentworth Food Budget *.csv`. (Repointed 2026-06-13.)
- **`invoice_processor/budget_sync.py`** — `_resolve_budget_file()` globs
  `data/budget/Men's Wentworth Food Budget *.xlsx` and picks the newest by
  mtime (fixed 2026-06-13; previously hardcoded a bare root filename that never
  existed). Drop a fresh budget workbook into `data/budget/` and the cron picks
  it up automatically. NOTE: `run_budget_sync.sh` (cron) *writes* totals back
  into that workbook.
- **`import_budget_csv`, `import_vendor_price_list`, `sysco_ordering_cadence`** —
  take file paths as explicit args (no runtime default). Docstring usage
  examples updated 2026-06-13 to point at the new `data/` paths.
