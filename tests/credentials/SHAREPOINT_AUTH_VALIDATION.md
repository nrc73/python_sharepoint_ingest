# SharePoint Credential/Auth Validation (Python)

Use this guide to validate app-based SharePoint authentication and site access
for each environment.

## Goal

Confirm that environment credentials can:

- obtain a Microsoft Graph access token via MSAL client-credentials grant
- reach the expected SharePoint site through the Graph API
- list expected folders/documents via Graph drives API

## Authentication path

This project uses the **Microsoft Graph API** with an Entra ID
`client_credentials` grant. The legacy SharePoint REST (`/_api/`) path is
**permanently blocked** on this tenant by the `x-ms-suspended-features`
app-only feature gate, which rejects all app-only tokens with:

    "Unsupported app only token"

regardless of what permissions are declared in the SPO token's `roles` claim.

```
MSAL ConfidentialClientApplication
  authority : https://login.microsoftonline.com/{tenant_id}
  scope     : https://graph.microsoft.com/.default          ← Graph (not SPO)
  grant     : client_credentials (client_id + client_secret)
  → SharePointClient._get_token()
  → requests.get("https://graph.microsoft.com/v1.0/sites/...")
```

> **Why Graph and not SharePoint REST?**
>
> The SPO REST `/_api/` endpoint enforces a per-app feature gate tracked via
> the `x-ms-suspended-features` response header. Even with `Sites.ReadWrite.All`
> on the SPO resource (`00000003-0000-0ff1-ce00-000000000000`) in the JWT
> `roles` claim, SharePoint returns 401 "Unsupported app only token". The
> Microsoft Graph API uses an independent authentication pipeline that honours
> `Sites.ReadWrite.All` on the **Graph** resource
> (`00000003-0000-0000-c000-000000000000`) without any tenant-level opt-in.

## Prerequisites

- App registration exists per environment in **Microsoft Entra ID**.
- **Microsoft Graph** application permission `Sites.ReadWrite.All`
  (app role ID `9492366f-7969-46a4-8d15-ed1a20078fff`) is **admin-consented**
  via an `appRoleAssignment` on the service principal.  This is the sole
  permission required — no site-level grant via PnP is needed.
- Environment-specific secrets are available in Key Vault:
  - `dm-sharepoint-{env}-client-id`
  - `dm-sharepoint-{env}-client-secret`
  - `dm-sharepoint-{env}-tenant-id`

> The Graph `Sites.ReadWrite.All` `appRoleAssignment` is created automatically
> by `provision_sharepoint_app_registrations.ps1`. To verify manually:
> ```bash
> SP_ID=$(az ad sp list --filter "appId eq '{app_id}'" --query "[0].id" -o tsv)
> az rest --method GET \
>   --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignments" \
>   --query "value[?appRoleId=='9492366f-7969-46a4-8d15-ed1a20078fff']"
> ```

## Example commands

```powershell
# dev site library is named "Documents" (verify with --folder root first):
python sharepoint_setup\sharepoint_auth_test.py --env dev --folder "/sites/data_ingest_dev/Documents"
python sharepoint_setup\sharepoint_auth_test.py --env prod --folder "/sites/data_ingestion_prod/Documents"
```

The library name is site-specific. If the `SP_FOLDER_OR_SITE_NOT_FOUND` error is raised, the
`Available libraries:` list in the error message shows the correct names for that site.

## What to validate

1. MSAL token acquisition succeeds (`aud=https://graph.microsoft.com`, `roles=['Sites.ReadWrite.All']`).
2. Site can be resolved via `GET /v1.0/sites/{hostname}:/{site_path}` (HTTP 200).
3. Drive can be listed via `GET /v1.0/sites/{site_id}/drives` (HTTP 200).
4. Target folder children can be listed (HTTP 200, even if empty).

## Failure categories

| Code | Meaning |
|------|---------|
| `SP_UNSUPPORTED_APP_ONLY_TOKEN` | The SPO REST `/_api/` path is in use (old code path). Switch to the Graph-API `SharePointClient` implementation. |
| `SP_AUTH_UNAUTHORIZED` | MSAL returned a token but Graph returned 401. Most common cause: `Sites.ReadWrite.All` AppRoleAssignment missing on the Graph SP. Run the provision script or create manually (see above). Admin consent propagation can take 2–3 minutes. |
| `SP_GRAPH_GENERAL_EXCEPTION` | Graph returned a `generalException` — AppRoleAssignment likely missing or not yet propagated. Wait 2–3 minutes and retry. |
| `SP_FORBIDDEN` | Graph token valid but 403. The `Sites.ReadWrite.All` AppRoleAssignment may exist but was not yet reflected in the token. Acquire a fresh token (MSAL cache clear) and retry. |
| `SP_FOLDER_OR_SITE_NOT_FOUND` | Site URL or folder server-relative path is wrong (404). Verify `SHAREPOINT_SITE_URL_DEV/PROD` and the folder path argument. |
| `SP_AUTH_UNKNOWN_ERROR` | Any other unexpected error. |

## Operational notes

- MSAL `ConfidentialClientApplication` caches tokens in-memory and refreshes
  them automatically before expiry. No manual token management is needed.
- The scope **must** be `https://graph.microsoft.com/.default` (Graph), not
  the SharePoint host scope. `SharePointClient._get_token()` hard-codes this.
- No PnP PowerShell site grant (`Grant-PnPAzureADAppSitePermission`) is
  required. `Sites.ReadWrite.All` on Graph grants tenant-wide access to all
  SharePoint sites without per-site grants.
- Pagination: `list_files()` follows `@odata.nextLink` pages automatically,
  so large libraries (>200 items) are handled correctly.

## Root cause reference — "Unsupported app only token" triage log

Triaged 2026-05-05. Root cause chain:

| Layer | Finding |
|-------|---------|
| Token format | `aud=00000003-0000-0ff1-ce00-000000000000` (SPO resource), `ver=1.0`, correct issuer — JWT valid ✅ |
| Entra ID permissions | `Sites.ReadWrite.All` AppRoleAssignment on SPO SP confirmed in JWT `roles` claim ✅ |
| SPO REST response | 401, `x-ms-suspended-features: features=""`, body="Unsupported app only token" — tenant-level app-only feature gate active ❌ |
| Graph API test | Token `aud=https://graph.microsoft.com`, `roles=['Sites.ReadWrite.All']`, `GET /v1.0/sites/...` → HTTP 200 ✅ |
| Fix | Added `Sites.ReadWrite.All` (Graph, `9492366f...`) AppRoleAssignment to both SPNs; rewrote `SharePointClient` to use Graph API via `requests`; dropped `office365-rest-python-client` dependency |
