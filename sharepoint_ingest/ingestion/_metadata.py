"""Ingestion metadata enrichment helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` (formerly the
``_find_existing_column_name`` and ``_apply_ingestion_metadata`` methods).
Both functions are stateless — they do not reference engine instance state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig


def find_existing_column_name(
    columns: list[str], target_column_name: str
) -> Optional[str]:
    """Return the first element of *columns* that matches *target_column_name*
    case-insensitively (after stripping whitespace), or ``None``.
    """
    target = target_column_name.strip().lower()
    for col in columns:
        if str(col).strip().lower() == target:
            return str(col)
    return None


def apply_ingestion_metadata(
    dataframe: pd.DataFrame,
    config: IngestionConfig,
    destination_columns: list[dict],
    file_name: str,
    source_kind: str,
    audit_id: Optional[int] = None,
) -> pd.DataFrame:
    """Enrich *dataframe* with framework metadata columns when they exist in
    *destination_columns*:

    * ``source_file_name`` — always set to *file_name*.
    * ``excel_tab_name`` — set to ``config.excel_tab_name`` for Excel sources,
      only on rows where the column is missing/blank (preserves values already
      populated by ``parse_excel_payload``).
    * ``sp_ingest_load_dt`` — set to current UTC timestamp for all rows.
    * ``audit_id`` — set to the ingestion audit row id for all rows when
      available.
    * ``__$job_instance_id`` — reserved system field,
      set to ``None`` (NULL) unless upstream orchestration populates them.

    Returns the enriched copy.  The original *dataframe* is not mutated.
    """
    destination_column_names = {
        str(col.get("column_name") or "").strip().lower()
        for col in destination_columns
        if str(col.get("column_name") or "").strip()
    }

    if not destination_column_names:
        return dataframe

    enriched = dataframe.copy()

    if "source_file_name" in destination_column_names:
        source_file_col = (
            find_existing_column_name(list(enriched.columns), "source_file_name")
            or "source_file_name"
        )
        enriched[source_file_col] = file_name

    if source_kind == "excel" and "excel_tab_name" in destination_column_names:
        excel_tab_col = (
            find_existing_column_name(list(enriched.columns), "excel_tab_name")
            or "excel_tab_name"
        )
        configured_tab_name = (config.excel_tab_name or "").strip()

        if excel_tab_col not in enriched.columns:
            enriched[excel_tab_col] = configured_tab_name
        elif configured_tab_name:
            current_values = enriched[excel_tab_col]
            missing_mask = current_values.isna() | (
                current_values.map(
                    lambda v: "" if v is None else str(v).strip()
                )
                == ""
            )
            enriched.loc[missing_mask, excel_tab_col] = configured_tab_name

    if "sp_ingest_load_dt" in destination_column_names:
        sp_ingest_load_dt_col = (
            find_existing_column_name(list(enriched.columns), "sp_ingest_load_dt")
            or "sp_ingest_load_dt"
        )
        enriched[sp_ingest_load_dt_col] = datetime.now(UTC)

    if "audit_id" in destination_column_names:
        audit_id_col = (
            find_existing_column_name(list(enriched.columns), "audit_id")
            or "audit_id"
        )
        enriched[audit_id_col] = audit_id

    if "__$job_instance_id" in destination_column_names:
        job_instance_col = (
            find_existing_column_name(list(enriched.columns), "__$job_instance_id")
            or "__$job_instance_id"
        )
        enriched[job_instance_col] = None

    return enriched
