"""Verify ingest_dev database state after reset/setup scripts."""
from __future__ import annotations

import sys
from dataclasses import replace

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient


def _count(sql_client: SqlClient, query: str) -> int:
    rows = sql_client.query_rows(query)
    return int(rows[0]["cnt"])


def main() -> int:
    settings = load_settings(env_override="dev")
    aud_sql = SqlClient(replace(settings.sql, database="ingest_audit_dev"))
    int_sql = SqlClient(replace(settings.sql, database="ingest_int_dev"))

    checks = {
        "dest_customers": _count(int_sql, "SELECT COUNT(1) AS cnt FROM sharepoint.dest_customers"),
        "dest_transactions": _count(int_sql, "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions"),
        "dest_transactions_parquet": _count(int_sql, "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions_parquet"),
        "dest_transactions_large": _count(int_sql, "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions_large"),
        "audit_log": _count(aud_sql, "SELECT COUNT(1) AS cnt FROM log.sharepoint_ingestion_audit"),
        "valid_config_workflows": _count(
            aud_sql,
            """
            SELECT COUNT(1) AS cnt
            FROM config.sharepoint_ingestion
            WHERE workflow_id IN (
                'wf-valid-customers',
                'wf-valid-transactions-standard',
                'wf-valid-transactions-parquet',
                'wf-valid-transactions-large'
            )
            AND is_active = '1'
            """,
        ),
        "all_active_test_workflows": _count(
            aud_sql,
            """
            SELECT COUNT(1) AS cnt
            FROM config.sharepoint_ingestion
            WHERE ingestion_scope = 'TEST'
              AND is_active = '1'
            """,
        ),
        "ole2_live_workflows": _count(
            aud_sql,
            """
            SELECT COUNT(1) AS cnt
            FROM config.sharepoint_ingestion
            WHERE workflow_id LIKE '%ole2%'
               OR staging_table_name LIKE '%ole2%'
               OR sharepoint_process_folder LIKE '%ole2%'
            """,
        ),
    }

    for key, value in checks.items():
        print(f"{key}|count={value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
