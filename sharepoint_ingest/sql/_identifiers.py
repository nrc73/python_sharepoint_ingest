"""SQL identifier quoting and table name parsing utilities."""

from __future__ import annotations

DEFAULT_DESTINATION_SCHEMA = "sharepoint"


def quote_identifier(name: str) -> str:
    """Bracket-quote a SQL Server identifier."""
    return "[" + name.replace("]", "]]") + "]"


def parse_table_name(table_name: str) -> tuple[str, str]:
    """Split a fully-qualified table name into (schema, table).

    If no schema is given, ``DEFAULT_DESTINATION_SCHEMA`` is used.
    """
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema.strip(), table.strip()
    return DEFAULT_DESTINATION_SCHEMA, table_name.strip()
