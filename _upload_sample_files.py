"""Upload local sample artifact files to the dev SharePoint site input folders.

Uploads:
  tests/sample_artifacts/valid/csv/valid_transactions_001.csv
      → /sites/data_ingest_dev/Documents/valid_transactions/
  tests/sample_artifacts/valid/csv/valid_transactions_002.csv
      → /sites/data_ingest_dev/Documents/valid_transactions/
  tests/sample_artifacts/valid/csv/valid_transactions_large.csv
      → /sites/data_ingest_dev/Documents/valid_transactions_large/
  tests/sample_artifacts/valid/excel/valid_customers_001.xlsx
      → /sites/data_ingest_dev/Documents/valid_customers/

Usage:
    python _upload_sample_files.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

from src.config import load_settings
from src.keyvault_client import maybe_build_provider
from src.sharepoint_client import SharePointClient, _GRAPH_BASE

import requests as _requests


def upload_file(sp: SharePointClient, local_path: Path, folder_server_relative_url: str) -> None:
    """Upload a local file into a SharePoint folder via Graph PUT."""
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

    print(f"  Uploading {local_path.name} ({len(data):,} bytes) -> {folder_server_relative_url}/", flush=True)
    timeout_seconds = 1800 if len(data) >= 50 * 1024 * 1024 else 120
    resp = _requests.put(upload_url, headers=headers, data=data, timeout=timeout_seconds)
    resp.raise_for_status()
    print(f"  OK {local_path.name} uploaded (HTTP {resp.status_code})", flush=True)


def main() -> int:
    settings = load_settings(env_override="dev")
    provider = maybe_build_provider(settings.key_vault)

    if provider is not None:
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")
    else:
        import os
        client_id     = os.getenv("SHAREPOINT_CLIENT_ID_DEV") or os.getenv("SHAREPOINT_CLIENT_ID", "")
        client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET_DEV") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        tenant_id     = os.getenv("SHAREPOINT_TENANT_ID_DEV") or os.getenv("SHAREPOINT_TENANT_ID", "")

    site_url  = settings.sharepoint.site_url          # https://mycompany715.sharepoint.com/sites/data_ingest_dev
    site_path = site_url.replace("https://mycompany715.sharepoint.com", "")  # /sites/data_ingest_dev

    print(f"Site URL  : {site_url}")
    print(f"Site path : {site_path}")

    sp = SharePointClient(
        site_url=site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    )

    uploads = [
        (
            Path("tests/sample_artifacts/valid/csv/valid_transactions_001.csv"),
            f"{site_path}/Documents/valid_transactions",
        ),
        (
            Path("tests/sample_artifacts/valid/csv/valid_transactions_002.csv"),
            f"{site_path}/Documents/valid_transactions",
        ),
        (
            Path("tests/sample_artifacts/valid/csv/valid_transactions_large.csv"),
            f"{site_path}/Documents/valid_transactions_large",
        ),
        (
            Path("tests/sample_artifacts/valid/excel/valid_customers_001.xlsx"),
            f"{site_path}/Documents/valid_customers",
        ),
    ]

    errors: list[str] = []
    for local_path, folder_url in uploads:
        if not local_path.exists():
            print(f"  SKIP (not found locally): {local_path}", flush=True)
            continue
        try:
            upload_file(sp, local_path, folder_url)
        except Exception as exc:
            msg = f"  FAILED {local_path.name}: {exc}"
            print(msg, flush=True)
            errors.append(msg)

    if errors:
        print(f"\nCompleted with {len(errors)} error(s).")
        return 1
    print("\nAll sample files uploaded successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
