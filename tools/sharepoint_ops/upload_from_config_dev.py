"""Upload regenerated sample artifacts to SharePoint dev input folders using config.sharepoint_ingestion.

This script:
- reads active TEST-scope configs from ingest_dev
- resolves each configured sharepoint_process_folder
- clears existing files in each configured input folder
- uploads matching local artifacts from tests/sample_artifacts
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path

import requests as _requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient, _GRAPH_BASE
from sharepoint_ingest.sql_client import SqlClient


def _resolve_sharepoint_credentials(env_name: str) -> tuple[str, str, str]:
    settings = load_settings(env_override=env_name)
    provider = maybe_build_provider(settings.key_vault)
    if provider is not None:
        return provider.get_sharepoint_credentials(env_name)

    env_key = env_name.upper().strip()
    client_id = os.getenv(f"SHAREPOINT_CLIENT_ID_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_ID", "")
    client_secret = os.getenv(f"SHAREPOINT_CLIENT_SECRET_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
    tenant_id = os.getenv(f"SHAREPOINT_TENANT_ID_{env_key}", "") or os.getenv("SHAREPOINT_TENANT_ID", "")
    if not (client_id and client_secret and tenant_id):
        raise ValueError("Missing SharePoint credentials from Key Vault and environment fallback")
    return client_id, client_secret, tenant_id


def _resolve_folder(site_path: str, configured_folder: str) -> str:
    value = (configured_folder or "").strip()
    if not value:
        raise ValueError("Empty configured sharepoint_process_folder")
    if value.startswith("/sites/") or value.startswith("/teams/"):
        return value.rstrip("/")
    if not value.startswith("/"):
        value = "/" + value
    return f"{site_path.rstrip('/')}{value}".rstrip("/")


def _delete_file(sp: SharePointClient, file_server_relative_url: str) -> None:
    drive_id, path_in_drive = sp._server_url_to_drive_path(file_server_relative_url)
    meta = sp._get_json(f"{_GRAPH_BASE}/drives/{drive_id}/root:{path_in_drive}:")
    item_id = meta["id"]
    delete_url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
    resp = _requests.delete(delete_url, headers=sp._auth_headers(), timeout=60)
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


def _upload_file(sp: SharePointClient, local_path: Path, folder_server_relative_url: str) -> None:
    drive_id, folder_graph_path = sp._server_url_to_drive_path(folder_server_relative_url)
    file_name = local_path.name
    if folder_graph_path in ("/", ""):
        upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{file_name}:/content"
    else:
        upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{folder_graph_path}/{file_name}:/content"

    headers = sp._auth_headers()
    headers["Content-Type"] = "application/octet-stream"
    with local_path.open("rb") as fh:
        data = fh.read()
    timeout_seconds = 1800 if len(data) >= 50 * 1024 * 1024 else 120
    resp = _requests.put(upload_url, headers=headers, data=data, timeout=timeout_seconds)
    resp.raise_for_status()


def _matches_pattern(name: str, pattern: str) -> bool:
    p = (pattern or "").strip()
    if not p:
        return False
    if fnmatch.fnmatch(name, p):
        return True
    try:
        return bool(re.fullmatch(p, name))
    except re.error:
        return False


def _artifact_candidates() -> list[Path]:
    root = PROJECT_ROOT / "tests" / "sample_artifacts"
    return sorted([
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".csv", ".xlsx", ".xlsm", ".xls", ".parquet"}
    ])


def main() -> int:
    env_name = "dev"
    settings = load_settings(env_override=env_name)
    client_id, client_secret, tenant_id = _resolve_sharepoint_credentials(env_name)
    sp = SharePointClient(settings.sharepoint.site_url, client_id, client_secret, tenant_id)
    sql_client = SqlClient(settings.sql)

    configs = sql_client.fetch_ingestion_configs(ingestion_scope="test", active_only=True)
    if not configs:
        print("No active TEST-scope configs found; nothing to upload.")
        return 1

    candidates = _artifact_candidates()
    print(f"Artifact candidates discovered: {len(candidates)}")

    folder_map: dict[str, list[Path]] = {}
    for cfg in configs:
        folder = _resolve_folder(sp._site_path, cfg.sharepoint_process_folder)
        pattern = (cfg.file_name_pattern or "").strip()
        selected = [p for p in candidates if _matches_pattern(p.name, pattern)]
        folder_map.setdefault(folder, [])
        for p in selected:
            if p not in folder_map[folder]:
                folder_map[folder].append(p)

    # clear each configured input folder first
    for folder in sorted(folder_map.keys()):
        existing = sp.list_files(folder)
        print(f"{folder}: clearing {len(existing)} file(s)")
        for item in existing:
            _delete_file(sp, item.server_relative_url)

    total_uploaded = 0
    for folder, files in sorted(folder_map.items(), key=lambda x: x[0]):
        print(f"{folder}: uploading {len(files)} file(s)")
        for p in sorted(files):
            _upload_file(sp, p, folder)
            total_uploaded += 1
            print(f"  uploaded {p.name}")

    print(f"Upload complete. Total uploaded files: {total_uploaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
