"""SharePoint folder/URL path normalisation helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` (formerly the
``_normalize_server_relative_sharepoint_path`` static method).
"""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_server_relative_path(value: str, site_path: str) -> str:
    """Normalise *value* to a server-relative SharePoint path.

    * Strips full URLs down to their path component.
    * Ensures a leading ``/``.
    * Prepends *site_path* when the path is relative (does not already
      start with ``/sites/`` or ``/teams/`` and is not already under
      *site_path*).
    """
    resolved = value.strip()
    if not resolved:
        return resolved

    if resolved.lower().startswith(("http://", "https://")):
        resolved = urlparse(resolved).path or "/"

    if not resolved.startswith("/"):
        resolved = "/" + resolved

    if site_path and (
        resolved == site_path or resolved.startswith(f"{site_path}/")
    ):
        return resolved.rstrip("/")

    if resolved.startswith(("/sites/", "/teams/")):
        return resolved.rstrip("/")

    if site_path:
        return f"{site_path.rstrip('/')}{resolved}".rstrip("/")

    return resolved.rstrip("/")
