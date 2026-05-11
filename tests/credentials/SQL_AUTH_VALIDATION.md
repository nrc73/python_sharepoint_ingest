# SQL Credential/Auth Validation (Python)

Use this guide to validate SQL connection settings and authentication behavior
per environment.

## Goal

Confirm that the resolved SQL settings for `dev` and `prod` can:

- connect to target SQL server/database
- execute minimal validation query (`SELECT 1`)
- verify basic object visibility (optional)

## Supported auth modes

1. **SQL authentication**
   - Requires username/password secrets in Key Vault.
2. **Integrated authentication (SSPI)**
   - Process should run under service identity.
   - No SQL password should be stored in app secrets.

## Example commands

```powershell
python sharepoint_setup\sql_connection_test.py --env dev
python sharepoint_setup\sql_connection_test.py --env prod
```

For mixed-mode environments:

- `dev`: set `SQL_AUTH_MODE_DEV=ad_integrated` to validate current-user AD auth.
- `prod`: set `SQL_AUTH_MODE_PROD=ad_password` and configure
  `KEYVAULT_SQL_USERNAME_SECRET_NAME_PROD` + `KEYVAULT_SQL_PASSWORD_SECRET_NAME_PROD`
  to validate prod service-account auth.

## What to validate

1. SQL server name resolution from config/Key Vault.
2. Database name resolution from config/Key Vault.
3. Connection open success.
4. `SELECT 1` success.
5. Optional: read from config/control table.
6. Confirm effective login identity (`SUSER_SNAME()` / `ORIGINAL_LOGIN()`).

## Recommended failure categories

- `SQL_SERVER_UNREACHABLE`
- `SQL_LOGIN_FAILED`
- `SQL_INTEGRATED_AUTH_FAILED`
- `SQL_DATABASE_NOT_FOUND`
- `SQL_PERMISSION_DENIED`
- `SQL_DRIVER_NOT_FOUND`
- `SQL_TIMEOUT`
- `SQL_TLS_TRUST_ERROR`

## Local container note

For local SQL Server Docker testing, SQL auth is the most common mode. Treat
this as a practical simulation and reserve full integrated-auth validation for
domain-capable environments.
