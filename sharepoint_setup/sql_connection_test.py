from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

# Ensure the project root is importable when running as:
# python sharepoint_setup/sql_connection_test.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_settings
from src.keyvault_client import maybe_build_provider
from src.sql_client import SqlClient


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
    sql_settings = settings.sql
    auth_mode = (sql_settings.auth_mode or "sql_password").strip().lower()

    if auth_mode not in {"ad_integrated", "integrated", "sspi", "trusted_connection", "active_directory_integrated"}:
        provider = maybe_build_provider(settings.key_vault)
        if provider is not None:
            username, password = provider.get_sql_credentials(env_name)
            sql_settings = replace(sql_settings, username=username, password=password)

    client = SqlClient(sql_settings)
    client.test_connection()

    row = client.query_rows("SELECT DB_NAME() AS current_db, SUSER_SNAME() AS login_name, ORIGINAL_LOGIN() AS original_login")
    current_db = row[0]["current_db"] if row else "unknown"
    login_name = row[0]["login_name"] if row else "unknown"
    original_login = row[0]["original_login"] if row else "unknown"

    print(f"[{env_name}] SQL connectivity successful")
    print(f"[{env_name}] Host     : {sql_settings.host}:{sql_settings.port}")
    print(f"[{env_name}] Database : {current_db}")
    print(f"[{env_name}] AuthMode : {sql_settings.auth_mode}")
    print(f"[{env_name}] Login    : {login_name}")
    print(f"[{env_name}] Original : {original_login}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SQL Server connectivity for local ingestion database")
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
        print(f"SQL pre-check failed for environment(s): {', '.join(failed_envs)}")
        return 1

    print("SQL pre-check passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
