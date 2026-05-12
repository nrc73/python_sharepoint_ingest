from __future__ import annotations

from datetime import date, datetime
import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

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
    _DATE_TYPE_NAMES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
    _SLASH_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(.*)$")

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

    def _runtime_sharepoint_site_url(self) -> str:
        sharepoint_settings = getattr(self.settings, "sharepoint", None)
        site_url = ""
        if sharepoint_settings is not None:
            site_url = str(getattr(sharepoint_settings, "site_url", "") or "")

        if not site_url:
            site_url = str(getattr(self.sharepoint_client, "site_url", "") or "")

        return site_url.rstrip("/")

    def _runtime_sharepoint_site_path(self) -> str:
        site_url = self._runtime_sharepoint_site_url()
        if site_url:
            parsed = urlparse(site_url)
            return parsed.path.rstrip("/")

        return str(getattr(self.sharepoint_client, "_site_path", "") or "").rstrip("/")

    @staticmethod
    def _normalize_server_relative_sharepoint_path(value: str, site_path: str) -> str:
        resolved = value.strip()
        if not resolved:
            return resolved

        if resolved.lower().startswith("http://") or resolved.lower().startswith("https://"):
            parsed = urlparse(resolved)
            resolved = parsed.path or "/"

        if not resolved.startswith("/"):
            resolved = "/" + resolved

        if site_path and (resolved == site_path or resolved.startswith(f"{site_path}/")):
            return resolved.rstrip("/")

        if resolved.startswith("/sites/") or resolved.startswith("/teams/"):
            return resolved.rstrip("/")

        if site_path:
            return f"{site_path.rstrip('/')}{resolved}".rstrip("/")

        return resolved.rstrip("/")

    def _resolve_sharepoint_value(self, configured_value: Optional[str], *, treat_as_path: bool) -> Optional[str]:
        if configured_value is None:
            return None

        resolved = str(configured_value).strip()
        if not resolved:
            return resolved

        site_url = self._runtime_sharepoint_site_url()
        site_path = self._runtime_sharepoint_site_path()
        env_name = str(getattr(self.settings, "env_name", "") or "")

        if site_url:
            resolved = resolved.replace("{env:sharepoint_site_url}", site_url)
            resolved = resolved.replace("{env:site_url}", site_url)
        if site_path:
            resolved = resolved.replace("{env:site_path}", site_path)
        resolved = resolved.replace("{env}", env_name)

        if not treat_as_path:
            return resolved

        return self._normalize_server_relative_sharepoint_path(resolved, site_path)

    def _resolve_sharepoint_folder(self, configured_folder: Optional[str]) -> Optional[str]:
        return self._resolve_sharepoint_value(configured_folder, treat_as_path=True)

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
        process_folder = self._resolve_sharepoint_folder(config.sharepoint_process_folder)
        archive_folder = self._resolve_sharepoint_folder(config.sharepoint_process_archive_folder)
        failed_folder = self._resolve_sharepoint_folder(config.sharepoint_process_failed_folder)
        resolved_base_url = self._resolve_sharepoint_value(config.sharepoint_base_url, treat_as_path=False)

        if resolved_base_url and resolved_base_url != config.sharepoint_base_url:
            self.logger.debug(
                "Config id=%s resolved sharepoint_base_url '%s' -> '%s'",
                config.id,
                config.sharepoint_base_url,
                resolved_base_url,
            )

        files = self.sharepoint_client.list_files(process_folder or config.sharepoint_process_folder)
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

        force_append_for_selected_files = len(matching_files) > 1

        for item in matching_files:
            try:
                row_count = self._process_single_file(
                    config,
                    item.server_relative_url,
                    item.name,
                    archive_folder=archive_folder,
                    force_append=force_append_for_selected_files,
                )
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

                if failed_folder:
                    try:
                        self.sharepoint_client.move_file(
                            item.server_relative_url,
                            failed_folder,
                        )
                    except Exception:
                        self.logger.exception(
                            "Unable to move failed file '%s' to '%s'",
                            item.server_relative_url,
                            failed_folder,
                        )

                self._notify_failure(config, err)

        return result

    def _process_single_file(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str,
        archive_folder: Optional[str] = None,
        force_append: bool = False,
    ) -> int:
        lower_name = file_name.lower()
        source_kind = "csv" if lower_name.endswith(".csv") else "excel"
        destination_columns = self.sql_client.get_table_columns(config.staging_table_name)
        resolved_load_strategy = self._resolve_load_strategy(config.load_strategy, force_append=force_append)

        if lower_name.endswith(".csv") and self.settings.enable_chunked_csv:
            return self._process_csv_file_in_chunks(
                config,
                server_relative_url,
                file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
            )

        payload = self.sharepoint_client.download_file_to_bytes(server_relative_url)
        dataframe = self._parse_file(config, payload, file_name)
        dataframe = self._apply_column_mapping_if_present(dataframe, config)
        dataframe = self._apply_ingestion_metadata(
            dataframe,
            config,
            destination_columns=destination_columns,
            file_name=file_name,
            source_kind=source_kind,
        )
        dataframe = self._normalize_dataframe(dataframe, source_kind=source_kind, destination_columns=destination_columns)

        if config.schema_check_enabled:
            self._run_schema_checks(config, dataframe, destination_columns=destination_columns)

        self._load_dataframe(config, dataframe, load_strategy=resolved_load_strategy)

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(config.sharepoint_process_archive_folder)
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

        return len(dataframe)

    def _process_csv_file_in_chunks(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str,
        archive_folder: Optional[str] = None,
        load_strategy: Optional[str] = None,
    ) -> int:
        buffer = self.sharepoint_client.download_file_to_buffer(server_relative_url)
        destination_columns = self.sql_client.get_table_columns(config.staging_table_name)
        chunk_iter = iter_csv_chunks_from_buffer(
            buffer,
            header_skip_rows=config.header_skip_rows,
            chunk_size=self.settings.ingest_chunk_size_rows,
        )

        resolved_load_strategy = self._resolve_load_strategy(load_strategy or config.load_strategy)
        total_rows = 0
        first_chunk = True
        processed_any_chunk = False

        for dataframe in chunk_iter:
            processed_any_chunk = True
            dataframe = self._apply_column_mapping_if_present(dataframe, config)
            dataframe = self._apply_ingestion_metadata(
                dataframe,
                config,
                destination_columns=destination_columns,
                file_name=file_name,
                source_kind="csv",
            )
            dataframe = self._normalize_dataframe(dataframe, source_kind="csv", destination_columns=destination_columns)

            if config.schema_check_enabled and first_chunk:
                self._run_schema_checks(config, dataframe, destination_columns=destination_columns)

            self._load_dataframe(
                config,
                dataframe,
                first_chunk=first_chunk,
                load_strategy=resolved_load_strategy,
            )
            total_rows += len(dataframe)
            first_chunk = False

        if not processed_any_chunk and resolved_load_strategy == "TRUNCATE":
            self.sql_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(config.sharepoint_process_archive_folder)
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

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

            ordered = [self._attach_excel_tab_name_column(all_sheets[name], name) for name in all_sheets.keys()]
            return pd.concat(ordered, ignore_index=True)

        if tab_name.upper().startswith("REGEX:"):
            pattern = tab_name.split(":", 1)[1].strip()
            regex = re.compile(pattern)
            all_sheets = read_all_excel_sheets_from_bytes(payload, header_skip_rows=config.header_skip_rows)
            matched = [
                self._attach_excel_tab_name_column(df, name)
                for name, df in all_sheets.items()
                if regex.search(name)
            ]
            if not matched:
                raise ValueError(f"No worksheet names matched regex pattern: {pattern}")
            return pd.concat(matched, ignore_index=True)

        return read_excel_from_bytes(payload, sheet_name=tab_name, header_skip_rows=config.header_skip_rows)

    @staticmethod
    def _attach_excel_tab_name_column(dataframe: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
        enriched = dataframe.copy()
        existing_column = next(
            (str(col) for col in enriched.columns if str(col).strip().lower() == "excel_tab_name"),
            None,
        )
        target_column = existing_column or "excel_tab_name"
        enriched[target_column] = sheet_name
        return enriched

    def _apply_column_mapping_if_present(self, dataframe: pd.DataFrame, config: IngestionConfig) -> pd.DataFrame:
        if not config.column_mapping_json:
            return dataframe

        mapping = json.loads(config.column_mapping_json)
        if not isinstance(mapping, dict):
            raise ValueError("column_mapping_json must contain a JSON object mapping source->destination names")

        return dataframe.rename(columns=mapping)

    @staticmethod
    def _find_existing_column_name(columns: list[str], target_column_name: str) -> Optional[str]:
        target = target_column_name.strip().lower()
        for col in columns:
            if str(col).strip().lower() == target:
                return str(col)
        return None

    def _apply_ingestion_metadata(
        self,
        dataframe: pd.DataFrame,
        config: IngestionConfig,
        destination_columns: list[dict],
        file_name: str,
        source_kind: str,
    ) -> pd.DataFrame:
        destination_column_names = {
            str(col.get("column_name") or "").strip().lower()
            for col in destination_columns
            if str(col.get("column_name") or "").strip()
        }

        if not destination_column_names:
            return dataframe

        enriched = dataframe.copy()

        if "source_file_name" in destination_column_names:
            source_file_col = self._find_existing_column_name(list(enriched.columns), "source_file_name") or "source_file_name"
            enriched[source_file_col] = file_name

        if source_kind == "excel" and "excel_tab_name" in destination_column_names:
            excel_tab_col = self._find_existing_column_name(list(enriched.columns), "excel_tab_name") or "excel_tab_name"
            configured_tab_name = (config.excel_tab_name or "").strip()

            if excel_tab_col not in enriched.columns:
                enriched[excel_tab_col] = configured_tab_name
            elif configured_tab_name:
                current_values = enriched[excel_tab_col]
                missing_mask = current_values.isna() | (current_values.map(lambda v: "" if v is None else str(v).strip()) == "")
                enriched.loc[missing_mask, excel_tab_col] = configured_tab_name

        return enriched

    def _normalize_dataframe(
        self,
        dataframe: pd.DataFrame,
        source_kind: str,
        destination_columns: list[dict],
    ) -> pd.DataFrame:
        normalized = dataframe.copy()
        normalized.columns = [str(col).strip() for col in normalized.columns]

        for col in normalized.columns:
            if pd.api.types.is_object_dtype(normalized[col]):
                normalized[col] = normalized[col].map(lambda v: v.strip() if isinstance(v, str) else v)

        datetime_columns = self._destination_datetime_columns(destination_columns)
        for source_col in normalized.columns:
            if source_col.strip().lower() not in datetime_columns:
                continue

            normalized[source_col] = self._convert_series_to_datetime(
                series=normalized[source_col],
                source_kind=source_kind,
                column_name=source_col,
            )

        return normalized

    def _run_schema_checks(
        self,
        config: IngestionConfig,
        dataframe: pd.DataFrame,
        destination_columns: Optional[list[dict]] = None,
    ) -> None:
        dest_columns = destination_columns or self.sql_client.get_table_columns(config.staging_table_name)
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

    @classmethod
    def _destination_datetime_columns(cls, destination_columns: list[dict]) -> set[str]:
        result: set[str] = set()
        for col in destination_columns:
            data_type = str(col.get("data_type") or "").strip().lower()
            if data_type in cls._DATE_TYPE_NAMES:
                result.add(str(col.get("column_name") or "").strip().lower())
        return result

    def _convert_series_to_datetime(self, series: pd.Series, source_kind: str, column_name: str) -> pd.Series:
        if pd.api.types.is_datetime64_any_dtype(series):
            return series

        out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
        ambiguous_positions: list[int] = []
        dmy_hints = 0
        mdy_hints = 0

        for idx, value in series.items():
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue

            if isinstance(value, str) and value.strip() == "":
                continue

            if isinstance(value, (pd.Timestamp, datetime, date)):
                out.at[idx] = pd.Timestamp(value)
                continue

            if source_kind == "excel" and isinstance(value, (int, float)):
                converted_excel = pd.to_datetime(value, unit="D", origin="1899-12-30", errors="coerce")
                if not pd.isna(converted_excel):
                    out.at[idx] = converted_excel
                    continue

            text_value = str(value).strip()
            iso_converted = pd.to_datetime(text_value, format="%Y-%m-%d", errors="coerce")
            if pd.isna(iso_converted):
                iso_converted = pd.to_datetime(text_value, format="%Y-%m-%d %H:%M:%S", errors="coerce")
            if pd.isna(iso_converted):
                iso_converted = pd.to_datetime(text_value, format="%Y-%m-%dT%H:%M:%S", errors="coerce")
            if not pd.isna(iso_converted):
                out.at[idx] = iso_converted
                continue

            slash_match = self._SLASH_DATE_RE.match(text_value)
            if slash_match:
                a = int(slash_match.group(1))
                b = int(slash_match.group(2))
                suffix = slash_match.group(4) or ""

                if a > 12 and b <= 12:
                    parsed = pd.to_datetime(f"{a:02d}/{b:02d}/{slash_match.group(3)}{suffix}", dayfirst=True, errors="coerce")
                    if pd.isna(parsed):
                        raise ValueError(f"Invalid date value '{text_value}' in column '{column_name}'.")
                    out.at[idx] = parsed
                    dmy_hints += 1
                    continue

                if b > 12 and a <= 12:
                    parsed = pd.to_datetime(f"{a:02d}/{b:02d}/{slash_match.group(3)}{suffix}", dayfirst=False, errors="coerce")
                    if pd.isna(parsed):
                        raise ValueError(f"Invalid date value '{text_value}' in column '{column_name}'.")
                    out.at[idx] = parsed
                    mdy_hints += 1
                    continue

                if a > 12 and b > 12:
                    raise ValueError(f"Invalid date value '{text_value}' in column '{column_name}'.")

                ambiguous_positions.append(idx)
                continue

            fallback = pd.to_datetime(text_value, errors="coerce")
            if pd.isna(fallback):
                raise ValueError(f"Unable to parse date value '{text_value}' in column '{column_name}'.")
            out.at[idx] = fallback

        if ambiguous_positions:
            if dmy_hints > 0 and mdy_hints == 0:
                dayfirst = True
            elif mdy_hints > 0 and dmy_hints == 0:
                dayfirst = False
            else:
                samples = ", ".join(str(series.at[i]) for i in ambiguous_positions[:5])
                raise ValueError(
                    f"Ambiguous date values in column '{column_name}'. "
                    f"Could not infer dd/MM vs MM/dd from: {samples}"
                )

            for idx in ambiguous_positions:
                parsed = pd.to_datetime(str(series.at[idx]).strip(), dayfirst=dayfirst, errors="coerce")
                if pd.isna(parsed):
                    raise ValueError(f"Invalid date value '{series.at[idx]}' in column '{column_name}'.")
                out.at[idx] = parsed

        return out

    def _load_dataframe(
        self,
        config: IngestionConfig,
        dataframe: pd.DataFrame,
        first_chunk: bool = True,
        load_strategy: Optional[str] = None,
    ) -> None:
        resolved_load_strategy = self._resolve_load_strategy(load_strategy or config.load_strategy)

        if resolved_load_strategy == "APPEND":
            self.sql_client.append_load(dataframe, config.staging_table_name)
            return

        if first_chunk:
            self.sql_client.truncate_and_load(dataframe, config.staging_table_name)
            return

        self.sql_client.append_load(dataframe, config.staging_table_name)

    def _resolve_load_strategy(self, configured_strategy: Optional[str], force_append: bool = False) -> str:
        if force_append:
            return "APPEND"

        raw_value = (configured_strategy or self.settings.default_load_strategy or "TRUNCATE").strip()
        if not raw_value:
            raw_value = "TRUNCATE"

        normalized = raw_value.replace("-", "_").upper()
        if normalized == "TRUNCATE_RELOAD":
            return "TRUNCATE"
        if normalized in {"TRUNCATE", "APPEND"}:
            return normalized

        raise ValueError(
            f"Unsupported load_strategy '{raw_value}'. Allowed values are TRUNCATE or APPEND."
        )

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
