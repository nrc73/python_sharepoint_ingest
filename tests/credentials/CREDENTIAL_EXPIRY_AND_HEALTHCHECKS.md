# Credential Expiry & Health Checks

Use this guide to proactively detect authentication risk before ingestion jobs
fail at runtime.

## Objective

Detect and report:

- expired or near-expiry secrets
- missing/invalid credential references
- permission drift (consent/grants removed)
- connectivity/auth degradation

## Suggested check cadence

- **Daily** lightweight checks for active environments.
- **Pre-run** checks before scheduled ingestion windows.
- **Release-time** checks before promoting config changes.

## Health check layers

1. **Key Vault layer**
   - required secrets readable
   - optional expiry metadata available
2. **SharePoint auth layer**
   - token acquisition succeeds
   - site/folder access still granted
3. **SQL auth layer**
   - login/integrated auth succeeds
   - minimal query succeeds

## Recommended status model

- `PASS` — fully healthy
- `WARN` — usable but at risk (e.g., secret expires soon)
- `FAIL` — not usable for ingestion

## Suggested warning thresholds

- `WARN` at <= 30 days to expiry
- `HIGH_WARN` at <= 14 days
- `CRITICAL` at <= 7 days
- `FAIL` if expired or unreadable

## Typical actions

### Secret near expiry

1. Create new secret value (app registration or SQL secret source).
2. Update Key Vault secret value.
3. Re-run Key Vault + auth validation.
4. Remove/deprecate old secret after confirmation.

### Permission drift

1. Validate **Office 365 SharePoint Online** application permission `Sites.Selected` is consented.
2. Validate site-specific grant still exists.
3. Re-run SharePoint auth test.

### SQL auth drift

1. Confirm target auth mode.
2. Validate principal/login still exists and has least required permissions.
3. Re-run SQL connectivity test.

## Logging guidance

- include environment, check category, and result code
- do not log secret values or raw tokens
- include next-action guidance for each FAIL/WARN

## Script currently available in this repo

Use the SPN health-check script to validate service principal status and secret
expiry posture:

```bash
python sharepoint_setup/spn_healthcheck_test.py --env all
```

Optional controls:

- `--fail-on critical` to fail CI/pre-run checks at `CRITICAL` or worse
- `--warn-days 30 --high-warn-days 14 --critical-days 7` to tune thresholds
