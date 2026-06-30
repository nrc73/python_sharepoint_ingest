"""One-shot helper: clear valid_parquet input + Processed + Failed folders, truncate dest table,
and upload the capped parquet artifact using a Graph API resumable upload session.

Safety guard: this script enforces a hard maximum parquet artifact size of 250 MiB
for dev ingestion runs.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.sharepoint_client import SharePointClient, _GRAPH_BASE
from sharepoint_ingest.sql_client import SqlClient

_CHUNK_SIZE = 60 * 1024 * 1024  # 60 MiB (must be multiple of 320 KiB)
_MAX_PARQUET_SIZE_BYTES = 512 * 1024 * 1024  # 512 MiB hard cap (matches ingestion engine)


def _upload_small_file_direct(
    sp: SharePointClient,
    drive_id: str,
    folder_path: str,
    file_name: str,
    data: bytes,
) -> None:
    """Upload file in a single PUT for files <= 250 MiB."""
    if folder_path in ("/", ""):
        upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{file_name}:/content"
    else:
        upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{folder_path}/{file_name}:/content"

    headers = sp._auth_headers()
    headers["Content-Type"] = "application/octet-stream"
    resp = requests.put(upload_url, headers=headers, data=data, timeout=1800)
    resp.raise_for_status()


def _delete_files_in_folder(sp: SharePointClient, folder_url: str) -> None:
    files = sp.list_files(folder_url)
    for f in files:
        drive_id, path_in_drive = sp._server_url_to_drive_path(f.server_relative_url)
        item_meta = sp._get_json(f"{_GRAPH_BASE}/drives/{drive_id}/root:{path_in_drive}:")
        item_id = item_meta["id"]
        resp = requests.delete(
            f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}",
            headers=sp._auth_headers(),
            timeout=60,
        )
        print(f"  Deleted {f.name}: HTTP {resp.status_code}")


def _create_upload_session(sp: SharePointClient, drive_id: str, folder_path: str, file_name: str) -> str:
    if folder_path in ("/", ""):
        item_path = f"/{file_name}"
    else:
        item_path = f"{folder_path}/{file_name}"

    url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{item_path}:/createUploadSession"
    headers = sp._auth_headers()
    headers["Content-Type"] = "application/json"
    body = {"item": {"@microsoft.graph.conflictBehavior": "replace", "name": file_name}}
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["uploadUrl"]


def _query_next_range(upload_url: str) -> int | None:
    """Ask the session which byte it expects next. Returns None if session is gone."""
    try:
        resp = requests.get(upload_url, timeout=30)
        if resp.status_code == 404:
            return None
        data = resp.json()
        next_expected = data.get("nextExpectedRanges", [])
        if next_expected:
            return int(next_expected[0].split("-")[0])
    except Exception:
        pass
    return 0  # fallback: restart from beginning


def _upload_in_chunks(upload_url: str, data: bytes, max_retries: int = 5) -> None:
    total = len(data)
    n_chunks = math.ceil(total / _CHUNK_SIZE)
    i = 0
    while i < n_chunks:
        start = i * _CHUNK_SIZE
        end = min(start + _CHUNK_SIZE, total)
        chunk = data[start:end]
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end - 1}/{total}",
        }
        attempt = 0
        while True:
            try:
                resp = requests.put(upload_url, headers=headers, data=chunk, timeout=600)
                if resp.status_code in (200, 201):
                    # Final chunk accepted
                    pct = (end / total) * 100
                    print(f"  chunk {i + 1}/{n_chunks}: bytes {start}-{end - 1} ({pct:.1f}%) ✓")
                    i += 1
                    break
                if resp.status_code == 202:
                    pct = (end / total) * 100
                    print(f"  chunk {i + 1}/{n_chunks}: bytes {start}-{end - 1} ({pct:.1f}%)")
                    i += 1
                    break
                raise RuntimeError(
                    f"Chunk {i + 1}/{n_chunks} failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                attempt += 1
                if attempt > max_retries:
                    raise RuntimeError(f"Chunk {i + 1}/{n_chunks} failed after {max_retries} retries: {exc}") from exc
                print(f"  chunk {i + 1}/{n_chunks}: transient error ({exc}), querying resume offset (attempt {attempt}/{max_retries})...")
                import time; time.sleep(5 * attempt)
                next_start = _query_next_range(upload_url)
                if next_start is None:
                    raise RuntimeError("Upload session expired — re-run the script to start a new session.")
                resume_chunk = next_start // _CHUNK_SIZE
                if resume_chunk != i:
                    print(f"  Resuming from chunk {resume_chunk + 1} (byte {next_start})")
                    i = resume_chunk
                    start = i * _CHUNK_SIZE
                    end = min(start + _CHUNK_SIZE, total)
                    chunk = data[start:end]
                    headers = {
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end - 1}/{total}",
                    }


def main() -> int:
    settings = load_settings(env_override="dev")
    provider = maybe_build_provider(settings.key_vault, settings.azure_auth)
    if provider:
        cid, cs, tid = provider.get_sharepoint_credentials("dev")
    else:
        cid = os.getenv("SHAREPOINT_CLIENT_ID", "")
        cs = os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        tid = os.getenv("SHAREPOINT_TENANT_ID", "")

    sp = SharePointClient(settings.sharepoint.site_url, cid, cs, tid)

    # ── 1. Truncate SQL destination table ────────────────────────────────────
    print("Truncating sharepoint.dest_transactions_parquet ...")
    sql = SqlClient(settings.sql)
    sql.execute("TRUNCATE TABLE sharepoint.dest_transactions_parquet")
    print("  done.")

    # ── 2. Clear input folder (partial/old files) ────────────────────────────
    input_folder = "/sites/data_ingest_dev/Documents/valid_parquet"
    print(f"Clearing input folder: {input_folder}")
    _delete_files_in_folder(sp, input_folder)

    # ── 3. Clear Processed folder ─────────────────────────────────────────────
    processed = "/sites/data_ingest_dev/Documents/valid_parquet/Processed"
    print(f"Clearing Processed folder: {processed}")
    _delete_files_in_folder(sp, processed)

    # ── 4. Clear Failed folder ────────────────────────────────────────────────
    failed = "/sites/data_ingest_dev/Documents/valid_parquet/Failed"
    print(f"Clearing Failed folder: {failed}")
    _delete_files_in_folder(sp, failed)

    # ── 5. Resumable upload of parquet file ───────────────────────────────────
    local = (
        PROJECT_ROOT
        / "tests"
        / "sample_artifacts"
        / "valid_transactions_parquet_5mb.parquet"
    )

    local_size_bytes = local.stat().st_size
    if local_size_bytes > _MAX_PARQUET_SIZE_BYTES:
        current_mb = local_size_bytes / (1024 * 1024)
        max_mb = _MAX_PARQUET_SIZE_BYTES / (1024 * 1024)
        raise ValueError(
            f"Refusing upload: {local.name} is {current_mb:.2f} MB, "
            f"which exceeds the capped limit of {max_mb:.0f} MB. "
            "Regenerate the artifact to <= 250 MB before rerunning."
        )

    drive_id, folder_path = sp._server_url_to_drive_path(input_folder)
    size_gb = local.stat().st_size / (1024 ** 3)
    with local.open("rb") as fh:
        data = fh.read()

    # Upload filename must match the workflow pattern: valid_transactions_parquet_*.parquet
    upload_name = "valid_transactions_parquet_large.parquet"

    # Files under 1 GiB are uploaded via a single request for stability.
    # Keep resumable chunked mode as a fallback for larger artifacts.
    if len(data) <= _MAX_PARQUET_SIZE_BYTES:
        size_mb = len(data) / (1024 * 1024)
        print(f"Uploading {local.name} as '{upload_name}' via resumable upload ({size_mb:.2f} MB) ...")
        upload_url = _create_upload_session(sp, drive_id, folder_path, upload_name)
        print(f"Uploading in {_CHUNK_SIZE // (1024 * 1024)} MiB chunks ...")
        _upload_in_chunks(upload_url, data)
    else:
        print(f"Creating upload session for {local.name} ({size_gb:.2f} GB) ...")
        upload_url = _create_upload_session(sp, drive_id, folder_path, upload_name)
        print(f"Uploading in {_CHUNK_SIZE // (1024 * 1024)} MiB chunks ...")
        _upload_in_chunks(upload_url, data)

    print(f"Upload complete: {upload_name} ({size_gb:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

