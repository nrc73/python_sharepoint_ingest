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
