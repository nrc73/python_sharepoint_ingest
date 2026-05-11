from __future__ import annotations

import argparse

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed SharePoint credentials into Azure Key Vault")
    parser.add_argument("--env", required=True, choices=["dev", "prod"], help="Environment name")
    parser.add_argument("--vault-url", required=True, help="Key Vault URL, e.g. https://keyvault-ingest.vault.azure.net/")
    parser.add_argument("--client-id", required=True, help="Entra App Registration client ID")
    parser.add_argument("--client-secret", required=True, help="Entra App Registration client secret")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID")
    parser.add_argument("--client-id-secret-name", required=False, help="Key Vault secret name for SharePoint client id")
    parser.add_argument(
        "--client-secret-secret-name",
        required=False,
        help="Key Vault secret name for SharePoint client secret",
    )
    parser.add_argument("--tenant-id-secret-name", required=False, help="Key Vault secret name for SharePoint tenant id")
    parser.add_argument("--sql-username", required=False, help="Optional SQL service account username")
    parser.add_argument("--sql-password", required=False, help="Optional SQL service account password")
    parser.add_argument("--sql-username-secret-name", required=False, help="Key Vault secret name for SQL username")
    parser.add_argument("--sql-password-secret-name", required=False, help="Key Vault secret name for SQL password")
    args = parser.parse_args()

    env_name = args.env.lower().strip()

    client_id_secret_name = args.client_id_secret_name or f"dm-sharepoint-client-id-{env_name}"
    client_secret_secret_name = args.client_secret_secret_name or f"dm-sharepoint-client-secret-{env_name}"
    tenant_id_secret_name = args.tenant_id_secret_name or f"dm-sharepoint-tenant-id-{env_name}"

    sql_username_secret_name = args.sql_username_secret_name or f"dm-sql-username-{env_name}"
    sql_password_secret_name = args.sql_password_secret_name or f"dm-sql-password-{env_name}"

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    secret_client = SecretClient(vault_url=args.vault_url, credential=credential)

    secret_client.set_secret(client_id_secret_name, args.client_id)
    secret_client.set_secret(client_secret_secret_name, args.client_secret)
    secret_client.set_secret(tenant_id_secret_name, args.tenant_id)

    if args.sql_username:
        secret_client.set_secret(sql_username_secret_name, args.sql_username)

    if args.sql_password:
        secret_client.set_secret(sql_password_secret_name, args.sql_password)

    print(f"Secrets written successfully for env='{env_name}':")
    print(f"- {client_id_secret_name}")
    print(f"- {client_secret_secret_name}")
    print(f"- {tenant_id_secret_name}")

    if args.sql_username:
        print(f"- {sql_username_secret_name}")
    if args.sql_password:
        print(f"- {sql_password_secret_name}")

    print("Remember to align .env secret-name variables to these names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
