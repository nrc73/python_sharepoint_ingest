"""Check recent audit log entries for the parquet workflow."""

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

settings = load_settings(env_override="dev")
sql = SqlClient(settings.sql)

rows = sql.query_rows(
    """
    SELECT TOP 5
        a.audit_id,
        a.workflow_id,
        a.file_name,
        a.status,
        a.records_loaded,
        a.rows_scanned,
        a.memory_peak_mb,
        a.duration_seconds,
        a.sp_ingest_created_utc
    FROM log.sharepoint_ingestion_audit a
    WHERE a.workflow_id = 'wf-valid-transactions-parquet'
    ORDER BY a.audit_id DESC
    """
)

if not rows:
    print("No audit records found for wf-valid-transactions-parquet")

for row in rows:
    print(f"audit_id={row['audit_id']}, workflow={row['workflow_id']}, file={row['file_name']}")
    print(
        f"  status={row['status']}, records_loaded={row['records_loaded']}, "
        f"rows_scanned={row['rows_scanned']}"
    )
    print(
        f"  memory_peak_mb={row['memory_peak_mb']}, duration_s={row['duration_seconds']}, "
        f"sp_ingest_created_utc={row['sp_ingest_created_utc']}"
    )
    print()
