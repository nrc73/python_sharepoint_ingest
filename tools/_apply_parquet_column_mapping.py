"""One-off: apply column_mapping_json for wf-valid-transactions-parquet."""

from sqlalchemy import text

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

MAPPING = (
    '{"TransactionId":"transaction_id",'
    '"CustomerId":"customer_id",'
    '"TransactionDate":"transaction_date",'
    '"Amount":"amount",'
    '"Currency":"currency",'
    '"Status":"status",'
    '"SourceSystem":"source_system"}'
)

settings = load_settings(env_override="dev")
sql = SqlClient(settings.sql)

with sql.engine.begin() as conn:
    result = conn.execute(
        text(
            """
            UPDATE config.sharepoint_ingestion
            SET column_mapping_json = :mapping,
                sp_ingest_modified_utc = SYSUTCDATETIME()
            WHERE workflow_id = :workflow_id
            """
        ),
        {"mapping": MAPPING, "workflow_id": "wf-valid-transactions-parquet"},
    )
    print(f"Rows updated: {result.rowcount}")

rows = sql.query_rows(
    """
    SELECT workflow_id, column_mapping_json
    FROM config.sharepoint_ingestion
    WHERE workflow_id = :workflow_id
    """,
    {"workflow_id": "wf-valid-transactions-parquet"},
)

if rows:
    row = rows[0]
    print(f"workflow_id       : {row['workflow_id']}")
    print(f"column_mapping_json: {row['column_mapping_json']}")
else:
    print("workflow row not found")
