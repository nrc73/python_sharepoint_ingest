"""Core ingestion orchestration engine for SharePoint-to-SQL pipelines.

Large helper groups have been extracted to ``sharepoint_ingest.ingestion.*``
sub-modules so this file stays focussed on orchestration and remains
navigable without a top-tier LLM context window:

* ``_datetime_utils``  — date parsing & Excel datetime text detection
* ``_excel_utils``     — workbook tab-selection and sheet-name tagging
* ``_file_parsing``    — source-kind / destination-table helpers
* ``_load_strategy``   — TRUNCATE / APPEND resolution
* ``_metadata``        — ingestion metadata column enrichment
* ``_notification_helpers`` — issue formatting & sheet-name extraction
* ``_sharepoint_paths`` — server-relative URL normalisation
* ``_telemetry``       — process memory sampling
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

from sharepoint_ingest.config import AppSettings
from sharepoint_ingest.file_processors import (
    SharePointRangeReader,
    iter_csv_chunks_from_buffer,
    iter_parquet_chunks_from_file,
    open_parquet_from_range_reader,
    read_csv_from_bytes,
    read_parquet_from_bytes,
)
from sharepoint_ingest.ingestion._datetime_utils import (
    convert_series_to_datetime,
    destination_datetime_columns,
    detect_excel_datetime_text_issues,
    is_date_like_text,
)
from sharepoint_ingest.ingestion._excel_utils import (
    attach_excel_tab_name_column,
    parse_excel_payload,
)
from sharepoint_ingest.ingestion._file_parsing import (
    parse_destination_table,
    resolve_source_kind,
)
from sharepoint_ingest.ingestion._load_strategy import resolve_load_strategy
from sharepoint_ingest.ingestion._metadata import (
    apply_ingestion_metadata,
    find_existing_column_name,
)
from sharepoint_ingest.ingestion._engine_notifications import (
    notify_failure as _eng_notify_failure,
    notify_pk_violation as _eng_notify_pk_violation,
    publish_and_notify_issues,
)
from sharepoint_ingest.ingestion._notification_helpers import (
    extract_sheet_name_from_issues,
    format_issue,
)
from sharepoint_ingest.ingestion._pk_checks import (
    check_for_intra_file_duplicate_keys as _check_pk_dups,
    resolve_merge_keys as _resolve_merge_keys_fn,
)
from sharepoint_ingest.ingestion._sharepoint_paths import normalize_server_relative_path
from sharepoint_ingest.ingestion._telemetry import read_process_memory_mb
from sharepoint_ingest.models import IngestionConfig, IngestionSummary, ValidationIssue
from sharepoint_ingest.notifications import EmailNotifier
from sharepoint_ingest.schema_validator import validate_source_against_destination
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient


@dataclass
class ProcessResult:
    config_id: int
    files_processed: int = 0
    files_failed: int = 0
    rows_loaded: int = 0
    errors: list[str] = field(default_factory=list)


# Maximum allowed Parquet file size for ingestion (permanent hard stop).
# Files larger than this are rejected immediately with a failure notification.
MAX_PARQUET_FILE_SIZE_BYTES: int = 512 * 1024 * 1024  # 512 MiB hard cap


class IngestionEngine:
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

    # ── per-file telemetry ────────────────────────────────────────────────────

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
                msg = (
                    f"  [{phase}]  {milestone:3d}%  "
                    f"{min(processed, total):,}/{total:,} {unit}"
                    f"{'  ✓ complete' if milestone >= 100 else ''}"
                )
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
                print(msg, flush=True)

    def _set_rows_scanned(self, value: int) -> None:
        self._last_file_rows_scanned = max(0, int(value))

    def _set_validation_error_count(self, value: int) -> None:
        self._last_file_validation_error_count = max(0, int(value))

    def _log_sql_ingestion_progress(
        self,
        *,
        processed_rows: int,
        total_rows: int,
        context: str,
    ) -> None:
        self._log_phase_progress(
            phase="sql-ingestion",
            processed=processed_rows,
            total=total_rows,
            context=context,
            unit="rows",
        )

    def _read_process_memory_mb(self) -> Optional[float]:
        """Delegate to the extracted telemetry helper."""
        return read_process_memory_mb()

    def _capture_memory_peak_mb(self) -> Optional[float]:
        current = self._read_process_memory_mb()
        if current is None:
            return self._current_file_memory_peak_mb
        if (
            self._current_file_memory_peak_mb is None
            or current > self._current_file_memory_peak_mb
        ):
            self._current_file_memory_peak_mb = current
        return self._current_file_memory_peak_mb

    # ── SharePoint path resolution ────────────────────────────────────────────

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
            return urlparse(site_url).path.rstrip("/")
        return str(getattr(self.sharepoint_client, "_site_path", "") or "").rstrip("/")

    def _resolve_sharepoint_value(
        self, configured_value: Optional[str], *, treat_as_path: bool
    ) -> Optional[str]:
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
        return normalize_server_relative_path(resolved, site_path)

    def _resolve_sharepoint_folder(self, configured_folder: Optional[str]) -> Optional[str]:
        return self._resolve_sharepoint_value(configured_folder, treat_as_path=True)

    def _enforce_prod_data_guard(self, configs: list[IngestionConfig]) -> None:
        env_name = str(getattr(self.settings, "env_name", "") or "").strip().lower()
        allow_test_data = bool(getattr(self.settings, "allow_test_data_in_prod", False))

        if env_name != "prod" or allow_test_data:
            return

        violating = []
        for cfg in configs:
            scope = str(cfg.ingestion_scope or "REAL").strip().upper()
            if not scope:
                scope = "TEST" if cfg.test_data_enabled else "REAL"

            if cfg.test_data_enabled or scope in {"TEST", "VALIDATION", "PERF_TEST"}:
                violating.append(f"id={cfg.id},workflow_id={cfg.workflow_id},scope={scope},is_test_data={cfg.test_data_enabled}")

        if violating:
            joined = "; ".join(violating)
            raise ValueError(
                "Guard rail violation: prod runtime selected test/validation configs. "
                "Remove these rows from prod or set ALLOW_TEST_DATA_IN_PROD=1 for a break-glass run. "
                f"Violations: {joined}"
            )

    # ── top-level run / config loop ───────────────────────────────────────────

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
        self._enforce_prod_data_guard(configs)
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
        resolved_base_url = self._resolve_sharepoint_value(
            config.sharepoint_base_url, treat_as_path=False
        )

        if resolved_base_url and resolved_base_url != config.sharepoint_base_url:
            self.logger.debug(
                "Config id=%s resolved sharepoint_base_url '%s' -> '%s'",
                config.id,
                config.sharepoint_base_url,
                resolved_base_url,
            )

        # Ensure Processed / Failed destination folders exist on first run.
        for _label, _folder in (
            ("archive/processed", archive_folder),
            ("failed", failed_folder),
        ):
            if _folder:
                try:
                    created = self.sharepoint_client.ensure_folder(_folder)
                    if created:
                        self.logger.info(
                            "Config id=%s created missing SharePoint %s folder: %s",
                            config.id, _label, _folder,
                        )
                    else:
                        self.logger.debug(
                            "Config id=%s %s folder already exists: %s",
                            config.id, _label, _folder,
                        )
                except Exception:
                    self.logger.warning(
                        "Config id=%s could not ensure %s folder '%s' — "
                        "ingestion will continue but file move may fail",
                        config.id, _label, _folder,
                        exc_info=True,
                    )

        files = self.sharepoint_client.list_files(
            process_folder or config.sharepoint_process_folder
        )
        matching_files = [f for f in files if fnmatch.fnmatch(f.name, pattern)]

        if not config.multi_file_enabled and matching_files:
            matching_files = [matching_files[0]]

        self.logger.info(
            "Config id=%s discovered %s file(s), selected %s using pattern '%s'",
            config.id, len(files), len(matching_files), pattern,
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
                            item.server_relative_url, failed_folder
                        )
                    except Exception:
                        self.logger.exception(
                            "Unable to move failed file '%s' to '%s'",
                            item.server_relative_url, failed_folder,
                        )
                is_pk_violation = str(exc).startswith("PRIMARY_KEY_VIOLATION:")
                if is_pk_violation:
                    self._notify_pk_violation(
                        config, err, file_name=item.name,
                        rows_scanned=self._last_file_rows_scanned,
                        memory_peak_mb=self._capture_memory_peak_mb(),
                        duration_seconds=round(time.perf_counter() - started, 2),
                    )
                else:
                    self._notify_failure(
                        config, err, file_name=item.name,
                        rows_scanned=self._last_file_rows_scanned,
                        memory_peak_mb=self._capture_memory_peak_mb(),
                        duration_seconds=round(time.perf_counter() - started, 2),
                    )

        return result

    # ── single file dispatch ──────────────────────────────────────────────────

    def _process_single_file(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str,
        archive_folder: Optional[str] = None,
        force_append: bool = False,
    ) -> int:
        lower_name = file_name.lower()
        source_kind = self._resolve_source_kind(file_name)
        destination_columns = self.sql_client.get_table_columns(config.staging_table_name)
        resolved_load_strategy = self._resolve_load_strategy(
            config.load_strategy, force_append=force_append
        )
        self._capture_memory_peak_mb()

        if lower_name.endswith(".csv") and self.settings.enable_chunked_csv:
            return self._process_csv_file_in_chunks(
                config, server_relative_url, file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
            )

        if lower_name.endswith(".parquet") and getattr(
            self.settings, "enable_chunked_parquet", True
        ):
            return self._process_parquet_file_in_chunks(
                config, server_relative_url, file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
            )

        payload = self.sharepoint_client.download_file_to_bytes(server_relative_url)
        self._capture_memory_peak_mb()
        dataframe = self._parse_file(config, payload, file_name)
        self._capture_memory_peak_mb()
        dataframe = self._apply_column_mapping_if_present(dataframe, config)
        dataframe = self._apply_ingestion_metadata(
            dataframe, config,
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
        precheck_issues = (
            self._detect_excel_datetime_text_issues(
                dataframe, destination_columns=destination_columns
            )
            if source_kind == "excel"
            else []
        )
        dataframe = self._normalize_dataframe(
            dataframe, source_kind=source_kind, destination_columns=destination_columns
        )
        self._capture_memory_peak_mb()

        if config.schema_check_enabled:
            self._run_schema_checks(
                config, dataframe,
                destination_columns=destination_columns,
                precomputed_issues=precheck_issues,
            )
        elif precheck_issues:
            self._publish_validation_issues(config, precheck_issues)

        self._check_for_intra_file_duplicate_keys(dataframe, config, resolved_load_strategy)
        self._load_dataframe(config, dataframe, load_strategy=resolved_load_strategy)
        self._log_sql_ingestion_progress(
            processed_rows=len(dataframe),
            total_rows=max(len(dataframe), 1),
            context=str(config.id),
        )
        self._capture_memory_peak_mb()

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(
            config.sharepoint_process_archive_folder
        )
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

        return len(dataframe)

    # ── CSV chunked ingestion ─────────────────────────────────────────────────

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
        resolved_load_strategy = self._resolve_load_strategy(
            load_strategy or config.load_strategy
        )
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
                dataframe, config,
                destination_columns=destination_columns,
                file_name=file_name,
                source_kind="csv",
            )
            dataframe = self._normalize_dataframe(
                dataframe, source_kind="csv", destination_columns=destination_columns
            )
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
                self._run_schema_checks(
                    config, dataframe, destination_columns=destination_columns
                )
            if first_chunk:
                self._check_for_intra_file_duplicate_keys(
                    dataframe, config, resolved_load_strategy
                )

            self._load_dataframe(
                config, dataframe, first_chunk=first_chunk,
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

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(
            config.sharepoint_process_archive_folder
        )
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

        self._capture_memory_peak_mb()
        return total_rows

    # ── Parquet single-pass streaming ingestion ───────────────────────────────

    @staticmethod
    def _parse_destination_table(table_name: str) -> tuple[str, str]:
        """Thin wrapper — delegates to :func:`~sharepoint_ingest.ingestion._file_parsing.parse_destination_table`."""
        return parse_destination_table(table_name)

    def _process_parquet_file_in_chunks(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str,
        archive_folder: Optional[str] = None,
        load_strategy: Optional[str] = None,
    ) -> int:
        """Stream-ingest a Parquet file via HTTP range requests in a single pass.

        Single-pass flow
        ────────────────
        1. ``get_file_item`` — 1 Graph API call, zero data download.
        2. Open :class:`~pyarrow.parquet.ParquetFile` via
           :class:`SharePointRangeReader` (reads footer only — 2–3 range
           requests).
        3. Single iteration over row groups — each row group is fetched once:
           transform → validate in-flight → load to temp table.
        4. After the complete pass: publish validation results, PK dup check
           (APPEND), then atomic swap temp → destination.
        """
        # ── 1. File metadata — size guard ─────────────────────────────────────
        print(f"\n>>> [Stage 1/4]  Metadata check — fetching file info: {file_name}", flush=True)
        file_item = self.sharepoint_client.get_file_item(server_relative_url)
        file_size = max(file_item.get("size", 1), 1)
        cdn_download_url = file_item.get("@microsoft.graph.downloadUrl")
        print(
            f"  file size: {file_size / (1024 * 1024):.2f} MB  "
            f"limit: {MAX_PARQUET_FILE_SIZE_BYTES // (1024 * 1024)} MB",
            flush=True,
        )

        if file_size > MAX_PARQUET_FILE_SIZE_BYTES:
            size_mib = file_size / (1024 * 1024)
            limit_mib = MAX_PARQUET_FILE_SIZE_BYTES / (1024 * 1024)
            raise ValueError(
                f"PARQUET_FILE_SIZE_LIMIT_EXCEEDED: File '{file_name}' is "
                f"{size_mib:.2f} MiB; maximum allowed is {limit_mib:.2f} MiB. "
                f"Split the file into smaller parts before ingesting."
            )

        reader = SharePointRangeReader(
            self.sharepoint_client, server_relative_url,
            file_size, download_url=cdn_download_url,
        )

        # ── 2. Open Parquet footer ─────────────────────────────────────────────
        print(f"\n>>> [Stage 2/4]  Opening Parquet footer (range-request) ...", flush=True)
        parquet_file = open_parquet_from_range_reader(reader)
        total_file_rows = max(parquet_file.metadata.num_rows, 1)
        self._capture_memory_peak_mb()
        n_row_groups = parquet_file.metadata.num_row_groups
        print(
            f"  rows: {total_file_rows:,}  row-groups: {n_row_groups}  ✓ footer read",
            flush=True,
        )

        destination_columns = self.sql_client.get_table_columns(config.staging_table_name)
        resolved_load_strategy = self._resolve_load_strategy(
            load_strategy or config.load_strategy
        )
        dest_schema, dest_table = parse_destination_table(config.staging_table_name)
        temp_table_name = f"_tmp_{dest_table}_{uuid.uuid4().hex[:8]}"

        aggregated_issues: list[ValidationIssue] = []
        total_rows = 0
        total_rows_scanned = 0
        first_chunk = True
        processed_any_chunk = False

        self._log_sql_ingestion_progress(
            processed_rows=0, total_rows=total_file_rows, context=str(config.id)
        )

        # ── 3. Single pass: transform → validate → temp-load ──────────────────
        print(
            f"\n>>> [Stage 3/4]  Single-pass stream: transform → validate → SQL temp table",
            flush=True,
        )
        print(
            f"  dest: {config.staging_table_name}  "
            f"temp: {temp_table_name}  strategy: {resolved_load_strategy}",
            flush=True,
        )
        try:
            for dataframe in iter_parquet_chunks_from_file(
                parquet_file, chunk_size=self.settings.ingest_chunk_size_rows
            ):
                processed_any_chunk = True
                self._capture_memory_peak_mb()

                dataframe = self._apply_column_mapping_if_present(dataframe, config)
                dataframe = self._apply_ingestion_metadata(
                    dataframe, config,
                    destination_columns=destination_columns,
                    file_name=file_name,
                    source_kind="parquet",
                )
                dataframe = self._normalize_dataframe(
                    dataframe, source_kind="parquet",
                    destination_columns=destination_columns,
                )
                self._capture_memory_peak_mb()

                total_rows_scanned += len(dataframe)
                self._set_rows_scanned(total_rows_scanned)
                self._log_phase_progress(
                    phase="validation",
                    processed=total_rows_scanned,
                    total=total_file_rows,
                    context=str(config.id),
                    unit="rows",
                )

                if config.schema_check_enabled:
                    chunk_issues = validate_source_against_destination(
                        source_df=dataframe,
                        destination_columns=destination_columns,
                        null_alert_threshold=self.settings.null_alert_threshold,
                    )
                    if chunk_issues:
                        aggregated_issues.extend(chunk_issues)

                self.sql_client.load_chunk_to_temp(
                    dataframe, temp_table_name, dest_schema, first_chunk
                )
                total_rows += len(dataframe)
                self._log_sql_ingestion_progress(
                    processed_rows=total_rows, total_rows=total_file_rows,
                    context=str(config.id),
                )
                self._log_phase_progress(
                    phase="import",
                    processed=total_rows, total=total_file_rows,
                    context=str(config.id), unit="rows",
                )
                first_chunk = False

            # ── 4a. Publish validation results ─────────────────────────────────
            if aggregated_issues:
                self._publish_validation_issues(config, aggregated_issues)
                blocking_errors = [
                    i for i in aggregated_issues if i.severity.upper() == "ERROR"
                ]
                self._set_validation_error_count(len(blocking_errors))
                if blocking_errors:
                    formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
                    raise ValueError(f"Schema validation failed: {formatted}")

            if not processed_any_chunk:
                if resolved_load_strategy == "TRUNCATE":
                    self.sql_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)
                self._set_rows_scanned(0)
            else:
                # ── 4b. SQL-side PK dup check for APPEND ──────────────────────
                if resolved_load_strategy == "APPEND":
                    try:
                        key_columns = self._resolve_merge_keys(config)
                        dup_count, sample_records = (
                            self.sql_client.check_temp_table_for_pk_duplicates(
                                temp_table_name, dest_schema, key_columns
                            )
                        )
                        if dup_count > 0:
                            raise ValueError(
                                f"PRIMARY_KEY_VIOLATION: File contains {dup_count} rows with "
                                f"duplicate values on key column(s) {key_columns} for table "
                                f"'{config.staging_table_name}'. "
                                f"Sample duplicate key values: {sample_records}"
                            )
                    except ValueError:
                        raise
                    except Exception:
                        pass  # non-fatal: skip check if key resolution fails

                # ── 4c. Atomic swap temp → destination ────────────────────────
                print(
                    f"\n>>> [Stage 4/4]  Atomic swap: "
                    f"{temp_table_name} → {config.staging_table_name} ...",
                    flush=True,
                )
                self.sql_client.swap_temp_to_destination(
                    temp_table_name, dest_schema, dest_table, resolved_load_strategy
                )
                print(
                    f"  ✓  {total_rows:,} rows committed to {config.staging_table_name}",
                    flush=True,
                )

        except Exception:
            try:
                self.sql_client.drop_temp_table(temp_table_name, dest_schema)
            except Exception:
                self.logger.warning(
                    "Config id=%s could not clean up temp table %s during error handling",
                    config.id, temp_table_name,
                )
            raise

        resolved_archive_folder = archive_folder or self._resolve_sharepoint_folder(
            config.sharepoint_process_archive_folder
        )
        if resolved_archive_folder:
            self.sharepoint_client.move_file(server_relative_url, resolved_archive_folder)

        self._capture_memory_peak_mb()
        return total_rows

    # ── file parsing ──────────────────────────────────────────────────────────

    def _parse_file(
        self, config: IngestionConfig, payload: bytes, file_name: str
    ) -> pd.DataFrame:
        lower_name = file_name.lower()
        if lower_name.endswith(".csv"):
            return read_csv_from_bytes(payload, header_skip_rows=config.header_skip_rows)
        if lower_name.endswith((".xlsx", ".xlsm", ".xls")):
            return self._parse_excel_payload(config, payload)
        if lower_name.endswith(".parquet"):
            return read_parquet_from_bytes(payload)
        raise ValueError(f"Unsupported file extension for {file_name}")

    @staticmethod
    def _resolve_source_kind(file_name: str) -> str:
        """Delegate to :func:`~sharepoint_ingest.ingestion._file_parsing.resolve_source_kind`."""
        return resolve_source_kind(file_name)

    def _parse_excel_payload(self, config: IngestionConfig, payload: bytes) -> pd.DataFrame:
        """Delegate to :func:`~sharepoint_ingest.ingestion._excel_utils.parse_excel_payload`."""
        return parse_excel_payload(config, payload)

    @staticmethod
    def _attach_excel_tab_name_column(
        dataframe: pd.DataFrame, sheet_name: str
    ) -> pd.DataFrame:
        """Delegate to :func:`~sharepoint_ingest.ingestion._excel_utils.attach_excel_tab_name_column`."""
        return attach_excel_tab_name_column(dataframe, sheet_name)

    # ── column mapping & metadata enrichment ─────────────────────────────────

    def _apply_column_mapping_if_present(
        self, dataframe: pd.DataFrame, config: IngestionConfig
    ) -> pd.DataFrame:
        if not config.column_mapping_json:
            return dataframe
        mapping = json.loads(config.column_mapping_json)
        if not isinstance(mapping, dict):
            raise ValueError(
                "column_mapping_json must contain a JSON object mapping source->destination names"
            )
        return dataframe.rename(columns=mapping)

    @staticmethod
    def _find_existing_column_name(
        columns: list[str], target_column_name: str
    ) -> Optional[str]:
        """Delegate to :func:`~sharepoint_ingest.ingestion._metadata.find_existing_column_name`."""
        return find_existing_column_name(columns, target_column_name)

    def _apply_ingestion_metadata(
        self,
        dataframe: pd.DataFrame,
        config: IngestionConfig,
        destination_columns: list[dict],
        file_name: str,
        source_kind: str,
    ) -> pd.DataFrame:
        """Delegate to :func:`~sharepoint_ingest.ingestion._metadata.apply_ingestion_metadata`."""
        return apply_ingestion_metadata(
            dataframe, config, destination_columns, file_name, source_kind
        )

    # ── normalisation & validation ────────────────────────────────────────────

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
                normalized[col] = normalized[col].map(
                    lambda v: v.strip() if isinstance(v, str) else v
                )
        dt_cols = destination_datetime_columns(destination_columns)
        for source_col in normalized.columns:
            if source_col.strip().lower() not in dt_cols:
                continue
            normalized[source_col] = convert_series_to_datetime(
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
        dest_columns = (
            destination_columns
            or self.sql_client.get_table_columns(config.staging_table_name)
        )
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

    def _publish_validation_issues(
        self, config: IngestionConfig, issues: list[ValidationIssue]
    ) -> None:
        """Log every issue and dispatch a validation notification email.

        Delegates to :func:`~sharepoint_ingest.ingestion._engine_notifications.publish_and_notify_issues`.
        """
        publish_and_notify_issues(config, issues, self.notifier, self.logger)

    # ── datetime helpers (thin class-level wrappers for backward compat) ──────

    @classmethod
    def _is_date_like_text(cls, text_value: str) -> bool:
        return is_date_like_text(text_value)

    def _detect_excel_datetime_text_issues(
        self,
        dataframe: pd.DataFrame,
        destination_columns: list[dict],
    ) -> list[ValidationIssue]:
        return detect_excel_datetime_text_issues(dataframe, destination_columns)

    @classmethod
    def _destination_datetime_columns(cls, destination_columns: list[dict]) -> set[str]:
        return destination_datetime_columns(destination_columns)

    def _convert_series_to_datetime(
        self, series: pd.Series, source_kind: str, column_name: str
    ) -> pd.Series:
        return convert_series_to_datetime(series, source_kind, column_name)

    # ── load strategy & SQL loading ───────────────────────────────────────────

    def _load_dataframe(
        self,
        config: IngestionConfig,
        dataframe: pd.DataFrame,
        first_chunk: bool = True,
        load_strategy: Optional[str] = None,
    ) -> None:
        resolved = self._resolve_load_strategy(load_strategy or config.load_strategy)
        if resolved == "APPEND":
            self.sql_client.append_load(dataframe, config.staging_table_name)
            return
        if first_chunk:
            self.sql_client.truncate_and_load(dataframe, config.staging_table_name)
            return
        self.sql_client.append_load(dataframe, config.staging_table_name)

    def _resolve_load_strategy(
        self, configured_strategy: Optional[str], force_append: bool = False
    ) -> str:
        """Delegate to :func:`~sharepoint_ingest.ingestion._load_strategy.resolve_load_strategy`."""
        return resolve_load_strategy(
            configured_strategy,
            default_strategy=getattr(self.settings, "default_load_strategy", "TRUNCATE"),
            force_append=force_append,
        )

    # ── primary-key duplicate detection ──────────────────────────────────────

    def _check_for_intra_file_duplicate_keys(
        self,
        dataframe: pd.DataFrame,
        config: IngestionConfig,
        resolved_load_strategy: str,
    ) -> None:
        """Delegate to :func:`~sharepoint_ingest.ingestion._pk_checks.check_for_intra_file_duplicate_keys`."""
        _check_pk_dups(dataframe, config, resolved_load_strategy, self.sql_client, self.logger)

    def _resolve_merge_keys(self, config: IngestionConfig) -> list[str]:
        """Delegate to :func:`~sharepoint_ingest.ingestion._pk_checks.resolve_merge_keys`."""
        return _resolve_merge_keys_fn(config, self.sql_client, self.logger)

    # ── notification helpers ──────────────────────────────────────────────────

    @staticmethod
    def _format_issue(issue: ValidationIssue) -> str:
        """Delegate to :func:`~sharepoint_ingest.ingestion._notification_helpers.format_issue`."""
        return format_issue(issue)

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
        """Delegate to :func:`~sharepoint_ingest.ingestion._engine_notifications.notify_failure`."""
        _eng_notify_failure(
            self.notifier, config, self.settings.env_name, error_message,
            file_name=file_name, rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb, duration_seconds=duration_seconds,
            logger=self.logger,
        )

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
        """Delegate to :func:`~sharepoint_ingest.ingestion._engine_notifications.notify_pk_violation`."""
        _eng_notify_pk_violation(
            self.notifier, config, self.settings.env_name, error_message,
            file_name=file_name, rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb, duration_seconds=duration_seconds,
            logger=self.logger,
        )

    @staticmethod
    def _extract_sheet_name_from_issues(
        issues: list[ValidationIssue],
    ) -> Optional[str]:
        """Delegate to :func:`~sharepoint_ingest.ingestion._notification_helpers.extract_sheet_name_from_issues`."""
        return extract_sheet_name_from_issues(issues)


