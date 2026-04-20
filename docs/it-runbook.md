# Kitchen Operations — IT Admin Runbook

**For:** IT admin at Wentworth (or managed IT provider)
**Time required:** ~5 minutes
**Purpose:** execute the three approved changes to the Azure AD / Microsoft Entra ID app registration for the Kitchen Operations system

---

## Prerequisites

- Access to **Microsoft Entra admin center** (entra.microsoft.com) or **Azure portal** (portal.azure.com)
- Global Administrator or Cloud Application Administrator role
- App registration name: **`<APP NAME — Sean to fill in>`** (Application/Client ID: `<fill in>`)

---

## Step 1 — Add the three Microsoft Graph API permissions

1. Navigate to **Microsoft Entra admin center → Identity → Applications → App registrations**
2. Find and click the app registration named above (or search by Application ID)
3. Left sidebar → **API permissions**
4. Click **+ Add a permission**
5. Select **Microsoft Graph**
6. Choose **Application permissions** (not Delegated, unless noted below)
7. Add each of these three, one at a time:

   | Permission | Type | Purpose |
   |---|---|---|
   | `Files.Read.All` | Application | Read OneDrive files (budget sheet, docs) |
   | `Files.ReadWrite.Selected` | Application | Write to a specific reports folder only |
   | `User.Read` | **Delegated** | SSO identity for Sean + Albert + kitchen staff |

8. After adding each, click **Add permissions** at the bottom.

## Step 2 — Grant admin consent

This is the "one button click" the system is blocked on:

1. Still on the **API permissions** page of the same app registration
2. Click **Grant admin consent for `<tenant name>`** (blue button at the top)
3. Confirm — the permissions' **Status** column should flip from blank to **Granted for `<tenant>`** with a green check

## Step 3 — Enable the Enterprise App for SSO

1. Navigate to **Microsoft Entra admin center → Identity → Applications → Enterprise applications**
2. Find the same app by name (it's auto-synced from the App registration in Step 1–2)
3. Left sidebar → **Single sign-on**
4. Pick **SAML** OR **OpenID Connect** (the existing app registration uses OIDC; confirm with "OpenID Connect" if prompted)
5. Save

## Step 4 — Send us confirmation

Reply to Sean with:

- **Tenant ID** (top of Entra admin center home page — a UUID)
- **Application (Client) ID** of the app registration
- Confirmation that **admin consent was granted** (screenshot of the API permissions page is ideal)

That's it — Sean wires those into the config and the OneDrive auto-sync + SSO go live within a few hours.

---

## What this does NOT do

- Does **not** grant tenant-wide write access — `Files.ReadWrite.Selected` only allows writes to drive items the user/app explicitly selects at first use.
- Does **not** grant Mail, Teams, SharePoint admin, Power Platform, or Dataverse scopes.
- Does **not** install anything on the tenant — it's an app registration + scope grant, entirely within Entra ID.

## Scope rationale (one-liners, in case asked)

- **Why not `Files.ReadWrite.All`?** We deliberately want a narrower write scope. `.Selected` forces the user to approve specific folders at first use.
- **Why `User.Read` delegated?** SSO needs to resolve the CURRENT user's identity (delegated flow). Application-only SSO wouldn't give per-user attribution.
- **Why not Graph subscriptions / webhooks?** Not in this phase. If we add near-real-time file change notifications later, that'd be a separate ask.

## Revocation

If at any time this needs to be revoked:

- **Entra → App registrations → `<app>` → API permissions → Remove permissions**, or
- **Enterprise applications → `<app>` → Properties → Enabled for users to sign-in? → No**, or
- Disable the app registration entirely
