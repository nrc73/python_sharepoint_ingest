# SharePoint + Azure + Local SQL Setup

This folder contains setup and validation assets for:

- local SQL Server (Windows-installed instance) as the primary dev host
- Azure Key Vault secret setup/validation for SharePoint credentials
- SharePoint app-permission connectivity testing

## Prerequisites

1. SQL Server installed locally (default instance or named instance)
2. Python virtual environment with `requirements.txt` installed
3. Azure CLI authenticated (`az login`)
4. Rights to create/update app registrations in Entra
5. SharePoint admin access to grant site permissions with PnP PowerShell

## 1) Preferred local SQL target (Windows SQL Server + SSPI)

For home/dev setups, use Windows Integrated auth against the local SQL instance.

Recommended `.env` settings:

```dotenv
SQL_SERVER_HOST=.
SQL_SERVER_HOST_DEV=.
SQL_AUTH_MODE_DEV=sspi
SQL_DATABASE_DEV=ingest_dev

SQL_SERVER_HOST_PROD=your-prod-sql-server.database.windows.net
SQL_AUTH_MODE_PROD=sspi
SQL_DATABASE_PROD=ingest_prod
```

This aligns with a common mixed setup:

- **dev** runs under a regular AD/Windows user (current interactive identity)
- **prod** runs under a Windows service account (service/scheduler runtime identity)

For integrated auth modes (`windows` / `sspi` / `trusted_connection` /
`ad_integrated` / `active_directory_integrated`), SQL identity is the Windows account
running the Python process.

If prod instead uses explicit credentials, set `SQL_AUTH_MODE_PROD=ad_password` and
configure the prod SQL credential secret names in Key Vault.

Validate:

```powershell
python sharepoint_setup\sql_connection_test.py --env all
```

Ensure your local SQL instance includes `ingest_dev` and `ingest_prod`.
If needed, create both databases in SSMS and run `bootstrap_sql_schema.py` for each environment.

## 2) Initialize SQL schema

```bash
python sharepoint_setup/bootstrap_sql_schema.py --env prod
```

Creates:

- `config.sharepoint_ingestion`
- `log.sharepoint_ingestion_audit`
- `sharepoint.sample_ingestion_target`

## 3) Seed Azure Key Vault secrets

This solution now uses separate vaults per environment:

- `kv-sp-ingest-dev`
- `kv-sp-ingest-prod`

```bash
python sharepoint_setup/keyvault_setup.py \
  --env prod \
  --vault-url https://kv-sp-ingest-prod.vault.azure.net/ \
  --client-id <APP_CLIENT_ID> \
  --client-secret <APP_CLIENT_SECRET> \
  --tenant-id <TENANT_ID>
```

Default secrets written:

- `dm-sharepoint-client-id-prod`
- `dm-sharepoint-client-secret-prod`
- `dm-sharepoint-tenant-id-prod`

For dev, run with `--env dev` (writes `...-dev` names).

Optional SQL secrets can also be seeded for prod service-account auth:

```bash
python sharepoint_setup/keyvault_setup.py \
  --env prod \
  --vault-url https://kv-sp-ingest-prod.vault.azure.net/ \
  --client-id <APP_CLIENT_ID> \
  --client-secret <APP_CLIENT_SECRET> \
  --tenant-id <TENANT_ID> \
  --sql-username <DOMAIN\\svc_ingest_prod> \
  --sql-password <PASSWORD>
```

Then align `.env` secret-name variables for each environment:

- `KEYVAULT_CLIENT_ID_SECRET_NAME_DEV`, `KEYVAULT_CLIENT_SECRET_SECRET_NAME_DEV`, `KEYVAULT_TENANT_ID_SECRET_NAME_DEV`
- `KEYVAULT_CLIENT_ID_SECRET_NAME_PROD`, `KEYVAULT_CLIENT_SECRET_SECRET_NAME_PROD`, `KEYVAULT_TENANT_ID_SECRET_NAME_PROD`
- `KEYVAULT_SQL_USERNAME_SECRET_NAME_PROD`, `KEYVAULT_SQL_PASSWORD_SECRET_NAME_PROD`

## 3b) Provision Entra app registrations + Key Vault secrets + SharePoint `Sites.Selected`

Use the provisioning script to create/refine dev/prod SharePoint app registrations and push their credentials/config into Key Vault.

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\provision_sharepoint_app_registrations.ps1 -Env all
```

What this script does:

- creates/reuses app registrations:
  - `spn-sharepoint-ingest-dev`
  - `spn-sharepoint-ingest-prod`
- ensures service principals exist
- assigns **Office 365 SharePoint Online** application permission `Sites.Selected`
- attempts admin consent
- generates client secrets (or keeps existing unless `-RotateClientSecrets`)
- stores values in environment-specific Key Vaults:
  - `kv-sp-ingest-dev` (for `-Env dev`)
  - `kv-sp-ingest-prod` (for `-Env prod`)
  - `dm-sharepoint-<env>-client-id`
  - `dm-sharepoint-<env>-client-secret`
  - `dm-sharepoint-<env>-tenant-id`
  - `dm-sharepoint-<env>-site-url`
  - `dm-sql-<env>-server`
  - `dm-sql-<env>-database`
- prints the SharePoint/PnP commands to grant site-specific `Write` access

> Runtime and setup in this repo are SharePoint-API based; Microsoft Graph permissions are not required.

Useful options:

```powershell
# Force new client secrets
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\provision_sharepoint_app_registrations.ps1 -Env all -RotateClientSecrets

# Skip steps that may require elevated tenant admin rights
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\provision_sharepoint_app_registrations.ps1 -Env all -SkipAdminConsent -SkipSiteGrant
```

> Note: Tenant operations (admin consent) may require Global Admin / Privileged Role Admin. Site grants require SharePoint Admin privileges.

## 4) Validate Key Vault secret reads

```bash
python sharepoint_setup/keyvault_secret_test.py --env prod
```

Environment selector supports:

- `--env dev`
- `--env prod`
- `--env all` (run pre-check for both dev and prod)

## 4c) Validate Key Vault context + RBAC + secret read access (Dev/Prod)

Use the PowerShell validator for consistent environment setup checks across dev/prod.

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\validate_keyvault_rbac.ps1 -Env prod -TestSecrets
```

Run both environments:

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\validate_keyvault_rbac.ps1 -Env all -TestSecrets
```

Optional context auto-switch (when expected subscription is configured):

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\validate_keyvault_rbac.ps1 -Env prod -TestSecrets -FixContext
```

Print plaintext secret values to screen (never writes to file):

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\validate_keyvault_rbac.ps1 -Env prod -TestSecrets -OutputSecretValues
```

What the script validates:

- active Azure CLI tenant/subscription context
- vault resolution (`KEY_VAULT_NAME[_ENV]` / `KEY_VAULT_URL[_ENV]`)
- token principal details (`appid`, `oid`, `tid`)
- direct RBAC assignments at vault scope
- actual secret-read access for expected secret names

Recommended `.env` configuration for context and vault selection:

- `AZURE_SUBSCRIPTION_ID=<subscription-guid>`
- `AZURE_TENANT_ID=<tenant-guid>`
- `AZURE_RESOURCE_GROUP=<resource-group>`
- `KEY_VAULT_NAME_DEV=kv-sp-ingest-dev`
- `KEY_VAULT_URL_DEV=https://kv-sp-ingest-dev.vault.azure.net/`
- `KEY_VAULT_NAME_PROD=kv-sp-ingest-prod`
- `KEY_VAULT_URL_PROD=https://kv-sp-ingest-prod.vault.azure.net/`

> ⚠️ `-OutputSecretValues` prints plaintext secrets to terminal output. Use only in controlled sessions.

## 4b) Validate SQL connectivity

```bash
python sharepoint_setup/sql_connection_test.py --env prod
```

Use `--env all` to validate both environments in one run.

The SQL test now reports:

- resolved auth mode (for example `sql_password`, `ad_password`, `active_directory_password`,
  `windows`, `sspi`, `trusted_connection`, `ad_integrated`, `active_directory_integrated`,
  or `managed_identity`)
- effective SQL login (`SUSER_SNAME()` / `ORIGINAL_LOGIN()`)

For Windows/local mode you should normally see your desktop identity (for example
`DESKTOP-UAOR3B4\User`) as the effective login.

This helps confirm the assumed credentials are actually being used.

## 5) Validate SharePoint app connectivity

```bash
python sharepoint_setup/sharepoint_auth_test.py --env prod --folder "/sites/data_ingestion_prod/Documents"
```

For all-environment checks, either:

- pass one shared folder with `--folder`, or
- set env-specific folders:
  - `SHAREPOINT_TEST_FOLDER_DEV`
  - `SHAREPOINT_TEST_FOLDER_PROD`

Then run:

```bash
python sharepoint_setup/sharepoint_auth_test.py --env all
```

This checks app-based auth and attempts folder listing.

## 5a) Provision dev/prod ingestion-group folders in SharePoint

Use the folder provisioning helper to create one folder per ingestion group, with
`Processed` and `Failed` subfolders under each:

- `valid_customers/{Processed,Failed}`
- `valid_transactions/{Processed,Failed}`
- `valid_transactions_large/{Processed,Failed}`

Run for dev:

```bash
python sharepoint_setup/provision_sharepoint_folders.py --env dev
```

Run for both environments:

```bash
python sharepoint_setup/provision_sharepoint_folders.py --env all
```

Optional: force a specific document library name (for example `Documents`):

```bash
python sharepoint_setup/provision_sharepoint_folders.py --env dev --library Documents
```

## 5b) Validate SPN active status + credential expiry

```bash
python sharepoint_setup/spn_healthcheck_test.py --env prod
```

Use `--env all` to run both dev and prod.

What this check validates:

- configured SPN client secret can still acquire a token
- service principal account is enabled (`accountEnabled=true`)
- application `passwordCredentials` expiry windows (WARN/HIGH_WARN/CRITICAL/FAIL)
- optional Key Vault secret expiry metadata for the client secret reference

Useful options:

- `--fail-on critical` (fail pipeline when status is CRITICAL or worse)
- `--include-key-credentials` (also evaluate certificate credentials)
- `--notify-on warn|high_warn|critical|fail` (email threshold)
- `--notify-email-to` / `--notify-email-cc` (single consolidated email recipients)

Consolidated notification example (one email per run, subject includes highest severity):

```bash
python sharepoint_setup/spn_healthcheck_test.py \
  --env all \
  --notify-on warn \
  --notify-email-to "ops@example.com;platform@example.com" \
  --notify-email-cc "manager@example.com,security@example.com"
```

Safe simulation example (no Azure expiry metadata/credential changes):

```bash
python sharepoint_setup/spn_healthcheck_test.py \
  --env prod \
  --simulate-expiry-days 20 \
  --notify-on warn \
  --notify-email-to "ops@example.com" \
  --notify-email-cc "manager@example.com"
```

When simulation is enabled, output and email details are marked with `SIMULATED`.

## 5c) Layer 5 — Validate SQL Database Mail capability (`sp_send_dbmail`)

```bash
python sharepoint_setup/dbmail_send_test.py --env prod --profile-name "Prod SQL Mail" --to "you@company.com"
```

You can also use `.env` defaults:

- `DBMAIL_PROFILE_NAME` / `DBMAIL_TEST_TO`
- `DBMAIL_PROFILE_NAME_DEV` / `DBMAIL_TEST_TO_DEV`
- `DBMAIL_PROFILE_NAME_PROD` / `DBMAIL_TEST_TO_PROD`

Then run:

```bash
python sharepoint_setup/dbmail_send_test.py --env prod
```

What Layer 5 validates:

- SQL connectivity to the target instance/database
- DB Mail profile existence in `msdb.dbo.sysmail_profile`
- ability to execute `msdb.dbo.sp_send_dbmail`
- returned `mailitem_id` and best-effort status/event lookup from `msdb` metadata

### Local development shortcut (no SendGrid/domain): smtp4dev

If you only need development validation and want to avoid DNS/DMARC/provider setup,
run a local SMTP capture server.

Start smtp4dev:

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\create_local_smtp4dev.ps1
```

Then configure SQL Database Mail profile using:

```text
sql/configure_local_dbmail_profile.sql
```

Important SMTP host value:

- SQL Server on Windows host: `localhost`

Finally run Layer 5 test:

```bash
python sharepoint_setup/dbmail_send_test.py --env dev --profile-name "Dev Local SMTP" --to "dev-test@local.invalid"
```

View captured messages in smtp4dev web UI:

```text
http://localhost:5000
```

## Notes on SSMS access

- If SSMS cannot connect, verify the SQL Server service is running.
- If using ODBC 18 for local SQL, `TrustServerCertificate=yes` can simplify local TLS behavior.
- To verify SQL from Python side, run `python sharepoint_setup/sql_connection_test.py --env prod`.

---

## Python Guide standards audit summary (`docs.python-guide.org`)

This section captures the deep standards review completed for this repository against guidance in [The Hitchhiker's Guide to Python](https://docs.python-guide.org).

### Audit scope

- Repository structure and package layout (`sharepoint_ingest/`, `src/`, `sharepoint_setup/`, `tools/`, `tests/`)
- Packaging and tooling (`pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.pre-commit-config.yaml`, `.gitignore`)
- Core runtime modules and import hygiene
- Test/runtime health checks and dependency consistency

### Verification checks executed

- `python -B -m pytest -q` → `40 passed`
- `python -m pip check` → `No broken requirements found`
- `python -B -m compileall -q sharepoint_ingest tests sharepoint_setup tools` → passed
- import/reference scans for legacy `src` imports in active code paths

To avoid generating bytecode cache artifacts (`__pycache__`, `*.pyc`) during ad-hoc local runs,
prefer `python -B ...` and/or set:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
```

### Compliance outcome

#### ✅ Strong alignment areas

1. **Environment/dependency discipline**
   - Virtual environment workflow documented and used.
   - Runtime and dev dependencies are separated (`requirements.txt` + `requirements-dev.txt`).

2. **Modern packaging metadata**
   - `pyproject.toml` is present with build-system metadata and tool sections.

3. **Testing baseline is healthy**
   - Automated tests are organized under `tests/` and currently all passing.

4. **Code organization and modularity**
   - Core ingestion code is split into focused modules (config, engine, clients, validator, notifications).

5. **Import/style anti-pattern reduction**
   - No wildcard imports (`from x import *`) found in audited code.

#### ⚠️ Partial alignment / cleanup items

1. **Dual package roots exist (`src/` and `sharepoint_ingest/`)**
   - Both trees currently exist and contain near-duplicate module sets.
   - This can create ambiguity for import path ownership.

2. **Pytest config appears in two places**
   - `pytest.ini` and `[tool.pytest.ini_options]` in `pyproject.toml` both exist.
   - Prefer a single source of truth.

3. **Docstring consistency gaps outside core package**
   - Several setup/tools/test modules still lack module-level docstrings.

4. **Lint tooling configured but not guaranteed installed in all environments**
   - Ruff config exists; ensure dev extras are installed where lint checks run.

5. **Repository hygiene cleanup still recommended**
   - Stray root artifacts detected (`2.32.3`, `=`).
   - Local `__pycache__` folders are present (not unusual locally, but should remain ignored/untracked).

### Priority remediation plan

#### P0 (high impact)

- Consolidate to one canonical package path (`sharepoint_ingest`) and phase out duplicate implementation ownership in `src/`.
- Enforce CI quality gates for:
  - `pytest -q`
  - `ruff check`

#### P1

- Unify pytest configuration into one location (prefer `pyproject.toml`).
- Remove stray root artifacts (`2.32.3`, `=`).

#### P2

- Add module-level docstrings for remaining setup/tool/test modules.
- Optionally rename operational scripts that end with `_test.py` to reduce ambiguity with test discovery semantics.

### Bottom line

The project is in **good shape and broadly aligned** with Python Guide recommendations for packaging, dependencies, and testability. The most important remaining improvement is to fully converge on a **single canonical package root** and complete a small set of tooling/repository hygiene cleanups.
