# SharePoint + Azure + Local SQL Setup

This folder contains setup and validation assets for:

- local SQL Server 2022 Developer container (Docker Desktop)
- Azure Key Vault secret setup/validation for SharePoint credentials
- SharePoint app-permission connectivity testing

## Prerequisites

1. Docker Desktop installed and running
2. Python virtual environment with `requirements.txt` installed
3. Azure CLI authenticated (`az login`)
4. Rights to create/update app registrations in Entra
5. SharePoint admin access to grant site permissions with PnP PowerShell

## 1) Start local SQL container (SSMS-accessible, persistent)

Use PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\create_sql_container.ps1
```

The startup script now:

- mounts a Docker volume (`sql2022-ingest-data`) for persistence
- ensures both databases exist: `ingest_dev`, `ingest_prod`
- ensures required ingestion tables exist in both databases

To drop all existing user databases and recreate only the expected dev/prod set:

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\create_sql_container.ps1 -ResetUserDatabases
```

Default connectivity configured:

- Server in SSMS: `localhost,1433`
- Authentication: `SQL Server Authentication`
- Login: `sa`
- Password: from `SA_PASSWORD` parameter or `.env`

The script publishes port 1433 and initializes databases `ingest_dev` and `ingest_prod`.

## 2) Initialize SQL schema

```bash
python sharepoint_setup/bootstrap_sql_schema.py --env prod
```

Creates:

- `config.sharepoint_ingestion`
- `log.sharepoint_ingestion_audit`
- `dbo.sample_ingestion_target`

## 3) Seed Azure Key Vault secrets

```bash
python sharepoint_setup/keyvault_setup.py \
  --env prod \
  --vault-url https://keyvault-ingest.vault.azure.net/ \
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
  --vault-url https://keyvault-ingest.vault.azure.net/ \
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
- stores values in Key Vault `keyvault-ingest`:
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
- vault resolution (`KEY_VAULT_NAME` / `KEY_VAULT_URL`)
- token principal details (`appid`, `oid`, `tid`)
- direct RBAC assignments at vault scope
- actual secret-read access for expected secret names

> ⚠️ `-OutputSecretValues` prints plaintext secrets to terminal output. Use only in controlled sessions.

## 4b) Validate SQL connectivity

```bash
python sharepoint_setup/sql_connection_test.py --env prod
```

Use `--env all` to validate both environments in one run.

The SQL test now reports:

- resolved auth mode (`sql_password`, `ad_password`, or `ad_integrated`)
- effective SQL login (`SUSER_SNAME()` / `ORIGINAL_LOGIN()`)

This helps confirm the assumed credentials are actually being used.

## 5) Validate SharePoint app connectivity

```bash
python sharepoint_setup/sharepoint_auth_test.py --env prod --folder "/sites/data_ingestion_prod/General/Input for ETL"
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

- SQL Server in Docker: `host.docker.internal`
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

- If SSMS cannot connect, verify Docker container is running and port `1433` is published.
- If another SQL instance uses 1433, update host port mapping in the script and connect using `localhost,<new_port>`.
- If using ODBC 18, trust server certificate for local container (`TrustServerCertificate=yes`).
- To verify SQL from Python side, run `python sharepoint_setup/sql_connection_test.py --env prod`.
