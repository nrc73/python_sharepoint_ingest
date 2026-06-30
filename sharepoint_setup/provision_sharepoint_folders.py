from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

# Ensure the project root is importable when running as:
# python sharepoint_setup/provision_sharepoint_folders.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient, _GRAPH_BASE


SUPPORTED_ENVIRONMENTS = ("dev", "prod")


@dataclass(frozen=True)
class FolderSpec:
    group_name: str
    input_folder: str
    processed_folder: str
    failed_folder: str


def _resolve_target_envs(env_arg: str) -> list[str]:
    normalized = env_arg.lower().strip()
    if normalized == "all":
        return list(SUPPORTED_ENVIRONMENTS)
    if normalized in SUPPORTED_ENVIRONMENTS:
        return [normalized]
    raise ValueError(f"Unsupported --env '{env_arg}'. Use dev, prod, or all.")


def _resolve_credentials(env_name: str) -> tuple[str, str, str]:
    settings = load_settings(env_override=env_name)
    provider = maybe_build_provider(settings.key_vault, settings.azure_auth)

    if provider is not None:
        return provider.get_sharepoint_credentials(env_name)

    env_key = env_name.upper().strip()
    client_id = os.getenv(f"SHAREPOINT_CLIENT_ID_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_ID", "")
    client_secret = os.getenv(f"SHAREPOINT_CLIENT_SECRET_{env_key}", "") or os.getenv("SHAREPOINT_CLIENT_SECRET", "")
    tenant_id = os.getenv(f"SHAREPOINT_TENANT_ID_{env_key}", "") or os.getenv("SHAREPOINT_TENANT_ID", "")

    if not (client_id and client_secret and tenant_id):
        raise ValueError("Missing SharePoint credentials from Key Vault and environment fallback")

    return client_id, client_secret, tenant_id


def _choose_library(sp_client: SharePointClient, explicit_library: str | None) -> str:
    if explicit_library and explicit_library.strip():
        return explicit_library.strip()

    drives = sp_client._get_json(f"{_GRAPH_BASE}/sites/{sp_client._site_id}/drives").get("value", [])
    drive_names = [str(d.get("name") or "") for d in drives if d.get("name")]

    for preferred in ("General", "Documents", "Shared Documents"):
        if preferred in drive_names:
            return preferred

    if not drive_names:
        raise RuntimeError("No document libraries (drives) returned by Graph for the target site")

    return drive_names[0]


def _folder_exists(sp_client: SharePointClient, folder_server_relative_url: str) -> bool:
    drive_id, path_in_library = sp_client._server_url_to_drive_path(folder_server_relative_url)
    if path_in_library in ("", "/"):
        return True

    url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path_in_library}"
    response = requests.get(url, headers=sp_client._auth_headers(), timeout=30)

    if response.status_code == 404:
        return False

    response.raise_for_status()
    return "folder" in response.json()


def _create_folder(sp_client: SharePointClient, folder_server_relative_url: str) -> None:
    drive_id, path_in_library = sp_client._server_url_to_drive_path(folder_server_relative_url)

    normalized = path_in_library.strip("/")
    if not normalized:
        return

    parts = normalized.split("/")
    folder_name = parts[-1]
    parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

    if parent_path in ("", "/"):
        create_url = f"{_GRAPH_BASE}/drives/{drive_id}/root/children"
    else:
        create_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{parent_path}:/children"

    headers = sp_client._auth_headers()
    headers["Content-Type"] = "application/json"

    body = {
        "name": folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }

    response = requests.post(create_url, headers=headers, json=body, timeout=30)

    if response.status_code == 409:
        # already exists
        return

    response.raise_for_status()


def _ensure_folder(sp_client: SharePointClient, folder_server_relative_url: str) -> tuple[bool, str]:
    normalized = folder_server_relative_url.rstrip("/")

    if _folder_exists(sp_client, normalized):
        return False, normalized

    _create_folder(sp_client, normalized)

    if not _folder_exists(sp_client, normalized):
        raise RuntimeError(f"Folder create call completed but folder still not found: {normalized}")

    return True, normalized


def _build_folder_specs(site_path: str, library_name: str) -> list[FolderSpec]:
    base = f"{site_path}/{library_name}".rstrip("/")
    groups = [
        "valid_customers",
        "valid_transactions",
        "valid_parquet",
        "valid_transactions_large",
        "invalid_csv",
        "invalid_excel",
        "invalid_parquet",
    ]

    specs: list[FolderSpec] = []
    for group in groups:
        input_folder = f"{base}/{group}"
        specs.append(
            FolderSpec(
                group_name=group,
                input_folder=input_folder,
                processed_folder=f"{input_folder}/Processed",
                failed_folder=f"{input_folder}/Failed",
            )
        )
    return specs


def _provision_for_env(env_name: str, explicit_library: str | None) -> None:
    settings = load_settings(env_override=env_name)
    client_id, client_secret, tenant_id = _resolve_credentials(env_name)

    sp_client = SharePointClient(
        site_url=settings.sharepoint.site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    )

    library_name = _choose_library(sp_client, explicit_library)
    specs = _build_folder_specs(sp_client._site_path, library_name)

    created_count = 0
    existing_count = 0

    print(f"[{env_name}] Site URL : {settings.sharepoint.site_url}")
    print(f"[{env_name}] Site path: {sp_client._site_path}")
    print(f"[{env_name}] Library  : {library_name}")

    for spec in specs:
        for folder in (spec.input_folder, spec.processed_folder, spec.failed_folder):
            created, normalized = _ensure_folder(sp_client, folder)
            if created:
                created_count += 1
                print(f"[{env_name}] CREATED : {normalized}")
            else:
                existing_count += 1
                print(f"[{env_name}] EXISTS  : {normalized}")

    print(
        f"[{env_name}] Folder provisioning complete "
        f"(created={created_count}, already_exists={existing_count})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision SharePoint ingestion-group folders and Processed/Failed subfolders"
    )
    parser.add_argument("--env", default="dev", help="Environment name: dev, prod, or all")
    parser.add_argument(
        "--library",
        required=False,
        help="Document library display name. If omitted, auto-selects General/Documents/Shared Documents/first available.",
    )
    args = parser.parse_args()

    target_envs = _resolve_target_envs(args.env)
    failed_envs: list[str] = []

    for env_name in target_envs:
        try:
            _provision_for_env(env_name, args.library)
        except Exception as exc:
            failed_envs.append(env_name)
            print(f"[{env_name}] FAILED: {exc}")

    if failed_envs:
        print(f"SharePoint folder provisioning failed for environment(s): {', '.join(failed_envs)}")
        return 1

    print("SharePoint folder provisioning passed for all requested environment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
