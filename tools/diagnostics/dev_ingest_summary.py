from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient


def main() -> int:
    settings = load_settings(env_override="dev")
    sql = SqlClient(settings.sql)

    checks = {
        "dest_customers": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_customers",
        "dest_transactions": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions",
        "dest_transactions_parquet": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions_parquet",
        "dest_transactions_large": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_transactions_large",
        "dest_invalid_csv": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_invalid_csv",
        "dest_invalid_excel": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_invalid_excel",
        "dest_invalid_parquet": "SELECT COUNT(1) AS cnt FROM sharepoint.dest_invalid_parquet",
        "audit_total": "SELECT COUNT(1) AS cnt FROM log.sharepoint_ingestion_audit",
        "audit_success": "SELECT COUNT(1) AS cnt FROM log.sharepoint_ingestion_audit WHERE status='SUCCESS'",
        "audit_failed": "SELECT COUNT(1) AS cnt FROM log.sharepoint_ingestion_audit WHERE status='FAILED'",
    }

    for key, query in checks.items():
        rows = sql.query_rows(query)
        print(f"{key}|{rows[0]['cnt']}")

    rows = sql.query_rows(
        """
        SELECT workflow_id, status, COUNT(1) AS cnt
        FROM log.sharepoint_ingestion_audit
        GROUP BY workflow_id, status
        ORDER BY workflow_id, status
        """
    )
    for row in rows:
        print(f"audit_by_workflow|{row['workflow_id']}|{row['status']}|{row['cnt']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

