# Production Azure Authentication Guide

## Why this is needed

Developer workstations can reach Azure Key Vault after `az login`. Production
on-prem Windows service accounts usually cannot use that interactive Azure CLI
profile. The application therefore supports a bootstrap SPN that can authenticate
to Azure first, then read the normal SharePoint and SQL secrets from Key Vault.

## Runtime fallback order

With `AZURE_AUTH_METHOD=auto`, the credential chain is:

1. Azure CLI cached token (`az login`) — useful for dev users.
2. PFX certificate auth (`AZURE_CLIENT_CERTIFICATE_PATH`).
3. Client secret auth (`AZURE_CLIENT_SECRET`).
4. Windows certificate store thumbprint (`AZURE_CLIENT_CERTIFICATE_THUMBPRINT`).
5. Browser auth only when `AZURE_AUTH_INTERACTIVE_BROWSER=1`.

## Secret value fallback order

For both the PFX password and client secret, the app resolves values in this
order:

1. Keyring: `*_KEYRING=service:key`
2. DPAPI encrypted file: `*_FILE=C:\ProgramData\IngestAuth\secret.enc`
3. Plain environment variable: `*_PASSWORD` or `*_SECRET`
4. None — valid for Windows certificate store or no-password PFX.

Supported variables:

- `AZURE_CLIENT_CERTIFICATE_PASSWORD_KEYRING`
- `AZURE_CLIENT_CERTIFICATE_PASSWORD_FILE`
- `AZURE_CLIENT_CERTIFICATE_PASSWORD`
- `AZURE_CLIENT_SECRET_KEYRING`
- `AZURE_CLIENT_SECRET_FILE`
- `AZURE_CLIENT_SECRET`

Each can also be environment-specific, for example
`AZURE_CLIENT_SECRET_PROD` or `AZURE_CLIENT_SECRET_FILE_DEV`.

## Recommended production options

### Best: Windows certificate store

Use `AZURE_AUTH_METHOD=cert_store` with:

```dotenv
AZURE_CLIENT_ID=<spn-client-id>
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_CERTIFICATE_THUMBPRINT=<thumbprint>
```

Import the certificate into `LocalMachine\My` and grant the service account
permission to use the private key. No PFX password is required at runtime.

### Good: PFX file + DPAPI password file

```dotenv
AZURE_AUTH_METHOD=env_cert
AZURE_CLIENT_ID=<spn-client-id>
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_CERTIFICATE_PATH=C:\ProgramData\IngestAuth\spn-kv-reader-prod.pfx
AZURE_CLIENT_CERTIFICATE_PASSWORD_FILE=C:\ProgramData\IngestAuth\pfx-prod.enc
```

Create the encrypted password file as the service account:

```powershell
python tools\protect_secret.py --encrypt --stdin --output C:\ProgramData\IngestAuth\pfx-prod.enc
```

### Acceptable: Client secret + DPAPI/keyring

```dotenv
AZURE_AUTH_METHOD=env_secret
AZURE_CLIENT_ID=<spn-client-id>
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_SECRET_FILE=C:\ProgramData\IngestAuth\client-secret-prod.enc
```

## SPN choice

You may reuse the SharePoint app registration SPN or create a separate
least-privilege Key Vault reader SPN. If using a separate SPN, grant it only
`Key Vault Secrets User` on the target vault.

## Validation

Run:

```powershell
python sharepoint_setup\prod_auth_test.py --env prod
python sharepoint_setup\prod_auth_test.py --env dev
```

This validates token acquisition and an actual Key Vault secret read.
