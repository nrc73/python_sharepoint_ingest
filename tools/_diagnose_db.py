"""Diagnose which tables exist and whether is_validated column is present."""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

settings = load_settings(env_override="dev")
sql = SqlClient(settings.sql)

with sql._engine.connect() as conn:
    # 1. Check what tables exist in config schema
    rows = conn.execute(text(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA IN ('config','log','dbo') ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )).fetchall()
    print("=== Tables in config/log/dbo ===")
    for r in rows:
        print(f"  {r[0]}.{r[1]}")

    # 2. Check columns in config.sharepoint_ingestion (if exists)
    rows2 = conn.execute(text(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA='config' AND TABLE_NAME='sharepoint_ingestion' "
        "ORDER BY ORDINAL_POSITION"
    )).fetchall()
    print("\n=== config.sharepoint_ingestion columns ===")
    for r in rows2:
        print(f"  {r[0]}  ({r[1]})")
    if not rows2:
        print("  (table not found or no columns)")
