"""CLI entrypoint for running configured SharePoint ingestion workflows."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from typing import Optional

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.logging_utils import configure_logging
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient, is_integrated_auth_mode


PROD_BLOCKED_SCOPES = {"test", "validation", "perf_test", "all"}


def _validate_prod_guard_rails(settings, ingestion_scope: str) -> None:
    normalized_scope = (ingestion_scope or "real").strip().lower()
    if settings.env_name != "prod":
        return
    if settings.allow_test_data_in_prod:
        return
    if normalized_scope in PROD_BLOCKED_SCOPES:
        raise ValueError(
            "Guard rail violation: non-real ingestion scopes are blocked in prod "
            "(test/validation/perf_test/all). Set ALLOW_TEST_DATA_IN_PROD=1 only for "
            "explicit break-glass scenarios."
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SharePoint ingestion runner")
    parser.add_argument("--env", default=None, help="Execution environment: dev, test, prod")
    parser.add_argument("--process-id", default=None, help="Optional process_id filter")
    parser.add_argument("--workflow-id", default=None, help="Optional workflow_id filter")
    parser.add_argument(
        "--ingestion-scope",
        default="real",
        choices=["real", "test", "validation", "perf_test", "all"],
        help="Filter ingestion configs by scope (default: real)",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive configs (is_active=0) in run",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logs (equivalent to LOG_LEVEL=DEBUG)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate SQL/SharePoint connectivity and selected config filters without loading data",
    )
    return parser


def _resolve_sharepoint_credentials(settings, provider=None) -> tuple[str, str, str]:
    provider = provider or maybe_build_provider(settings.key_vault)
    if provider is not None:
        return provider.get_sharepoint_credentials(settings.env_name)

    import os

    env_key = settings.env_name.upper()
    client_id = os.getenv(f"SHAREPOINT_CLIENT_ID_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_ID", "")
    client_secret = os.getenv(f"SHAREPOINT_CLIENT_SECRET_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
    tenant_id = os.getenv(f"SHAREPOINT_TENANT_ID_{env_key}", "") or os.getenv("SHAREPOINT_TENANT_ID", "")
    if not (client_id and client_secret and tenant_id):
        raise ValueError(
            "SharePoint credentials not available. Configure Key Vault or "
            "SHAREPOINT_CLIENT_ID[_ENV]/SHAREPOINT_CLIENT_SECRET[_ENV]/SHAREPOINT_TENANT_ID[_ENV]."
        )
    return client_id, client_secret, tenant_id


def _resolve_sql_settings(settings, provider=None):
    sql_settings = settings.sql
    auth_mode = sql_settings.auth_mode

    if is_integrated_auth_mode(auth_mode):
        return sql_settings

    provider = provider or maybe_build_provider(settings.key_vault)
    if provider is not None:
        try:
            username, password = provider.get_sql_credentials(settings.env_name)
            return replace(sql_settings, username=username, password=password)
        except Exception:
            # allow existing env-based SQL settings to be used as fallback
            pass

    if not sql_settings.username or not sql_settings.password:
        raise ValueError(
            "SQL credentials are required for the configured auth mode. "
            "Provide KEYVAULT_SQL_USERNAME_SECRET_NAME[_ENV] / KEYVAULT_SQL_PASSWORD_SECRET_NAME[_ENV], "
            "or SQL_SERVER_USERNAME[_ENV] / SQL_SERVER_PASSWORD[_ENV]."
        )

    return sql_settings


def run(argv: Optional[list[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    settings = load_settings(env_override=args.env)
    _validate_prod_guard_rails(settings, args.ingestion_scope)
    log_level = "DEBUG" if args.verbose else settings.log_level
    logger = configure_logging(log_level)

    logger.info("Starting SharePoint ingestion in env='%s'", settings.env_name)

    try:
        provider = maybe_build_provider(settings.key_vault)

        client_id, client_secret, tenant_id = _resolve_sharepoint_credentials(settings, provider=provider)
        logger.debug("Resolved SharePoint credentials from Key Vault or environment fallback")

        # Resolve SharePoint site URL from Key Vault.  The env-var value set by
        # _sharepoint_url_for_env() is only an emergency local-dev fallback.
        if provider and settings.key_vault.site_url_secret_name:
            try:
                site_url = provider.get_secret(settings.key_vault.site_url_secret_name)
                settings = replace(settings, sharepoint=replace(settings.sharepoint, site_url=site_url))
                logger.debug("Resolved SharePoint site URL from Key Vault")
            except Exception:
                logger.warning(
                    "Could not fetch site URL from Key Vault secret '%s'; "
                    "falling back to env-var value.",
                    settings.key_vault.site_url_secret_name,
                )

        resolved_sql_settings = _resolve_sql_settings(settings, provider=provider)
        # Audit DB (config + log) — primary client used for ingestion orchestration
        sql_client = SqlClient(resolved_sql_settings, logger=logger)
        sql_client.test_connection()
        logger.info(
            "SQL connection established (aud) to %s:%s/%s (auth_mode=%s)",
            resolved_sql_settings.host,
            resolved_sql_settings.port,
            resolved_sql_settings.database,
            resolved_sql_settings.auth_mode,
        )

        # Staging DB — data is always TRUNCATE-loaded here first
        from dataclasses import replace as _dc_replace
        stg_settings = _dc_replace(resolved_sql_settings, database=settings.sql_stg.database)
        stg_sql_client = SqlClient(stg_settings, logger=logger)

        # Integrated DB — data is promoted here after stg, per configured load_strategy
        int_settings = _dc_replace(resolved_sql_settings, database=settings.sql_int.database)
        int_sql_client = SqlClient(int_settings, logger=logger)

        sharepoint_client = SharePointClient(
            site_url=settings.sharepoint.site_url,
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=tenant_id,
        )

        engine = IngestionEngine(
            settings=settings,
            sql_client=sql_client,
            sharepoint_client=sharepoint_client,
            logger=logger,
            stg_sql_client=stg_sql_client,
            int_sql_client=int_sql_client,
        )

        if args.dry_run:
            planned = sql_client.fetch_ingestion_configs(
                process_id=args.process_id,
                workflow_id=args.workflow_id,
                ingestion_scope=args.ingestion_scope,
                active_only=not args.include_inactive,
            )
            logger.info("Dry run successful. Selected %s config(s).", len(planned))
            return 0

        summary = engine.run(
            process_id=args.process_id,
            workflow_id=args.workflow_id,
            ingestion_scope=args.ingestion_scope,
            include_inactive=args.include_inactive,
        )

        logger.info(
            "Ingestion complete. files_processed=%s files_failed=%s rows_loaded=%s errors=%s",
            summary.files_processed,
            summary.files_failed,
            summary.rows_loaded,
            len(summary.errors),
        )

        if summary.errors:
            for err in summary.errors:
                logger.error(err)
            return 2
        return 0

    except Exception as exc:  # pragma: no cover - integration path
        logger.exception("Fatal ingestion error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(run())
