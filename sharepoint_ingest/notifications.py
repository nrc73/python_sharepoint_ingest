"""Email notification helpers for validation and ingestion failures."""

from __future__ import annotations

from collections import Counter
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Optional

from sharepoint_ingest.config import EmailSettings


logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, settings: EmailSettings):
        self._settings = settings

    @staticmethod
    def _normalize_recipients(recipients: Optional[str | Iterable[str]]) -> list[str]:
        if recipients is None:
            return []

        if isinstance(recipients, str):
            raw_items = [recipients]
        else:
            raw_items = list(recipients)

        resolved: list[str] = []
        for item in raw_items:
            if item is None:
                continue
            for part in str(item).replace(";", ",").split(","):
                value = part.strip()
                if value:
                    resolved.append(value)
        return resolved

    def send(
        self,
        to_address: Optional[str | Iterable[str]],
        subject: str,
        body: str,
        cc_addresses: Optional[str | Iterable[str]] = None,
    ) -> bool:
        to_recipients = self._normalize_recipients(to_address)
        cc_recipients = self._normalize_recipients(cc_addresses)

        if not self._settings.enabled:
            return False
        if not to_recipients:
            return False
        if not self._settings.host:
            return False

        msg = MIMEMultipart()
        msg["From"] = self._settings.from_address
        msg["To"] = ", ".join(to_recipients)
        if cc_recipients:
            msg["Cc"] = ", ".join(cc_recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        all_recipients = [*to_recipients, *cc_recipients]

        try:
            with smtplib.SMTP(self._settings.host, self._settings.port) as smtp:
                if self._settings.use_tls:
                    smtp.starttls()
                if self._settings.username and self._settings.password:
                    smtp.login(self._settings.username, self._settings.password)
                smtp.send_message(msg, to_addrs=all_recipients)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning(
                "Email notification could not be sent via %s:%s: %s",
                self._settings.host,
                self._settings.port,
                exc,
            )
            return False
        return True


def build_validation_email_body(
    process_name: str,
    issues: Iterable[str],
    *,
    file_name: Optional[str] = None,
    sheet_name: Optional[str] = None,
    max_issue_lines: int = 15,
) -> str:
    issue_list = list(issues)
    total_issues = len(issue_list)
    severity_counts = Counter()
    code_counts = Counter()

    for issue in issue_list:
        if issue.startswith("[") and "]" in issue:
            severity = issue.split("]", 1)[0].strip("[]")
            if severity:
                severity_counts[severity] += 1

        if ":" in issue:
            left = issue.split(":", 1)[0]
            if "]" in left:
                code = left.rsplit("]", 1)[-1].strip()
                if code:
                    code_counts[code] += 1

    lines = [f"Ingestion validation issues for process: {process_name}"]

    if file_name:
        lines.append(f"File: {file_name}")
    if sheet_name:
        lines.append(f"Sheet: {sheet_name}")

    lines.extend(
        [
            "",
            "Summary:",
            f"- Total issues: {total_issues}",
            f"- Error issues: {severity_counts.get('ERROR', 0)}",
            f"- Warning issues: {severity_counts.get('WARNING', 0)}",
        ]
    )

    if code_counts:
        lines.append("- Issue groups:")
        for code, count in code_counts.most_common(10):
            lines.append(f"  - {code}: {count}")

    lines.append("")
    lines.append("Top issues:")
    for issue in issue_list[:max_issue_lines]:
        lines.append(f"- {issue}")

    if total_issues > max_issue_lines:
        lines.append(f"- ... {total_issues - max_issue_lines} more issue(s) not shown")

    return "\n".join(lines)


def build_pk_violation_email_body(
    process_name: str,
    error_message: str,
    *,
    file_name: Optional[str] = None,
    table_name: Optional[str] = None,
    key_columns: Optional[list[str]] = None,
    duplicate_count: Optional[int] = None,
    sample_values: Optional[list[dict]] = None,
    rows_scanned: Optional[int] = None,
    memory_peak_mb: Optional[float] = None,
    duration_seconds: Optional[float] = None,
) -> str:
    """Build a plain-text email body for a PRIMARY_KEY_VIOLATION failure.

    Provides targeted remediation guidance alongside the standard resource
    telemetry so operators can quickly determine whether this is a reload
    scenario (file already loaded), an intra-file duplicate issue, or a
    parallel-write contention problem.
    """
    lines = [
        f"PRIMARY KEY VIOLATION — ingestion failure for process: {process_name}",
    ]
    if file_name:
        lines.append(f"File        : {file_name}")
    if table_name:
        lines.append(f"Table       : {table_name}")
    if key_columns:
        lines.append(f"Key columns : {', '.join(key_columns)}")

    lines.append("")
    lines.append("Error:")
    lines.append(error_message)

    if duplicate_count is not None or sample_values:
        lines.append("")
        lines.append("Duplicate key detail:")
        if duplicate_count is not None:
            lines.append(f"  Rows with duplicate key values : {duplicate_count}")
        if sample_values:
            lines.append("  Sample duplicate values:")
            for sv in sample_values[:5]:
                lines.append(f"    {sv}")

    lines.append("")
    lines.append("Remediation options:")
    lines.append(
        "  1. FULL RELOAD   — change load_strategy to TRUNCATE (or TRUNCATE_RELOAD) in "
        "config.sharepoint_ingestion. The table will be cleared before each load."
    )
    lines.append(
        "  2. MANUAL CLEAN  — delete the already-loaded rows from the target table and "
        "restore the file to the SharePoint process folder to trigger a re-run."
    )

    lines.append("")
    lines.append("Resource telemetry:")
    lines.append(f"  Rows scanned before failure : {rows_scanned if rows_scanned is not None else 'n/a'}")
    lines.append(f"  Peak memory (process)       : {f'{memory_peak_mb:.1f} MB' if memory_peak_mb is not None else 'n/a'}")
    lines.append(f"  Elapsed time                : {f'{duration_seconds:.1f}s' if duration_seconds is not None else 'n/a'}")

    lines.append("")
    lines.append(
        "NOTE: If rows_scanned > 0 and the violation was raised by the database (not the "
        "pre-flight check), earlier chunks may have already been committed to the target "
        "table. Inspect the table before reprocessing."
    )

    return "\n".join(lines)


def build_failure_email_body(
    process_name: str,
    error_message: str,
    *,
    file_name: Optional[str] = None,
    rows_scanned: Optional[int] = None,
    memory_peak_mb: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    host_hint: Optional[str] = None,
) -> str:
    """Build a plain-text failure notification body that includes resource telemetry.

    The resource fields are optional so the function remains backward compatible;
    callers can omit them when the metrics are not yet available (e.g. early-phase
    failures before any rows are read).

    Including resource telemetry is especially useful in production environments
    where multiple ingestion processes may run in parallel.  Operators can triage
    whether a failure was caused by:
        - a data/schema problem (low rows_scanned, early error)
        - a resource pressure problem (high memory_peak_mb, long duration, OOM)
        - a SQL blocking or contention problem (long duration, low rows_scanned)
    """
    lines = [
        f"Ingestion failure for process: {process_name}",
    ]
    if file_name:
        lines.append(f"File: {file_name}")

    lines.append("")
    lines.append("Error:")
    lines.append(error_message)

    lines.append("")
    lines.append("Resource telemetry:")
    lines.append(f"  Rows scanned before failure : {rows_scanned if rows_scanned is not None else 'n/a'}")
    lines.append(f"  Peak memory (process)       : {f'{memory_peak_mb:.1f} MB' if memory_peak_mb is not None else 'n/a'}")
    lines.append(f"  Elapsed time                : {f'{duration_seconds:.1f}s' if duration_seconds is not None else 'n/a'}")
    if host_hint:
        lines.append(f"  Host / runner               : {host_hint}")

    lines.append("")
    lines.append(
        "NOTE: If multiple ingestion workflows run in parallel, check "
        "log.sharepoint_ingestion_audit and sys.dm_exec_requests on the SQL Server "
        "for blocking sessions, high log usage, or contention on the destination table "
        "before reprocessing this file."
    )

    return "\n".join(lines)
