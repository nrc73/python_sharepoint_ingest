from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the project root is importable when running as:
# python sharepoint_setup/keyvault_secret_test.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_settings
from src.keyvault_client import KeyVaultSecretProvider


SUPPORTED_ENVIRONMENTS = ("dev", "prod")


def _resolve_target_envs(env_arg: str) -> list[str]:
    normalized = env_arg.lower().strip()
    if normalized == "all":
        return list(SUPPORTED_ENVIRONMENTS)
    if normalized in SUPPORTED_ENVIRONMENTS:
        return [normalized]
    raise ValueError(f"Unsupported --env '{env_arg}'. Use dev, prod, or all.")


def _run_for_env(env_name: str) -> None:
    settings = load_settings(env_override=env_name)
    provider = KeyVaultSecretProvider(settings.key_vault)
    client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)

    print(f"[{env_name}] Key Vault secret retrieval successful")
    print(f"[{env_name}] client_id prefix   : {client_id[:8]}...")
    print(f"[{env_name}] client_secret len : {len(client_secret)}")
    print(f"[{env_name}] tenant_id         : {tenant_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Key Vault secret retrieval")
    parser.add_argument("--env", default="prod", help="Environment name: dev, prod, or all")
    args = parser.parse_args()

    target_envs = _resolve_target_envs(args.env)
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            _run_for_env(env_name)
        except Exception as exc:
            failed_envs.append(env_name)
            print(f"[{env_name}] FAILED: {exc}")

    if failed_envs:
        print(f"Key Vault pre-check failed for environment(s): {', '.join(failed_envs)}")
        return 1

    print("Key Vault pre-check passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
