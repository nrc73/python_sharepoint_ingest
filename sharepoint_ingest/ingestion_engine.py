"""Core ingestion orchestration engine for SharePoint-to-SQL pipelines."""

from __future__ import annotations

from datetime import date, datetime
import os
import fnmatch
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

try:
    import psutil
except ImportError:  # pragma: no cover - dependency availability check
    psutil = None  # type: ignore[assignment]

from sharepoint_ingest.config import AppSettings
from sharepoint_ingest.file_processors import (
    iter_csv_chunks_from_buffer,
    read_all_excel_sheets_from_bytes,
    read_csv_from_bytes,
    read_excel_from_bytes,
)
from sharepoint_ingest.models import IngestionConfig, IngestionSummary, ValidationIssue
from sharepoint_ingest.notifications import EmailNotifier, build_failure_email_body, build_pk_violation_email_body, build_validation_email_body
from sharepoint_ingest.schema_validator import MANAGED_DESTINATION_COLUMNS, validate_source_against_destination
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient


@dataclass
class ProcessResult:
    config_id: int
    files_processed: int = 0
    files_failed: int = 0
    rows_loaded: int = 0
    errors: list[str] = field(default_factory=list)


class IngestionEngine:
    _DATE_TYPE_NAMES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
    _SLASH_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(.*)$")
    _DATE_LIKE_TEXT_RE = re.compile(r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$")
    _PROGRESS_PCT_STEPS = (0, 10, 25, 50, 75, 90, 100)

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
        self._last_file_rows_scanned: Optional[int] = None
        self._last_file_validation_error_count: int = 0
        self._current_file_memory_peak_mb: Optional[float] = None
        self._progress_markers: dict[str, set[int]] = {}

    def _reset_file_telemetry(self) -> None:
        self._last_file_rows_scanned = None
        self._last_file_validation_error_count = 0
        self._current_file_memory_peak_mb = None
        self._progress_markers = {}

    def _log_phase_progress(
        self,
        *,
        phase: str,
        processed: int,
        total: int,
        context: str,
        unit: str = "rows",
    ) -> None:
        if total <= 0:
            return

        ratio = max(0.0, min(1.0, float(processed) / float(total)))
        pct = int(round(ratio * 100))

        emitted = self._progress_markers.setdefault(phase, set())
        for milestone in self._PROGRESS_PCT_STEPS:
            if pct >= milestone and milestone not in emitted:
                emitted.add(milestone)
                self.logger.info(
                    "Config id=%s %s progress: %s%% (%s/%s %s)%s",
                    context,
                    phase,
                    milestone,
                    min(processed, total),
                    total,
                    unit,
                    "" if milestone < 100 else " complete",
                )

    def _set_rows_scanned(self, value: int) -> None:
        self._last_file_rows_scanned = max(0, int(value))

    def _set_validation_error_count(self, value: int) -> None:
        self._last_file_validation_error_count = max(0, int(value))

    def _read_process_memory_mb(self) -> Optional[float]:
        if psutil is not None:
            try:
                rss_bytes = psutil.Process().memory_info().rss
                return round(float(rss_bytes) / (1024 * 1024), 2)
            except Exception:  # pragma: no cover - platform/runtime dependent
                pass

        # Fallback for Windows environments where psutil is unavailable at runtime.
        if os.name != "nt":
            return None

        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)

            kernel32 = ctypes.WinDLL("Kernel32.dll")
            psapi = ctypes.WinDLL("Psapi.dll")

            process_handle = kernel32.GetCurrentProcess()
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL

            ok = get_process_memory_info(
                process_handle,
                ctypes.byref(counters),
                counters.cb,
            )
            if not ok:
                return None

            return round(float(counters.WorkingSetSize) / (1024 * 1024), 2)
        except Exception:  # pragma: no cover - platform/runtime dependent
            return None

    def _capture_memory_peak_mb(self) -> Optional[float]:
        current = self._read_process_memory_mb()
        if current is None:
            return self._current_file_memory_peak_mb

        if self._current_file_memory_peak_mb is None or current > self._current_file_memory_peak_mb:
            self._current_file_memory_peak_mb = current
        return self._current_file_memory_peak_mb

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
        ingestion_scope: Optional[str] = "real",
        include_inactive: bool = False,
    ) -> IngestionSummary:
        configs = self.sql_client.fetch_ingestion_configs(
            process_id=process_id,
            workflow_id=workflow_id,
            ingestion_scope=ingestion_scope,
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
            self._reset_file_telemetry()
            batch_id = str(uuid.uuid4())
            started = time.perf_counter()
            self._capture_memory_peak_mb()
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
                    batch_id=batch_id,
                    rows_scanned=self._last_file_rows_scanned,
                    validation_error_count=self._last_file_validation_error_count,
                    memory_peak_mb=self._capture_memory_peak_mb(),
                    duration_seconds=round(time.perf_counter() - started, 2),
                    ingestion_scope=config.ingestion_scope,
                    ingestion_domain=config.ingestion_domain,
                    is_test_data=config.test_data_enabled,
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
                    batch_id=batch_id,
                    rows_scanned=self._last_file_rows_scanned,
                    validation_error_count=self._last_file_validation_error_count,
                    memory_peak_mb=self._capture_memory_peak_mb(),
                    duration_seconds=round(time.perf_counter() - started, 2),
                    ingestion_scope=config.ingestion_scope,
                    ingestion_domain=config.ingestion_domain,
                    is_test_data=config.test_data_enabled,
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

                is_pk_violation = str(exc).startswith("PRIMARY_KEY_VIOLATION:")
                if is_pk_violation:
                    self._notify_pk_violation(
                        config,
                        err,
                        file_name=item.name,
                        rows_scanned=self._last_file_rows_scanned,
                        memory_peak_mb=self._capture_memory_peak_mb(),
                        duration_seconds=round(time.perf_counter() - started, 2),
                    )
                else:
                    self._notify_failure(
                        config,
                        err,
                        file_name=item.name,
                        rows_scanned=self._last_file_rows_scanned,
                        memory_peak_mb=self._capture_memory_peak_mb(),
                        duration_seconds=round(time.perf_counter() - started, 2),
                    )

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
        self._capture_memory_peak_mb()

        if lower_name.endswith(".csv") and self.settings.enable_chunked_csv:
            return self._process_csv_file_in_chunks(
                config,
                server_relative_url,
                file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
            )

        payload = self.sharepoint_client.download_file_to_bytes(server_relative_url)
        self._capture_memory_peak_mb()
        dataframe = self._parse_file(config, payload, file_name)
        self._capture_memory_peak_mb()
        dataframe = self._apply_column_mapping_if_present(dataframe, config)
        dataframe = self._apply_ingestion_metadata(
            dataframe,
            config,
            destination_columns=destination_columns,
            file_name=file_name,
            source_kind=source_kind,
        )
        self._set_rows_scanned(len(dataframe))
        self._log_phase_progress(
            phase="validation",
            processed=len(dataframe),
            total=len(dataframe),
            context=str(config.id),
        )
        precheck_issues = self._detect_excel_datetime_text_issues(
            dataframe,
            destination_columns=destination_columns,
        ) if source_kind == "excel" else []
        dataframe = self._normalize_dataframe(dataframe, source_kind=source_kind, destination_columns=destination_columns)
        self._capture_memory_peak_mb()

        if config.schema_check_enabled:
            self._run_schema_checks(
                config,
                dataframe,
                destination_columns=destination_columns,
                precomputed_issues=precheck_issues,
            )
        elif precheck_issues:
            self._publish_validation_issues(config, precheck_issues)

        self._check_for_intra_file_duplicate_keys(dataframe, config, resolved_load_strategy)
        self._load_dataframe(config, dataframe, load_strategy=resolved_load_strategy)
        self._log_phase_progress(
            phase="import",
            processed=len(dataframe),
            total=len(dataframe),
            context=str(config.id),
        )
        self._capture_memory_peak_mb()

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
        self._capture_memory_peak_mb()
        destination_columns = self.sql_client.get_table_columns(config.staging_table_name)
        chunk_iter = iter_csv_chunks_from_buffer(
            buffer,
            header_skip_rows=config.header_skip_rows,
            chunk_size=self.settings.ingest_chunk_size_rows,
        )

        resolved_load_strategy = self._resolve_load_strategy(load_strategy or config.load_strategy)
        total_rows = 0
        total_rows_scanned = 0
        first_chunk = True
        processed_any_chunk = False

        try:
            buffer.seek(0, 2)
            total_buffer_bytes = max(buffer.tell(), 1)
            buffer.seek(0)
        except Exception:
            total_buffer_bytes = 1

        for dataframe in chunk_iter:
            processed_any_chunk = True
            self._capture_memory_peak_mb()
            dataframe = self._apply_column_mapping_if_present(dataframe, config)
            dataframe = self._apply_ingestion_metadata(
                dataframe,
                config,
                destination_columns=destination_columns,
                file_name=file_name,
                source_kind="csv",
            )
            dataframe = self._normalize_dataframe(dataframe, source_kind="csv", destination_columns=destination_columns)
            self._capture_memory_peak_mb()

            try:
                processed_bytes = buffer.tell()
            except Exception:
                processed_bytes = 0

            self._log_phase_progress(
                phase="validation",
                processed=processed_bytes,
                total=total_buffer_bytes,
                context=str(config.id),
                unit="bytes",
            )

            if config.schema_check_enabled and first_chunk:
                self._run_schema_checks(config, dataframe, destination_columns=destination_columns)

            if first_chunk:
                self._check_for_intra_file_duplicate_keys(dataframe, config, resolved_load_strategy)

            self._load_dataframe(
                config,
                dataframe,
                first_chunk=first_chunk,
                load_strategy=resolved_load_strategy,
            )
            total_rows += len(dataframe)
            total_rows_scanned += len(dataframe)
            self._set_rows_scanned(total_rows_scanned)

            try:
                processed_bytes = buffer.tell()
            except Exception:
                processed_bytes = 0
            self._log_phase_progress(
                phase="import",
                processed=processed_bytes,
                total=total_buffer_bytes,
                context=str(config.id),
                unit="bytes",
            )
            first_chunk = False

        if not processed_any_chunk and resolved_load_strategy == "TRUNCATE":
            self.sql_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)
            self._set_rows_scanned(0)

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(config.sharepoint_process_archive_folder)
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

        self._capture_memory_peak_mb()

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
        precomputed_issues: Optional[list[ValidationIssue]] = None,
    ) -> None:
        dest_columns = destination_columns or self.sql_client.get_table_columns(config.staging_table_name)
        issues = validate_source_against_destination(
            source_df=dataframe,
            destination_columns=dest_columns,
            null_alert_threshold=self.settings.null_alert_threshold,
        )

        if precomputed_issues:
            issues = [*precomputed_issues, *issues]

        if not issues:
            return

        self._publish_validation_issues(config, issues)

        blocking_errors = [i for i in issues if i.severity.upper() == "ERROR"]
        self._set_validation_error_count(len(blocking_errors))
        if blocking_errors:
            formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
            raise ValueError(f"Schema validation failed: {formatted}")

    def _publish_validation_issues(self, config: IngestionConfig, issues: list[ValidationIssue]) -> None:
        issue_strings = [self._format_issue(i) for i in issues]
        for issue in issue_strings:
            self.logger.warning("Config id=%s validation: %s", config.id, issue)
        self._notify_validation_issues(config, issue_strings, issues=issues)

    @classmethod
    def _is_date_like_text(cls, text_value: str) -> bool:
        return bool(cls._DATE_LIKE_TEXT_RE.match(text_value.strip()))

    def _detect_excel_datetime_text_issues(
        self,
        dataframe: pd.DataFrame,
        destination_columns: list[dict],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        datetime_columns = self._destination_datetime_columns(destination_columns)
        if not datetime_columns:
            return issues

        for source_col in dataframe.columns:
            if source_col.strip().lower() not in datetime_columns:
                continue

            series = dataframe[source_col]
            text_date_values: list[str] = []

            for value in series:
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                if isinstance(value, (pd.Timestamp, datetime, date)):
                    continue
                if isinstance(value, str):
                    candidate = value.strip()
                    if candidate and self._is_date_like_text(candidate):
                        text_date_values.append(candidate)

            if not text_date_values:
                continue

            samples = ", ".join(text_date_values[:5])
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="EXCEL_DATETIME_STORED_AS_TEXT",
                    message=(
                        f"Date/datetime column '{source_col}' contains date-like values stored as text in Excel."
                    ),
                    details=f"count={len(text_date_values)}, samples={samples}",
                )
            )

        return issues

    @classmethod
    def _destination_datetime_columns(cls, destination_columns: list[dict]) -> set[str]:
        result: set[str] = set()
        for col in destination_columns:
            column_name = str(col.get("column_name") or "").strip().lower()
            if column_name in MANAGED_DESTINATION_COLUMNS:
                continue
            data_type = str(col.get("data_type") or "").strip().lower()
            if data_type in cls._DATE_TYPE_NAMES:
                result.add(column_name)
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

    def _check_for_intra_file_duplicate_keys(
        self,
        dataframe: pd.DataFrame,
        config: IngestionConfig,
        resolved_load_strategy: str,
    ) -> None:
        """Pre-flight check: detect duplicate primary key values within the incoming
        DataFrame before any SQL insert is attempted.

        Only runs when load_strategy is APPEND.  Raising here (before touching the
        database) ensures no partial data is committed in chunked CSV scenarios and
        provides a clear, actionable error message rather than a raw DB exception.
        """
        if resolved_load_strategy != "APPEND":
            return

        try:
            key_columns = self._resolve_merge_keys(config)
        except Exception:
            # If key resolution fails (e.g. no DB connectivity, no columns configured),
            # skip the pre-flight check rather than masking a legitimate connection error.
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

    def _notify_failure(
        self,
        config: IngestionConfig,
        error_message: str,
        *,
        file_name: Optional[str] = None,
        rows_scanned: Optional[int] = None,
        memory_peak_mb: Optional[float] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        subject = f"SharePoint ingestion failure - config {config.id}"
        process_name = (
            f"config_id={config.id}, workflow_id={config.workflow_id}, "
            f"process_id={config.process_id}, env={self.settings.env_name}"
        )
        body = build_failure_email_body(
            process_name=process_name,
            error_message=error_message,
            file_name=file_name,
            rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb,
            duration_seconds=duration_seconds,
        )

        sent = self.notifier.send(config.error_notification_email_address, subject, body)
        if sent:
            self.logger.info("Failure notification sent to %s", config.error_notification_email_address)

    def _notify_pk_violation(
        self,
        config: IngestionConfig,
        error_message: str,
        *,
        file_name: Optional[str] = None,
        rows_scanned: Optional[int] = None,
        memory_peak_mb: Optional[float] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Send a targeted PK violation email with remediation guidance."""
        subject = f"SharePoint ingestion PRIMARY KEY VIOLATION - config {config.id}"
        process_name = (
            f"config_id={config.id}, workflow_id={config.workflow_id}, "
            f"process_id={config.process_id}, env={self.settings.env_name}"
        )

        key_columns: list[str] = []
        if config.merge_key_columns:
            key_columns = [c.strip() for c in config.merge_key_columns.split(",") if c.strip()]

        duplicate_count: Optional[int] = None
        sample_values: Optional[list] = None

        dup_match = re.search(r"(\d+) rows with duplicate", error_message)
        if dup_match:
            duplicate_count = int(dup_match.group(1))

        sample_match = re.search(r"Sample duplicate key values: (\[.+\])", error_message)
        if sample_match:
            try:
                import ast
                sample_values = ast.literal_eval(sample_match.group(1))
            except Exception:
                pass

        body = build_pk_violation_email_body(
            process_name=process_name,
            error_message=error_message,
            file_name=file_name,
            table_name=config.staging_table_name,
            key_columns=key_columns or None,
            duplicate_count=duplicate_count,
            sample_values=sample_values,
            rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb,
            duration_seconds=duration_seconds,
        )

        sent = self.notifier.send(config.error_notification_email_address, subject, body)
        if sent:
            self.logger.info("PK violation notification sent to %s", config.error_notification_email_address)

    @staticmethod
    def _extract_sheet_name_from_issues(issues: list[ValidationIssue]) -> Optional[str]:
        sheet_names: list[str] = []
        sheet_re = re.compile(r"excel_tab_name\s*=\s*([^,;]+)", re.IGNORECASE)
        for issue in issues:
            details = str(issue.details or "")
            if not details:
                continue
            match = sheet_re.search(details)
            if match:
                candidate = match.group(1).strip().strip("\"'")
                if candidate:
                    sheet_names.append(candidate)

        if not sheet_names:
            return None

        unique_names = sorted(set(sheet_names))
        if len(unique_names) == 1:
            return unique_names[0]
        return f"multiple ({', '.join(unique_names[:3])}{'...' if len(unique_names) > 3 else ''})"

    def _notify_validation_issues(
        self,
        config: IngestionConfig,
        issue_messages: list[str],
        *,
        issues: Optional[list[ValidationIssue]] = None,
    ) -> None:
        subject = f"SharePoint ingestion validation warning - config {config.id}"
        source_file_name = None
        sheet_name = None

        if issues:
            sheet_name = self._extract_sheet_name_from_issues(issues)

        # best-effort file context from in-memory issue details/messages
        issue_blob = "\n".join(issue_messages)
        file_match = re.search(r"source_file_name\s*=\s*([^,;\n]+)", issue_blob, re.IGNORECASE)
        if file_match:
            source_file_name = file_match.group(1).strip().strip("\"'")

        body = build_validation_email_body(
            process_name=f"config_id={config.id}, workflow_id={config.workflow_id}",
            issues=issue_messages,
            file_name=source_file_name,
            sheet_name=sheet_name,
            max_issue_lines=15,
        )
        sent = self.notifier.send(config.error_notification_email_address, subject, body)
        if sent:
            self.logger.info("Validation notification sent to %s", config.error_notification_email_address)
