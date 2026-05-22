"""Shared helpers for ingestion validation/error notification formatting.

Extracted from ``sharepoint_ingest.ingestion_engine`` to keep the engine
module focused on orchestration concerns.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sharepoint_ingest.models import ValidationIssue


def format_issue(issue: ValidationIssue) -> str:
    """Return a single-line human-readable string for a *ValidationIssue*."""
    detail = f" ({issue.details})" if issue.details else ""
    return f"[{issue.severity}] {issue.code}: {issue.message}{detail}"


def extract_sheet_name_from_issues(
    issues: list[ValidationIssue],
) -> Optional[str]:
    """Best-effort extraction of the Excel sheet name from issue details.

    Returns a single sheet name, a ``"multiple (…)"`` summary, or
    ``None`` when no sheet name is found.
    """
    _SHEET_RE = re.compile(r"excel_tab_name\s*=\s*([^,;]+)", re.IGNORECASE)
    sheet_names: list[str] = []

    for issue in issues:
        details = str(issue.details or "")
        match = _SHEET_RE.search(details)
        if match:
            candidate = match.group(1).strip().strip("\"'")
            if candidate:
                sheet_names.append(candidate)

    if not sheet_names:
        return None

    unique_names = sorted(set(sheet_names))
    if len(unique_names) == 1:
        return unique_names[0]
    suffix = "..." if len(unique_names) > 3 else ""
    return f"multiple ({', '.join(unique_names[:3])}{suffix})"
