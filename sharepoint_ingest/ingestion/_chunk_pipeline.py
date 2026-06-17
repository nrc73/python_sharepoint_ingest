"""Reusable helpers for chunked ingestion pipelines.

CSV and Parquet ingestion have different source-opening and progress-reporting
concerns, but they share the same per-chunk business pipeline: column mapping,
metadata enrichment, normalisation, schema validation, duplicate-key detection,
and staged loading.  This module extracts the reusable pieces while leaving
source-specific orchestration in ``IngestionEngine``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

import pandas as pd

from sharepoint_ingest.models import IngestionConfig, ValidationIssue
from sharepoint_ingest.schema_validator import validate_source_against_destination


def prepare_ingestion_chunk(
    dataframe: pd.DataFrame,
    *,
    config: IngestionConfig,
    destination_columns: list[dict],
    file_name: str,
    source_kind: str,
    audit_id: Optional[int],
    apply_column_mapping: Callable[[pd.DataFrame, IngestionConfig], pd.DataFrame],
    apply_ingestion_metadata: Callable[..., pd.DataFrame],
    normalize_dataframe: Callable[..., pd.DataFrame],
    date_order_hints: Optional[dict[str, bool | None]] = None,
    force_all_us_dates_to_au: bool = False,
) -> pd.DataFrame:
    """Apply the common transform/enrichment/normalisation sequence to a chunk."""
    prepared = apply_column_mapping(dataframe, config)
    prepared = apply_ingestion_metadata(
        prepared,
        config,
        destination_columns=destination_columns,
        file_name=file_name,
        source_kind=source_kind,
        audit_id=audit_id,
    )
    normalize_kwargs = {
        "source_kind": source_kind,
        "destination_columns": destination_columns,
    }
    if date_order_hints is not None:
        normalize_kwargs["date_order_hints"] = date_order_hints
    if source_kind == "csv" and force_all_us_dates_to_au:
        normalize_kwargs["force_all_us_dates_to_au"] = True
    return normalize_dataframe(prepared, **normalize_kwargs)


class ChunkValidationTracker:
    """Aggregate schema-validation issues across chunks."""

    def __init__(
        self,
        *,
        enabled: bool,
        destination_columns: list[dict],
        null_alert_threshold: float,
    ) -> None:
        self.enabled = bool(enabled)
        self.destination_columns = destination_columns
        self.null_alert_threshold = null_alert_threshold
        self.issues: list[ValidationIssue] = []

    def validate(self, dataframe: pd.DataFrame) -> None:
        if not self.enabled:
            return
        chunk_issues = validate_source_against_destination(
            source_df=dataframe,
            destination_columns=self.destination_columns,
            null_alert_threshold=self.null_alert_threshold,
        )
        if chunk_issues:
            self.issues.extend(chunk_issues)

    @property
    def blocking_errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity.upper() == "ERROR"]

    @property
    def non_blocking_count(self) -> int:
        return sum(1 for i in self.issues if i.severity.upper() != "ERROR")


class CrossChunkDuplicateKeyTracker:
    """Detect within-chunk and cross-chunk duplicate key values."""

    def __init__(
        self,
        *,
        key_columns: list[str],
        table_name: str,
        violation_context: str,
    ) -> None:
        self.key_columns = [str(k).strip() for k in key_columns if str(k).strip()]
        self.table_name = table_name
        self.violation_context = violation_context
        self._seen_key_tuples: set[tuple] = set()

    def check(self, dataframe: pd.DataFrame) -> None:
        if not self.key_columns:
            return

        available_keys = [k for k in self.key_columns if k in dataframe.columns]
        if not available_keys:
            return

        chunk_tuples = [
            tuple(row)
            for row in dataframe[available_keys].itertuples(index=False, name=None)
        ]
        within_dup_mask = dataframe.duplicated(subset=available_keys, keep=False)
        cross_chunk_mask = pd.Series(
            [t in self._seen_key_tuples for t in chunk_tuples],
            index=dataframe.index,
        )
        dup_mask = within_dup_mask | cross_chunk_mask
        if dup_mask.any():
            dup_count = int(dup_mask.sum())
            sample_records = (
                dataframe.loc[dup_mask, available_keys]
                .drop_duplicates()
                .head(5)
                .to_dict(orient="records")
            )
            raise ValueError(
                f"PRIMARY_KEY_VIOLATION: File contains {dup_count} rows with "
                f"duplicate values on key column(s) {available_keys} for table "
                f"'{self.table_name}'. This will cause a primary key constraint "
                f"violation {self.violation_context}. "
                f"Sample duplicate key values: {sample_records}"
            )

        self._seen_key_tuples.update(chunk_tuples)
