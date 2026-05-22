"""Check recent audit log entries for the parquet workflow."""
import os
import pyodbc
from dotenv import load_dotenv

load_dotenv()

conn_str = (
    f"DRIVER={{{os.getenv('SQL_ODBC_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
    f"SERVER={os.getenv('SQL_SERVER_HOST', 'localhost')},{os.getenv('SQL_SERVER_PORT', '1433')};"
    f"DATABASE={os.getenv('SQL_DATABASE_DEV', 'ingest_dev')};"
    f"UID={os.getenv('SQL_SERVER_USERNAME')};PWD={os.getenv('SQL_SERVER_PASSWORD')};"
    "TrustServerCertificate=yes;Encrypt=yes;"
)

with pyodbc.connect(conn_str) as conn:
    cur = conn.cursor()
    cur.execute(
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
        FROM   log.sharepoint_ingestion_audit a
        WHERE  a.workflow_id = 'wf-valid-transactions-parquet'
        ORDER BY a.audit_id DESC
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("No audit records found for wf-valid-transactions-parquet")
    for row in rows:
        print(f"audit_id={row[0]}, workflow={row[1]}, file={row[2]}")
        print(f"  status={row[3]}, records_loaded={row[4]}, rows_scanned={row[5]}")
        print(f"  memory_peak_mb={row[6]}, duration_s={row[7]}, sp_ingest_created_utc={row[8]}")
        print()
