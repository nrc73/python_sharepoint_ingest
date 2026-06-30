"""Purge files from SharePoint dev Processed/Failed folders for valid artifact groups.

Usage:
    python tools/sharepoint_ops/_purge_dev_processed_failed_files.py
"""
from __future__ import annotations

import sys

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests as _requests

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient, _GRAPH_BASE


def delete_file(sp: SharePointClient, file_server_relative_url: str) -> None:
    """Delete a SharePoint file via Graph API."""
    drive_id, path_in_drive = sp._server_url_to_drive_path(file_server_relative_url)
    meta = sp._get_json(f"{_GRAPH_BASE}/drives/{drive_id}/root:{path_in_drive}:")
    item_id = meta["id"]

    delete_url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
    resp = _requests.delete(delete_url, headers=sp._auth_headers(), timeout=60)
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


def main() -> int:
    settings = load_settings(env_override="dev")
    provider = maybe_build_provider(settings.key_vault, settings.azure_auth)

    if provider is not None:
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials("dev")
    else:
        import os

        client_id = os.getenv("SHAREPOINT_CLIENT_ID_DEV") or os.getenv("SHAREPOINT_CLIENT_ID", "")
        client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET_DEV") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        tenant_id = os.getenv("SHAREPOINT_TENANT_ID_DEV") or os.getenv("SHAREPOINT_TENANT_ID", "")

    sp = SharePointClient(
        site_url=settings.sharepoint.site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    )

    site_path = sp._site_path
    target_folders = [
        f"{site_path}/Documents/valid_customers/Processed",
        f"{site_path}/Documents/valid_customers/Failed",
        f"{site_path}/Documents/valid_transactions/Processed",
        f"{site_path}/Documents/valid_transactions/Failed",
        f"{site_path}/Documents/valid_transactions_large/Processed",
        f"{site_path}/Documents/valid_transactions_large/Failed",
    ]

    deleted = 0
    errors: list[str] = []

    for folder in target_folders:
        try:
            files = sp.list_files(folder)
        except Exception as exc:
            msg = f"Could not list {folder}: {exc}"
            print(msg, flush=True)
            errors.append(msg)
            continue

        print(f"{folder}: {len(files)} file(s)", flush=True)
        for item in files:
            try:
                delete_file(sp, item.server_relative_url)
                print(f"  DELETED: {item.server_relative_url}", flush=True)
                deleted += 1
            except Exception as exc:
                msg = f"  FAILED delete {item.server_relative_url}: {exc}"
                print(msg, flush=True)
                errors.append(msg)

    print(f"\nTotal deleted files: {deleted}", flush=True)
    if errors:
        print(f"Total errors: {len(errors)}", flush=True)
        return 1

    print("Purge completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
