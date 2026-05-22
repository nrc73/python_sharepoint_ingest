"""Microsoft Graph SharePoint client used by ingestion workflows."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Optional dependency guard – keeps unit tests runnable without the full
# requests / msal stack installed.
# ---------------------------------------------------------------------------
try:
    import requests as _requests
    from msal import ConfidentialClientApplication
except ImportError as import_error:  # pragma: no cover - dependency availability check
    _requests = None  # type: ignore[assignment]
    ConfidentialClientApplication = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: Exception | None = import_error
else:
    _IMPORT_ERROR = None

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class SharePointFileItem:
    name: str
    server_relative_url: str


@dataclass
class SharePointFolderItem:
    name: str
    server_relative_url: str


class SharePointClient:
    """Authenticate to SharePoint Online using the **Microsoft Graph API**.

    Why Graph instead of the SharePoint REST (``/_api/``) path
    ──────────────────────────────────────────────────────────
    Modern SharePoint Online tenants enforce a feature gate controlled by
    the ``x-ms-suspended-features`` response header.  Any app-only token
    that arrives via the legacy ``/_api/`` endpoint is rejected with:

        "Unsupported app only token"

    …regardless of whether ``Sites.ReadWrite.All`` (SPO resource) is present
    in the JWT ``roles`` claim.  The gate applies to the SharePoint REST
    pipeline only.

    The Microsoft Graph API (``graph.microsoft.com``) uses a separate
    authentication pipeline and honours ``Sites.ReadWrite.All`` (Graph
    resource: ``00000003-0000-0000-c000-000000000000``) without any
    tenant-level opt-in.

    Authentication flow
    ───────────────────
    *  MSAL ``acquire_token_for_client`` with scope
       ``https://graph.microsoft.com/.default``
    *  Token is refreshed automatically by MSAL's in-memory cache
    *  No legacy ACS / ``ClientCredential`` calls
    """

    def __init__(
        self,
        site_url: str,
        client_id: str,
        client_secret: str,
        tenant_id: str,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(
                "requests and msal are required. "
                "Install dependencies from requirements.txt"
            ) from _IMPORT_ERROR
        if not site_url:
            raise ValueError("SharePoint site URL is required")
        if not tenant_id:
            raise ValueError(
                "tenant_id is required for Azure AD (Entra ID) client-credentials auth. "
                "ACS-based auth (no tenant_id) is not supported."
            )

        self.site_url = site_url

        parsed = urlparse(site_url)
        self._hostname: str = parsed.hostname or ""  # e.g. "mycompany715.sharepoint.com"
        self._site_path: str = parsed.path.rstrip("/")  # e.g. "/sites/data_ingest_dev"
        # Path relative to hostname, without leading slash – used in Graph URL
        self._site_relative: str = self._site_path.lstrip("/")  # e.g. "sites/data_ingest_dev"

        self._msal_app = ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )

        # Resolve site ID eagerly so callers get a fast-fail on bad URLs/creds
        self._site_id: str = self._resolve_site_id()
        # Library display-name → Graph drive ID (populated lazily)
        self._drive_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Token / HTTP helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Acquire (or return cached) Graph access token via MSAL."""
        result = self._msal_app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            description = result.get("error_description", "no description returned")
            raise RuntimeError(
                f"MSAL token acquisition failed [{error}]: {description}"
            )
        return result["access_token"]

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }

    def _get_json(self, url: str) -> dict:
        response = _requests.get(url, headers=self._auth_headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def _is_transient_request_error(self, exc: Exception) -> bool:
        if _requests is None:
            return False

        req_exc = _requests.exceptions
        if isinstance(exc, (req_exc.Timeout, req_exc.ConnectionError, req_exc.ChunkedEncodingError)):
            return True

        if isinstance(exc, req_exc.HTTPError):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in {408, 409, 425, 429, 500, 502, 503, 504}:
                return True

        return False

    def _get_bytes(self, url: str) -> bytes:
        attempts = 4
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = _requests.get(url, headers=self._auth_headers(), timeout=180)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts or not self._is_transient_request_error(exc):
                    raise
                time.sleep(min(8, 2 ** (attempt - 1)))

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unexpected download failure without exception details")

    def get_file_item(self, server_relative_url: str) -> dict:
        """Return the Graph driveItem metadata dict for a file.

        The response includes ``size`` (file size in bytes) and
        ``@microsoft.graph.downloadUrl`` — a pre-authenticated CDN URL that
        supports HTTP range requests without an Authorization header and avoids
        the CDN redirect issued by the ``/content`` endpoint.
        """
        drive_id, path = self._server_url_to_drive_path(server_relative_url)
        return self._get_json(f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:")

    def download_file_range_bytes(
        self,
        server_relative_url: str,
        start_byte: int,
        end_byte: int,
        *,
        download_url: str | None = None,
    ) -> bytes:
        """Download the byte range ``[start_byte, end_byte]`` (inclusive) from a file.

        Sends ``Range: bytes={start}-{end}`` on an HTTP GET.  When *download_url*
        is provided (the ``@microsoft.graph.downloadUrl`` value from
        :meth:`get_file_item`) it is used directly without an Authorization
        header — significantly reducing per-request overhead and avoiding the
        redirect that the Graph ``/content`` endpoint issues.
        """
        if end_byte < start_byte:
            return b""

        range_header = f"bytes={start_byte}-{end_byte}"
        attempts = 4
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                if download_url:
                    resp = _requests.get(
                        download_url, headers={"Range": range_header}, timeout=180
                    )
                    # The pre-authenticated CDN URL can expire mid-run on long
                    # validations. If it does, fall back to the Graph /content
                    # flow for this request (fresh auth, fresh redirect URL).
                    if resp.status_code in {401, 403, 404}:
                        drive_id, path = self._server_url_to_drive_path(server_relative_url)
                        content_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:/content"
                        redir = _requests.get(
                            content_url,
                            headers=self._auth_headers(),
                            allow_redirects=False,
                            timeout=30,
                        )
                        redir.raise_for_status()
                        cdn_url = redir.headers.get("Location", content_url)
                        resp = _requests.get(cdn_url, headers={"Range": range_header}, timeout=180)
                else:
                    # Resolve the pre-authenticated CDN URL first to avoid the
                    # redirect stripping the Range header.
                    drive_id, path = self._server_url_to_drive_path(server_relative_url)
                    content_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:/content"
                    redir = _requests.get(
                        content_url,
                        headers=self._auth_headers(),
                        allow_redirects=False,
                        timeout=30,
                    )
                    cdn_url = redir.headers.get("Location", content_url)
                    resp = _requests.get(cdn_url, headers={"Range": range_header}, timeout=180)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts or not self._is_transient_request_error(exc):
                    raise
                time.sleep(min(8, 2 ** (attempt - 1)))

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unexpected range-download failure without exception details")

    def _patch_json(self, url: str, body: dict) -> dict:
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        response = _requests.patch(url, headers=headers, json=body, timeout=30)
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Site / drive resolution
    # ------------------------------------------------------------------

    def _resolve_site_id(self) -> str:
        """Return the Graph composite site ID for the configured site URL."""
        url = f"{_GRAPH_BASE}/sites/{self._hostname}:/{self._site_relative}"
        data = self._get_json(url)
        return data["id"]

    def _get_drive_id(self, library_name: str) -> str:
        """Return the Graph drive ID for a document library name.

        The first call populates an in-memory cache from
        ``GET /v1.0/sites/{site_id}/drives``.
        """
        if library_name not in self._drive_cache:
            data = self._get_json(f"{_GRAPH_BASE}/sites/{self._site_id}/drives")
            for drive in data.get("value", []):
                name = drive.get("name", "")
                if name:
                    self._drive_cache[name] = drive["id"]
        if library_name not in self._drive_cache:
            raise ValueError(
                f"Document library '{library_name}' not found on site '{self.site_url}'. "
                f"Available libraries: {sorted(self._drive_cache.keys())}"
            )
        return self._drive_cache[library_name]

    def _server_url_to_drive_path(self, server_relative_url: str) -> tuple[str, str]:
        """Convert a SharePoint server-relative URL to a (drive_id, graph_path) pair.

        Example
        -------
        ``/sites/data_ingest_dev/Shared Documents/IncomingFiles/report.csv``
        →  ``(drive_id_for_shared_docs, "/IncomingFiles/report.csv")``
        """
        # Strip the "/sites/{site_name}" prefix that we own
        url = server_relative_url
        if url.startswith(self._site_path):
            url = url[len(self._site_path):]
        url = url.lstrip("/")  # "Shared Documents/IncomingFiles/report.csv"

        parts = url.split("/", 1)
        library_name = parts[0]  # e.g. "Shared Documents"
        path_in_library = "/" + parts[1] if len(parts) > 1 else "/"  # "/IncomingFiles/report.csv"

        drive_id = self._get_drive_id(library_name)
        return drive_id, path_in_library

    # ------------------------------------------------------------------
    # Public API  (mirrors original SharePointClient interface)
    # ------------------------------------------------------------------

    def list_files(self, folder_server_relative_url: str) -> list[SharePointFileItem]:
        """List all *files* (non-folders) directly inside the given folder.

        Parameters
        ----------
        folder_server_relative_url:
            SharePoint server-relative path to the folder, e.g.
            ``/sites/data_ingest_dev/Shared Documents/IncomingFiles``
        """
        drive_id, path = self._server_url_to_drive_path(folder_server_relative_url)

        if path in ("/", ""):
            url = f"{_GRAPH_BASE}/drives/{drive_id}/root/children"
        else:
            url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:/children"

        result: list[SharePointFileItem] = []

        # Follow @odata.nextLink pages (large libraries)
        while url:
            data = self._get_json(url)
            for item in data.get("value", []):
                if "file" in item:  # skip sub-folders
                    item_name: str = item["name"]
                    item_url = f"{folder_server_relative_url.rstrip('/')}/{item_name}"
                    result.append(
                        SharePointFileItem(name=item_name, server_relative_url=item_url)
                    )
            url = data.get("@odata.nextLink", "")  # empty string stops the loop

        return result

    def list_folders(self, folder_server_relative_url: str) -> list[SharePointFolderItem]:
        """List all *sub-folders* directly inside the given folder.

        Parameters
        ----------
        folder_server_relative_url:
            SharePoint server-relative path to the folder, e.g.
            ``/sites/data_ingest_dev/Shared Documents/IncomingFiles``
        """
        drive_id, path = self._server_url_to_drive_path(folder_server_relative_url)

        if path in ("/", ""):
            url = f"{_GRAPH_BASE}/drives/{drive_id}/root/children"
        else:
            url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:/children"

        result: list[SharePointFolderItem] = []

        # Follow @odata.nextLink pages (large libraries)
        while url:
            data = self._get_json(url)
            for item in data.get("value", []):
                if "folder" in item:  # skip files
                    item_name: str = item["name"]
                    item_url = f"{folder_server_relative_url.rstrip('/')}/{item_name}"
                    result.append(
                        SharePointFolderItem(name=item_name, server_relative_url=item_url)
                    )
            url = data.get("@odata.nextLink", "")  # empty string stops the loop

        return result

    def get_file_count(self, folder_server_relative_url: str) -> int:
        return len(self.list_files(folder_server_relative_url))

    def download_file_to_bytes(self, server_relative_url: str) -> bytes:
        drive_id, path = self._server_url_to_drive_path(server_relative_url)
        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:/content"
        return self._get_bytes(url)

    def download_file_to_buffer(self, server_relative_url: str) -> BytesIO:
        return BytesIO(self.download_file_to_bytes(server_relative_url))

    def folder_exists(self, folder_server_relative_url: str) -> bool:
        """Return ``True`` if the folder exists on SharePoint, ``False`` otherwise.

        Uses a lightweight HEAD-equivalent GET against the Graph drive item
        endpoint and treats a 404 as "does not exist" without raising.
        """
        normalized = folder_server_relative_url.rstrip("/")
        drive_id, path = self._server_url_to_drive_path(normalized)

        # The library root always exists; no API call needed.
        if path in ("", "/"):
            return True

        url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{path}:"
        response = _requests.get(url, headers=self._auth_headers(), timeout=30)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return "folder" in response.json()

    def create_folder(self, folder_server_relative_url: str) -> None:
        """Create a folder on SharePoint.

        Only creates the final segment; the parent must already exist.
        Treats a ``409 Conflict`` response as "already exists" so the call is
        safe to retry (idempotent on the server side).
        """
        normalized = folder_server_relative_url.rstrip("/")
        drive_id, path = self._server_url_to_drive_path(normalized)

        path_stripped = path.strip("/")
        if not path_stripped:
            return  # nothing to do for the library root

        parts = path_stripped.split("/")
        folder_name = parts[-1]
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

        if parent_path in ("", "/"):
            create_url = f"{_GRAPH_BASE}/drives/{drive_id}/root/children"
        else:
            create_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:{parent_path}:/children"

        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        body = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        response = _requests.post(create_url, headers=headers, json=body, timeout=30)
        if response.status_code == 409:
            # Already exists – treat as success (idempotent)
            return
        response.raise_for_status()

    def ensure_folder(self, folder_server_relative_url: str) -> bool:
        """Ensure *folder* exists on SharePoint, creating it if necessary.

        Returns
        -------
        bool
            ``True`` if the folder was **created** by this call.
            ``False`` if it already existed.

        This is the runtime equivalent of the ``_ensure_folder`` helper in
        ``sharepoint_setup/provision_sharepoint_folders.py``.  It is safe to
        call on every ingestion run — when the folder already exists the only
        overhead is a single lightweight GET.
        """
        normalized = folder_server_relative_url.rstrip("/")
        if self.folder_exists(normalized):
            return False
        self.create_folder(normalized)
        return True

    def move_file(
        self,
        src_server_relative_url: str,
        dest_folder_server_relative_url: str,
    ) -> str:
        """Move *src* file into *dest_folder* and return the new server-relative URL.

        Uses the Graph ``PATCH /drives/{id}/items/{item-id}`` endpoint with
        a ``parentReference`` body, which is atomic and supports cross-library
        moves within the same site.
        """
        src_drive_id, src_path = self._server_url_to_drive_path(src_server_relative_url)
        dest_drive_id, dest_path = self._server_url_to_drive_path(dest_folder_server_relative_url)

        file_name = os.path.basename(src_server_relative_url)

        # Resolve the source item ID
        item_meta = self._get_json(
            f"{_GRAPH_BASE}/drives/{src_drive_id}/root:{src_path}:"
        )
        item_id: str = item_meta["id"]

        # Move: PATCH the item's parentReference.
        # "@microsoft.graph.conflictBehavior": "replace" ensures the move
        # succeeds even when a same-named file already exists at the destination
        # (e.g. on a re-run after a previous partial failure).
        self._patch_json(
            f"{_GRAPH_BASE}/drives/{src_drive_id}/items/{item_id}",
            {
                "parentReference": {
                    "driveId": dest_drive_id,
                    "path": f"/drive/root:{dest_path}",
                },
                "name": file_name,
                "@microsoft.graph.conflictBehavior": "replace",
            },
        )

        return f"{dest_folder_server_relative_url.rstrip('/')}/{file_name}"
