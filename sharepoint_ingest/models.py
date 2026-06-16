"""Dataclasses for ingestion configuration, validation issues, and run summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _flag_enabled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


@dataclass
class IngestionConfig:
    id: int
    sharepoint_base_url: str
    sharepoint_process_folder: str
    excel_tab_name: str
    sharepoint_process_archive_folder: Optional[str]
    sharepoint_process_failed_folder: Optional[str]
    process_frequency: Optional[str]
    header_skip_rows: int
    check_source_dest_columns: Any
    multi_file_ingest: Any
    to_email_address: Optional[str]
    process_id: Optional[str]
    workflow_id: Optional[str]
    staging_table_name: str
    is_active: Any = "1"
    ingestion_scope: str = "REAL"
    is_test_data: Any = 0
    file_name_pattern: Optional[str] = None
    load_strategy: Optional[str] = None
    merge_key_columns: Optional[str] = None
    column_mapping_json: Optional[str] = None
    cc_email_address: Optional[str] = None
    integrated_table_name: Optional[str] = None

    # ---------------------------------------------------------------------------
    # Backward-compat aliases — keep existing engine/notification code working
    # without renaming every call site in one pass.
    # ---------------------------------------------------------------------------

    @property
    def error_notification_email_address(self) -> Optional[str]:
        """Alias for to_email_address (legacy engine/notification call sites)."""
        return self.to_email_address

    @error_notification_email_address.setter
    def error_notification_email_address(self, value: Optional[str]) -> None:
        self.to_email_address = value

    @property
    def error_notification_cc_email_address(self) -> Optional[str]:
        """Alias for cc_email_address (legacy engine/notification call sites)."""
        return self.cc_email_address

    @error_notification_cc_email_address.setter
    def error_notification_cc_email_address(self, value: Optional[str]) -> None:
        self.cc_email_address = value

    @property
    def schema_check_enabled(self) -> bool:
        return _flag_enabled(self.check_source_dest_columns)

    @property
    def multi_file_enabled(self) -> bool:
        return _flag_enabled(self.multi_file_ingest)

    @property
    def active(self) -> bool:
        return _flag_enabled(self.is_active)

    @property
    def test_data_enabled(self) -> bool:
        return _flag_enabled(self.is_test_data)

    # ---------------------------------------------------------------------------
    # Factory: build from a SQL result-set row
    # ---------------------------------------------------------------------------

    @classmethod
    def from_sql_row(cls, row: dict[str, Any]) -> "IngestionConfig":
        """Construct an :class:`IngestionConfig` from a ``config.sharepoint_ingestion`` row.

        Centralises the DB→dataclass mapping so ``SqlClient`` and any future
        callers (e.g. diagnostic tools) share the same parsing rules without
        duplicating the field-name normalisation logic.
        """
        process_id = row.get("process_id")
        if process_id is not None:
            process_id = str(process_id)

        raw_column_mapping_json = row.get("column_mapping_json")
        normalized_column_mapping_json = (
            str(raw_column_mapping_json).strip()
            if raw_column_mapping_json is not None
            else ""
        )
        if not normalized_column_mapping_json:
            normalized_column_mapping_json = "{}"

        return cls(
            id=int(row.get("id")),
            sharepoint_base_url=str(row.get("sharepoint_base_url") or ""),
            sharepoint_process_folder=str(row.get("sharepoint_process_folder") or ""),
            excel_tab_name=str(row.get("excel_tab_name") or ""),
            sharepoint_process_archive_folder=row.get("sharepoint_process_archive_folder"),
            sharepoint_process_failed_folder=row.get("sharepoint_process_failed_folder"),
            process_frequency=row.get("process_frequency"),
            header_skip_rows=int(row.get("header_skip_rows") or 0),
            check_source_dest_columns=row.get("check_source_dest_columns"),
            multi_file_ingest=row.get("multi_file_ingest"),
            # Prefer new column names; fall back to legacy names for any
            # existing DBs that have not yet been migrated.
            to_email_address=(
                row.get("to_email_address")
                or row.get("error_notification_email_address")
            ),
            process_id=process_id,
            workflow_id=row.get("workflow_id"),
            staging_table_name=str(row.get("staging_table_name") or ""),
            is_active=row.get("is_active", "1"),
            ingestion_scope=str(row.get("ingestion_scope") or "REAL"),
            is_test_data=row.get("is_test_data", 0),
            file_name_pattern=row.get("file_name_pattern"),
            load_strategy=row.get("load_strategy"),
            merge_key_columns=row.get("merge_key_columns"),
            column_mapping_json=normalized_column_mapping_json,
            cc_email_address=(
                row.get("cc_email_address")
                or row.get("error_notification_cc_email_address")
            ),
            integrated_table_name=row.get("integrated_table_name") or None,
        )


@dataclass
class ValidationIssue:
    severity: str
    code: str
    message: str
    details: Optional[str] = None


@dataclass
class IngestionSummary:
    process_id: Optional[str]
    workflow_id: Optional[str]
    files_processed: int = 0
    files_failed: int = 0
    rows_loaded: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
