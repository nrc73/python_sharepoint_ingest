# Security Baseline for Credential Handling

This project should treat credentials as runtime-only sensitive data.

## Core principles

1. **In-memory only where possible**
   - Fetch secrets from Azure Key Vault at runtime.
   - Do not persist secrets to `.env` files, source code, or local scripts.
2. **Environment isolation**
   - Supported environments: `dev`, `prod`.
   - No `test` environment.
   - Separate credentials and app registrations per environment.
3. **Least privilege**
   - SharePoint app permissions should be site-scoped (`Sites.Selected` model).
   - SQL identities should only have required table/procedure access.
4. **Credential observability without leakage**
   - Log error categories and IDs, never raw secrets.
   - Redact connection strings and bearer tokens in logs.
5. **Rotation-ready design**
   - Secret names should be stable; secret values rotate.
   - Health checks should detect upcoming expiry and auth drift.

## Recommended secret naming pattern

Use Azure-safe, hyphenated key vault secret names with `dm-` prefix:

- `dm-sharepoint-dev-client-id`
- `dm-sharepoint-dev-client-secret`
- `dm-sharepoint-dev-tenant-id`
- `dm-sharepoint-dev-site-url`
- `dm-sql-dev-server`
- `dm-sql-dev-database`

- `dm-sharepoint-prod-client-id`
- `dm-sharepoint-prod-client-secret`
- `dm-sharepoint-prod-tenant-id`
- `dm-sharepoint-prod-site-url`
- `dm-sql-prod-server`
- `dm-sql-prod-database`

If SQL auth is used:

- `dm-sql-dev-username`, `dm-sql-dev-password`
- `dm-sql-prod-username`, `dm-sql-prod-password`

## Pre-run security checklist

- [ ] Key Vault secrets exist for target environment.
- [ ] SharePoint app consent/grants are valid for target site.
- [ ] SQL auth mode for environment is clear (`sql` vs `integrated`).
- [ ] Credential tests pass before ingestion run.
- [ ] No sensitive values are printed by debug/verbose logging.
