"""Quick check of the parquet workflow config row."""
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
        "SELECT workflow_id, check_source_dest_columns, load_strategy "
        "FROM config.sharepoint_ingestion "
        "WHERE workflow_id = 'wf-valid-transactions-parquet'"
    )
    row = cur.fetchone()
    if row:
        print(f"workflow_id              : {row[0]}")
        print(f"check_source_dest_columns: {row[1]}")
        print(f"load_strategy            : {row[2]}")
    else:
        print("No row found")
