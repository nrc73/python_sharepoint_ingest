"""Audit-row lifecycle helpers for ingestion orchestration.

The ingestion engine owns business orchestration, while this module owns the
repeated mechanics of creating/updating audit rows and building consistent audit
payloads.  It deliberately delegates to the existing ``SqlClient`` public
methods so database compatibility behaviour remains unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig


@dataclass(frozen=True)
class AuditDestination:
    """Resolved destination details stored on audit rows."""

    database: Optional[str]
    table: Optional[str]


@dataclass(frozen=True)
class AuditMetrics:
    """Runtime metrics captured for an audit-row state transition."""

    rows_scanned: Optional[int] = None
    validation_error_count: Optional[int] = None
    memory_peak_mb: Optional[float] = None
    duration_seconds: Optional[float] = None


class AuditLifecycleRecorder:
    """Create and finalise SharePoint ingestion audit rows.

    The recorder is intentionally thin: it centralises payload construction and
    update-fallback-insert behaviour while preserving the existing ``SqlClient``
    API and error propagation semantics used by ``IngestionEngine``.
    """

    def __init__(self, sql_client, logger: logging.Logger):
        self._sql_client = sql_client
        self._logger = logger

    def _payload(
        self,
        config: "IngestionConfig",
        *,
        file_name: Optional[str],
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
        metrics: AuditMetrics,
        destination: AuditDestination,
    ) -> dict:
        return {
            "config_id": config.id,
            "workflow_id": config.workflow_id,
            "process_id": config.process_id,
            "file_name": file_name,
            "status": status,
            "records_loaded": records_loaded,
            "message": message,
            "rows_scanned": metrics.rows_scanned,
            "validation_error_count": metrics.validation_error_count,
            "memory_peak_mb": metrics.memory_peak_mb,
            "duration_seconds": metrics.duration_seconds,
            "ingestion_scope": config.ingestion_scope,
            "is_test_data": config.test_data_enabled,
            "destination_database": destination.database,
            "destination_table": destination.table,
        }

    def insert_state(
        self,
        config: "IngestionConfig",
        *,
        file_name: Optional[str],
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
        metrics: AuditMetrics,
        destination: AuditDestination,
    ) -> Optional[int]:
        """Insert an audit row for a lifecycle state and return its audit id."""
        return self._sql_client.insert_audit_record(
            **self._payload(
                config,
                file_name=file_name,
                status=status,
                records_loaded=records_loaded,
                message=message,
                metrics=metrics,
                destination=destination,
            )
        )

    def start_file(
        self,
        config: "IngestionConfig",
        *,
        file_name: str,
        metrics: AuditMetrics,
        destination: AuditDestination,
    ) -> Optional[int]:
        """Create a STARTED audit row, preserving best-effort failure semantics."""
        try:
            return self.insert_state(
                config,
                file_name=file_name,
                status="STARTED",
                records_loaded=0,
                message=None,
                metrics=metrics,
                destination=destination,
            )
        except Exception:
            self._logger.warning(
                "Config id=%s could not create STARTED audit row for file %s",
                config.id,
                file_name,
                exc_info=True,
            )
            return None

    def finalise_file(
        self,
        config: "IngestionConfig",
        *,
        audit_id: Optional[int],
        file_name: str,
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
        metrics: AuditMetrics,
        destination: AuditDestination,
    ) -> None:
        """Update an existing audit row, or insert one if no row/update exists."""
        update_payload = self._payload(
            config,
            file_name=file_name,
            status=status,
            records_loaded=records_loaded,
            message=message,
            metrics=metrics,
            destination=destination,
        )
        if audit_id is not None:
            updated = self._sql_client.update_audit_record(
                audit_id=audit_id,
                status=status,
                records_loaded=records_loaded,
                message=message,
                rows_scanned=metrics.rows_scanned,
                validation_error_count=metrics.validation_error_count,
                memory_peak_mb=metrics.memory_peak_mb,
                duration_seconds=metrics.duration_seconds,
                ingestion_scope=config.ingestion_scope,
                is_test_data=config.test_data_enabled,
                destination_database=destination.database,
                destination_table=destination.table,
            )
            if updated:
                return

        self._sql_client.insert_audit_record(**update_payload)

    def best_effort_failure(
        self,
        config: "IngestionConfig",
        *,
        file_name: Optional[str],
        message: str,
        metrics: AuditMetrics,
        destination: AuditDestination,
        warning_context: str,
    ) -> None:
        """Insert a FAILED audit row and swallow audit-write failures.

        Used for pre-file validation failures where ingestion cannot proceed but
        audit logging itself must not mask the original validation error.
        """
        try:
            self.insert_state(
                config,
                file_name=file_name,
                status="FAILED",
                records_loaded=0,
                message=message,
                metrics=metrics,
                destination=destination,
            )
        except Exception:
            self._logger.warning(
                "Config id=%s could not create FAILED audit row for %s",
                config.id,
                warning_context,
                exc_info=True,
            )
