"""Quick check of the parquet workflow config row."""

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

settings = load_settings(env_override="dev")
sql = SqlClient(settings.sql)

rows = sql.query_rows(
    """
    SELECT workflow_id, check_source_dest_columns, load_strategy
    FROM config.sharepoint_ingestion
    WHERE workflow_id = 'wf-valid-transactions-parquet'
    """
)

if rows:
    row = rows[0]
    print(f"workflow_id              : {row['workflow_id']}")
    print(f"check_source_dest_columns: {row['check_source_dest_columns']}")
    print(f"load_strategy            : {row['load_strategy']}")
else:
    print("No row found")
