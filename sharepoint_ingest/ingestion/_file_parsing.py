"""File-type resolution and destination table parsing helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` (formerly the
``_resolve_source_kind`` and ``_parse_destination_table`` static methods).

The canonical identifier utilities live in ``sharepoint_ingest.sql._identifiers``;
this module re-exports ``DEFAULT_DESTINATION_SCHEMA`` and exposes
``parse_destination_table`` as a public alias so callers are not coupled to the
SQL sub-package directly.
"""

from __future__ import annotations

from sharepoint_ingest.sql._identifiers import (
    DEFAULT_DESTINATION_SCHEMA,
    parse_table_name as parse_destination_table,
)

__all__ = ["DEFAULT_DESTINATION_SCHEMA", "parse_destination_table", "resolve_source_kind"]


def resolve_source_kind(file_name: str) -> str:
    """Return ``"csv"``, ``"parquet"``, or ``"excel"`` from *file_name*.

    Raises ``ValueError`` for unrecognised extensions.
    """
    lower_name = file_name.lower()
    if lower_name.endswith(".csv"):
        return "csv"
    if lower_name.endswith(".parquet"):
        return "parquet"
    if lower_name.endswith((".xlsx", ".xlsm", ".xls")):
        return "excel"
    raise ValueError(f"Unsupported file extension for {file_name}")
