# Package: Department Accounts / Cost-Center Attribution

> **Written 2026-06-08.** Spec + build plan for partitioning invoice spend by department/account, so non-food spend (Operations cleaning supplies, Coffee/Concessions) leaves the food budget while still informing prices — and so accounting gets clean, pre-coded, per-account data that maps to QuickBooks Classes. Sign-off artifact; build follows this.

## 1. The operating model (Sean, 2026-06-08)

- Spend now spans **multiple departments**, not just the kitchen. Each gets its own **account / cost center**.
- **Three accounts to start (designed to extend):**
  | Account | What it holds | Owner |
  |---|---|---|
  | **Food/Kitchen** (default) | All food/ingredient spend | Sean / kitchen |
  | **Operations** | Monthly Sysco order of *exclusively* chemical + paper cleaning goods | Operations manager |
  | **Coffee/Concessions** | The curated Coffee/Concessions basket (already a `Product.category`; ~$25k/yr, 50 Sysco items per the 6/3 cadence pull) | — |
- **Workflow reality:** departments are ordered as **separate invoices** (one invoice = one account). This is deliberate — it keeps QuickBooks coding clean (see §4). Mixed invoices are the exception to guard against, not the norm.
- **Recurring pattern:** beginning of each month, one Sysco invoice of exclusively chem/paper → Operations.

## 2. Core principle — attribute, don't exclude

A boolean "exclude from food budget" throws away *where the money went*. The fix is an **account axis**: every line item carries an account; the food budget is just "filter to Food." Non-food spend is preserved, attributed, and reportable per department. (Per the schema-encodes-operating-model + completeness LAWs.)

## 3. Two consumer classes (the upstream→downstream split)

| Class | Consumers | Behavior |
|---|---|---|
| **Account-aware** (filter to Food) | budget view, `/cogs/`, `/categories/`, `budget_sync` (Wentworth xlsx), invoice-totals cache | Sum only Food-budget accounts |
| **Account-blind** (unchanged) | price history, `VendorPriceList`, mapping, price alerts, mapper | Use all lines regardless of account — the chem/coffee lines still inform prices |

This separation is the crux: accounting attribution changes *budget/COGS* math only; it never touches the *catalog/price* layer.

## 4. QuickBooks streamline (the accountant-facing value)

- **QB tracks departments via Classes.** Per-line Class assignment exists but is tedious; a whole bill on one Class is clean. **Separate invoices = one bill = one Class = zero line-splitting.** The separate-invoice workflow is already designed around QB's friction.
  - *Caveat: confirm with the accountant — exact QB setup (Online vs Desktop, class tracking enabled) is theirs to verify before the export format is locked.*
- **We do the coding upstream.** Each invoice is auto-suggested an account on arrival; by the time it reaches accounting it's already classified.
- **Account → QB Class map** (1:1). Class-level P&L *is* the spend-by-department report.
- **Monthly per-account export** (CSV / QB-importable), each bill pre-tagged with its Class — the accountant codes nothing by hand.
- **Mixed-invoice guard:** if an invoice ever spans accounts, flag it before it reaches QB. Protects the clean separate-invoice discipline.
- **Roadmap fit:** this Account/Class axis + coded export is the first real rung of `project_quickbooks_roadmap` (foundation for the later direct-QB push).

## 5. Schema

```
Account
  name          CharField unique   # 'Food/Kitchen', 'Operations', 'Coffee/Concessions'
  qb_class      CharField blank     # QuickBooks Class name/code (1:1 map)
  is_default    Bool                # which account untagged invoices fall to (Food/Kitchen)
  is_food_budget Bool               # included in food-budget / COGS aggregations (only Food today)
  sort_order    Int

InvoiceLineItem.account  FK(Account, null=True, on_delete=SET_NULL)
  - null is treated as the default (Food) by consumers, as a safety net
  - primary UX sets it per-invoice (bulk all lines); line-level supported but rarely needed
```

## 6. Auto-suggest (suggest, never silent-write)

On invoice review, suggest an account from content — one-click confirm, never auto-committed (avoids the confident-wrong-bulk-write trap):
- **Operations** ← all/▲ lines in chem/paper sections (`PAPER & DISP`, `CHEMICAL & JANITORIAL`, `SUPPLY & EQUIPMENT`) AND no food lines.
- **Coffee/Concessions** ← lines whose Product.category == 'Coffee/Concessions' dominate.
- **Food/Kitchen** ← default / anything else.
- **Mixed flag** ← lines span >1 candidate account → surface for a human decision.

## 7. Build order (bottom-up, local-first, each step gated)

1. **Schema** — `Account` model + `ILI.account` FK + migration (seed 3 accounts; backfill all existing ILI → Food). ← *foundation*
2. **Auto-suggest helper** — `suggest_account(ivs_or_lines)` pure function + tests.
3. **UI** — `/invoices/`: account badge + filter; `invoice_detail`: set/change the invoice's account (bulk), with the suggestion surfaced.
4. **Consumer filtering** — budget / `/cogs/` / `/categories/` / `budget_sync` / totals-cache filter to Food-budget accounts.
5. **Operations + Coffee/Concessions spend views** — per-account totals (parallel to the food budget).
6. **QB-Class export** — monthly per-account CSV (format pending accountant confirm).
7. **Deploy + tag** — ship to Pi; recover the 6/1 $749.92 Sysco invoice (775917562, date-correct to 6/1) and tag it **Operations** as instance #1.

## 8. Backfill / data

- All existing `InvoiceLineItem` → **Food/Kitchen** (default), so nothing changes until tagged.
- The recovered **6/1 Sysco $749.92 (inv 775917562)** = first **Operations** invoice (worked example).

## 9. Open items / confirms

- Accountant to confirm QB class-tracking setup → locks export format (§4 caveat).
- More accounts later (6 properties / other cost centers) — model is built to extend.
- 775917562 still needs its IVS recovered (empty OCR cache) — folds into step 7.
