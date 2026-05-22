"""Quick check: show workflow config row for the parquet workflow."""
from __future__ import annotations
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

settings = load_settings(env_override="dev")
sql = SqlClient(settings.sql)
    rows = sql.query_rows(
        "SELECT workflow_id, is_active, ingestion_scope, is_test_data "
        "FROM config.sharepoint_ingestion "
        "WHERE workflow_id = 'wf-valid-transactions-parquet'"
    )
for r in rows:
    print(dict(r))
if not rows:
    print("(no rows found)")
