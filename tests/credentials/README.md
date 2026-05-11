# Credential & Security Test Guides

This folder contains operational guides for validating credential health and
authentication paths for the ingestion framework.

These guides are focused on:

- secure handling of credentials (in-memory where possible)
- Azure Key Vault secret validation
- SharePoint app credential/auth validation
- SQL authentication/connectivity validation
- SQL Database Mail capability validation (Layer 5)
- expiry and credential-health checks

## Guides

- `SECURITY.md`
- `KEYVAULT_CONFIGURATION_VALIDATION.md`
- `SHAREPOINT_AUTH_VALIDATION.md`
- `SQL_AUTH_VALIDATION.md`
- `CREDENTIAL_EXPIRY_AND_HEALTHCHECKS.md`

## Intended usage

1. Run Key Vault validation checks first.
2. Run SharePoint credential/auth checks.
3. Run SQL connectivity/auth checks.
4. Run Layer 5 SQL Database Mail capability test.
5. Review expiry/health checks before scheduled ingestion runs.
