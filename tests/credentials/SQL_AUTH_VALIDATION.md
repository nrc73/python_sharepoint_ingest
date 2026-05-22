# SQL Credential/Auth Validation (Python)

Use this guide to validate SQL connection settings and authentication behavior
per environment.

## Goal

Confirm that the resolved SQL settings for `dev` and `prod` can:

- connect to target SQL server/database
- execute minimal validation query (`SELECT 1`)
- verify basic object visibility (optional)

## Supported auth modes

1. **Credential-based**
   - `sql_password`
   - `ad_password` / `active_directory_password`
   - Requires username/password (normally sourced from Key Vault).
2. **Passwordless integrated (Windows/SSPI)**
   - `windows` / `sspi` / `trusted_connection`
   - `ad_integrated` / `active_directory_integrated`
   - Process must run under the intended Windows identity.
3. **Passwordless token-based**
   - `managed_identity` (future-compatible / Azure-hosted runtime)

## Example commands

```powershell
python sharepoint_setup\sql_connection_test.py --env dev
python sharepoint_setup\sql_connection_test.py --env prod
```

For mixed-environment setups (recommended when both are Windows integrated auth):

- `dev`: set `SQL_AUTH_MODE_DEV=sspi` (or `windows` / `trusted_connection`) and run as the developer AD user.
- `prod`: set `SQL_AUTH_MODE_PROD=sspi` (or `windows` / `trusted_connection`) and run the service/scheduler process as the prod Windows service account.

Credential-based prod alternative:

- `prod`: set `SQL_AUTH_MODE_PROD=ad_password` and configure
  `KEYVAULT_SQL_USERNAME_SECRET_NAME_PROD` + `KEYVAULT_SQL_PASSWORD_SECRET_NAME_PROD`.

For this home/local setup with default local SQL Server instance:

- `SQL_SERVER_HOST_DEV=.`
- `SQL_DATABASE_DEV=ingest_dev`
- `SQL_AUTH_MODE_DEV=windows`

And similarly for local prod simulation:

- `SQL_SERVER_HOST_PROD=.`
- `SQL_DATABASE_PROD=ingest_prod`
- `SQL_AUTH_MODE_PROD=windows`

## What to validate

1. SQL server name resolution from config/Key Vault.
2. Database name resolution from config/Key Vault.
3. Connection open success.
4. `SELECT 1` success.
5. Optional: read from config/control table.
6. Confirm effective login identity (`SUSER_SNAME()` / `ORIGINAL_LOGIN()`).

## Identity model reminder (SSPI/integrated auth)

For `windows` / `sspi` / `trusted_connection` / `ad_integrated` modes, SQL identity is
the Windows account running the Python process:

- local dev shell: your current AD/desktop user
- production service host: the configured Windows service account

You do **not** pass SQL username/password for these modes.

## Recommended failure categories

- `SQL_SERVER_UNREACHABLE`
- `SQL_LOGIN_FAILED`
- `SQL_INTEGRATED_AUTH_FAILED`
- `SQL_DATABASE_NOT_FOUND`
- `SQL_PERMISSION_DENIED`
- `SQL_DRIVER_NOT_FOUND`
- `SQL_TIMEOUT`
- `SQL_TLS_TRUST_ERROR`

## Local desktop SQL note

This project now standardizes on local Windows SQL Server for development and
pre-production validation. Prefer integrated auth (`SQL_AUTH_MODE_*=windows`) and
host `.` for local runs.
