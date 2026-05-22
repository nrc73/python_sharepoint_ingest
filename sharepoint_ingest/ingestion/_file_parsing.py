"""File-type resolution and destination table parsing helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` (formerly the
``_resolve_source_kind`` and ``_parse_destination_table`` static methods).
"""

from __future__ import annotations


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


def parse_destination_table(table_name: str) -> tuple[str, str]:
    """Parse a ``[schema.]table`` name into a ``(schema, table)`` pair.

    Returns ``("dbo", table_name)`` when no schema prefix is present.
    """
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema.strip(), table.strip()
    return "dbo", table_name.strip()
