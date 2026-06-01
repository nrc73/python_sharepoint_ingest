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

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sql_client import SqlClient, is_integrated_auth_mode


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
    auth_mode = settings.sql.auth_mode

    provider = maybe_build_provider(settings.key_vault)

    # ── Resolve SQL server + database names from Key Vault ──────────────────
    kv_server: str | None = None
    kv_aud_db: str | None = None
    kv_stg_db: str | None = None
    kv_int_db: str | None = None

    if provider is not None:
        try:
            kv_info = provider.get_sql_connection_info()
            kv_server  = kv_info.get("server")
            kv_aud_db  = kv_info.get("aud_database")
            kv_stg_db  = kv_info.get("stg_database")
            kv_int_db  = kv_info.get("int_database")
        except Exception as exc:
            print(f"[{env_name}] Warning: could not resolve SQL connection info from Key Vault: {exc}")

    # Override host (and optionally port) if Key Vault returned a server name.
    # KV secrets may store the server as plain "hostname" or as "hostname:port"
    # or "hostname,port" (SQL Server named instance / port suffix styles).
    def _parse_kv_server(kv_server_value: str) -> tuple[str, int | None]:
        """Return (host, port_or_None) from a KV server string."""
        value = kv_server_value.strip()
        # SQL Server style: HOST,PORT
        if "," in value:
            parts = value.split(",", 1)
            try:
                return parts[0].strip(), int(parts[1].strip())
            except ValueError:
                return value, None
        # URI style: HOST:PORT (only split if the last segment is numeric)
        if ":" in value:
            parts = value.rsplit(":", 1)
            try:
                return parts[0].strip(), int(parts[1].strip())
            except ValueError:
                return value, None
        return value, None

    def _apply_kv_overrides(base_settings, kv_host_raw, kv_database):
        updated = base_settings
        if kv_host_raw:
            host_clean, port_clean = _parse_kv_server(kv_host_raw)
            updated = replace(updated, host=host_clean)
            if port_clean is not None:
                updated = replace(updated, port=port_clean)
        if kv_database:
            updated = replace(updated, database=kv_database)
        return updated

    aud_settings = _apply_kv_overrides(settings.sql,     kv_server, kv_aud_db)
    stg_settings = _apply_kv_overrides(settings.sql_stg, kv_server, kv_stg_db)
    int_settings = _apply_kv_overrides(settings.sql_int, kv_server, kv_int_db)

    # ── Resolve SQL credentials from Key Vault for credential-based auth ────
    if not is_integrated_auth_mode(auth_mode) and provider is not None:
        try:
            username, password = provider.get_sql_credentials(env_name)
            aud_settings = replace(aud_settings, username=username, password=password)
            stg_settings = replace(stg_settings, username=username, password=password)
            int_settings = replace(int_settings, username=username, password=password)
        except Exception as exc:
            print(f"[{env_name}] Warning: could not resolve SQL credentials from Key Vault: {exc}")

    # ── Report resolved host ─────────────────────────────────────────────────
    host_source = "Key Vault" if kv_server else "env / .env"
    print(f"[{env_name}] SQL host   : {aud_settings.host}:{aud_settings.port}  (source: {host_source})")
    print(f"[{env_name}] AuthMode   : {auth_mode}")

    # ── Test each database ───────────────────────────────────────────────────
    db_specs = [
        ("AUD", aud_settings, "audit DB (config + log)"),
        ("STG", stg_settings, "staging DB"),
        ("INT", int_settings, "integrated DB"),
    ]

    for label, db_settings, description in db_specs:
        db_source = "Key Vault" if (
            (label == "AUD" and kv_aud_db) or
            (label == "STG" and kv_stg_db) or
            (label == "INT" and kv_int_db)
        ) else "env / .env"

        try:
            client = SqlClient(db_settings)
            client.test_connection()
            rows = client.query_rows(
                "SELECT DB_NAME() AS current_db, SUSER_SNAME() AS login_name"
            )
            current_db = rows[0]["current_db"] if rows else "unknown"
            login_name = rows[0]["login_name"] if rows else "unknown"
            print(
                f"[{env_name}] [{label}] PASS  db={current_db!r}  "
                f"login={login_name!r}  ({description}, source: {db_source})"
            )
        except Exception as exc:
            raise RuntimeError(
                f"[{label}] {description} connectivity failed "
                f"(host={db_settings.host}, db={db_settings.database}): {exc}"
            ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SQL Server connectivity for local ingestion databases")
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
