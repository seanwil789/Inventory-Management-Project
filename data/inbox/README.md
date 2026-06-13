# data/inbox — drop point

Drop any new data file here (vendor export, budget workbook, invoice PDF,
reference doc, etc.). This is the single known path — you don't have to decide
where it goes.

Next session, Claude triages: moves it to the right `data/` subfolder, renames
to `YYYY-MM-DD_source_type.ext` where practical, and logs it in `../INDEX.md`.

Subfolders: `vendor_exports/` · `budget/` · `invoices_misc/` · `reference/` ·
`generated/` · `archive/`.

(This README is tracked so the inbox stays visible in git even when empty;
everything else under `data/` is gitignored.)
