from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from pathlib import Path

# Ensure the project root is importable when running as:
# python sharepoint_setup/sharepoint_auth_test.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import KeyVaultSecretProvider, maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient


SUPPORTED_ENVIRONMENTS = ("dev", "prod")


def _resolve_target_envs(env_arg: str) -> list[str]:
    normalized = env_arg.lower().strip()
    if normalized == "all":
        return list(SUPPORTED_ENVIRONMENTS)
    if normalized in SUPPORTED_ENVIRONMENTS:
        return [normalized]
    raise ValueError(f"Unsupported --env '{env_arg}'. Use dev, prod, or all.")


def _folder_for_env(env_name: str, default_folder: str | None) -> str:
    env_key = env_name.upper()
    env_specific = os.getenv(f"SHAREPOINT_TEST_FOLDER_{env_key}", "").strip()
    if env_specific:
        return env_specific

    if default_folder:
        return default_folder

    raise ValueError(
        f"No folder supplied for env '{env_name}'. Use --folder or set SHAREPOINT_TEST_FOLDER_{env_key}."
    )


def _resolve_site_url_from_keyvault(
    provider: KeyVaultSecretProvider,
    secret_name: str,
    vault_url: str,
    env_name: str,
) -> str:
    """Read the SharePoint site URL exclusively from Key Vault.

    On failure, raises a descriptive exception that includes the vault URL,
    secret name, and the stored value when readable (to help diagnose blank
    or incorrectly-seeded secrets).

    No environment-variable fallback is used — the Key Vault secret is the
    single authoritative source.
    """
    try:
        value = provider.get_secret(secret_name)
    except Exception as exc:
        raise ValueError(
            f"SP_SITE_URL_KEYVAULT_READ_FAILED\n"
            f"  Key Vault URL       : {vault_url}\n"
            f"  Key Vault secret    : {secret_name}\n"
            f"  Stored secret value : <READ FAILED — {exc}>\n"
            f"  Resolution          : Ensure the secret exists in the vault and\n"
            f"                        that your az CLI identity has 'Key Vault Secrets User'\n"
            f"                        RBAC at the vault scope.\n"
            f"  Seeding command     : python sharepoint_setup/keyvault_setup.py --env {env_name} "
            f"--vault-url {vault_url} --site-url https://<tenant>.sharepoint.com/sites/<site-name> ..."
        ) from exc

    stored_display = repr(value) if value is not None else "<None>"

    if not value or not value.strip():
        raise ValueError(
            f"SP_SITE_URL_MISSING_IN_KEYVAULT\n"
            f"  Key Vault URL       : {vault_url}\n"
            f"  Key Vault secret    : {secret_name}\n"
            f"  Stored secret value : {stored_display}\n"
            f"  Expected shape      : https://<tenant>.sharepoint.com/sites/<site-name>\n"
            f"  Resolution          : Seed the secret with the correct SharePoint site URL:\n"
            f"                        python sharepoint_setup/keyvault_setup.py --env {env_name} "
            f"--vault-url {vault_url} --site-url https://<tenant>.sharepoint.com/sites/<site-name> ..."
        )

    return value.strip()


def _format_sharepoint_error(exc: Exception) -> str:
    """Map common SharePoint/Graph errors to actionable diagnostics.

    The client now uses the Microsoft Graph API (graph.microsoft.com) with
    scope ``https://graph.microsoft.com/.default`` instead of the legacy
    SharePoint REST (/_api/) path.  The legacy path was blocked by the
    ``x-ms-suspended-features`` app-only feature gate on this tenant with:

        "Unsupported app only token"

    The Graph API path requires ``Sites.ReadWrite.All`` on the **Graph**
    resource (00000003-0000-0000-c000-000000000000), not the SPO resource.
    """
    message = str(exc)
    lowered = message.lower()

    if "unsupported app only token" in lowered:
        return (
            "SP_UNSUPPORTED_APP_ONLY_TOKEN: SharePoint REST /_api/ endpoint rejected the "
            "app-only token via the x-ms-suspended-features gate. This tenant blocks all "
            "SPO REST app-only access regardless of Sites.ReadWrite.All on the SPO resource. "
            "The client should be using the Graph API path (scope: graph.microsoft.com/.default). "
            "Ensure SharePointClient is using the rewritten Graph-API-based implementation. "
            f"Details: {message}"
        )

    if "401" in message or "unauthorized" in lowered or "invalid_client" in lowered:
        return (
            "SP_AUTH_UNAUTHORIZED: Graph API returned 401. Common causes: "
            "(1) Bad credentials — check client_secret in Key Vault; "
            "(2) Missing Sites.ReadWrite.All on the GRAPH resource — confirm via "
            "'az rest --url .../servicePrincipals/{sp_id}/appRoleAssignments' that "
            "appRoleId=9492366f-7969-46a4-8d15-ed1a20078fff is assigned to the SPN; "
            "(3) Admin consent not yet propagated — wait 2-3 minutes and retry. "
            f"Details: {message}"
        )

    if "403" in message or "forbidden" in lowered or "access denied" in lowered:
        return (
            "SP_FORBIDDEN: Graph token is valid but site access is denied (403). "
            "Ensure Sites.ReadWrite.All (Graph) AppRoleAssignment exists for this SPN. "
            "Check: az rest --url 'https://graph.microsoft.com/v1.0/servicePrincipals/{id}/appRoleAssignments' "
            f"Details: {message}"
        )

    if "404" in message or "not found" in lowered:
        return (
            "SP_FOLDER_OR_SITE_NOT_FOUND: site URL or folder path is wrong (404). "
            "Verify the 'dm-sharepoint-{env}-site-url' secret in Key Vault and the folder server-relative path. "
            f"Details: {message}"
        )

    if "generalexception" in lowered or "general exception" in lowered:
        return (
            "SP_GRAPH_GENERAL_EXCEPTION: Graph returned a general exception. "
            "This often means Sites.ReadWrite.All Graph AppRoleAssignment is missing or "
            "admin consent has not propagated. Wait 2-3 minutes and retry. "
            f"Details: {message}"
        )

    if "sp_site_url_missing_in_keyvault" in lowered or "sp_site_url_keyvault_read_failed" in lowered:
        # Already a well-formatted diagnostic — pass through as-is
        return message

    return f"SP_AUTH_UNKNOWN_ERROR: {message}"


def _run_for_env(env_name: str, default_folder: str | None) -> None:
    settings = load_settings(env_override=env_name)
    provider = maybe_build_provider(settings.key_vault, settings.azure_auth)

    if provider is None:
        vault_url = settings.key_vault.vault_url or settings.key_vault.vault_name
        raise ValueError(
            f"SP_NO_KEYVAULT_CONFIGURED\n"
            f"  No Key Vault URL could be resolved for env '{env_name}'.\n"
            f"  Expected env vars   : KEY_VAULT_URL_{env_name.upper()} or KEY_VAULT_NAME_{env_name.upper()}\n"
            f"  Current value       : {vault_url!r}\n"
            f"  Resolution          : Set KEY_VAULT_URL_{env_name.upper()}=https://<vault-name>.vault.azure.net/ in .env"
        )

    vault_url = settings.key_vault.vault_url
    site_url_secret = settings.key_vault.site_url_secret_name or f"dm-sharepoint-{env_name.lower()}-site-url"

    # Resolve SharePoint site URL from Key Vault — no env-var fallback.
    site_url = _resolve_site_url_from_keyvault(provider, site_url_secret, vault_url, env_name)

    client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)

    if not (client_id and client_secret and tenant_id):
        raise ValueError("Missing SharePoint credentials from Key Vault")

    folder = _folder_for_env(env_name, default_folder)

    sp_client = SharePointClient(
        site_url=site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    )

    count = sp_client.get_file_count(folder)
    print(f"[{env_name}] SharePoint authentication successful")
    print(f"[{env_name}] Site URL         : {site_url}")
    print(f"[{env_name}] Site URL source  : Key Vault secret '{site_url_secret}' in {vault_url}")
    print(f"[{env_name}] Folder           : {folder}")
    print(f"[{env_name}] FileCount        : {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SharePoint app authentication and folder listing")
    parser.add_argument("--env", default="prod", help="Environment name: dev, prod, or all")
    parser.add_argument(
        "--folder",
        required=False,
        help="SharePoint server-relative folder path to test, e.g. /sites/data_ingestion_prod/General/Input for ETL",
    )
    args = parser.parse_args()

    target_envs = _resolve_target_envs(args.env)
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            _run_for_env(env_name, args.folder)
        except Exception as exc:
            failed_envs.append(env_name)
            print(f"[{env_name}] FAILED: {_format_sharepoint_error(exc)}")

    if failed_envs:
        print(f"SharePoint pre-check failed for environment(s): {', '.join(failed_envs)}")
        return 1

    print("SharePoint pre-check passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
