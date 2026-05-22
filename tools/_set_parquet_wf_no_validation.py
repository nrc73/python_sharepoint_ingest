"""One-shot: set is_validated=0 and column_mapping_json=NULL for the parquet workflow
so the 0.5 GB artifact (with its wide schema) loads without schema-check failures."""
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
# ── Step 1: add is_validated column if it doesn't exist ──────────────────────
# COLUMNPROPERTY(OBJECT_ID(...)) handles schema-qualified names correctly,
# unlike COL_LENGTH which can be fooled by naming conventions.
# We omit a named CONSTRAINT so there is no constraint-name collision risk.
sql.execute(
    """
    IF COLUMNPROPERTY(OBJECT_ID('config.sharepoint_ingestion'), 'is_validated', 'ColumnId') IS NULL
        ALTER TABLE config.sharepoint_ingestion
            ADD is_validated BIT NOT NULL DEFAULT 1;
    """
)
print("config.sharepoint_ingestion.is_validated ensured.")

# ── Same for the audit log table ──────────────────────────────────────────────
sql.execute(
    """
    IF COLUMNPROPERTY(OBJECT_ID('log.sharepoint_ingestion_audit'), 'is_validated', 'ColumnId') IS NULL
        ALTER TABLE log.sharepoint_ingestion_audit
            ADD is_validated BIT NULL;
    """
)
print("log.sharepoint_ingestion_audit.is_validated ensured.")

# ── Step 2: set is_validated=0 for the parquet workflow ──────────────────────
sql.execute(
    """
    UPDATE config.sharepoint_ingestion
    SET is_validated           = 0,
        sp_ingest_modified_utc = SYSUTCDATETIME()
    WHERE workflow_id = 'wf-valid-transactions-parquet'
    """
)
print("Updated wf-valid-transactions-parquet: is_validated=0 (column_mapping_json unchanged)")
