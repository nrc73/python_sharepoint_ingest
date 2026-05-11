from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.config import AppSettings
from src.file_processors import (
    iter_csv_chunks_from_buffer,
    read_all_excel_sheets_from_bytes,
    read_csv_from_bytes,
    read_excel_from_bytes,
)
from src.models import IngestionConfig, IngestionSummary, ValidationIssue
from src.notifications import EmailNotifier, build_validation_email_body
from src.schema_validator import validate_source_against_destination
from src.sharepoint_client import SharePointClient
from src.sql_client import SqlClient


@dataclass
class ProcessResult:
    config_id: int
    files_processed: int = 0
    files_failed: int = 0
    rows_loaded: int = 0
    errors: list[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class IngestionEngine:
    def __init__(
        self,
        settings: AppSettings,
        sql_client: SqlClient,
        sharepoint_client: SharePointClient,
        logger: logging.Logger,
    ):
        self.settings = settings
        self.sql_client = sql_client
        self.sharepoint_client = sharepoint_client
        self.logger = logger
        self.notifier = EmailNotifier(settings.email)

    def run(
        self,
        process_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        include_inactive: bool = False,
    ) -> IngestionSummary:
        configs = self.sql_client.fetch_ingestion_configs(
            process_id=process_id,
            workflow_id=workflow_id,
            active_only=not include_inactive,
        )

        summary = IngestionSummary(process_id=process_id, workflow_id=workflow_id)
        self.logger.info("Loaded %s ingestion config row(s)", len(configs))

        for config in configs:
            result = self._process_config(config)
            summary.files_processed += result.files_processed
            summary.files_failed += result.files_failed
            summary.rows_loaded += result.rows_loaded
            summary.errors.extend(result.errors)

        return summary

    def _process_config(self, config: IngestionConfig) -> ProcessResult:
        self.logger.info("Starting config id=%s workflow=%s", config.id, config.workflow_id)

        result = ProcessResult(config_id=config.id)
        pattern = config.file_name_pattern or self.settings.default_file_pattern or "*"

        files = self.sharepoint_client.list_files(config.sharepoint_process_folder)
        matching_files = [f for f in files if fnmatch.fnmatch(f.name, pattern)]

        if not config.multi_file_enabled and matching_files:
            matching_files = [matching_files[0]]

        self.logger.info(
            "Config id=%s discovered %s file(s), selected %s using pattern '%s'",
            config.id,
            len(files),
            len(matching_files),
            pattern,
        )

        for item in matching_files:
            try:
                row_count = self._process_single_file(config, item.server_relative_url, item.name)
                result.files_processed += 1
                result.rows_loaded += row_count
                self.sql_client.insert_audit_record(
                    config_id=config.id,
                    workflow_id=config.workflow_id,
                    process_id=config.process_id,
                    file_name=item.name,
                    status="SUCCESS",
                    records_loaded=row_count,
                    message=None,
                )
            except Exception as exc:  # pragma: no cover - integration path
                err = f"Config {config.id} failed for file {item.name}: {exc}"
                self.logger.exception(err)
                result.errors.append(err)
                result.files_failed += 1
                self.sql_client.insert_audit_record(
                    config_id=config.id,
                    workflow_id=config.workflow_id,
                    process_id=config.process_id,
                    file_name=item.name,
                    status="FAILED",
                    records_loaded=None,
                    message=err,
                )

                if config.sharepoint_process_failed_folder:
                    try:
                        self.sharepoint_client.move_file(
                            item.server_relative_url,
                            config.sharepoint_process_failed_folder,
                        )
                    except Exception:
                        self.logger.exception(
                            "Unable to move failed file '%s' to '%s'",
                            item.server_relative_url,
                            config.sharepoint_process_failed_folder,
                        )

                self._notify_failure(config, err)

        return result

    def _process_single_file(self, config: IngestionConfig, server_relative_url: str, file_name: str) -> int:
        lower_name = file_name.lower()

        if lower_name.endswith(".csv") and self.settings.enable_chunked_csv:
            return self._process_csv_file_in_chunks(config, server_relative_url, file_name)

        payload = self.sharepoint_client.download_file_to_bytes(server_relative_url)
        dataframe = self._parse_file(config, payload, file_name)
        dataframe = self._apply_column_mapping_if_present(dataframe, config)
        dataframe = self._normalize_dataframe(dataframe)

        if config.schema_check_enabled:
            self._run_schema_checks(config, dataframe)

        self._load_dataframe(config, dataframe)

        if config.sharepoint_process_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, config.sharepoint_process_archive_folder)

        return len(dataframe)

    def _process_csv_file_in_chunks(self, config: IngestionConfig, server_relative_url: str, file_name: str) -> int:
        buffer = self.sharepoint_client.download_file_to_buffer(server_relative_url)
        chunk_iter = iter_csv_chunks_from_buffer(
            buffer,
            header_skip_rows=config.header_skip_rows,
            chunk_size=self.settings.ingest_chunk_size_rows,
        )

        load_strategy = (config.load_strategy or self.settings.default_load_strategy or "truncate_reload").lower().strip()
        total_rows = 0
        first_chunk = True
        processed_any_chunk = False

        for dataframe in chunk_iter:
            processed_any_chunk = True
            dataframe = self._apply_column_mapping_if_present(dataframe, config)
            dataframe = self._normalize_dataframe(dataframe)

            if config.schema_check_enabled and first_chunk:
                self._run_schema_checks(config, dataframe)

            self._load_dataframe(config, dataframe, first_chunk=first_chunk, load_strategy=load_strategy)
            total_rows += len(dataframe)
            first_chunk = False

        if not processed_any_chunk and load_strategy == "truncate_reload":
            self.sql_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)

        if config.sharepoint_process_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, config.sharepoint_process_archive_folder)

        return total_rows

    def _parse_file(self, config: IngestionConfig, payload: bytes, file_name: str) -> pd.DataFrame:
        lower_name = file_name.lower()
        if lower_name.endswith(".csv"):
            return read_csv_from_bytes(payload, header_skip_rows=config.header_skip_rows)
        if lower_name.endswith(".xlsx") or lower_name.endswith(".xlsm") or lower_name.endswith(".xls"):
            return self._parse_excel_payload(config, payload)
        raise ValueError(f"Unsupported file extension for {file_name}")

    def _parse_excel_payload(self, config: IngestionConfig, payload: bytes) -> pd.DataFrame:
        tab_name = (config.excel_tab_name or "").strip()
        if not tab_name:
            return read_excel_from_bytes(payload, sheet_name=None, header_skip_rows=config.header_skip_rows)

        if tab_name.upper() in {"*", "ALL", "ALL_SHEETS"}:
            all_sheets = read_all_excel_sheets_from_bytes(payload, header_skip_rows=config.header_skip_rows)
            if not all_sheets:
                return pd.DataFrame()

            ordered = [all_sheets[name] for name in all_sheets.keys()]
            return pd.concat(ordered, ignore_index=True)

        if tab_name.upper().startswith("REGEX:"):
            pattern = tab_name.split(":", 1)[1].strip()
            regex = re.compile(pattern)
            all_sheets = read_all_excel_sheets_from_bytes(payload, header_skip_rows=config.header_skip_rows)
            matched = [df for name, df in all_sheets.items() if regex.search(name)]
            if not matched:
                raise ValueError(f"No worksheet names matched regex pattern: {pattern}")
            return pd.concat(matched, ignore_index=True)

        return read_excel_from_bytes(payload, sheet_name=tab_name, header_skip_rows=config.header_skip_rows)

    def _apply_column_mapping_if_present(self, dataframe: pd.DataFrame, config: IngestionConfig) -> pd.DataFrame:
        if not config.column_mapping_json:
            return dataframe

        mapping = json.loads(config.column_mapping_json)
        if not isinstance(mapping, dict):
            raise ValueError("column_mapping_json must contain a JSON object mapping source->destination names")

        return dataframe.rename(columns=mapping)

    @staticmethod
    def _normalize_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
        normalized = dataframe.copy()
        normalized.columns = [str(col).strip() for col in normalized.columns]
        for col in normalized.columns:
            if pd.api.types.is_object_dtype(normalized[col]):
                normalized[col] = normalized[col].astype(str).str.strip()

        for col in normalized.columns:
            if pd.api.types.is_object_dtype(normalized[col]):
                converted_date = pd.to_datetime(normalized[col], errors="ignore", dayfirst=True)
                if pd.api.types.is_datetime64_any_dtype(converted_date):
                    normalized[col] = converted_date

        return normalized

    def _run_schema_checks(self, config: IngestionConfig, dataframe: pd.DataFrame) -> None:
        dest_columns = self.sql_client.get_table_columns(config.staging_table_name)
        issues = validate_source_against_destination(
            source_df=dataframe,
            destination_columns=dest_columns,
            null_alert_threshold=self.settings.null_alert_threshold,
        )

        if not issues:
            return

        issue_strings = [self._format_issue(i) for i in issues]
        for issue in issue_strings:
            self.logger.warning("Config id=%s validation: %s", config.id, issue)

        self._notify_validation_issues(config, issue_strings)

        blocking_errors = [i for i in issues if i.severity.upper() == "ERROR"]
        if blocking_errors:
            formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
            raise ValueError(f"Schema validation failed: {formatted}")

    def _load_dataframe(
        self,
        config: IngestionConfig,
        dataframe: pd.DataFrame,
        first_chunk: bool = True,
        load_strategy: Optional[str] = None,
    ) -> None:
        load_strategy = (load_strategy or config.load_strategy or self.settings.default_load_strategy or "truncate_reload").lower().strip()

        if load_strategy == "merge":
            merge_keys = self._resolve_merge_keys(config)
            self.sql_client.merge_load(dataframe, config.staging_table_name, merge_keys=merge_keys)
            return

        if load_strategy == "append":
            self.sql_client.append_load(dataframe, config.staging_table_name)
            return

        if first_chunk:
            self.sql_client.truncate_and_load(dataframe, config.staging_table_name)
            return

        self.sql_client.append_load(dataframe, config.staging_table_name)

    def _resolve_merge_keys(self, config: IngestionConfig) -> list[str]:
        if config.merge_key_columns:
            return [c.strip() for c in config.merge_key_columns.split(",") if c.strip()]

        table_name = config.staging_table_name
        self.logger.warning(
            "No merge_key_columns configured for config id=%s table=%s. Falling back to first destination column.",
            config.id,
            table_name,
        )
        pk_columns = self.sql_client.get_primary_key_columns(table_name)
        if pk_columns:
            return pk_columns

        columns = self.sql_client.get_table_columns(table_name)
        if not columns:
            raise ValueError(f"Cannot resolve merge keys for {table_name}; no destination columns found")
        return [str(columns[0]["column_name"])]

    @staticmethod
    def _format_issue(issue: ValidationIssue) -> str:
        detail = f" ({issue.details})" if issue.details else ""
        return f"[{issue.severity}] {issue.code}: {issue.message}{detail}"

    def _notify_failure(self, config: IngestionConfig, error_message: str) -> None:
        subject = f"SharePoint ingestion failure - config {config.id}"
        body = (
            f"Configuration ID: {config.id}\n"
            f"Workflow ID: {config.workflow_id}\n"
            f"Process ID: {config.process_id}\n"
            f"Environment: {self.settings.env_name}\n\n"
            f"Error:\n{error_message}"
        )

        sent = self.notifier.send(config.error_notification_email_address, subject, body)
        if sent:
            self.logger.info("Failure notification sent to %s", config.error_notification_email_address)

    def _notify_validation_issues(self, config: IngestionConfig, issue_messages: list[str]) -> None:
        subject = f"SharePoint ingestion validation warning - config {config.id}"
        body = build_validation_email_body(
            process_name=f"config_id={config.id}, workflow_id={config.workflow_id}",
            issues=issue_messages,
        )
        sent = self.notifier.send(config.error_notification_email_address, subject, body)
        if sent:
            self.logger.info("Validation notification sent to %s", config.error_notification_email_address)
