"""Date/datetime parsing and detection utilities for ingestion normalisation.

Extracted from ``sharepoint_ingest.ingestion_engine`` so the module stays
navigable by ordinary LLMs.  All functions are stateless — no reference to
``IngestionEngine`` or its instance state.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd

from sharepoint_ingest.schema_validator import MANAGED_DESTINATION_COLUMNS

if TYPE_CHECKING:
    from sharepoint_ingest.models import ValidationIssue

# ---------------------------------------------------------------------------
# Module-level constants (formerly IngestionEngine class attributes)
# ---------------------------------------------------------------------------

_DATE_TYPE_NAMES: frozenset[str] = frozenset(
    {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
)

_SLASH_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(.*)$")

_DATE_LIKE_TEXT_RE = re.compile(
    r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$"
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_date_like_text(text_value: str) -> bool:
    """Return *True* if *text_value* looks like a date/datetime string."""
    return bool(_DATE_LIKE_TEXT_RE.match(text_value.strip()))


def destination_datetime_columns(destination_columns: list[dict]) -> set[str]:
    """Return the lowercase column names from *destination_columns* whose
    SQL type is a date/datetime variant, excluding framework-managed columns.
    """
    result: set[str] = set()
    for col in destination_columns:
        column_name = str(col.get("column_name") or "").strip().lower()
        if column_name in MANAGED_DESTINATION_COLUMNS:
            continue
        data_type = str(col.get("data_type") or "").strip().lower()
        if data_type in _DATE_TYPE_NAMES:
            result.add(column_name)
    return result


def detect_excel_datetime_text_issues(
    dataframe: pd.DataFrame,
    destination_columns: list[dict],
) -> list[ValidationIssue]:
    """Scan *dataframe* for date-like text values in destination datetime columns.

    Returns a list of ``WARNING`` :class:`~sharepoint_ingest.models.ValidationIssue`
    objects — one per affected column.
    """
    from sharepoint_ingest.models import ValidationIssue  # avoid circular at import time

    issues: list[ValidationIssue] = []
    dt_columns = destination_datetime_columns(destination_columns)
    if not dt_columns:
        return issues

    for source_col in dataframe.columns:
        if source_col.strip().lower() not in dt_columns:
            continue

        text_date_values: list[str] = []
        for value in dataframe[source_col]:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if isinstance(value, (pd.Timestamp, datetime, date)):
                continue
            if isinstance(value, str):
                candidate = value.strip()
                if candidate and is_date_like_text(candidate):
                    text_date_values.append(candidate)

        if not text_date_values:
            continue

        samples = ", ".join(text_date_values[:5])
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="EXCEL_DATETIME_STORED_AS_TEXT",
                message=(
                    f"Date/datetime column '{source_col}' contains "
                    "date-like values stored as text in Excel."
                ),
                details=f"count={len(text_date_values)}, samples={samples}",
            )
        )

    return issues


def convert_series_to_datetime(
    series: pd.Series,
    source_kind: str,
    column_name: str,
) -> pd.Series:
    """Convert *series* to ``datetime64[ns]``, handling ISO 8601, Excel serial
    numbers, and ambiguous slash-separated dates (dd/MM vs MM/dd).

    Raises ``ValueError`` for invalid or unresolvably ambiguous values.
    """
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

        # Excel stores dates as floating-point serial numbers
        if source_kind == "excel" and isinstance(value, (int, float)):
            converted = pd.to_datetime(value, unit="D", origin="1899-12-30", errors="coerce")
            if not pd.isna(converted):
                out.at[idx] = converted
                continue

        text_value = str(value).strip()

        # Try ISO 8601 variants first (unambiguous)
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            ts = pd.to_datetime(text_value, format=fmt, errors="coerce")
            if not pd.isna(ts):
                out.at[idx] = ts
                break
        else:
            # Slash-separated dates may be ambiguous (dd/MM or MM/dd)
            slash_match = _SLASH_DATE_RE.match(text_value)
            if slash_match:
                a = int(slash_match.group(1))
                b = int(slash_match.group(2))
                suffix = slash_match.group(4) or ""
                year_part = slash_match.group(3)

                if a > 12 and b <= 12:
                    parsed = pd.to_datetime(
                        f"{a:02d}/{b:02d}/{year_part}{suffix}",
                        dayfirst=True, errors="coerce",
                    )
                    if pd.isna(parsed):
                        raise ValueError(
                            f"Invalid date value '{text_value}' in column '{column_name}'."
                        )
                    out.at[idx] = parsed
                    dmy_hints += 1
                    continue

                if b > 12 and a <= 12:
                    parsed = pd.to_datetime(
                        f"{a:02d}/{b:02d}/{year_part}{suffix}",
                        dayfirst=False, errors="coerce",
                    )
                    if pd.isna(parsed):
                        raise ValueError(
                            f"Invalid date value '{text_value}' in column '{column_name}'."
                        )
                    out.at[idx] = parsed
                    mdy_hints += 1
                    continue

                if a > 12 and b > 12:
                    raise ValueError(
                        f"Invalid date value '{text_value}' in column '{column_name}'."
                    )

                ambiguous_positions.append(idx)
                continue

            fallback = pd.to_datetime(text_value, errors="coerce")
            if pd.isna(fallback):
                raise ValueError(
                    f"Unable to parse date value '{text_value}' in column '{column_name}'."
                )
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
            parsed = pd.to_datetime(
                str(series.at[idx]).strip(), dayfirst=dayfirst, errors="coerce"
            )
            if pd.isna(parsed):
                raise ValueError(
                    f"Invalid date value '{series.at[idx]}' in column '{column_name}'."
                )
            out.at[idx] = parsed

    return out
