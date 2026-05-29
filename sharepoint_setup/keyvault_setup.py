from __future__ import annotations

import argparse

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed SharePoint credentials into Azure Key Vault")
    parser.add_argument("--env", required=True, choices=["dev", "prod"], help="Environment name")
    parser.add_argument("--vault-url", required=True, help="Key Vault URL, e.g. https://kv-sp-ingest-prod.vault.azure.net/")

    # SharePoint app registration credentials
    parser.add_argument("--client-id", required=True, help="Entra App Registration client ID")
    parser.add_argument("--client-secret", required=True, help="Entra App Registration client secret")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID")
    parser.add_argument("--site-url", required=False, help="SharePoint site URL (optional)")

    # SharePoint secret name overrides (rarely needed — defaults match KV convention)
    parser.add_argument("--client-id-secret-name", required=False, help="Key Vault secret name for SharePoint client id")
    parser.add_argument(
        "--client-secret-secret-name",
        required=False,
        help="Key Vault secret name for SharePoint client secret",
    )
    parser.add_argument("--tenant-id-secret-name", required=False, help="Key Vault secret name for SharePoint tenant id")
    parser.add_argument("--site-url-secret-name", required=False, help="Key Vault secret name for SharePoint site URL")

    # SQL server / database names (optional — set these to store connection info in KV)
    parser.add_argument("--sql-server", required=False, help="SQL Server hostname")
    parser.add_argument("--sql-int-database", required=False, help="Integrated database name (e.g. ingest_int_prod)")
    parser.add_argument("--sql-stg-database", required=False, help="Staging database name (e.g. ingest_stg_prod)")
    parser.add_argument("--sql-aud-database", required=False, help="Audit database name (e.g. ingest_audit_prod)")

    # SQL server secret name overrides
    parser.add_argument("--sql-server-secret-name", required=False, help="Key Vault secret name for SQL server")
    parser.add_argument("--sql-int-database-secret-name", required=False, help="Key Vault secret name for integrated database")
    parser.add_argument("--sql-stg-database-secret-name", required=False, help="Key Vault secret name for staging database")
    parser.add_argument("--sql-aud-database-secret-name", required=False, help="Key Vault secret name for audit database")

    # Legacy SQL credential secrets (only required for credential-based auth modes)
    parser.add_argument("--sql-username", required=False, help="Optional SQL service account username")
    parser.add_argument("--sql-password", required=False, help="Optional SQL service account password")
    parser.add_argument("--sql-username-secret-name", required=False, help="Key Vault secret name for SQL username")
    parser.add_argument("--sql-password-secret-name", required=False, help="Key Vault secret name for SQL password")

    args = parser.parse_args()

    env_name = args.env.lower().strip()

    # ── SharePoint credential secret names ──────────────────────────────────
    # Default convention: dm-sharepoint-<env>-<type>
    # kv-sp-ingest-dev  → dm-sharepoint-dev-client-id, dm-sharepoint-dev-client-secret, …
    # kv-sp-ingest-prod → dm-sharepoint-prod-client-id, dm-sharepoint-prod-client-secret, …
    client_id_secret_name = args.client_id_secret_name or f"dm-sharepoint-{env_name}-client-id"
    client_secret_secret_name = args.client_secret_secret_name or f"dm-sharepoint-{env_name}-client-secret"
    tenant_id_secret_name = args.tenant_id_secret_name or f"dm-sharepoint-{env_name}-tenant-id"
    site_url_secret_name = args.site_url_secret_name or f"dm-sharepoint-{env_name}-site-url"

    # ── SQL connection secret names ──────────────────────────────────────────
    # Default convention: dm-sql-<env>-<type>
    # kv-sp-ingest-dev  → dm-sql-dev-server, dm-sql-dev-int-database, …
    # kv-sp-ingest-prod → dm-sql-prod-server, dm-sql-prod-int-database, …
    sql_server_secret_name = args.sql_server_secret_name or f"dm-sql-{env_name}-server"
    sql_int_database_secret_name = args.sql_int_database_secret_name or f"dm-sql-{env_name}-int-database"
    sql_stg_database_secret_name = args.sql_stg_database_secret_name or f"dm-sql-{env_name}-stg-database"
    sql_aud_database_secret_name = args.sql_aud_database_secret_name or f"dm-sql-{env_name}-aud-database"

    # ── Legacy SQL credential secret names ──────────────────────────────────
    sql_username_secret_name = args.sql_username_secret_name or f"dm-sql-{env_name}-username"
    sql_password_secret_name = args.sql_password_secret_name or f"dm-sql-{env_name}-password"

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    secret_client = SecretClient(vault_url=args.vault_url, credential=credential)

    # ── Always write SharePoint app registration secrets ─────────────────────
    secret_client.set_secret(client_id_secret_name, args.client_id)
    secret_client.set_secret(client_secret_secret_name, args.client_secret)
    secret_client.set_secret(tenant_id_secret_name, args.tenant_id)

    written: list[str] = [client_id_secret_name, client_secret_secret_name, tenant_id_secret_name]

    if args.site_url:
        secret_client.set_secret(site_url_secret_name, args.site_url)
        written.append(site_url_secret_name)

    # ── Optional SQL server / database secrets ───────────────────────────────
    if args.sql_server:
        secret_client.set_secret(sql_server_secret_name, args.sql_server)
        written.append(sql_server_secret_name)

    if args.sql_int_database:
        secret_client.set_secret(sql_int_database_secret_name, args.sql_int_database)
        written.append(sql_int_database_secret_name)

    if args.sql_stg_database:
        secret_client.set_secret(sql_stg_database_secret_name, args.sql_stg_database)
        written.append(sql_stg_database_secret_name)

    if args.sql_aud_database:
        secret_client.set_secret(sql_aud_database_secret_name, args.sql_aud_database)
        written.append(sql_aud_database_secret_name)

    # ── Optional legacy SQL credential secrets ───────────────────────────────
    if args.sql_username:
        secret_client.set_secret(sql_username_secret_name, args.sql_username)
        written.append(sql_username_secret_name)

    if args.sql_password:
        secret_client.set_secret(sql_password_secret_name, args.sql_password)
        written.append(sql_password_secret_name)

    print(f"Secrets written successfully to '{args.vault_url}' for env='{env_name}':")
    for name in written:
        print(f"  - {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
