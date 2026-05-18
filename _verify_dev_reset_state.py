"""Verify ingest_dev database state after reset/setup scripts."""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from src.config import load_settings
from src.sql_client import SqlClient


def _count(sql_client: SqlClient, query: str) -> int:
    rows = sql_client.query_rows(query)
    return int(rows[0]["cnt"])


def main() -> int:
    settings = load_settings(env_override="dev")
    sql_client = SqlClient(settings.sql)

    checks = {
        "dest_customers": _count(sql_client, "SELECT COUNT(1) AS cnt FROM dbo.dest_customers"),
        "dest_transactions": _count(sql_client, "SELECT COUNT(1) AS cnt FROM dbo.dest_transactions"),
        "dest_transactions_large": _count(sql_client, "SELECT COUNT(1) AS cnt FROM dbo.dest_transactions_large"),
        "audit_log": _count(sql_client, "SELECT COUNT(1) AS cnt FROM log.sharepoint_ingestion_audit"),
        "valid_config_workflows": _count(
            sql_client,
            """
            SELECT COUNT(1) AS cnt
            FROM config.sharepoint_ingestion
            WHERE workflow_id IN (
                'wf-valid-customers',
                'wf-valid-transactions-standard',
                'wf-valid-transactions-large'
            )
            AND is_active = '1'
            """,
        ),
    }

    for key, value in checks.items():
        print(f"{key}|count={value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
