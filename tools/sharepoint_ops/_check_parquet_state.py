"""Check the current state of the valid_parquet SharePoint folder and dest table."""
from __future__ import annotations
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient

settings = load_settings(env_override="dev")
provider = maybe_build_provider(settings.key_vault)
cid, cs, tid = provider.get_sharepoint_credentials("dev") if provider else (
    os.getenv("SHAREPOINT_CLIENT_ID", ""), os.getenv("SHAREPOINT_CLIENT_SECRET", ""), os.getenv("SHAREPOINT_TENANT_ID", ""))

sp = SharePointClient(settings.sharepoint.site_url, cid, cs, tid)

for folder in [
    "/sites/data_ingest_dev/Documents/valid_parquet",
    "/sites/data_ingest_dev/Documents/valid_parquet/Processed",
    "/sites/data_ingest_dev/Documents/valid_parquet/Failed",
]:
    files = sp.list_files(folder)
    for f in files:
        size_mb = getattr(f, "size_bytes", 0) / (1024 * 1024)
        print(f"{folder} -> {f.name} ({size_mb:.1f} MB)")
    if not files:
        print(f"{folder} -> (empty)")

sql = SqlClient(settings.sql)
rows = sql.query_rows("SELECT COUNT(1) AS cnt FROM dbo.dest_transactions_parquet")
print(f"dest_transactions_parquet rows: {rows[0]['cnt']}")
