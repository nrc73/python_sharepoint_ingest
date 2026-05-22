from __future__ import annotations

import argparse
import sys

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.logging_utils import configure_logging
from sharepoint_ingest.sql_client import SqlClient


def _count_violations(sql_client: SqlClient) -> list[dict]:
    sql_text = """
    SELECT
        id,
        workflow_id,
        ingestion_scope,
        ingestion_domain,
        is_test_data,
        is_active
    FROM config.sharepoint_ingestion
    WHERE
        ISNULL(is_test_data, 0) = 1
        OR UPPER(LTRIM(RTRIM(ISNULL(ingestion_scope, 'REAL')))) IN ('TEST', 'VALIDATION', 'PERF_TEST')
        OR UPPER(LTRIM(RTRIM(ISNULL(ingestion_domain, '')))) = 'SAMPLE_ARTIFACTS'
    ORDER BY id
    """
    return sql_client.query_rows(sql_text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate prod guard rails: ensure no TEST/sample configs exist in prod"
    )
    parser.add_argument("--env", default="prod", help="Environment to validate (default: prod)")
    args = parser.parse_args()

    settings = load_settings(env_override=args.env)
    logger = configure_logging(settings.log_level)

    if settings.env_name != "prod":
        print(
            f"FAILED: validate_prod_guardrails.py must run with env=prod (resolved env={settings.env_name!r})."
        )
        return 2

    sql_client = SqlClient(settings.sql, logger=logger)
    sql_client.test_connection()

    violations = _count_violations(sql_client)
    if violations:
        print("FAILED: Guard rail violations detected in config.sharepoint_ingestion:")
        for row in violations:
            print(
                " - "
                f"id={row.get('id')} "
                f"workflow_id={row.get('workflow_id')} "
                f"scope={row.get('ingestion_scope')} "
                f"domain={row.get('ingestion_domain')} "
                f"is_test_data={row.get('is_test_data')} "
                f"is_active={row.get('is_active')}"
            )
        return 1

    print("PASS: No TEST/VALIDATION/PERF_TEST/sample_artifacts rows found in prod config.sharepoint_ingestion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
