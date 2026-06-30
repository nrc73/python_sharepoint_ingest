from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

# Ensure the project root is importable when running as:
# python sharepoint_setup/dbmail_send_test.py
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


def _env_with_fallback(base_name: str, env_name: str) -> str:
    env_specific = os.getenv(f"{base_name}_{env_name.upper()}", "").strip()
    if env_specific:
        return env_specific
    return os.getenv(base_name, "").strip()


def _resolve_sql_client(env_name: str) -> SqlClient:
    settings = load_settings(env_override=env_name)
    sql_settings = settings.sql
    auth_mode = sql_settings.auth_mode

    if not is_integrated_auth_mode(auth_mode):
        provider = maybe_build_provider(settings.key_vault, settings.azure_auth)
        if provider is not None:
            username, password = provider.get_sql_credentials(env_name)
            sql_settings = replace(sql_settings, username=username, password=password)

    return SqlClient(sql_settings)


def _resolve_profile_name(env_name: str, cli_profile_name: str | None) -> str:
    if cli_profile_name and cli_profile_name.strip():
        return cli_profile_name.strip()

    profile_name = _env_with_fallback("DBMAIL_PROFILE_NAME", env_name)
    if profile_name:
        return profile_name

    raise ValueError(
        f"No DB Mail profile supplied for env '{env_name}'. "
        f"Use --profile-name or set DBMAIL_PROFILE_NAME_{env_name.upper()} / DBMAIL_PROFILE_NAME."
    )


def _resolve_recipient(env_name: str, cli_to: str | None) -> str:
    if cli_to and cli_to.strip():
        return cli_to.strip()

    recipient = _env_with_fallback("DBMAIL_TEST_TO", env_name)
    if recipient:
        return recipient

    raise ValueError(
        f"No DB Mail recipient supplied for env '{env_name}'. "
        f"Use --to or set DBMAIL_TEST_TO_{env_name.upper()} / DBMAIL_TEST_TO."
    )


def _build_subject(env_name: str) -> str:
    prefix = os.getenv("DBMAIL_TEST_SUBJECT_PREFIX", "sharepoint-ingest").strip() or "sharepoint-ingest"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return f"[{prefix}][{env_name}] Layer 5 DB Mail capability test - {timestamp}"


def _assert_profile_exists(client: SqlClient, profile_name: str) -> None:
    rows = client.query_rows(
        """
        SELECT p.name
        FROM msdb.dbo.sysmail_profile AS p
        WHERE p.name = :profile_name
        """,
        {"profile_name": profile_name},
    )
    if not rows:
        raise RuntimeError(f"DB Mail profile not found: {profile_name}")


def _send_test_email(client: SqlClient, profile_name: str, recipient: str, subject: str, body: str) -> int:
    with client.engine.begin() as conn:
        conn.execute(
            text(
                """
                EXEC msdb.dbo.sp_send_dbmail
                    @profile_name = :profile_name,
                    @recipients = :recipients,
                    @subject = :subject,
                    @body = :body;
                """
            ),
            {
                "profile_name": profile_name,
                "recipients": recipient,
                "subject": subject,
                "body": body,
            },
        )

    recipient_like = f"%{recipient}%"
    for _ in range(5):
        rows = client.query_rows(
            """
            SELECT TOP (1) mailitem_id
            FROM msdb.dbo.sysmail_allitems
            WHERE [subject] = :subject
              AND recipients LIKE :recipient_like
            ORDER BY mailitem_id DESC
            """,
            {"subject": subject, "recipient_like": recipient_like},
        )
        if rows and rows[0].get("mailitem_id") is not None:
            return int(rows[0]["mailitem_id"])
        time.sleep(1)

    raise RuntimeError("sp_send_dbmail executed, but mailitem_id could not be resolved from sysmail_allitems")


def _get_mail_status(client: SqlClient, mailitem_id: int) -> tuple[str, str | None]:
    status_rows = client.query_rows(
        """
        SELECT TOP (1)
            sent_status,
            CAST(last_mod_date AS DATETIME) AS last_mod_date
        FROM msdb.dbo.sysmail_allitems
        WHERE mailitem_id = :mailitem_id
        ORDER BY last_mod_date DESC
        """,
        {"mailitem_id": mailitem_id},
    )

    sent_status = str(status_rows[0].get("sent_status") or "unknown") if status_rows else "unknown"

    event_rows = client.query_rows(
        """
        SELECT TOP (1)
            [description]
        FROM msdb.dbo.sysmail_event_log
        WHERE mailitem_id = :mailitem_id
        ORDER BY log_date DESC
        """,
        {"mailitem_id": mailitem_id},
    )
    description = str(event_rows[0].get("description")) if event_rows and event_rows[0].get("description") else None
    return sent_status, description


def _run_for_env(env_name: str, args: argparse.Namespace) -> None:
    client = _resolve_sql_client(env_name)
    profile_name = _resolve_profile_name(env_name, args.profile_name)
    recipient = _resolve_recipient(env_name, args.to)

    print(f"[{env_name}] Layer 5 DB Mail test started")
    client.test_connection()
    print(f"[{env_name}] sql_connection: PASS")

    _assert_profile_exists(client, profile_name)
    print(f"[{env_name}] dbmail_profile: PASS ({profile_name})")

    subject = _build_subject(env_name)
    body = (
        "SharePoint ingestion Layer 5 DB Mail capability test.\n\n"
        f"Environment: {env_name}\n"
        f"Profile: {profile_name}\n"
        f"UTC Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
    )

    mailitem_id = _send_test_email(
        client=client,
        profile_name=profile_name,
        recipient=recipient,
        subject=subject,
        body=body,
    )
    print(f"[{env_name}] sp_send_dbmail: PASS (mailitem_id={mailitem_id})")

    try:
        sent_status, event_description = _get_mail_status(client, mailitem_id)
        print(f"[{env_name}] dbmail_status: {sent_status}")
        if event_description:
            print(f"[{env_name}] dbmail_event: {event_description}")
    except Exception as exc:
        print(f"[{env_name}] dbmail_status: WARN (unable to query sysmail metadata: {exc})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Layer 5 test: validate SQL Database Mail capability via sp_send_dbmail"
    )
    parser.add_argument("--env", default="prod", help="Environment name: dev, prod, or all")
    parser.add_argument("--profile-name", required=False, help="Database Mail profile name")
    parser.add_argument("--to", required=False, help="Test recipient email address")
    args = parser.parse_args()

    target_envs = _resolve_target_envs(args.env)
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            _run_for_env(env_name, args)
        except Exception as exc:
            failed_envs.append(env_name)
            print(f"[{env_name}] FAILED: {exc}")

    if failed_envs:
        print(f"Layer 5 DB Mail test failed for environment(s): {', '.join(failed_envs)}")
        return 1

    print("Layer 5 DB Mail test passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())