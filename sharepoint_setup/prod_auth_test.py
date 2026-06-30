"""Validate Azure bootstrap authentication for Key Vault access.

This checks the exact credential chain used by the application to reach
Azure Key Vault before SharePoint/SQL secrets are available.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from azure.keyvault.secrets import SecretClient

from sharepoint_ingest._azure_credential import build_azure_credential
from sharepoint_ingest.config import load_settings


def _run_for_env(env_name: str) -> None:
    settings = load_settings(env_override=env_name)
    if not settings.key_vault.vault_url:
        raise ValueError(f"[{env_name}] KEY_VAULT_URL[_ENV] is not configured")

    auth = settings.azure_auth
    credential = build_azure_credential(
        auth_method=auth.auth_method if auth else None,
        allow_interactive_browser=auth.allow_interactive_browser if auth else False,
        env_name=env_name,
    )

    # Force token acquisition first for a clearer auth-specific failure.
    token = credential.get_token("https://vault.azure.net/.default")
    if not token or not token.token:
        raise RuntimeError("Token acquisition returned an empty token")

    client = SecretClient(vault_url=settings.key_vault.vault_url, credential=credential)
    secret_name = settings.key_vault.client_id_secret_name
    secret = client.get_secret(secret_name)
    if not secret.value:
        raise RuntimeError(f"Secret '{secret_name}' was readable but empty")

    print(f"[{env_name}] PASS: Azure auth method '{auth.auth_method if auth else 'auto'}' can read Key Vault")
    print(f"[{env_name}] Vault : {settings.key_vault.vault_url}")
    print(f"[{env_name}] Secret: {secret_name} (len={len(secret.value)})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Azure bootstrap auth for Key Vault")
    parser.add_argument("--env", choices=["dev", "prod", "all"], default="prod")
    args = parser.parse_args()

    envs = ["dev", "prod"] if args.env == "all" else [args.env]
    failed: list[str] = []
    for env_name in envs:
        try:
            _run_for_env(env_name)
        except Exception as exc:
            failed.append(env_name)
            print(f"[{env_name}] FAILED: {exc}")

    if failed:
        print(f"Azure bootstrap auth failed for: {', '.join(failed)}")
        return 1
    print("Azure bootstrap auth passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
