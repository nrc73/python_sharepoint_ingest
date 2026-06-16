"""Date/datetime parsing and detection utilities for ingestion normalisation.

Extracted from ``sharepoint_ingest.ingestion_engine`` so the module stays
navigable by ordinary LLMs.  All functions are stateless — no reference to
``IngestionEngine`` or its instance state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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

_DAY_MONTH_YEAR_RE = re.compile(r"^\s*(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})(.*)\s*$")
_YEAR_MONTH_DAY_RE = re.compile(r"^\s*(\d{4})([/.\-])(\d{1,2})\2(\d{1,2})(.*)\s*$")
_TIME_SUFFIX_RE = re.compile(
    r"^\s*(?:T\s*)?(?:(\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?(?:\s*[AP]M)?))?\s*$",
    re.IGNORECASE,
)

_DATE_LIKE_TEXT_RE = re.compile(
    r"^\s*(?:\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})"
    r"(?:\s+|T)?(?:\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?(?:\s*[AP]M)?)?\s*$",
    re.IGNORECASE,
)


@dataclass
class CsvDateOrderEvidence:
    """Streaming evidence for digit-only CSV date columns.

    ``dmy_hints`` are values where the first component is impossible as a month
    (e.g. ``15/04/2026``).  ``mdy_hints`` are values where the second component
    is impossible as a month (e.g. ``4/15/2026 0:00``).  Ambiguous values such as
    ``04/05/2026`` are resolved only when the full-column evidence contains one
    unambiguous date order.
    """

    values_count: int = 0
    dmy_hints: int = 0
    mdy_hints: int = 0
    ambiguous_count: int = 0
    year_first_count: int = 0
    has_time: bool = False
    invalid_samples: list[str] = field(default_factory=list)

    def merge(self, other: "CsvDateOrderEvidence") -> "CsvDateOrderEvidence":
        self.values_count += other.values_count
        self.dmy_hints += other.dmy_hints
        self.mdy_hints += other.mdy_hints
        self.ambiguous_count += other.ambiguous_count
        self.year_first_count += other.year_first_count
        self.has_time = self.has_time or other.has_time
        if other.invalid_samples:
            remaining = max(0, 5 - len(self.invalid_samples))
            self.invalid_samples.extend(other.invalid_samples[:remaining])
        return self

    def resolve_dayfirst(self, *, column_name: str = "") -> bool | None:
        """Return ``True`` for AU/DMY, ``False`` for US/MDY, or ``None``.

        ``None`` means no day/month decision is required (for example ISO-only
        values).  Ambiguous-only CSV columns default to AU/DMY because this
        project standardises CSV dates to numeric AU output.  Conflicting
        unambiguous hints raise because silently mixing AU and US source
        semantics corrupts data.
        """
        if self.dmy_hints and self.mdy_hints:
            label = f" in column '{column_name}'" if column_name else ""
            raise ValueError(
                f"Conflicting CSV date formats{label}: found both dd-MM-yyyy/dd/MM/yyyy "
                "and MM-dd-yyyy/MM/dd/yyyy evidence. Split or cleanse the source file; "
                "the ingestion pipeline will not mix AU and US date orders."
            )
        if self.dmy_hints:
            return True
        if self.mdy_hints:
            return False
        if self.ambiguous_count:
            return True
        return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_date_like_text(text_value: str) -> bool:
    """Return *True* if *text_value* looks like a date/datetime string."""
    return bool(_DATE_LIKE_TEXT_RE.match(text_value.strip()))


def _normalise_time_suffix(suffix: str) -> str | None:
    match = _TIME_SUFFIX_RE.match(suffix or "")
    if not match:
        return None
    time_part = match.group(1)
    return f" {time_part.strip().upper()}" if time_part else ""


def _parse_ymd_digit_date(text_value: str) -> tuple[pd.Timestamp, bool] | None:
    match = _YEAR_MONTH_DAY_RE.match(text_value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(3))
    day = int(match.group(4))
    suffix = _normalise_time_suffix(match.group(5) or "")
    if suffix is None:
        return None
    parsed = pd.to_datetime(
        f"{year:04d}-{month:02d}-{day:02d}{suffix}", errors="coerce"
    )
    if pd.isna(parsed):
        return None
    ts = pd.Timestamp(parsed)
    return ts, bool(suffix) or ts.hour != 0 or ts.minute != 0 or ts.second != 0 or ts.microsecond != 0


def _parse_dmy_mdy_digit_date(text_value: str, *, dayfirst: bool) -> tuple[pd.Timestamp, bool] | None:
    match = _DAY_MONTH_YEAR_RE.match(text_value)
    if not match:
        return None
    first = int(match.group(1))
    second = int(match.group(3))
    year = int(match.group(4))
    suffix = _normalise_time_suffix(match.group(5) or "")
    if suffix is None:
        return None
    day = first if dayfirst else second
    month = second if dayfirst else first
    parsed = pd.to_datetime(
        f"{year:04d}-{month:02d}-{day:02d}{suffix}", errors="coerce"
    )
    if pd.isna(parsed):
        return None
    ts = pd.Timestamp(parsed)
    return ts, bool(suffix) or ts.hour != 0 or ts.minute != 0 or ts.second != 0 or ts.microsecond != 0


def collect_csv_date_order_evidence(values: pd.Series) -> CsvDateOrderEvidence:
    """Scan values and return full-column CSV date-order evidence.

    The function is deliberately strict about digit date shapes.  Free text such
    as ``"about 4/15/2026"`` is captured as invalid evidence rather than being
    accepted by pandas' permissive parser.
    """
    evidence = CsvDateOrderEvidence()
    for value in values.dropna():
        if isinstance(value, str):
            text_value = value.strip()
            if text_value == "":
                continue
        elif isinstance(value, (pd.Timestamp, datetime, date)):
            ts = pd.Timestamp(value)
            evidence.values_count += 1
            evidence.has_time = evidence.has_time or any(
                (ts.hour, ts.minute, ts.second, ts.microsecond)
            )
            continue
        else:
            text_value = str(value).strip()
            if text_value == "":
                continue

        evidence.values_count += 1

        ymd = _parse_ymd_digit_date(text_value)
        if ymd is not None:
            _ts, has_time = ymd
            evidence.year_first_count += 1
            evidence.has_time = evidence.has_time or has_time
            continue

        match = _DAY_MONTH_YEAR_RE.match(text_value)
        if not match:
            if len(evidence.invalid_samples) < 5:
                evidence.invalid_samples.append(text_value)
            continue

        first = int(match.group(1))
        second = int(match.group(3))
        suffix = _normalise_time_suffix(match.group(5) or "")
        if suffix is None:
            if len(evidence.invalid_samples) < 5:
                evidence.invalid_samples.append(text_value)
            continue

        if first > 12 and second > 12:
            if len(evidence.invalid_samples) < 5:
                evidence.invalid_samples.append(text_value)
            continue
        if first > 12:
            parsed = _parse_dmy_mdy_digit_date(text_value, dayfirst=True)
            if parsed is None:
                if len(evidence.invalid_samples) < 5:
                    evidence.invalid_samples.append(text_value)
                continue
            _ts, has_time = parsed
            evidence.dmy_hints += 1
            evidence.has_time = evidence.has_time or has_time
            continue
        if second > 12:
            parsed = _parse_dmy_mdy_digit_date(text_value, dayfirst=False)
            if parsed is None:
                if len(evidence.invalid_samples) < 5:
                    evidence.invalid_samples.append(text_value)
                continue
            _ts, has_time = parsed
            evidence.mdy_hints += 1
            evidence.has_time = evidence.has_time or has_time
            continue

        evidence.ambiguous_count += 1
        evidence.has_time = evidence.has_time or bool(suffix)

    return evidence


def format_datetime_series_as_au_text(series: pd.Series, *, include_time: bool = False) -> pd.Series:
    """Return date/datetime values as numeric AU text using ``dd-MM-yyyy``.

    This helper intentionally uses a numeric month to avoid mixed outputs such as
    ``dd/MM/yyyy`` in one place and ``dd-MMM-yy`` elsewhere.
    """
    parsed = pd.to_datetime(series, errors="coerce")
    fmt = "%d-%m-%Y %H:%M:%S" if include_time else "%d-%m-%Y"
    return parsed.dt.strftime(fmt).where(parsed.notna(), None)


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
    date_order_hint: bool | None = None,
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

    if source_kind == "csv" and date_order_hint is None:
        evidence = collect_csv_date_order_evidence(series)
        date_order_hint = evidence.resolve_dayfirst(column_name=column_name)

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
            ymd = _parse_ymd_digit_date(text_value)
            if ymd is not None:
                parsed, _has_time = ymd
                out.at[idx] = parsed
                continue

            # Digit dates may be ambiguous (dd/MM or MM/dd, also dd-MM/MM-dd).
            digit_date_match = _DAY_MONTH_YEAR_RE.match(text_value)
            if digit_date_match:
                a = int(digit_date_match.group(1))
                b = int(digit_date_match.group(3))

                if date_order_hint is not None:
                    parsed_tuple = _parse_dmy_mdy_digit_date(
                        text_value, dayfirst=date_order_hint
                    )
                    if parsed_tuple is None:
                        raise ValueError(
                            f"Invalid date value '{text_value}' in column '{column_name}'."
                        )
                    parsed, _has_time = parsed_tuple
                    out.at[idx] = parsed
                    continue

                if a > 12 and b <= 12:
                    parsed_tuple = _parse_dmy_mdy_digit_date(
                        text_value, dayfirst=True
                    )
                    if parsed_tuple is None:
                        raise ValueError(
                            f"Invalid date value '{text_value}' in column '{column_name}'."
                        )
                    parsed, _has_time = parsed_tuple
                    out.at[idx] = parsed
                    dmy_hints += 1
                    continue

                if b > 12 and a <= 12:
                    parsed_tuple = _parse_dmy_mdy_digit_date(
                        text_value, dayfirst=False
                    )
                    if parsed_tuple is None:
                        raise ValueError(
                            f"Invalid date value '{text_value}' in column '{column_name}'."
                        )
                    parsed, _has_time = parsed_tuple
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
            parsed_tuple = _parse_dmy_mdy_digit_date(
                str(series.at[idx]).strip(), dayfirst=dayfirst
            )
            if parsed_tuple is None:
                raise ValueError(
                    f"Invalid date value '{series.at[idx]}' in column '{column_name}'."
                )
            parsed, _has_time = parsed_tuple
            out.at[idx] = parsed

    return out
