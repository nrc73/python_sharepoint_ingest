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
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

from sharepoint_ingest.config import AppSettings
from sharepoint_ingest.file_processors import (
    EncryptedExcelPayloadError,
    SharePointRangeReader,
    iter_csv_chunks_from_buffer,
    iter_parquet_chunks_from_file,
    open_parquet_from_range_reader,
    read_csv_from_bytes,
    read_excel_via_graph,
    read_parquet_from_bytes,
)
from sharepoint_ingest.ingestion._audit_lifecycle import (
    AuditDestination,
    AuditLifecycleRecorder,
    AuditMetrics,
)
from sharepoint_ingest.ingestion._chunk_pipeline import (
    ChunkValidationTracker,
    CrossChunkDuplicateKeyTracker,
    prepare_ingestion_chunk,
)
from sharepoint_ingest.ingestion._datetime_utils import (
    collect_csv_date_order_evidence,
    column_contains_non_date_text,
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
    _GRAPH_EXCEL_MODES = {"binary_only", "protected_auto", "cloud_excel_only"}
    _BIT_TRUE_TEXT_VALUES = {"1", "true", "t"}
    _BIT_FALSE_TEXT_VALUES = {"0", "false", "f"}
    _BIT_TEXT_VALUES = _BIT_TRUE_TEXT_VALUES | _BIT_FALSE_TEXT_VALUES

    def __init__(
        self,
        settings: AppSettings,
        sql_client: SqlClient,
        sharepoint_client: SharePointClient,
        logger: logging.Logger,
        stg_sql_client: Optional[SqlClient] = None,
        int_sql_client: Optional[SqlClient] = None,
    ):
        self.settings = settings
        # Audit DB client — used for config loading and audit log writes.
        self.sql_client = sql_client
        # Staging DB client — all file data is written here first (TRUNCATE daily).
        # Falls back to sql_client when not supplied (e.g. unit-test scenarios).
        self.stg_sql_client = stg_sql_client
        # Integrated DB client — data is promoted here after staging.
        # When None the promotion step is skipped (backward-compat / tests).
        self.int_sql_client = int_sql_client
        self.sharepoint_client = sharepoint_client
        self.logger = logger
        self.notifier = EmailNotifier(settings.email)
        self._audit_recorder = AuditLifecycleRecorder(self.sql_client, self.logger)
        self._last_file_rows_scanned: Optional[int] = None
        self._last_file_validation_error_count: int = 0
        self._current_file_memory_peak_mb: Optional[float] = None
        self._progress_markers: dict[str, set[int]] = {}

    # ── staging / integrated DB helpers ──────────────────────────────────────

    @property
    def _stg_client(self) -> SqlClient:
        """Return the staging-DB SqlClient, or fall back to the aud client."""
        return self.stg_sql_client if self.stg_sql_client is not None else self.sql_client

    @property
    def _int_client(self) -> Optional[SqlClient]:
        """Return the integrated-DB SqlClient (None if not configured)."""
        return self.int_sql_client

    @property
    def _int_database(self) -> Optional[str]:
        """Return the integrated DB name, or None."""
        if self.int_sql_client is not None:
            return self.int_sql_client._settings.database
        return None

    @property
    def _stg_database(self) -> Optional[str]:
        """Return the staging DB name, or None when the client is a test double."""
        try:
            return self._stg_client._settings.database
        except AttributeError:
            return None

    @staticmethod
    def _normalized_config_scope(config: "IngestionConfig") -> str:
        scope = str(config.ingestion_scope or "").strip().upper()
        if not scope:
            scope = "TEST" if config.test_data_enabled else "REAL"
        return scope

    def _effective_stg_only(self, config: "IngestionConfig", ingest_stg_only: bool) -> bool:
        """Return whether staging-only mode applies to this config.

        When requested, staging-only mode applies to every selected config scope,
        including TEST. The scope filter still controls which configs are selected;
        this helper only controls the destination behavior after selection.
        """
        return bool(ingest_stg_only)

    def _audit_destination_database(
        self, config: "IngestionConfig", *, ingest_stg_only: bool = False
    ) -> Optional[str]:
        """Return the database name that will ultimately hold the data.

        In staging-only mode this is the stg DB.  Otherwise it prefers the
        integrated DB when configured (that is where the final promoted data
        lives); falls back to the staging DB name.  Returns ``None`` when the
        client does not expose ``_settings`` (e.g. test doubles).
        """
        if self._effective_stg_only(config, ingest_stg_only):
            return self._stg_database
        if self._int_database:
            return self._int_database
        return self._stg_database

    def _audit_destination_table(
        self, config: "IngestionConfig", *, ingest_stg_only: bool = False
    ) -> Optional[str]:
        """Return the fully-qualified table name that will ultimately hold the data.

        In staging-only mode this is always ``staging_table_name``.  Otherwise it
        prefers ``integrated_table_name`` when set; falls back to
        ``staging_table_name``.
        """
        if self._effective_stg_only(config, ingest_stg_only):
            return config.staging_table_name
        int_table = (config.integrated_table_name or "").strip()
        return int_table if int_table else config.staging_table_name

    def _destination_context(
        self, config: "IngestionConfig", *, ingest_stg_only: bool = False
    ) -> str:
        database = self._audit_destination_database(config, ingest_stg_only=ingest_stg_only) or "unknown-db"
        table = self._audit_destination_table(config, ingest_stg_only=ingest_stg_only) or "unknown-table"
        return f"destination={database}.{table}"

    def _audit_destination(
        self, config: "IngestionConfig", *, ingest_stg_only: bool = False
    ) -> AuditDestination:
        """Return resolved destination values for audit rows."""
        return AuditDestination(
            database=self._audit_destination_database(config, ingest_stg_only=ingest_stg_only),
            table=self._audit_destination_table(config, ingest_stg_only=ingest_stg_only),
        )

    def _staging_primary_key_columns(self, config: "IngestionConfig") -> list[str]:
        """Return primary-key columns from the configured staging table only."""
        try:
            return [
                str(c).strip()
                for c in self._stg_client.get_primary_key_columns(config.staging_table_name)
                if str(c).strip()
            ]
        except Exception:
            self.logger.warning(
                "Config id=%s could not read primary key metadata for staging table '%s'",
                config.id,
                config.staging_table_name,
                exc_info=True,
            )
            return []

    def _validate_staging_only_destination(self, config: "IngestionConfig") -> None:
        """Fail fast when staging-only mode cannot resolve its staging target."""
        stg_table = (config.staging_table_name or "").strip()
        if not stg_table:
            raise ValueError(
                f"Config id={config.id} has blank staging_table_name; "
                "--ingest-stg-only requires a populated staging destination and "
                "does not fall back to integrated_table_name."
            )

        columns = self._stg_client.get_table_columns(stg_table)
        if not columns:
            raise ValueError(
                f"Config id={config.id} staging table '{stg_table}' was not found "
                "in the staging database or has no columns."
            )

    def _promote_stg_to_int(self, config: "IngestionConfig") -> None:
        """Copy all rows from staging → integrated table after a successful stg load.

        Skipped silently when:
        * ``int_sql_client`` is not configured (backward-compat / test mode), or
        * ``config.integrated_table_name`` is not set.
        """
        if self._int_client is None or self._int_database is None:
            return
        int_table = (config.integrated_table_name or "").strip()
        if not int_table:
            return

        load_strategy = self._resolve_load_strategy(config.load_strategy)
        self._validate_integrated_promotion_keys(config, int_table, load_strategy)
        self.logger.info(
            "Config id=%s  promoting stg→int  stg=%s  int=%s/%s  strategy=%s",
            config.id,
            config.staging_table_name,
            self._int_database,
            int_table,
            load_strategy,
        )
        rows_copied = self._stg_client.copy_stg_to_int(
            stg_table_name=config.staging_table_name,
            int_table_name=int_table,
            int_database=self._int_database,
            load_strategy=load_strategy,
        )
        self.logger.info(
            "Config id=%s  stg→int complete: %s rows promoted to %s/%s",
            config.id,
            rows_copied,
            self._int_database,
            int_table,
        )

    def _validate_integrated_promotion_keys(
        self, config: "IngestionConfig", int_table: str, load_strategy: str
    ) -> None:
        """Best-effort PK validation before staging→integrated promotion.

        Staging loads are validated before each write.  This additional guard
        catches duplicate keys in the full staged set, and APPEND conflicts with
        the integrated table, before SQL Server raises a less actionable insert
        failure during promotion.
        """
        if not hasattr(self._stg_client, "count_duplicate_keys"):
            return

        key_columns = [c.strip() for c in (config.merge_key_columns or "").split(",") if c.strip()]
        if not key_columns:
            try:
                if hasattr(self._stg_client, "get_primary_key_columns_in_database") and self._int_database:
                    key_columns = self._stg_client.get_primary_key_columns_in_database(self._int_database, int_table)
                if not key_columns:
                    key_columns = self._stg_client.get_primary_key_columns(config.staging_table_name)
            except Exception:
                key_columns = []

        if not key_columns:
            return

        duplicate_count = int(self._stg_client.count_duplicate_keys(config.staging_table_name, key_columns) or 0)
        if duplicate_count:
            raise ValueError(
                f"PRIMARY_KEY_VIOLATION: Staging table '{config.staging_table_name}' contains "
                f"{duplicate_count} duplicate key group(s) on key column(s) {key_columns}; cannot promote "
                f"to integrated table '{self._int_database}.{int_table}'."
            )

        if load_strategy != "APPEND" or not hasattr(self._stg_client, "count_key_conflicts_with_int"):
            return

        conflict_count = int(
            self._stg_client.count_key_conflicts_with_int(
                config.staging_table_name, self._int_database, int_table, key_columns
            )
            or 0
        )
        if conflict_count:
            raise ValueError(
                f"PRIMARY_KEY_VIOLATION: Staging table '{config.staging_table_name}' contains "
                f"{conflict_count} key value(s) that already exist in integrated table "
                f"'{self._int_database}.{int_table}' on key column(s) {key_columns}."
            )

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
        ingest_stg_only: bool = False,
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
            result = self._process_config(config, ingest_stg_only=ingest_stg_only)
            summary.files_processed += result.files_processed
            summary.files_failed += result.files_failed
            summary.rows_loaded += result.rows_loaded
            summary.errors.extend(result.errors)
        return summary

    def _process_config(
        self, config: IngestionConfig, *, ingest_stg_only: bool = False
    ) -> ProcessResult:
        effective_stg_only = self._effective_stg_only(config, ingest_stg_only)
        self.logger.info(
            "Starting config id=%s workflow=%s %s",
            config.id,
            config.workflow_id,
            self._destination_context(config, ingest_stg_only=ingest_stg_only),
        )

        result = ProcessResult(config_id=config.id)

        if effective_stg_only:
            try:
                self._validate_staging_only_destination(config)
            except Exception as exc:
                err = f"Config {config.id} staging-only destination validation failed: {exc}"
                self.logger.exception(err)
                result.errors.append(err)
                result.files_failed += 1
                validation_memory_peak = self._capture_memory_peak_mb()
                self._audit_recorder.best_effort_failure(
                    config,
                    file_name=None,
                    message=err,
                    metrics=AuditMetrics(
                        rows_scanned=0,
                        validation_error_count=0,
                        memory_peak_mb=validation_memory_peak,
                        duration_seconds=0,
                    ),
                    destination=self._audit_destination(config, ingest_stg_only=ingest_stg_only),
                    warning_context="staging-only validation",
                )
                self._notify_failure(
                    config,
                    err,
                    file_name=None,
                    rows_scanned=0,
                    memory_peak_mb=validation_memory_peak,
                    duration_seconds=0,
                    ingest_stg_only=ingest_stg_only,
                )
                return result

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

        for file_index, item in enumerate(matching_files):
            self._reset_file_telemetry()
            started = time.perf_counter()
            self._capture_memory_peak_mb()
            audit_destination = self._audit_destination(
                config, ingest_stg_only=ingest_stg_only
            )
            audit_id = self._audit_recorder.start_file(
                config,
                file_name=item.name,
                metrics=AuditMetrics(
                    rows_scanned=0,
                    validation_error_count=0,
                    memory_peak_mb=self._capture_memory_peak_mb(),
                    duration_seconds=None,
                ),
                destination=audit_destination,
            )

            try:
                force_append_for_file = (
                    file_index > 0 if effective_stg_only else force_append_for_selected_files
                )
                row_count = self._process_single_file(
                    config,
                    item.server_relative_url,
                    item.name,
                    archive_folder=archive_folder,
                    force_append=force_append_for_file,
                    audit_id=audit_id,
                    ingest_stg_only=ingest_stg_only,
                )
                result.files_processed += 1
                result.rows_loaded += row_count

                self._audit_recorder.finalise_file(
                    config,
                    audit_id=audit_id,
                    file_name=item.name,
                    status="SUCCESS",
                    records_loaded=int(row_count or 0),
                    message=None,
                    metrics=AuditMetrics(
                        rows_scanned=self._last_file_rows_scanned or 0,
                        validation_error_count=self._last_file_validation_error_count,
                        memory_peak_mb=self._capture_memory_peak_mb(),
                        duration_seconds=round(time.perf_counter() - started, 2),
                    ),
                    destination=audit_destination,
                )
            except Exception as exc:  # pragma: no cover - integration path
                err = f"Config {config.id} failed for file {item.name}: {exc}"
                self.logger.exception(err)
                result.errors.append(err)
                result.files_failed += 1
                failure_memory_peak = self._capture_memory_peak_mb()
                failure_duration = round(time.perf_counter() - started, 2)
                self._audit_recorder.finalise_file(
                    config,
                    audit_id=audit_id,
                    file_name=item.name,
                    status="FAILED",
                    records_loaded=0,
                    message=err,
                    metrics=AuditMetrics(
                        rows_scanned=self._last_file_rows_scanned or 0,
                        validation_error_count=self._last_file_validation_error_count,
                        memory_peak_mb=failure_memory_peak,
                        duration_seconds=failure_duration,
                    ),
                    destination=audit_destination,
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
                        memory_peak_mb=failure_memory_peak,
                        duration_seconds=failure_duration,
                        ingest_stg_only=ingest_stg_only,
                    )
                else:
                    self._notify_failure(
                        config, err, file_name=item.name,
                        rows_scanned=self._last_file_rows_scanned,
                        memory_peak_mb=failure_memory_peak,
                        duration_seconds=failure_duration,
                        ingest_stg_only=ingest_stg_only,
                    )

        # ── staging → integrated promotion ────────────────────────────────────
        # After all files for this config have been loaded into the stg DB, promote
        # them to the int DB using the configured load_strategy.  Skip when no files
        # were processed or any file failed (to avoid promoting stale/partial data).
        if result.files_processed > 0 and result.files_failed == 0 and not effective_stg_only:
            try:
                self._promote_stg_to_int(config)
            except Exception as exc:  # pragma: no cover - integration path
                err = f"Config {config.id} stg→int promotion failed: {exc}"
                self.logger.exception(err)
                result.errors.append(err)

        return result

    # ── single file dispatch ──────────────────────────────────────────────────

    def _process_single_file(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str,
        archive_folder: Optional[str] = None,
        force_append: bool = False,
        audit_id: Optional[int] = None,
        ingest_stg_only: bool = False,
    ) -> int:
        lower_name = file_name.lower()
        source_kind = self._resolve_source_kind(file_name)
        destination_columns = self._stg_client.get_table_columns(config.staging_table_name)
        if self._effective_stg_only(config, ingest_stg_only):
            resolved_load_strategy = "APPEND" if force_append else "TRUNCATE"
        else:
            resolved_load_strategy = self._resolve_load_strategy(
                config.load_strategy, force_append=force_append
            )
        self._capture_memory_peak_mb()

        if lower_name.endswith(".csv") and self.settings.enable_chunked_csv:
            return self._process_csv_file_in_chunks(
                config, server_relative_url, file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
                audit_id=audit_id,
                ingest_stg_only=ingest_stg_only,
            )

        if lower_name.endswith(".parquet") and getattr(
            self.settings, "enable_chunked_parquet", True
        ):
            return self._process_parquet_file_in_chunks(
                config, server_relative_url, file_name,
                archive_folder=archive_folder,
                load_strategy=resolved_load_strategy,
                audit_id=audit_id,
                ingest_stg_only=ingest_stg_only,
            )

        if source_kind == "excel" and self._graph_excel_extraction_mode() == "cloud_excel_only":
            self.logger.info(
                "Config id=%s reading Excel file '%s' via Graph workbook APIs (mode=cloud_excel_only)",
                config.id,
                file_name,
            )
            dataframe = self._parse_excel_via_graph(config, server_relative_url, file_name=file_name)
        else:
            payload = self.sharepoint_client.download_file_to_bytes(server_relative_url)
            self._capture_memory_peak_mb()
            try:
                dataframe = self._parse_file(config, payload, file_name)
            except EncryptedExcelPayloadError:
                if source_kind != "excel" or self._graph_excel_extraction_mode() != "protected_auto":
                    raise
                self.logger.info(
                    "Config id=%s detected protected Excel payload for '%s'; retrying via Graph workbook APIs (mode=protected_auto)",
                    config.id,
                    file_name,
                )
                dataframe = self._parse_excel_via_graph(config, server_relative_url, file_name=file_name)
        self._capture_memory_peak_mb()
        dataframe = self._apply_column_mapping_if_present(dataframe, config)
        dataframe = self._apply_ingestion_metadata(
            dataframe, config,
            destination_columns=destination_columns,
            file_name=file_name,
            source_kind=source_kind,
            audit_id=audit_id,
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
                ingest_stg_only=ingest_stg_only,
            )
        elif precheck_issues:
            self._publish_validation_issues(
                config, precheck_issues, ingest_stg_only=ingest_stg_only
            )

        self._check_for_intra_file_duplicate_keys(
            dataframe,
            config,
            resolved_load_strategy,
            enforce_for_truncate=self._effective_stg_only(config, ingest_stg_only),
        )
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
        audit_id: Optional[int] = None,
        ingest_stg_only: bool = False,
    ) -> int:
        buffer = self.sharepoint_client.download_file_to_buffer(server_relative_url)
        self._capture_memory_peak_mb()
        destination_columns = self._stg_client.get_table_columns(config.staging_table_name)
        csv_date_order_hints = self._infer_csv_datetime_order_hints(
            buffer,
            config,
            destination_columns,
        )
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

        enforce_truncate_keys = self._effective_stg_only(config, ingest_stg_only)

        # ── Streaming duplicate-key tracker for APPEND/staging-only mode ─────
        # Resolve merge keys once so we can detect cross-chunk duplicates.
        append_key_columns: list[str] = []
        if enforce_truncate_keys:
            append_key_columns = self._staging_primary_key_columns(config)
        elif resolved_load_strategy == "APPEND":
            try:
                append_key_columns = self._resolve_merge_keys(config)
            except Exception:
                append_key_columns = []

        validation_tracker = ChunkValidationTracker(
            enabled=config.schema_check_enabled,
            destination_columns=destination_columns,
            null_alert_threshold=self.settings.null_alert_threshold,
        )
        duplicate_tracker = CrossChunkDuplicateKeyTracker(
            key_columns=(
                append_key_columns
                if (resolved_load_strategy == "APPEND" or enforce_truncate_keys)
                else []
            ),
            table_name=config.staging_table_name,
            violation_context="when appended",
        )

        # ── 3. Single pass: transform → validate → staging load ───────────────
        for dataframe in chunk_iter:
            processed_any_chunk = True
            self._capture_memory_peak_mb()
            dataframe = prepare_ingestion_chunk(
                dataframe,
                config=config,
                destination_columns=destination_columns,
                file_name=file_name,
                source_kind="csv",
                audit_id=audit_id,
                apply_column_mapping=self._apply_column_mapping_if_present,
                apply_ingestion_metadata=self._apply_ingestion_metadata,
                normalize_dataframe=self._normalize_dataframe,
                date_order_hints=csv_date_order_hints,
            )
            self._capture_memory_peak_mb()

            total_rows_scanned += len(dataframe)
            self._set_rows_scanned(total_rows_scanned)

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

            # ── Per-chunk schema validation (every chunk, not just first) ─────
            validation_tracker.validate(dataframe)
            blocking_errors = validation_tracker.blocking_errors
            if blocking_errors:
                self._publish_validation_issues(
                    config, validation_tracker.issues, ingest_stg_only=ingest_stg_only
                )
                self._set_validation_error_count(len(blocking_errors))
                formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
                raise ValueError(f"Schema validation failed: {formatted}")

            # ── Streaming cross-chunk duplicate-key check ─────────────────────
            duplicate_tracker.check(dataframe)

            self._load_dataframe(
                config, dataframe, first_chunk=first_chunk,
                load_strategy=resolved_load_strategy,
            )
            total_rows += len(dataframe)

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

        # ── Publish any remaining non-blocking validation results ─────────────
        if validation_tracker.issues:
            self._publish_validation_issues(
                config, validation_tracker.issues, ingest_stg_only=ingest_stg_only
            )
            self._set_validation_error_count(validation_tracker.non_blocking_count)

        if not processed_any_chunk and resolved_load_strategy == "TRUNCATE":
            self._stg_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)
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
        audit_id: Optional[int] = None,
        ingest_stg_only: bool = False,
    ) -> int:
        """Stream-ingest a Parquet file via HTTP range requests in a single pass.

        Single-pass flow
        ────────────────
        1. ``get_file_item`` — 1 Graph API call, zero data download.
        2. Open :class:`~pyarrow.parquet.ParquetFile` via
           :class:`SharePointRangeReader` (reads footer only — 2–3 range
           requests).
        3. Single iteration over row groups — each row group is fetched once:
           transform → validate in-flight → load directly to the configured
           staging table (first chunk truncates on TRUNCATE strategy; all
           subsequent chunks append).
        4. After the complete pass: publish validation results and archive file.
        """
        # ── 1. File metadata — size guard ─────────────────────────────────────
        print(f"\n>>> [Stage 1/3]  Metadata check — fetching file info: {file_name}", flush=True)
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
        print(f"\n>>> [Stage 2/3]  Opening Parquet footer (range-request) ...", flush=True)
        parquet_file = open_parquet_from_range_reader(reader)
        total_file_rows = max(parquet_file.metadata.num_rows, 1)
        self._capture_memory_peak_mb()
        n_row_groups = parquet_file.metadata.num_row_groups
        print(
            f"  rows: {total_file_rows:,}  row-groups: {n_row_groups}  ✓ footer read",
            flush=True,
        )

        destination_columns = self._stg_client.get_table_columns(config.staging_table_name)
        resolved_load_strategy = self._resolve_load_strategy(
            load_strategy or config.load_strategy
        )

        total_rows = 0
        total_rows_scanned = 0
        first_chunk = True
        processed_any_chunk = False
        enforce_truncate_keys = self._effective_stg_only(config, ingest_stg_only)
        key_columns: list[str] = []
        if enforce_truncate_keys:
            key_columns = self._staging_primary_key_columns(config)

        validation_tracker = ChunkValidationTracker(
            enabled=config.schema_check_enabled,
            destination_columns=destination_columns,
            null_alert_threshold=self.settings.null_alert_threshold,
        )
        duplicate_tracker = CrossChunkDuplicateKeyTracker(
            key_columns=key_columns if enforce_truncate_keys else [],
            table_name=config.staging_table_name,
            violation_context="during staging-only reload",
        )

        self._log_sql_ingestion_progress(
            processed_rows=0, total_rows=total_file_rows, context=str(config.id)
        )

        # ── 3. Single pass: transform → validate → staging load ───────────────
        print(
            f"\n>>> [Stage 3/3]  Single-pass stream: transform → validate → staging table",
            flush=True,
        )
        print(
            f"  dest: {config.staging_table_name}  strategy: {resolved_load_strategy}",
            flush=True,
        )
        for dataframe in iter_parquet_chunks_from_file(
            parquet_file, chunk_size=self.settings.ingest_chunk_size_rows
        ):
            processed_any_chunk = True
            self._capture_memory_peak_mb()

            dataframe = prepare_ingestion_chunk(
                dataframe,
                config=config,
                destination_columns=destination_columns,
                file_name=file_name,
                source_kind="parquet",
                audit_id=audit_id,
                apply_column_mapping=self._apply_column_mapping_if_present,
                apply_ingestion_metadata=self._apply_ingestion_metadata,
                normalize_dataframe=self._normalize_dataframe,
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

            validation_tracker.validate(dataframe)
            blocking_errors = validation_tracker.blocking_errors
            if blocking_errors:
                self._publish_validation_issues(
                    config, validation_tracker.issues, ingest_stg_only=ingest_stg_only
                )
                self._set_validation_error_count(len(blocking_errors))
                formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
                raise ValueError(f"Schema validation failed: {formatted}")

            duplicate_tracker.check(dataframe)

            self._load_dataframe(
                config, dataframe,
                first_chunk=first_chunk,
                load_strategy=resolved_load_strategy,
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

        # ── Publish any remaining (non-blocking) validation results ───────────
        if validation_tracker.issues:
            self._publish_validation_issues(
                config, validation_tracker.issues, ingest_stg_only=ingest_stg_only
            )
            self._set_validation_error_count(validation_tracker.non_blocking_count)

        if not processed_any_chunk:
            if resolved_load_strategy == "TRUNCATE":
                self._stg_client.truncate_and_load(pd.DataFrame(), config.staging_table_name)
            self._set_rows_scanned(0)
        else:
            print(
                f"  ✓  {total_rows:,} rows loaded to {config.staging_table_name}",
                flush=True,
            )

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
            return self._parse_excel_payload(config, payload, file_name=file_name)
        if lower_name.endswith(".parquet"):
            return read_parquet_from_bytes(payload)
        raise ValueError(f"Unsupported file extension for {file_name}")

    @staticmethod
    def _resolve_source_kind(file_name: str) -> str:
        """Delegate to :func:`~sharepoint_ingest.ingestion._file_parsing.resolve_source_kind`."""
        return resolve_source_kind(file_name)

    def _parse_excel_payload(
        self, config: IngestionConfig, payload: bytes, file_name: str = ""
    ) -> pd.DataFrame:
        """Delegate to :func:`~sharepoint_ingest.ingestion._excel_utils.parse_excel_payload`."""
        return parse_excel_payload(config, payload, file_name=file_name)

    def _graph_excel_extraction_mode(self) -> str:
        """Return the configured Graph Excel extraction mode.

        ``binary_only`` preserves the existing download-and-parse behaviour.
        ``protected_auto`` falls back to Graph workbook APIs only when the binary
        parser detects an encrypted Office package.  ``cloud_excel_only`` always
        uses Graph workbook APIs for Excel files and avoids binary downloads.
        """
        mode = str(
            getattr(self.settings, "graph_excel_extraction_mode", "binary_only")
            or "binary_only"
        ).strip().lower()
        if mode not in self._GRAPH_EXCEL_MODES:
            raise ValueError(
                "Invalid GRAPH_EXCEL_EXTRACTION_MODE value "
                f"'{mode}'. Expected one of: {sorted(self._GRAPH_EXCEL_MODES)}"
            )
        return mode

    def _parse_excel_via_graph(
        self,
        config: IngestionConfig,
        server_relative_url: str,
        file_name: str = "",
    ) -> pd.DataFrame:
        """Read an Excel workbook through Microsoft Graph workbook endpoints."""
        display_name = file_name or server_relative_url
        try:
            return read_excel_via_graph(self.sharepoint_client, server_relative_url, config)
        except Exception as exc:
            raise ValueError(
                "Graph Excel extraction failed for "
                f"'{display_name}'. Confirm the token can create workbook sessions "
                "and the identity has sensitivity-label rights to read the workbook: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

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
        audit_id: Optional[int] = None,
    ) -> pd.DataFrame:
        """Delegate to :func:`~sharepoint_ingest.ingestion._metadata.apply_ingestion_metadata`."""
        return apply_ingestion_metadata(
            dataframe, config, destination_columns, file_name, source_kind, audit_id
        )

    # ── normalisation & validation ────────────────────────────────────────────

    def _normalize_dataframe(
        self,
        dataframe: pd.DataFrame,
        source_kind: str,
        destination_columns: list[dict],
        date_order_hints: Optional[dict[str, bool | None]] = None,
    ) -> pd.DataFrame:
        normalized = dataframe.copy()
        normalized.columns = [str(col).strip() for col in normalized.columns]
        for col in normalized.columns:
            if pd.api.types.is_object_dtype(normalized[col]):
                normalized[col] = normalized[col].map(
                    lambda v: v.strip() if isinstance(v, str) else v
                )
        bit_cols = self._destination_bit_columns(destination_columns)
        for source_col in normalized.columns:
            if source_col.strip().lower() not in bit_cols:
                continue
            normalized[source_col] = self._coerce_bit_series(
                normalized[source_col], column_name=source_col
            )
        dt_cols = destination_datetime_columns(destination_columns)
        for source_col in normalized.columns:
            if source_col.strip().lower() not in dt_cols:
                continue
            # Pass-through guard: if ANY non-null value in the column cannot be
            # parsed as a date (e.g. "Withdrawal due 01-Jul-26 00:00"), leave the
            # entire column unchanged.  Partial conversion would corrupt mixed
            # text+date columns by silently dropping the free-text rows as NaT.
            if column_contains_non_date_text(normalized[source_col]):
                continue
            normalized[source_col] = convert_series_to_datetime(
                series=normalized[source_col],
                source_kind=source_kind,
                column_name=source_col,
                date_order_hint=(date_order_hints or {}).get(source_col.strip().lower()),
            )
        return normalized

    def _infer_csv_datetime_order_hints(
        self,
        buffer,
        config: IngestionConfig,
        destination_columns: list[dict],
    ) -> dict[str, bool | None]:
        """Scan the full CSV once and infer date order for datetime columns.

        Chunked ingestion normalises each chunk independently, so a US hint such
        as ``4/15/2026 0:00`` in a later chunk must be collected before the first
        chunk is loaded.  This pre-scan is intentionally validation-like: mixed
        free text or conflicting AU/US hints fail before any partial SQL write.
        """
        dt_cols = destination_datetime_columns(destination_columns)
        if not dt_cols:
            try:
                buffer.seek(0)
            except Exception:
                pass
            return {}

        evidence_by_col = {}
        try:
            buffer.seek(0)
            for chunk in iter_csv_chunks_from_buffer(
                buffer,
                header_skip_rows=config.header_skip_rows,
                chunk_size=self.settings.ingest_chunk_size_rows,
            ):
                chunk = self._apply_column_mapping_if_present(chunk, config)
                chunk.columns = [str(col).strip() for col in chunk.columns]
                for source_col in chunk.columns:
                    key = source_col.strip().lower()
                    if key not in dt_cols:
                        continue
                    # Skip mixed text+date columns — they will be passed through
                    # unchanged by _normalize_dataframe and must not be counted
                    # as invalid date evidence here.
                    if column_contains_non_date_text(chunk[source_col]):
                        continue
                    evidence = collect_csv_date_order_evidence(chunk[source_col])
                    if key in evidence_by_col:
                        evidence_by_col[key].merge(evidence)
                    else:
                        evidence_by_col[key] = evidence

            hints: dict[str, bool | None] = {}
            for key, evidence in evidence_by_col.items():
                if evidence.invalid_samples:
                    samples = ", ".join(evidence.invalid_samples[:5])
                    raise ValueError(
                        f"Invalid or mixed CSV date values in datetime column '{key}'. "
                        "Expected digit-only dates in yyyy-MM-dd, dd-MM-yyyy/dd/MM/yyyy, "
                        "or MM-dd-yyyy/MM/dd/yyyy form. "
                        f"Samples: {samples}"
                    )
                hints[key] = evidence.resolve_dayfirst(column_name=key)
            return hints
        finally:
            try:
                buffer.seek(0)
            except Exception:
                pass

    @classmethod
    def _destination_bit_columns(cls, destination_columns: list[dict]) -> set[str]:
        result: set[str] = set()
        for col in destination_columns:
            column_name = str(col.get("column_name") or "").strip().lower()
            data_type = str(col.get("data_type") or "").strip().lower()
            if column_name and data_type == "bit":
                result.add(column_name)
        return result

    @classmethod
    def _coerce_bit_series(cls, series: pd.Series, *, column_name: str) -> pd.Series:
        """Convert strict bit-compatible tokens to Python bool/None values.

        Deliberately do not treat ``Yes``/``No`` or ``Y``/``N`` as booleans.
        Those are business text values in this project.  A destination SQL ``bit``
        column cannot preserve them, so fail before SQLAlchemy emits a very large
        executemany error dump.
        """
        if pd.api.types.is_bool_dtype(series):
            return series.astype(object).where(series.notna(), None)

        def _coerce(value):
            if value is None or pd.isna(value):
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and not isinstance(value, bool):
                if value in (0, 1):
                    return bool(value)
                raise ValueError(
                    f"Invalid boolean value for bit column '{column_name}': {value!r}. "
                    "Expected only true/false/t/f/1/0 values. Yes/No and Y/N are treated as strings; "
                    "use a text destination column if those values must be preserved."
                )
            text = str(value).strip()
            if text == "":
                return None
            key = text.lower()
            if key in cls._BIT_TRUE_TEXT_VALUES:
                return True
            if key in cls._BIT_FALSE_TEXT_VALUES:
                return False
            raise ValueError(
                f"Invalid boolean value for bit column '{column_name}': {value!r}. "
                "Expected only true/false/t/f/1/0 values. Yes/No and Y/N are treated as strings; "
                "use a text destination column if those values must be preserved."
            )

        return series.map(_coerce).astype(object)

    def _run_schema_checks(
        self,
        config: IngestionConfig,
        dataframe: pd.DataFrame,
        destination_columns: Optional[list[dict]] = None,
        precomputed_issues: Optional[list[ValidationIssue]] = None,
        ingest_stg_only: bool = False,
    ) -> None:
        dest_columns = (
            destination_columns
            or self._stg_client.get_table_columns(config.staging_table_name)
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
        self._publish_validation_issues(config, issues, ingest_stg_only=ingest_stg_only)
        blocking_errors = [i for i in issues if i.severity.upper() == "ERROR"]
        self._set_validation_error_count(len(blocking_errors))
        if blocking_errors:
            formatted = "; ".join(self._format_issue(i) for i in blocking_errors)
            raise ValueError(f"Schema validation failed: {formatted}")

    def _publish_validation_issues(
        self,
        config: IngestionConfig,
        issues: list[ValidationIssue],
        *,
        ingest_stg_only: bool = False,
    ) -> None:
        """Log every issue and dispatch a validation notification email.

        Delegates to :func:`~sharepoint_ingest.ingestion._engine_notifications.publish_and_notify_issues`.
        """
        publish_and_notify_issues(
            config,
            issues,
            self.notifier,
            self.logger,
            destination_context=self._destination_context(
                config, ingest_stg_only=ingest_stg_only
            ),
        )

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
            self._stg_client.append_load(dataframe, config.staging_table_name)
            return
        if first_chunk:
            self._stg_client.truncate_and_load(dataframe, config.staging_table_name)
            return
        self._stg_client.append_load(dataframe, config.staging_table_name)

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
        *,
        enforce_for_truncate: bool = False,
    ) -> None:
        """Delegate to :func:`~sharepoint_ingest.ingestion._pk_checks.check_for_intra_file_duplicate_keys`."""
        _check_pk_dups(
            dataframe,
            config,
            resolved_load_strategy,
            self._stg_client,
            self.logger,
            enforce_for_truncate=enforce_for_truncate,
            key_columns=(
                self._staging_primary_key_columns(config)
                if enforce_for_truncate
                else None
            ),
        )

    def _resolve_merge_keys(self, config: IngestionConfig) -> list[str]:
        """Delegate to :func:`~sharepoint_ingest.ingestion._pk_checks.resolve_merge_keys`."""
        return _resolve_merge_keys_fn(config, self._stg_client, self.logger)

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
        ingest_stg_only: bool = False,
    ) -> None:
        """Delegate to :func:`~sharepoint_ingest.ingestion._engine_notifications.notify_failure`."""
        _eng_notify_failure(
            self.notifier, config, self.settings.env_name, error_message,
            file_name=file_name, rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb, duration_seconds=duration_seconds,
            destination_context=self._destination_context(config, ingest_stg_only=ingest_stg_only),
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
        ingest_stg_only: bool = False,
    ) -> None:
        """Delegate to :func:`~sharepoint_ingest.ingestion._engine_notifications.notify_pk_violation`."""
        _eng_notify_pk_violation(
            self.notifier, config, self.settings.env_name, error_message,
            file_name=file_name, rows_scanned=rows_scanned,
            memory_peak_mb=memory_peak_mb, duration_seconds=duration_seconds,
            destination_context=self._destination_context(config, ingest_stg_only=ingest_stg_only),
            logger=self.logger,
        )

    @staticmethod
    def _extract_sheet_name_from_issues(
        issues: list[ValidationIssue],
    ) -> Optional[str]:
        """Delegate to :func:`~sharepoint_ingest.ingestion._notification_helpers.extract_sheet_name_from_issues`."""
        return extract_sheet_name_from_issues(issues)
