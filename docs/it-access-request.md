# Kitchen Operations System — IT Access Request

**From:** Sean Willcox, Kitchen Manager (Wentworth)
**Date:** April 2026
**Status requested:** Admin consent + three Microsoft Graph API scopes on an already-registered Azure AD application

---

## What we need — five minutes of an IT admin's time

1. **Admin consent** for the existing Azure AD app registration.
   The app is already registered under your tenant; it is blocked waiting
   on a single button click.

2. **Microsoft Graph API scopes** enabled on that registration:

   | Scope | Purpose |
   |---|---|
   | `Files.Read.All` | Auto-sync the budget sheet, Kitchen Coordination docs, Recipe Book, and Menu Guide from OneDrive. Eliminates the weekly zip-extract step and the "stale April census" problem. |
   | `Files.ReadWrite.Selected` | Write monthly COGs reports and order guides to a **specific reports folder only** — nothing else. |
   | `User.Read` | SSO identity for Sean, Albert, and future kitchen staff. |

3. **Azure AD enterprise app enabled for SSO** on the existing registration.

---

## What we are NOT asking for

- Tenant admin access
- `Files.ReadWrite.All` (no blanket write across OneDrive)
- Mail, Teams, or SharePoint admin scopes
- Power Platform, Dataverse, or licensed services
- Delegated on-behalf-of scopes

All requested scopes are **application-scoped** (least-privilege) with the
single write scope restricted to one folder.

---

## Data flow

**Pull-dominant:** your Microsoft ecosystem → our application, read-only
for operational data.
**Narrow push:** monthly COGs + order guides → a single `Kitchen Reports/`
folder in OneDrive. No two-way sync on user data.

---

## Why approving this pays off

1. **Always-current data.** The program director updates the budget sheet
   in OneDrive → the system picks up the change within the hour. No more
   mid-month staleness.

2. **Single sign-on.** No new passwords for kitchen staff. Revocation
   follows Microsoft identity automatically — when an employee leaves,
   their kitchen access disappears without a separate offboarding step.

3. **Automated reporting.** Monthly COGs reports and order guides land
   in OneDrive every month with no manual export step.

4. **Audit trail.** Every menu edit, recipe change, and order decision
   is attributed to a Microsoft identity.

5. **Zero risk to user data.** Scopes are read-only across the tenant
   with writes confined to a dedicated reports folder.

---

## Why this is the right time

- The application is already built and validated internally.
- Phase 1 deployment (Tailscale mesh, internal-only) is live and running.
  This is not a speculative ask — the system is in operational use today.
- Manual zip extraction of the OneDrive snapshot does not scale; staleness
  bugs compound the longer we defer.

---

## Current internal state (for context)

- **Invoice processing pipeline:** hourly automated, ~8,000 line items
  captured to date, ~88% auto-mapped to canonical products.
- **Recipe library:** 80 recipes with ingredient linkage.
- **Menu calendar:** biweekly planning with live cost estimation,
  protein-rotation rule warnings, and dietary conflict badges.
- **COGs dashboard:** current-month spend vs budget, 4-month trend,
  per-resident-per-day metrics.
- **Order guide:** headcount-scaled ingredient demand, vendor-grouped.
- **Kitchen display:** wall-mounted real-time menu view.

---

## Next step

Brief conversation to walk through the Azure portal together. 5 to 10
minutes. The approval gates no data movement outside the tenant.

---

*Questions: contact Sean Willcox, Wentworth kitchen.*
