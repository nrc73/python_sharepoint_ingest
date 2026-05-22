"""Notification orchestration helpers for IngestionEngine.

Extracted from ``IngestionEngine`` so the notification-building logic is
isolated, independently testable, and easy to extend without touching the
main orchestration file.

Public API
----------
notify_failure              — generic ingestion failure email
notify_pk_violation         — targeted PRIMARY KEY VIOLATION email
publish_and_notify_issues   — log + email every validation issue
"""
from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig, ValidationIssue

from sharepoint_ingest.ingestion._notification_helpers import (
    extract_sheet_name_from_issues,
    format_issue,
)
from sharepoint_ingest.notifications import (
    build_failure_email_body,
    build_pk_violation_email_body,
    build_validation_email_body,
)


def notify_failure(
    notifier,
    config: "IngestionConfig",
    env_name: str,
    error_message: str,
    *,
    file_name: Optional[str] = None,
    rows_scanned: Optional[int] = None,
    memory_peak_mb: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    logger: logging.Logger,
) -> None:
    """Build and send a generic ingestion-failure notification email."""
    subject = f"SharePoint ingestion failure - config {config.id}"
    process_name = (
        f"config_id={config.id}, workflow_id={config.workflow_id}, "
        f"process_id={config.process_id}, env={env_name}"
    )
    body = build_failure_email_body(
        process_name=process_name,
        error_message=error_message,
        file_name=file_name,
        rows_scanned=rows_scanned,
        memory_peak_mb=memory_peak_mb,
        duration_seconds=duration_seconds,
    )
    sent = notifier.send(config.error_notification_email_address, subject, body)
    if sent:
        logger.info("Failure notification sent to %s", config.error_notification_email_address)


def notify_pk_violation(
    notifier,
    config: "IngestionConfig",
    env_name: str,
    error_message: str,
    *,
    file_name: Optional[str] = None,
    rows_scanned: Optional[int] = None,
    memory_peak_mb: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    logger: logging.Logger,
) -> None:
    """Build and send a targeted PRIMARY KEY VIOLATION notification email.

    Parses the duplicate-count and sample key values out of *error_message*
    so the email body contains actionable remediation context.
    """
    subject = f"SharePoint ingestion PRIMARY KEY VIOLATION - config {config.id}"
    process_name = (
        f"config_id={config.id}, workflow_id={config.workflow_id}, "
        f"process_id={config.process_id}, env={env_name}"
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
    sent = notifier.send(config.error_notification_email_address, subject, body)
    if sent:
        logger.info(
            "PK violation notification sent to %s",
            config.error_notification_email_address,
        )


def publish_and_notify_issues(
    config: "IngestionConfig",
    issues: "list[ValidationIssue]",
    notifier,
    logger: logging.Logger,
) -> None:
    """Log every validation issue and send a validation notification email."""
    issue_strings = [format_issue(i) for i in issues]
    for issue_str in issue_strings:
        logger.warning("Config id=%s validation: %s", config.id, issue_str)
    _send_validation_notification(notifier, config, issue_strings, issues=issues, logger=logger)


def _send_validation_notification(
    notifier,
    config: "IngestionConfig",
    issue_messages: list[str],
    *,
    issues: "Optional[list[ValidationIssue]]" = None,
    logger: logging.Logger,
) -> None:
    """Assemble and dispatch the validation-warning email."""
    subject = f"SharePoint ingestion validation warning - config {config.id}"
    source_file_name: Optional[str] = None
    sheet_name: Optional[str] = None

    if issues:
        sheet_name = extract_sheet_name_from_issues(issues)

    issue_blob = "\n".join(issue_messages)
    file_match = re.search(
        r"source_file_name\s*=\s*([^,;\n]+)", issue_blob, re.IGNORECASE
    )
    if file_match:
        source_file_name = file_match.group(1).strip().strip("\"'")

    body = build_validation_email_body(
        process_name=f"config_id={config.id}, workflow_id={config.workflow_id}",
        issues=issue_messages,
        file_name=source_file_name,
        sheet_name=sheet_name,
        max_issue_lines=15,
    )
    sent = notifier.send(config.error_notification_email_address, subject, body)
    if sent:
        logger.info(
            "Validation notification sent to %s",
            config.error_notification_email_address,
        )
