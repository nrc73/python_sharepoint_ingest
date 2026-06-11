"""Primary-key duplicate detection helpers.

Extracted from ``IngestionEngine`` so the logic is independently testable and
the engine file stays focussed on orchestration.

Public API
----------
check_for_intra_file_duplicate_keys
    Pre-flight check — raises ``ValueError`` with ``PRIMARY_KEY_VIOLATION:``
    prefix when duplicate key values are found in the incoming DataFrame.
resolve_merge_keys
    Resolve the list of key column names from config or SQL metadata.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig


def resolve_merge_keys(config: "IngestionConfig", sql_client, logger: logging.Logger) -> list[str]:
    """Return the list of key columns to use for duplicate detection.

    Priority order:
    1. ``config.merge_key_columns`` (comma-separated string, if set)
    2. SQL primary-key columns from ``sql_client.get_primary_key_columns``
    3. First column returned by ``sql_client.get_table_columns`` (last resort)
    """
    if config.merge_key_columns:
        return [c.strip() for c in config.merge_key_columns.split(",") if c.strip()]

    table_name = config.staging_table_name
    logger.warning(
        "No merge_key_columns configured for config id=%s table=%s. "
        "Falling back to first destination column.",
        config.id,
        table_name,
    )
    pk_columns = sql_client.get_primary_key_columns(table_name)
    if pk_columns:
        return pk_columns
    columns = sql_client.get_table_columns(table_name)
    if not columns:
        raise ValueError(
            f"Cannot resolve merge keys for {table_name}; no destination columns found"
        )
    return [str(columns[0]["column_name"])]


def check_for_intra_file_duplicate_keys(
    dataframe: pd.DataFrame,
    config: "IngestionConfig",
    resolved_load_strategy: str,
    sql_client,
    logger: logging.Logger,
    *,
    enforce_for_truncate: bool = False,
    key_columns: list[str] | None = None,
) -> None:
    """Pre-flight duplicate-key check — runs before any SQL write.

    Active when *resolved_load_strategy* is ``"APPEND"``.  Callers may also
    set *enforce_for_truncate* for strict full-reload modes where duplicate
    values in the incoming data would violate a staging-table PK even after the
    table has been truncated.  If duplicate values are found on the resolved key
    columns the function raises a
    ``ValueError`` with the ``PRIMARY_KEY_VIOLATION:`` prefix so the engine
    can route it to the dedicated PK violation notification path.
    """
    if resolved_load_strategy != "APPEND" and not enforce_for_truncate:
        return

    if key_columns is None:
        try:
            key_columns = resolve_merge_keys(config, sql_client, logger)
        except Exception:
            return
    else:
        key_columns = [c.strip() for c in key_columns if c and c.strip()]

    if not key_columns:
        return

    available_keys = [k for k in key_columns if k in dataframe.columns]
    if not available_keys:
        return

    duplicated_mask = dataframe.duplicated(subset=available_keys, keep=False)
    if not duplicated_mask.any():
        return

    dup_count = int(duplicated_mask.sum())
    sample_records = (
        dataframe.loc[duplicated_mask, available_keys]
        .drop_duplicates()
        .head(5)
        .to_dict(orient="records")
    )
    raise ValueError(
        f"PRIMARY_KEY_VIOLATION: File contains {dup_count} rows with duplicate values "
        f"on key column(s) {available_keys} for table '{config.staging_table_name}'. "
        f"This will cause a primary key constraint violation when appended. "
        f"Sample duplicate key values: {sample_records}"
    )
