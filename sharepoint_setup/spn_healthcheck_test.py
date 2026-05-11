from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# Ensure the project root is importable when running as:
# python sharepoint_setup/spn_healthcheck_test.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_settings
from src.keyvault_client import maybe_build_provider


SUPPORTED_ENVIRONMENTS = ("dev", "prod")

SEVERITY_ORDER = {
    "PASS": 0,
    "WARN": 1,
    "HIGH_WARN": 2,
    "CRITICAL": 3,
    "FAIL": 4,
}


@dataclass
class CredentialCheckResult:
    kind: str
    key_id: str
    display_name: str
    end_date: Optional[datetime]
    days_remaining: Optional[int]
    status: str


def _resolve_target_envs(env_arg: str) -> list[str]:
    normalized = env_arg.lower().strip()
    if normalized == "all":
        return list(SUPPORTED_ENVIRONMENTS)
    if normalized in SUPPORTED_ENVIRONMENTS:
        return [normalized]
    raise ValueError(f"Unsupported --env '{env_arg}'. Use dev, prod, or all.")


def _severity_for(status: str) -> int:
    return SEVERITY_ORDER.get(status.upper(), SEVERITY_ORDER["FAIL"])


def _max_status(a: str, b: str) -> str:
    return a if _severity_for(a) >= _severity_for(b) else b


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_remaining(end_date: datetime, now_utc: datetime) -> int:
    delta_seconds = (end_date - now_utc).total_seconds()
    return math.floor(delta_seconds / 86400)


def _status_for_days(days: int, warn_days: int, high_warn_days: int, critical_days: int) -> str:
    if days < 0:
        return "FAIL"
    if days <= critical_days:
        return "CRITICAL"
    if days <= high_warn_days:
        return "HIGH_WARN"
    if days <= warn_days:
        return "WARN"
    return "PASS"


def _run_az_json(arguments: list[str], allow_failure: bool = False) -> Any:
    # On Windows the Azure CLI is installed as `az.cmd` (a batch file).
    # subprocess.run without shell=True cannot locate batch files by name alone,
    # so we must set shell=True on Windows.
    use_shell = platform.system() == "Windows"
    cmd = ["az", *arguments, "--output", "json"]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        shell=use_shell,
    )

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if completed.returncode != 0:
        if allow_failure:
            return None
        detail = stderr or stdout or "unknown error"
        raise RuntimeError(f"az {' '.join(arguments)} failed: {detail}")

    if not stdout:
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"az {' '.join(arguments)} did not return valid JSON") from exc


def _get_service_principal(app_id: str) -> dict[str, Any]:
    sp = _run_az_json(["ad", "sp", "show", "--id", app_id], allow_failure=True)
    if not sp:
        raise RuntimeError("No service principal found for the configured client ID")
    return sp


def _get_application(app_id: str) -> dict[str, Any]:
    app = _run_az_json(["ad", "app", "show", "--id", app_id], allow_failure=True)
    if not app:
        raise RuntimeError("No application registration found for the configured client ID")
    return app


def _sharepoint_scope_for_site(site_url: str) -> str:
    parsed = urllib.parse.urlparse(site_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid SharePoint site URL: {site_url}")
    return f"{parsed.scheme}://{parsed.netloc}/.default"


def _validate_client_secret_token(tenant_id: str, client_id: str, client_secret: str, scope: str) -> None:
    credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    credential.get_token(scope)


def _get_keyvault_secret_expiry(vault_url: str, secret_name: str) -> Optional[datetime]:
    if not vault_url:
        return None
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    client = SecretClient(vault_url=vault_url, credential=credential)
    secret = client.get_secret(secret_name)
    return secret.properties.expires_on


def _evaluate_credentials(
    credentials: list[dict[str, Any]],
    kind: str,
    now_utc: datetime,
    warn_days: int,
    high_warn_days: int,
    critical_days: int,
) -> list[CredentialCheckResult]:
    result: list[CredentialCheckResult] = []

    for credential in credentials:
        end_date = _parse_datetime(credential.get("endDateTime"))
        key_id = str(credential.get("keyId") or "")
        display_name = str(credential.get("displayName") or "(unnamed)")

        if end_date is None:
            result.append(
                CredentialCheckResult(
                    kind=kind,
                    key_id=key_id,
                    display_name=display_name,
                    end_date=None,
                    days_remaining=None,
                    status="WARN",
                )
            )
            continue

        days = _days_remaining(end_date, now_utc)
        result.append(
            CredentialCheckResult(
                kind=kind,
                key_id=key_id,
                display_name=display_name,
                end_date=end_date,
                days_remaining=days,
                status=_status_for_days(days, warn_days, high_warn_days, critical_days),
            )
        )

    return result


def _validate_thresholds(warn_days: int, high_warn_days: int, critical_days: int) -> None:
    if warn_days < high_warn_days or high_warn_days < critical_days:
        raise ValueError("Invalid thresholds. Expected warn_days >= high_warn_days >= critical_days")


def _run_for_env(env_name: str, args: argparse.Namespace) -> str:
    now_utc = datetime.now(timezone.utc)
    settings = load_settings(env_override=env_name)
    provider = maybe_build_provider(settings.key_vault)

    if provider is not None:
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)
    else:
        env_key = env_name.upper()
        client_id = os.getenv(f"SHAREPOINT_CLIENT_ID_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_ID", "")
        client_secret = os.getenv(f"SHAREPOINT_CLIENT_SECRET_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        tenant_id = os.getenv(f"SHAREPOINT_TENANT_ID_{env_key}", "") or os.getenv("SHAREPOINT_TENANT_ID", "")

    if not (client_id and client_secret and tenant_id):
        raise ValueError("Missing SharePoint credentials from Key Vault and environment fallback")

    status = "PASS"

    print(f"[{env_name}] SPN health check started")
    print(f"[{env_name}] client_id: {client_id}")

    sharepoint_scope = _sharepoint_scope_for_site(settings.sharepoint.site_url)

    try:
        _validate_client_secret_token(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            scope=sharepoint_scope,
        )
        print(f"[{env_name}] token_check: PASS (client secret can acquire SharePoint token)")
    except Exception as exc:
        status = _max_status(status, "FAIL")
        print(f"[{env_name}] token_check: FAIL ({exc})")

    service_principal = _get_service_principal(client_id)
    application = _get_application(client_id)

    sp_display_name = service_principal.get("displayName") or "unknown"
    sp_enabled = bool(service_principal.get("accountEnabled", False))

    print(f"[{env_name}] service_principal: {sp_display_name}")
    print(f"[{env_name}] sp_account_enabled: {sp_enabled}")

    if not sp_enabled:
        status = _max_status(status, "FAIL")

    password_creds = list(application.get("passwordCredentials") or [])
    key_creds = list(application.get("keyCredentials") or []) if args.include_key_credentials else []

    credential_results = _evaluate_credentials(
        credentials=password_creds,
        kind="passwordCredential",
        now_utc=now_utc,
        warn_days=args.warn_days,
        high_warn_days=args.high_warn_days,
        critical_days=args.critical_days,
    )

    if args.include_key_credentials:
        credential_results.extend(
            _evaluate_credentials(
                credentials=key_creds,
                kind="keyCredential",
                now_utc=now_utc,
                warn_days=args.warn_days,
                high_warn_days=args.high_warn_days,
                critical_days=args.critical_days,
            )
        )

    if not credential_results:
        status = _max_status(status, "WARN")
        print(
            f"[{env_name}] credential_expiry: WARN (no credentials returned from Entra; "
            "verify app uses client secret/cert auth and app registration is accessible)"
        )
    else:
        print(f"[{env_name}] credential_expiry: {len(credential_results)} credential(s) found")
        for item in credential_results:
            status = _max_status(status, item.status)
            end_date_display = item.end_date.isoformat() if item.end_date else "unknown"
            days_display = str(item.days_remaining) if item.days_remaining is not None else "unknown"
            key_id_display = item.key_id[:8] + "..." if item.key_id else "(none)"
            print(
                f"[{env_name}] - {item.kind} key_id={key_id_display} display={item.display_name} "
                f"expires={end_date_display} days_remaining={days_display} status={item.status}"
            )

    try:
        kv_expiry = _get_keyvault_secret_expiry(
            vault_url=settings.key_vault.vault_url,
            secret_name=settings.key_vault.client_secret_secret_name,
        )
        if kv_expiry is not None:
            kv_days = _days_remaining(kv_expiry.astimezone(timezone.utc), now_utc)
            kv_status = _status_for_days(kv_days, args.warn_days, args.high_warn_days, args.critical_days)
            status = _max_status(status, kv_status)
            print(
                f"[{env_name}] keyvault_secret_expiry: {kv_expiry.isoformat()} "
                f"(days_remaining={kv_days}, status={kv_status})"
            )
        else:
            print(f"[{env_name}] keyvault_secret_expiry: not set")
    except Exception as exc:
        status = _max_status(status, "WARN")
        print(f"[{env_name}] keyvault_secret_expiry: WARN (unable to read expiry metadata: {exc})")

    print(f"[{env_name}] overall_status: {status}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check SPN active status and credential expiry health for SharePoint ingestion app credentials"
    )
    parser.add_argument("--env", default="prod", help="Environment name: dev, prod, or all")
    parser.add_argument("--warn-days", type=int, default=30, help="Warn threshold in days (default: 30)")
    parser.add_argument("--high-warn-days", type=int, default=14, help="High warn threshold in days (default: 14)")
    parser.add_argument("--critical-days", type=int, default=7, help="Critical threshold in days (default: 7)")
    parser.add_argument(
        "--fail-on",
        default="fail",
        choices=["warn", "high_warn", "critical", "fail"],
        help="Exit with non-zero when status reaches this level or worse (default: fail)",
    )
    parser.add_argument(
        "--include-key-credentials",
        action="store_true",
        help="Also evaluate keyCredentials (certificate credentials), not only passwordCredentials",
    )
    args = parser.parse_args()

    _validate_thresholds(args.warn_days, args.high_warn_days, args.critical_days)

    target_envs = _resolve_target_envs(args.env)
    fail_threshold = _severity_for(args.fail_on.upper())
    highest_seen = "PASS"
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            env_status = _run_for_env(env_name, args)
            highest_seen = _max_status(highest_seen, env_status)
            if _severity_for(env_status) >= fail_threshold:
                failed_envs.append(env_name)
        except Exception as exc:
            failed_envs.append(env_name)
            highest_seen = _max_status(highest_seen, "FAIL")
            print(f"[{env_name}] FAILED: {exc}")

    if failed_envs:
        print(
            "SPN health check reached configured fail threshold "
            f"('{args.fail_on}') for environment(s): {', '.join(failed_envs)}"
        )
        return 1

    print(f"SPN health check passed for all requested environment(s). Highest status seen: {highest_seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
