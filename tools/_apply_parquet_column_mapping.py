"""One-off: apply column_mapping_json for wf-valid-transactions-parquet."""
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

MAPPING = (
    '{"TransactionId":"transaction_id",'
    '"CustomerId":"customer_id",'
    '"TransactionDate":"transaction_date",'
    '"Amount":"amount",'
    '"Currency":"currency",'
    '"Status":"status",'
    '"SourceSystem":"source_system"}'
)

with pyodbc.connect(conn_str) as conn:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE config.sharepoint_ingestion
        SET    column_mapping_json    = ?,
               sp_ingest_modified_utc = SYSUTCDATETIME()
        WHERE  workflow_id = ?
        """,
        (MAPPING, "wf-valid-transactions-parquet"),
    )
    conn.commit()
    print(f"Rows updated: {cur.rowcount}")

    cur.execute(
        "SELECT workflow_id, column_mapping_json "
        "FROM config.sharepoint_ingestion "
        "WHERE workflow_id = ?",
        ("wf-valid-transactions-parquet",),
    )
    row = cur.fetchone()
    print(f"workflow_id       : {row[0]}")
    print(f"column_mapping_json: {row[1]}")
