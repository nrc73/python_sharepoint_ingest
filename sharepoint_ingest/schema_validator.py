"""Schema comparison and data-profile validation utilities."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd

from sharepoint_ingest.models import ValidationIssue


MANAGED_DESTINATION_COLUMNS = {
    "sp_ingest_load_dt",
    "audit_id",
    "__$batch_id",
    "__$job_instance_id",
}


def _normalize(name: str) -> str:
    return name.strip().lower()


def _column_type_family(dtype: str) -> str:
    dt = dtype.lower()
    if dt in {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "smallmoney"}:
        return "numeric"
    if dt in {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}:
        return "datetime"
    if dt in {"bit"}:
        return "bool"
    if dt in {"char", "nchar", "varchar", "nvarchar", "text", "ntext", "uniqueidentifier"}:
        return "string"
    if dt in {"binary", "varbinary", "image"}:
        return "binary"
    return "other"


def _pandas_type_family(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "numeric"
    if pd.api.types.is_float_dtype(series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_bool_dtype(series):
        return "bool"
    return "string"


def _safe_max_string_length(series: pd.Series) -> int:
    non_null = series.dropna()
    if non_null.empty:
        return 0
    return non_null.astype(str).str.len().max()


def _numeric_digit_profile(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None

    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None

    # normalized tuple: sign, digits, exponent
    sign, digits, exponent = dec.as_tuple()
    total_digits = len(digits)
    if exponent >= 0:
        # e.g. 12300, exp=2 => fraction=0
        fraction_digits = 0
        total_digits = total_digits + exponent
    else:
        fraction_digits = -exponent
        # keep at least one integer digit for values like 0.01
        if total_digits < fraction_digits:
            total_digits = fraction_digits

    if total_digits == 0:
        total_digits = 1

    return total_digits, fraction_digits


def validate_source_against_destination(
    source_df: pd.DataFrame,
    destination_columns: list[dict],
    null_alert_threshold: float = 0.90,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    source_columns = list(source_df.columns)
    source_map = {
        _normalize(c): c
        for c in source_columns
        if _normalize(c) not in MANAGED_DESTINATION_COLUMNS
    }

    dest_map: dict[str, dict] = {}
    dest_order: list[str] = []
    for col in destination_columns:
        col_name = str(col["column_name"])
        key = _normalize(col_name)
        if key in MANAGED_DESTINATION_COLUMNS:
            continue
        dest_map[key] = col
        dest_order.append(key)

    source_keys = list(source_map.keys())

    missing_in_source = [dest_map[k]["column_name"] for k in dest_order if k not in source_map]
    additional_in_source = [source_map[k] for k in source_keys if k not in dest_map]

    if missing_in_source:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="MISSING_DEST_COLUMNS_IN_SOURCE",
                message="Source file is missing expected destination columns.",
                details=", ".join(missing_in_source),
            )
        )

    if additional_in_source:
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="ADDITIONAL_SOURCE_COLUMNS",
                message="Source file has additional columns not found in destination.",
                details=", ".join(additional_in_source),
            )
        )

    shared_keys = [k for k in source_keys if k in dest_map]

    shared_dest_order = [k for k in dest_order if k in source_map]
    if shared_keys != shared_dest_order:
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="COLUMN_REORDERING_DETECTED",
                message="Column ordering differs between source and destination metadata.",
                details=f"source_order={shared_keys}; destination_order={shared_dest_order}",
            )
        )

    for key in shared_keys:
        source_name = source_map[key]
        source_series = source_df[source_name]
        source_family = _pandas_type_family(source_series)

        dest_meta = dest_map[key]
        dest_type = str(dest_meta.get("data_type") or "")
        dest_family = _column_type_family(dest_type)

        if dest_family in {"numeric", "datetime", "bool"} and source_family != dest_family:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="TYPE_MISMATCH",
                    message=f"Type mismatch for column '{source_name}'.",
                    details=f"source={source_family}, destination={dest_family} ({dest_type})",
                )
            )

        dest_len = dest_meta.get("character_maximum_length")
        if source_family == "string" and dest_len not in (None, -1):
            max_len = _safe_max_string_length(source_series)
            if max_len > int(dest_len):
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="STRING_LENGTH_EXCEEDED",
                        message=f"Potential string truncation in column '{source_name}'.",
                        details=f"source_max_len={max_len}, destination_max_len={dest_len}",
                    )
                )

        if source_family == "numeric" and dest_family == "numeric":
            # SQL Server reports approximate numeric FLOAT/REAL metadata with
            # numeric_scale=0 (and FLOAT precision commonly 53).  That scale is
            # storage metadata, not a constraint that fractional source values
            # must have zero decimal places.  Keep precision/scale enforcement
            # for exact numeric types only.
            if dest_type.lower() in {"float", "real"}:
                continue
            precision = dest_meta.get("numeric_precision")
            scale = dest_meta.get("numeric_scale")
            if precision is not None:
                try:
                    max_precision = int(precision)
                except (TypeError, ValueError):
                    max_precision = None
                try:
                    max_scale = int(scale) if scale is not None else 0
                except (TypeError, ValueError):
                    max_scale = 0

                if max_precision is not None and max_precision > 0:
                    observed_max_precision = 0
                    observed_max_scale = 0
                    for raw in source_series.dropna():
                        profile = _numeric_digit_profile(raw)
                        if profile is None:
                            continue
                        total_digits, fraction_digits = profile
                        observed_max_precision = max(observed_max_precision, total_digits)
                        observed_max_scale = max(observed_max_scale, fraction_digits)

                    if observed_max_precision > max_precision:
                        issues.append(
                            ValidationIssue(
                                severity="ERROR",
                                code="NUMERIC_PRECISION_EXCEEDED",
                                message=f"Numeric precision exceeded for column '{source_name}'.",
                                details=(
                                    f"source_max_precision={observed_max_precision}, "
                                    f"destination_precision={max_precision}, "
                                    f"destination_scale={max_scale}"
                                ),
                            )
                        )

                    if observed_max_scale > max_scale:
                        issues.append(
                            ValidationIssue(
                                severity="ERROR",
                                code="NUMERIC_SCALE_EXCEEDED",
                                message=f"Numeric scale exceeded for column '{source_name}'.",
                                details=(
                                    f"source_max_scale={observed_max_scale}, "
                                    f"destination_scale={max_scale}, "
                                    f"destination_precision={max_precision}"
                                ),
                            )
                        )

        null_ratio = float(source_series.isna().mean()) if len(source_series) else 0.0
        if null_ratio >= null_alert_threshold:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="HIGH_NULL_RATIO",
                    message=f"High null ratio detected for column '{source_name}'.",
                    details=f"null_ratio={null_ratio:.2%}, threshold={null_alert_threshold:.2%}",
                )
            )

    return issues
