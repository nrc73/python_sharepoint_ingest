# Key Vault Configuration Validation (Python)

Use this guide to validate that environment-specific configuration can be
resolved from Azure Key Vault before running ingestion.

## Goal

Confirm for `dev` and `prod` that:

- required secrets exist
- secret reads succeed
- values are non-empty
- expected environment naming conventions are followed

## Required secret set

### Dev

- `dm-sharepoint-dev-client-id`
- `dm-sharepoint-dev-client-secret`
- `dm-sharepoint-dev-tenant-id`
- `dm-sharepoint-dev-site-url`
- `dm-sql-dev-server`
- `dm-sql-dev-database`

Optional (if SQL auth mode is used):

- `dm-sql-dev-username`
- `dm-sql-dev-password`

### Prod

- `dm-sharepoint-prod-client-id`
- `dm-sharepoint-prod-client-secret`
- `dm-sharepoint-prod-tenant-id`
- `dm-sharepoint-prod-site-url`
- `dm-sql-prod-server`
- `dm-sql-prod-database`

Optional (if SQL auth mode is used):

- `dm-sql-prod-username`
- `dm-sql-prod-password`

## Python validation approach

1. Resolve environment (`dev` or `prod`).
2. Build expected secret-name list from environment prefix.
3. For each secret:
   - attempt Key Vault read
   - fail if missing/forbidden/empty
4. Print PASS/FAIL summary with reason codes.

## Example command

```powershell
python sharepoint_setup\keyvault_secret_test.py --env dev
python sharepoint_setup\keyvault_secret_test.py --env prod
```

## Recommended failure categories

- `KV_SECRET_MISSING`
- `KV_ACCESS_FORBIDDEN`
- `KV_SECRET_EMPTY`
- `KV_VAULT_UNREACHABLE`
- `KV_UNEXPECTED_ERROR`

## Security note

Never print secret values during validation. Log secret names and status only.
