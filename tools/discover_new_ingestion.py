"""discover_new_ingestion.py
==========================
Read-only discovery tool that:

1. Queries [config].[sharepoint_ingestion] to get all configured process folders.
2. Lists SharePoint sub-folders (metadata-only) and finds any not already configured
   that contain >= 1 file.
3. Downloads and profiles every file in each qualifying folder, scanning ALL rows to
   infer accurate SQL Server data types (+20% length padding on VARCHAR/NVARCHAR).
4. Multi-sheet Excel workbooks land in ONE destination table with excel_tab_name added.
   CSV / Parquet multi-file ingestions are merged into one table as well.
5. Performs data-driven composite PK inference by testing uniqueness of candidate
   column combinations across the full dataset.
6. Prints ready-to-run T-SQL:
   - CREATE TABLE [schema].[<folder>] with system columns matching existing
     conventions: source_file_name, sp_ingest_load_dt, audit_id, __$batch_id,
     __$job_instance_id, and excel_tab_name (for Excel ingestions).
   - INSERT INTO [config].[sharepoint_ingestion] ...

Usage (DEV only)
----------------
    python tools/discover_new_ingestion.py [--env dev]
                                           [--base-folder PATH]
                                           [--dest-schema sharepoint]
                                           [--no-profile]
                                           [--padding 0.20]
                                           [--csv-mapping-rows]
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime
import fnmatch
import io
import itertools
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.keyvault_client import maybe_build_provider
from sharepoint_ingest.main import _resolve_database_names, _resolve_sql_settings
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SYSTEM_COLUMNS_EXCEL = [
    ("excel_tab_name",          "VARCHAR(100)",  "NOT NULL"),
    ("source_file_name",        "VARCHAR(255)",  "NULL"),
    ("sp_ingest_load_dt",       "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
    ("audit_id",                "BIGINT",        "NULL"),
    ("__$batch_id",             "INT",           "NULL"),
    ("__$job_instance_id",      "INT",           "NULL"),
]
_SYSTEM_COLUMNS_PLAIN = [
    ("source_file_name",        "VARCHAR(255)",  "NULL"),
    ("sp_ingest_load_dt",       "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
    ("audit_id",                "BIGINT",        "NULL"),
    ("__$batch_id",             "INT",           "NULL"),
    ("__$job_instance_id",      "INT",           "NULL"),
]
_SKIP_FOLDER_NAMES = {"processed", "failed", "archive", "_archive", "_processed", "_failed"}
_DEFAULT_NOTIFICATION_TO = "NathanChapman@company715.onmicrosoft.com"
_DEFAULT_DEST_SCHEMA = "sharepoint"

# Column name patterns that indicate a good PK candidate (id/no/guid suffix or prefix)
_PK_NAME_RE = re.compile(
    r"(^id$|_id$|^id_|_id_|^no$|_no$|^no_|_guid$|^guid$|^guid_|_guid_)",
    re.IGNORECASE,
)
# Numeric SQL types eligible as PK (integers only – floats are too imprecise)
_PK_NUMERIC_TYPES = {"INT", "BIGINT"}

# Code-like VARCHAR heuristic thresholds
_CODE_MAX_LEN = 50           # max observed character length – codes are short
_CODE_CV_THRESHOLD = 0.40    # coefficient of variation (std/mean) – codes are consistent length
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][-_/A-Za-z0-9]*$")  # no spaces, structured chars


@dataclass
class _ProfileCandidate:
    """One profiled file candidate (potentially with multiple sheets)."""

    file_name: str
    server_relative_url: str
    file_kind: str  # csv|parquet|excel
    normalized_business_columns: tuple[str, ...]
    combined_profile: dict[str, str]
    full_frames: list[pd.DataFrame] = field(default_factory=list)
    any_excel: bool = False


@dataclass
class _DiscoveryGroup:
    """A generated ingestion recommendation unit."""

    group_key: str
    file_names: list[str]
    files: list[_ProfileCandidate]
    combined_profile: dict[str, str]
    full_frames: list[pd.DataFrame]
    any_excel: bool
    multi_file_ingest: int
    file_name_pattern: str


def _is_code_like_varchar(series: pd.Series) -> bool:
    """Return True if a VARCHAR series looks like structured codes (short, consistent
    length, alphanumeric/separator pattern) rather than free-form prose.

    Rules (all must pass):
    1. All values are non-null (we only test candidate series from full scans).
    2. Max length <= _CODE_MAX_LEN (codes are short).
    3. No whitespace-only or empty values.
    4. Coefficient of variation of value lengths < _CODE_CV_THRESHOLD
       (values are consistently sized, not wildly variable like sentences).
    5. A majority (>= 80%) of values match the structured-code regex pattern
       (starts with alphanum, contains only alphanum plus -, _, /).
    6. At least 70% of values contain BOTH a letter AND a digit (mixed alpha+numeric),
       which distinguishes codes like 'TXN-001' from plain words or plain numbers.
    """
    non_null = series.dropna().astype(str)
    if non_null.empty:
        return False

    # Rule 2: short values only
    lengths = non_null.str.len()
    if lengths.max() > _CODE_MAX_LEN:
        return False

    # Rule 3: no blank/whitespace values
    if non_null.str.strip().eq("").any():
        return False

    # Rule 4: consistent length (low coefficient of variation)
    mean_len = lengths.mean()
    if mean_len == 0:
        return False
    cv = lengths.std() / mean_len
    if cv > _CODE_CV_THRESHOLD:
        return False

    # Rule 5: majority match structured-code pattern (no spaces, no special chars)
    pattern_match_ratio = non_null.apply(lambda v: bool(_CODE_PATTERN.match(v))).mean()
    if pattern_match_ratio < 0.80:
        return False

    # Rule 6: at least 70% contain both a letter and a digit
    has_alpha = non_null.str.contains(r"[A-Za-z]", regex=True)
    has_digit = non_null.str.contains(r"[0-9]", regex=True)
    mixed_ratio = (has_alpha & has_digit).mean()
    if mixed_ratio < 0.70:
        return False

    return True

# ---------------------------------------------------------------------------
# Type-inference helpers
# ---------------------------------------------------------------------------
_BIT_VALUES = {"0", "1", "true", "false", "t", "f"}
_DECIMAL_MAX_SCALE = 5
_DECIMAL_DEFAULT_PRECISION = 18
_DECIMAL_MAX_PRECISION = 38
_CSV_DATE_TEXT_RE = re.compile(
    r"^\s*(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|"
    r"\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    r")"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?(?:\s*[AP]M)?)?\s*$",
    re.IGNORECASE,
)
_CSV_TIME_TEXT_RE = re.compile(
    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?(?:\s*[AP]M)?",
    re.IGNORECASE,
)
_TEXT_SEMANTIC_COLUMN_RE = re.compile(
    r"(^|_)(name|description|desc|text|label|comment)(_|$)",
    re.IGNORECASE,
)


def _decimal_digit_profile(value: object) -> tuple[int, int] | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None

    _sign, digits, exponent = dec.as_tuple()
    total_digits = len(digits)
    if exponent >= 0:
        fraction_digits = 0
        total_digits += exponent
    else:
        fraction_digits = -exponent
        if total_digits < fraction_digits:
            total_digits = fraction_digits
    return max(total_digits, 1), fraction_digits


def _decimal_type_for_values(values: pd.Series) -> str:
    observed_precision = 0
    observed_scale = 0
    for raw in values.dropna():
        profile = _decimal_digit_profile(raw)
        if profile is None:
            return "FLOAT"
        precision, scale = profile
        observed_precision = max(observed_precision, precision)
        observed_scale = max(observed_scale, scale)

    if observed_scale > _DECIMAL_MAX_SCALE or observed_precision > _DECIMAL_MAX_PRECISION:
        return "FLOAT"
    precision = max(_DECIMAL_DEFAULT_PRECISION, observed_precision, observed_scale + 1)
    precision = min(precision, _DECIMAL_MAX_PRECISION)
    return f"DECIMAL:{precision}:{observed_scale}"


def _infer_datetime_text_type(str_vals: pd.Series) -> str | None:
    """Infer DATE/DATETIME2 for consistently formatted CSV-style text values."""
    normalized = str_vals.astype(str).str.strip()
    normalized = normalized[normalized != ""]
    if normalized.empty:
        return None
    if not normalized.map(lambda v: bool(_CSV_DATE_TEXT_RE.match(v))).all():
        return None

    # Collapse repeated spaces so values like "4/1/2026  12:00:00 AM" parse reliably.
    parse_values = normalized.map(lambda v: re.sub(r"\s+", " ", v))
    parsed = parse_values.map(lambda v: pd.to_datetime(v, errors="coerce"))
    if parsed.isna().any():
        return None

    has_explicit_time = parse_values.map(lambda v: bool(_CSV_TIME_TEXT_RE.search(v))).any()
    has_non_midnight_time = any(
        ts.hour != 0 or ts.minute != 0 or ts.second != 0 or ts.microsecond != 0
        for ts in parsed
    )
    return "DATETIME2(3)" if has_explicit_time or has_non_midnight_time else "DATE"


def _parse_decimal_raw_type(t: str) -> tuple[int, int] | None:
    if t.startswith("DECIMAL:"):
        _prefix, precision, scale = t.split(":", 2)
        return int(precision), int(scale)
    match = re.match(r"^DECIMAL\((\d+)\s*,\s*(\d+)\)$", t, flags=re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _infer_series(series: pd.Series) -> str:
    """Return narrowest raw SQL type for a pandas Series.

    VARCHAR is returned as ``VARCHAR:<n>`` and DECIMAL as ``DECIMAL:<p>:<s>``
    until finalisation so profiles can be merged across files/sheets.
    """
    non_null = series.dropna()
    if non_null.empty:
        return "VARCHAR(255)"

    dtype = series.dtype

    if pd.api.types.is_bool_dtype(dtype):
        return "BIT"
    if pd.api.types.is_integer_dtype(dtype):
        return "INT" if non_null.abs().max() <= 2_147_483_647 else "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        # Preserve decimal semantics for float source columns.  Values such as
        # 1.0/2.0 must produce DECIMAL(..., 1), not INT, because the source file
        # explicitly contains decimal values.
        return _decimal_type_for_values(non_null)
    if pd.api.types.is_datetime64_any_dtype(dtype):
        has_time = any(t.hour or t.minute or t.second for t in non_null.dt.time)
        return "DATETIME2(3)" if has_time else "DATE"

    # Object / string
    str_vals = non_null.astype(str)
    # Name/description-style columns are business text even if a particular file
    # happens to contain only numeric-looking values (e.g. ``short_name = 1.0``).
    # Without this guard discovery can generate a numeric destination column that
    # is semantically wrong and fragile for subsequent files.
    if _TEXT_SEMANTIC_COLUMN_RE.search(str(series.name or "")):
        return f"VARCHAR:{int(str_vals.str.len().max())}"
    # Try datetime
    try:
        inferred_datetime = _infer_datetime_text_type(str_vals)
        if inferred_datetime:
            return inferred_datetime
    except Exception:
        pass
    # Try numeric
    try:
        nums = pd.to_numeric(str_vals, errors="coerce")
        if nums.notna().all():
            if str_vals.str.strip().str.contains(r"[\.eE]", regex=True).any():
                return _decimal_type_for_values(str_vals)
            if (nums == nums.round(0)).all():
                return "INT" if nums.abs().max() <= 2_147_483_647 else "BIGINT"
            return _decimal_type_for_values(str_vals)
    except Exception:
        pass
    # Bit-like
    if set(str_vals.str.lower().unique()) <= _BIT_VALUES:
        return "BIT"
    # Character
    return f"VARCHAR:{int(str_vals.str.len().max())}"


def _merge_types(a: str, b: str) -> str:
    if a == b:
        return a
    _NUM = {"BIT": 0, "INT": 1, "BIGINT": 2, "FLOAT": 3}
    _TMP = {"DATE": 0, "DATETIME2(3)": 1, "DATETIME2(7)": 2}

    def _varchar_len(t: str):
        if t.startswith("VARCHAR:"):
            return int(t.split(":")[1])
        if t.startswith("NVARCHAR(") or t.startswith("VARCHAR("):
            inner = t.split("(")[1].rstrip(")")
            return 1_000_000_000 if inner == "MAX" else int(inner)
        return None

    la, lb = _varchar_len(a), _varchar_len(b)
    if la is not None or lb is not None:
        _fallback = {
            "BIT": 1, "INT": 11, "BIGINT": 20, "FLOAT": 25,
            "DATE": 10, "DATETIME2(3)": 27, "DATETIME2(7)": 27,
        }
        da, db = _parse_decimal_raw_type(a), _parse_decimal_raw_type(b)
        if da is not None:
            _fallback[a] = da[0] + 2
        if db is not None:
            _fallback[b] = db[0] + 2
        wa = la if la is not None else _fallback.get(a, 255)
        wb = lb if lb is not None else _fallback.get(b, 255)
        return f"VARCHAR:{max(wa, wb)}"
    da, db = _parse_decimal_raw_type(a), _parse_decimal_raw_type(b)
    if da is not None or db is not None:
        if a == "FLOAT" or b == "FLOAT":
            return "FLOAT"
        int_digits = {"BIT": 1, "INT": 10, "BIGINT": 19}
        a_int = da[0] - da[1] if da is not None else int_digits.get(a)
        b_int = db[0] - db[1] if db is not None else int_digits.get(b)
        if a_int is None or b_int is None:
            return "VARCHAR:50"
        scale = max(da[1] if da is not None else 0, db[1] if db is not None else 0)
        if scale > _DECIMAL_MAX_SCALE:
            return "FLOAT"
        precision = max(_DECIMAL_DEFAULT_PRECISION, a_int, b_int, a_int + scale, b_int + scale)
        if precision > _DECIMAL_MAX_PRECISION:
            return "FLOAT"
        return f"DECIMAL:{precision}:{scale}"
    if a in _NUM and b in _NUM:
        return a if _NUM[a] >= _NUM[b] else b
    if a in _TMP and b in _TMP:
        return a if _TMP[a] >= _TMP[b] else b
    return f"VARCHAR:50"


def _finalize_type(raw: str, padding: float = 0.20) -> str:
    if raw.startswith("VARCHAR:"):
        raw_len = int(raw.split(":")[1])
        # Apply padding then round UP to the nearest 10
        padded_exact = max(10, raw_len * (1 + padding))
        padded = int(math.ceil(padded_exact / 10.0)) * 10
        return "VARCHAR(MAX)" if padded > 8000 else f"VARCHAR({padded})"
    decimal_type = _parse_decimal_raw_type(raw)
    if decimal_type is not None:
        precision, scale = decimal_type
        return f"DECIMAL({precision},{scale})"
    return raw


# ---------------------------------------------------------------------------
# Profiling: read file bytes → {column: raw_type}
# ---------------------------------------------------------------------------

def _profile_df(df: pd.DataFrame) -> dict[str, str]:
    return {str(col): _infer_series(df[col]) for col in df.columns}


def _merge_profiles(base: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    for col, t in new.items():
        merged[col] = _merge_types(merged[col], t) if col in merged else t
    return merged


def _http_status_from_exception(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(401|403|404|429|5\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


def _http_response_snippet(exc: Exception, *, max_chars: int = 300) -> str:
    response = getattr(exc, "response", None)
    text = str(getattr(response, "text", "") or "").strip()
    reason = str(getattr(response, "reason", "") or "").strip()
    snippet = text or reason
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "…"
    return snippet


def _print_graph_excel_failure_diagnostics(
    *,
    file_name: str,
    server_relative_url: str,
    exc: Exception,
) -> None:
    status = _http_status_from_exception(exc)
    snippet = _http_response_snippet(exc)
    status_text = f"HTTP {status}" if status is not None else type(exc).__name__
    print(f"    [ERROR] Graph workbook extraction failed for '{file_name}': {status_text}: {exc}")
    if snippet:
        print(f"    [ERROR] Graph response: {snippet}")

    if status == 401:
        print("    [PERMISSION] The SPN could not authenticate to Microsoft Graph (401 Unauthorized).")
        print("    [PERMISSION] Check the client id/secret/tenant in Key Vault or environment variables and confirm the secret has not expired.")
    elif status == 403:
        print("    [PERMISSION] The SPN token is valid but is not authorised to open this workbook via Graph Excel APIs (403 Forbidden).")
        print("    [PERMISSION] Check Graph application permissions/admin consent, SharePoint site access, and sensitivity-label/MIP rights for the SPN.")
    elif status == 404:
        print("    [PERMISSION] Graph could not resolve the workbook item (404 Not Found). Check the server-relative URL, site URL, and document library path.")
    else:
        print("    [PERMISSION] If this workbook is Purview/sensitivity-label protected, verify the SPN is allowed to read the file through Office Online/Graph workbook APIs.")

    print(
        "    [PERMISSION] Suggested diagnostic: "
        f"python tools/diagnostics/graph_excel_probe.py --env dev --file-url \"{server_relative_url}\""
    )


def _read_excel_sheets_via_graph_for_discovery(sp, server_relative_url: str, file_name: str) -> dict[str, pd.DataFrame]:
    from sharepoint_ingest.file_processors import read_excel_sheets_via_graph

    print("    [excel] method=graph-workbook: attempting createSession/read worksheets/usedRange")
    sheets = read_excel_sheets_via_graph(
        sp,
        server_relative_url,
        header_skip_rows=0,
        sheet_selector="ALL_SHEETS",
        progress=lambda message: print(f"    [excel] method=graph-workbook: {message}"),
    )
    print(f"    [excel] method=graph-workbook: success ({len(sheets)} sheet(s))")
    return sheets


def _read_file_sheets(
    file_bytes: bytes,
    file_name: str,
    *,
    sp=None,
    server_relative_url: str = "",
) -> dict[str, pd.DataFrame]:
    """Return {sheet_key: dataframe}.  Non-Excel files get key 'default'."""
    lower = file_name.lower()
    try:
        if lower.endswith(".csv") or lower.endswith(".txt"):
            from sharepoint_ingest.file_processors import read_csv_from_bytes
            return {"default": read_csv_from_bytes(file_bytes)}
        if lower.endswith(".parquet"):
            from sharepoint_ingest.file_processors import read_parquet_from_bytes
            return {"default": read_parquet_from_bytes(BytesIO(file_bytes))}
        if any(lower.endswith(e) for e in (".xlsx", ".xls", ".xlsm")):
            from sharepoint_ingest.file_processors import read_all_excel_sheets_from_bytes
            print("    [excel] method=binary-parse: attempting pandas/openpyxl/xlrd parse")
            sheets = read_all_excel_sheets_from_bytes(file_bytes, file_name=file_name)
            print(f"    [excel] method=binary-parse: success ({len(sheets)} sheet(s))")
            return sheets
        print(f"    [SKIP] Unsupported file type: {file_name}")
        return {}
    except Exception as exc:
        try:
            from sharepoint_ingest.file_processors import (
                EncryptedExcelPayloadError,
                ExcelPayloadError,
                InvalidExcelPayloadError,
            )
        except Exception:  # pragma: no cover - defensive import fallback
            EncryptedExcelPayloadError = InvalidExcelPayloadError = ExcelPayloadError = ()  # type: ignore[assignment]

        if isinstance(exc, EncryptedExcelPayloadError):
            print(f"    [excel] method=binary-parse: detected encrypted/protected payload: {exc}")
            if sp is not None and server_relative_url:
                try:
                    return _read_excel_sheets_via_graph_for_discovery(
                        sp,
                        server_relative_url,
                        file_name,
                    )
                except Exception as graph_exc:
                    _print_graph_excel_failure_diagnostics(
                        file_name=file_name,
                        server_relative_url=server_relative_url,
                        exc=graph_exc,
                    )
                    print(f"    [WARN] Skipping encrypted Excel file '{file_name}' after Graph workbook fallback failed.")
                    return {}
            print(f"    [WARN] Skipping encrypted Excel file '{file_name}': {exc}")
            return {}
        if isinstance(exc, InvalidExcelPayloadError):
            print(f"    [WARN] Skipping unreadable Excel file '{file_name}': {exc}")
            return {}
        if isinstance(exc, ExcelPayloadError):
            print(f"    [WARN] Skipping Excel file '{file_name}': {exc}")
            return {}
        print(f"    [WARN] Could not read '{file_name}': {exc}")
        return {}


def _is_excel(file_name: str) -> bool:
    lower = file_name.lower()
    return any(lower.endswith(e) for e in (".xlsx", ".xls", ".xlsm"))


# ---------------------------------------------------------------------------
# PK inference from a concatenated DataFrame
# ---------------------------------------------------------------------------
_MAX_COMBO_LEN = 3
_MAX_CANDIDATE_COLS = 30  # don't try all columns if there are tons


def _pk_inference(df: pd.DataFrame, is_excel_ingest: bool, col_raw_types: dict[str, str] | None = None) -> dict:
    """Return information about the best composite PK found in df.

    Candidates are restricted to:
      1. Columns whose name matches _PK_NAME_RE (contains 'id', 'no', or 'guid' as
         a word boundary prefix/suffix), OR
      2. Columns whose inferred raw SQL type is INT or BIGINT (numeric – convertible
         to integer and unique).

    Text (VARCHAR) columns are intentionally excluded from PK consideration because
    they are only coincidentally unique in small datasets.

    For Excel ingestions the system column 'excel_tab_name' is added as a composite
    key ingredient when needed to achieve uniqueness.
    """
    n = len(df)
    if n == 0:
        return _no_pk(n)

    _sys_excl = {
        "source_file_name",
        "excel_tab_name",
        "sp_ingest_load_dt",
        "audit_id",
        "__$batch_id",
        "__$job_instance_id",
    }

    # Build set of numeric columns (from the profiled raw types when available)
    numeric_cols: set[str] = set()
    if col_raw_types:
        for c, t in col_raw_types.items():
            if t in _PK_NUMERIC_TYPES:
                numeric_cols.add(c)
    else:
        # Fall back: check actual dtype in the dataframe
        for c in df.columns:
            if pd.api.types.is_integer_dtype(df[c].dtype) or pd.api.types.is_float_dtype(df[c].dtype):
                # Only include if all non-null values are whole numbers
                non_null = df[c].dropna()
                if non_null.empty:
                    continue
                if pd.api.types.is_float_dtype(df[c].dtype):
                    if not (non_null == non_null.round(0)).all():
                        continue
                numeric_cols.add(c)

    # Code-like VARCHAR columns: short, consistent-length, alphanumeric codes
    # e.g. "TXN-001", "CUST_AU_01" – eligible as PK even though they are text
    code_like_cols: set[str] = set()
    for c in df.columns:
        if c.lower() in _sys_excl or c in numeric_cols:
            continue
        raw_type = (col_raw_types or {}).get(c, "")
        if raw_type.startswith("VARCHAR:") or raw_type.startswith("VARCHAR("):
            # Only run the heuristic on the non-null part of the series
            if _is_code_like_varchar(df[c]):
                code_like_cols.add(c)

    # Qualified candidates: name-hint match OR numeric type OR code-like VARCHAR
    candidates = [
        c for c in df.columns
        if c.lower() not in _sys_excl
        and (_PK_NAME_RE.search(c) or c in numeric_cols or c in code_like_cols)
    ]

    # excel_tab_name is used as a composite key extension for Excel ingestions
    extra = ["excel_tab_name"] if (is_excel_ingest and "excel_tab_name" in df.columns) else []

    def _test_combo(cols: list[str]) -> dict | None:
        null_mask = df[cols].isnull().any(axis=1)
        null_rows = int(null_mask.sum())
        sub = df.loc[~null_mask, cols]
        distinct = len(sub.drop_duplicates())
        non_null_count = len(sub)
        dups = non_null_count - distinct
        if dups == 0 and null_rows == 0:
            return {
                "pk_columns": cols,
                "rows_scanned": n,
                "distinct_count": distinct,
                "null_key_rows": null_rows,
                "duplicate_rows": dups,
                "confident": True,
                "strategy": "APPEND",
            }
        return None

    if not candidates and not extra:
        return _no_pk(n)

    # Pass 1: try candidate combos alone (without excel_tab_name)
    for combo_len in range(1, _MAX_COMBO_LEN + 1):
        for combo in itertools.combinations(candidates, combo_len):
            result = _test_combo(list(combo))
            if result:
                return result

    # Pass 2: try candidate combos WITH excel_tab_name (for Excel ingestions)
    if extra and candidates:
        for combo_len in range(1, _MAX_COMBO_LEN + 1):
            for combo in itertools.combinations(candidates, combo_len):
                result = _test_combo(list(combo) + extra)
                if result:
                    return result

    # Pass 3: if NO name-match candidates exist but we have excel_tab_name + numeric
    #         cols, try numeric cols + excel_tab_name
    if extra and not candidates:
        numeric_list = [c for c in df.columns if c in numeric_cols]
        for combo_len in range(1, _MAX_COMBO_LEN + 1):
            for combo in itertools.combinations(numeric_list, combo_len):
                result = _test_combo(list(combo) + extra)
                if result:
                    return result

    # No reliable PK found with the restricted candidate set
    return _no_pk(n)


def _no_pk(n: int) -> dict:
    return {
        "pk_columns": [],
        "rows_scanned": n,
        "distinct_count": 0,
        "null_key_rows": 0,
        "duplicate_rows": 0,
        "confident": False,
        "strategy": "TRUNCATE",
    }


# ---------------------------------------------------------------------------
# CSV mapping rows helper
# ---------------------------------------------------------------------------

_MAPPING_CSV_HEADER = (
    "Object name",
    "Last update",
    "Source object",
    "Source column",
    "Staging column",
    "Staging type",
    "Integrated column",
    "Integrated type",
    "Primary key?",
    "Transform",
    "Comments",
)

_MAPPING_FIRST_ROW_COMMENT = "Mapping provided manually, it cannot or meant to be generated"


def _col_type_with_nullability(raw_type: str, *, is_pk: bool, padding: float) -> str:
    """Return a finalized SQL type string with nullability appended, e.g. 'VARCHAR(100) NOT NULL'."""
    final = _finalize_type(raw_type, padding)
    nullability = "NOT NULL" if is_pk else "NULL"
    return f"{final} {nullability}"


def _system_col_type_with_nullability(col_type: str, col_extra: str) -> str:
    """Return a clean 'TYPE NULLABILITY' string from a system column definition tuple.

    The col_extra may contain DEFAULT expressions; we strip those and keep only
    the nullability keyword (NOT NULL / NULL).
    """
    extra_upper = col_extra.upper()
    if "NOT NULL" in extra_upper:
        nullability = "NOT NULL"
    elif "NULL" in extra_upper:
        nullability = "NULL"
    else:
        nullability = ""
    return f"{col_type} {nullability}".strip()


def _generate_mapping_csv_rows(
    *,
    object_name: str,
    source_object: str,
    data_columns: dict[str, str],
    system_columns: list[tuple],
    pk_columns: list[str],
    column_mapping: dict[str, str] | None = None,
    padding: float = 0.20,
    as_of_date: datetime.date | None = None,
) -> str:
    """Return a CSV string (header + one row per column) suitable for appending to a
    mapping tracking CSV file.

    Columns emitted:
        Object name, Last update, Source object, Source column,
        Staging column, Staging type, Integrated column, Integrated type,
        Primary key?, Transform, Comments

    The first data row carries a comment noting that the mapping was generated
    automatically.

    Args:
        object_name:    Destination table name without schema prefix (e.g. 'team_effort_register').
        source_object:  Human-readable SharePoint folder name (e.g. 'team effort register').
        data_columns:   Ordered dict of {col_name: raw_type} from profiling.
        system_columns: List of (name, type, extra) tuples (e.g. _SYSTEM_COLUMNS_PLAIN).
        pk_columns:     Destination PK column names.
        column_mapping: Optional source→destination business column mapping.
        padding:        VARCHAR length padding fraction (default 0.20).
        as_of_date:     Date to stamp in 'Last update'; defaults to today.
    """
    if as_of_date is None:
        as_of_date = datetime.date.today()
    date_str = as_of_date.strftime("%d/%m/%Y")

    pk_set = set(pk_columns)
    column_mapping = column_mapping or {str(c): str(c) for c in data_columns}
    sys_col_names_lower = {c[0].lower() for c in system_columns}

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_MAPPING_CSV_HEADER)

    first_row = True

    # ── Business columns ─────────────────────────────────────────────────────
    for col_name, raw_type in data_columns.items():
        if col_name.lower() in sys_col_names_lower:
            continue  # system columns emitted separately below

        dest_col_name = column_mapping.get(str(col_name), str(col_name))
        is_pk = dest_col_name in pk_set
        col_type_str = _col_type_with_nullability(raw_type, is_pk=is_pk, padding=padding)

        writer.writerow([
            object_name,
            date_str,
            source_object,
            col_name,
            dest_col_name,
            col_type_str,
            dest_col_name,
            col_type_str,
            "Y" if is_pk else "",
            "",
            _MAPPING_FIRST_ROW_COMMENT if first_row else "",
        ])
        first_row = False

    # ── System columns ────────────────────────────────────────────────────────
    # System columns are added by the ingestion engine — they are NOT present in
    # the source file and never appear as keys in column_mapping_json.  Following
    # the same source→destination pattern as column_mapping_json, their Source
    # column is left blank (no source key) while Staging/Integrated columns are
    # populated with the engine-managed column name.
    for col_name, col_type, col_extra in system_columns:
        is_pk = col_name in pk_set
        if is_pk:
            col_type_str = f"{col_type} NOT NULL"
        else:
            col_type_str = _system_col_type_with_nullability(col_type, col_extra)

        writer.writerow([
            object_name,
            date_str,
            source_object,
            "",          # Source column — empty: not present in source file
            col_name,    # Staging column
            col_type_str,
            col_name,    # Integrated column
            col_type_str,
            "Y" if is_pk else "",
            "",
            _MAPPING_FIRST_ROW_COMMENT if first_row else "",
        ])
        first_row = False

    return buf.getvalue()


# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------

def _q(name: str) -> str:
    """Bracket-quote a SQL identifier."""
    return "[" + name.replace("]", "]]") + "]"


def _generate_create_table(
    schema: str,
    table_name: str,
    data_columns: dict[str, str],
    system_columns: list[tuple],
    pk_columns: list[str],
    *,
    padding: float = 0.20,
) -> str:
    lines = [f"CREATE TABLE [{schema}].[{table_name}] ("]
    parts = []

    # Business columns first
    for col_name, raw_type in data_columns.items():
        if col_name.lower() in {c[0].lower() for c in system_columns}:
            continue  # system columns handled separately
        final_type = _finalize_type(raw_type, padding)
        is_pk = col_name in pk_columns
        nullable = "NOT NULL" if is_pk else "NULL"
        parts.append(f"    {_q(col_name):<45} {final_type:<20}  {nullable}")

    # System columns
    for col_name, col_type, col_extra in system_columns:
        is_pk = col_name in pk_columns
        if is_pk:
            parts.append(f"    {_q(col_name):<45} {col_type:<20}  NOT NULL")
        else:
            parts.append(f"    {_q(col_name):<45} {col_type:<20}  {col_extra}")

    # PK constraint
    if pk_columns:
        pk_cols_str = ", ".join(_q(c) for c in pk_columns)
        safe_name = table_name.replace(" ", "_").replace("-", "_")
        parts.append(
            f"    CONSTRAINT [PK_{safe_name}] PRIMARY KEY CLUSTERED ({pk_cols_str})"
        )

    lines.append(",\n".join(parts))
    lines.append(");")
    return "\n".join(lines)


def _generate_config_insert(
    *,
    sharepoint_base_url: str,
    sharepoint_process_folder: str,
    sharepoint_process_archive_folder: str,
    sharepoint_process_failed_folder: str,
    staging_table_name: str,
    integrated_table_name: str = "",
    excel_tab_name: str = "",
    process_frequency: str = "DAILY",
    header_skip_rows: int = 0,
    multi_file_ingest: int = 1,
    check_source_dest_columns: int = 1,
    is_active: int = 1,
    ingestion_scope: str = "REAL",
    load_strategy: str = "TRUNCATE",
    merge_key_columns: str = "",
    column_mapping_json: str = "{}",
    file_name_pattern: str = "",
    # New field names (old names kept as aliases for call-site compat)
    to_email_address: str = "",
    cc_email_address: str = "",
    # Legacy aliases — forwarded to the new field names
    error_notification_email_address: str = "",
    error_notification_cc_email_address: str = "",
) -> str:
    def _s(v: str) -> str:
        v = str(v) if v else ""
        return "NULL" if not v else f"N'{v.replace(chr(39), chr(39)*2)}'"

    # Merge legacy aliases (caller may use either name).  Discovery output should
    # not suggest default recipients; explicit caller-supplied values are kept.
    resolved_to = to_email_address or error_notification_email_address
    resolved_cc = cc_email_address or error_notification_cc_email_address
    resolved_column_mapping_json = (
        str(column_mapping_json).strip() if column_mapping_json is not None else ""
    )
    if not resolved_column_mapping_json:
        resolved_column_mapping_json = "{}"

    # For the integrated table, derive from staging if not supplied:
    # staging: schema.X  →  integrated: schema.X  (same schema/name, different DB)
    resolved_int = integrated_table_name or staging_table_name

    new_workflow_id = f"wf-{staging_table_name.split('.')[-1].replace('_', '-').lower()}-{uuid.uuid4().hex[:8]}"

    return f"""INSERT INTO [config].[sharepoint_ingestion] (
    [sharepoint_base_url],
    [sharepoint_process_folder],
    [sharepoint_process_archive_folder],
    [sharepoint_process_failed_folder],
    [excel_tab_name],
    [process_frequency],
    [header_skip_rows],
    [check_source_dest_columns],
    [multi_file_ingest],
    [staging_table_name],
    [integrated_table_name],
    [is_active],
    [ingestion_scope],
    [file_name_pattern],
    [load_strategy],
    [merge_key_columns],
    [column_mapping_json],
    [to_email_address],
    [cc_email_address],
    [workflow_id]
) VALUES (
    {_s(sharepoint_base_url)},
    {_s(sharepoint_process_folder)},
    {_s(sharepoint_process_archive_folder)},
    {_s(sharepoint_process_failed_folder)},
    {_s(excel_tab_name)},
    {_s(process_frequency)},
    {header_skip_rows},
    {check_source_dest_columns},
    {multi_file_ingest},
    {_s(staging_table_name)},
    {_s(resolved_int)},
    {is_active},
    {_s(ingestion_scope)},
    {_s(file_name_pattern)},
    {_s(load_strategy)},
    {_s(merge_key_columns)},
    {_s(resolved_column_mapping_json)},
    {_s(resolved_to)},
    {_s(resolved_cc)},
    {_s(new_workflow_id)}
);"""


# ---------------------------------------------------------------------------
# File name pattern helper
# ---------------------------------------------------------------------------

def _derive_file_pattern(file_names: list[str]) -> str:
    """Derive a simple glob/regex pattern from a list of file names."""
    if not file_names:
        return ""
    if len(file_names) == 1:
        return file_names[0]
    # All same extension?
    exts = {os.path.splitext(f)[1].lower() for f in file_names}
    if len(exts) == 1:
        ext = exts.pop()
        return f"*{ext}"
    return f"*"


def _normalize_business_col_name(name: str) -> str:
    return str(name).strip().lower()


def _file_kind_from_name(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        return "excel"
    if lower.endswith(".parquet"):
        return "parquet"
    if lower.endswith((".csv", ".txt")):
        return "csv"
    return "other"


def _name_stem_signature(file_name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(file_name.lower())
    stem_norm = re.sub(r"[_\-\s]+", "_", stem)
    stem_norm = re.sub(r"\d+", "{n}", stem_norm).strip("_")
    return stem_norm, ext


def _same_filename_family(file_names: list[str]) -> bool:
    if len(file_names) < 2:
        return False
    signatures = [_name_stem_signature(n) for n in file_names]
    exts = {ext for _, ext in signatures}
    stems = {stem for stem, _ in signatures}
    return len(exts) == 1 and len(stems) == 1


def _derive_family_pattern(file_names: list[str]) -> str:
    if not file_names:
        return ""
    if len(file_names) == 1:
        return file_names[0]
    stem_sig, ext = _name_stem_signature(file_names[0])
    stem_glob = stem_sig.replace("{n}", "*")
    stem_glob = re.sub(r"_+", "_", stem_glob).strip("_")
    if not stem_glob:
        stem_glob = "*"
    return f"{stem_glob}{ext}"


def _snake_case_identifier_fragment(value: str, *, fallback: str) -> str:
    """Normalize a free-form name to a safe snake_case SQL identifier fragment."""
    text = str(value or "").strip()
    if not text:
        return fallback
    # Split CamelCase/PascalCase boundaries before punctuation cleanup.
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or fallback


def _build_column_mapping_json(data_columns: dict[str, str]) -> tuple[dict[str, str], str]:
    """Return source→destination column mapping and compact JSON text.

    Source files can contain spaces, punctuation, or mixed-case headers.  The
    destination tables generated by this tool use safe ``snake_case`` business
    column names, and ``column_mapping_json`` must mirror those generated
    destination names because the ingestion engine interprets it as
    ``source-column -> destination-column``.

    If two source columns normalize to the same destination name, append a
    stable numeric suffix to keep the generated destination columns unique.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for idx, source_col in enumerate(data_columns.keys(), start=1):
        base = _snake_case_identifier_fragment(source_col, fallback=f"column_{idx}")
        dest_col = base
        suffix = 2
        while dest_col.lower() in used:
            dest_col = f"{base}_{suffix}"
            suffix += 1
        used.add(dest_col.lower())
        mapping[str(source_col)] = dest_col

    return mapping, json.dumps(mapping, separators=(",", ":"))


def _apply_destination_column_mapping(
    data_columns: dict[str, str],
    column_mapping: dict[str, str],
) -> dict[str, str]:
    """Return profiled columns keyed by generated destination column name."""
    return {
        column_mapping.get(str(source_col), str(source_col)): raw_type
        for source_col, raw_type in data_columns.items()
    }


def _map_pk_columns_to_destination(
    pk_columns: list[str],
    column_mapping: dict[str, str],
) -> list[str]:
    """Translate inferred source PK columns to destination PK column names."""
    return [column_mapping.get(str(col), str(col)) for col in pk_columns]


def _build_profile_candidate(sp, fi) -> _ProfileCandidate | None:
    try:
        print("    [excel] method=binary-download: starting" if _is_excel(fi.name) else "    [download] starting")
        raw = sp.download_file_to_bytes(fi.server_relative_url)
        if _is_excel(fi.name):
            print(f"    [excel] method=binary-download: downloaded {len(raw)} bytes")
        else:
            print(f"    [download] downloaded {len(raw)} bytes")
    except Exception as exc:
        print(f"    [WARN] Download failed for '{fi.name}': {exc}")
        return None

    sheets = _read_file_sheets(raw, fi.name, sp=sp, server_relative_url=fi.server_relative_url)
    if not sheets:
        return None

    file_kind = _file_kind_from_name(fi.name)
    any_excel = file_kind == "excel"
    combined_profile: dict[str, str] = {}
    full_frames: list[pd.DataFrame] = []

    for sheet_key, df in sheets.items():
        local = df.copy()
        if any_excel:
            local["excel_tab_name"] = sheet_key
        local["source_file_name"] = fi.name
        full_frames.append(local)
        prof = _profile_df(local.drop(columns=["source_file_name", "excel_tab_name"], errors="ignore"))
        combined_profile = _merge_profiles(combined_profile, prof)

    normalized_business_columns = tuple(_normalize_business_col_name(c) for c in combined_profile.keys())
    return _ProfileCandidate(
        file_name=fi.name,
        server_relative_url=fi.server_relative_url,
        file_kind=file_kind,
        normalized_business_columns=normalized_business_columns,
        combined_profile=combined_profile,
        full_frames=full_frames,
        any_excel=any_excel,
    )


def _layout_key(candidate: _ProfileCandidate) -> str:
    cols = "|".join(candidate.normalized_business_columns)
    return f"{candidate.file_kind}::{cols}"


def _merge_candidates_to_group(
    *,
    group_key: str,
    candidates: list[_ProfileCandidate],
    force_single_file: bool,
) -> _DiscoveryGroup:
    combined_profile: dict[str, str] = {}
    full_frames: list[pd.DataFrame] = []
    any_excel = False
    file_names: list[str] = []
    for c in candidates:
        file_names.append(c.file_name)
        any_excel = any_excel or c.any_excel
        full_frames.extend(c.full_frames)
        combined_profile = _merge_profiles(combined_profile, c.combined_profile)

    can_multi = (not force_single_file) and len(candidates) > 1 and _same_filename_family(file_names)
    if can_multi:
        pattern = _derive_family_pattern(file_names)
        multi = 1
    else:
        # caller should split into single-file groups if mixed names/layouts.
        pattern = file_names[0] if len(file_names) == 1 else _derive_file_pattern(file_names)
        multi = 0 if len(file_names) == 1 else 0

    return _DiscoveryGroup(
        group_key=group_key,
        file_names=file_names,
        files=candidates,
        combined_profile=combined_profile,
        full_frames=full_frames,
        any_excel=any_excel,
        multi_file_ingest=multi,
        file_name_pattern=pattern,
    )


def _build_discovery_groups(candidates: list[_ProfileCandidate]) -> list[_DiscoveryGroup]:
    """Group files by layout; multi-file only for same-family same-layout.

    If file names differ and appear unrelated, force single-file recommendations.
    """
    by_layout: dict[str, list[_ProfileCandidate]] = defaultdict(list)
    for c in candidates:
        by_layout[_layout_key(c)].append(c)

    groups: list[_DiscoveryGroup] = []
    for key, layout_candidates in by_layout.items():
        layout_candidates = sorted(layout_candidates, key=lambda c: c.file_name.lower())
        if len(layout_candidates) == 1:
            groups.append(_merge_candidates_to_group(group_key=key, candidates=layout_candidates, force_single_file=True))
            continue

        if _same_filename_family([c.file_name for c in layout_candidates]):
            groups.append(_merge_candidates_to_group(group_key=key, candidates=layout_candidates, force_single_file=False))
            continue

        # Same layout but unrelated file names: emit separate single-file configs.
        for c in layout_candidates:
            groups.append(_merge_candidates_to_group(group_key=f"{key}::{c.file_name}", candidates=[c], force_single_file=True))

    return groups


def _safe_suffix_from_file_name(file_name: str) -> str:
    stem = os.path.splitext(file_name)[0]
    return _snake_case_identifier_fragment(stem, fallback="file")


def _call_with_optional_stdout_suppressed(suppress: bool, func, *args, **kwargs):
    """Call *func*, optionally suppressing stdout from noisy discovery helpers."""
    if not suppress:
        return func(*args, **kwargs)
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def _print_group_sql(
    *,
    group: _DiscoveryGroup,
    folder_name: str,
    folder_safe_name: str,
    folder_server_relative_url: str,
    default_base_url: str,
    dest_schema: str,
    padding: float,
    all_file_names_in_folder: list[str],
    notification_to: str,
    notification_cc: str,
    csv_mapping_rows_only: bool = False,
) -> None:
    archive_folder = f"{folder_server_relative_url}/Processed"
    failed_folder = f"{folder_server_relative_url}/Failed"

    if group.full_frames:
        try:
            full_df = pd.concat(group.full_frames, ignore_index=True)
            pk_info = _pk_inference(full_df, group.any_excel, col_raw_types=group.combined_profile)
        except Exception as exc:
            if not csv_mapping_rows_only:
                print(f"    [WARN] PK inference failed for group '{group.group_key}': {exc}")
            pk_info = _no_pk(0)
    else:
        pk_info = _no_pk(0)

    pk_cols = pk_info["pk_columns"]
    load_strategy = pk_info["strategy"]
    sys_cols = _SYSTEM_COLUMNS_EXCEL if group.any_excel else _SYSTEM_COLUMNS_PLAIN
    excel_tab_name_cfg = "ALL_SHEETS" if group.any_excel else ""

    column_mapping, column_mapping_json = _build_column_mapping_json(group.combined_profile)
    destination_profile = _apply_destination_column_mapping(group.combined_profile, column_mapping)
    destination_pk_cols = _map_pk_columns_to_destination(pk_cols, column_mapping)
    merge_key = ",".join(destination_pk_cols)

    # If we split by file, suffix destination table to avoid collisions.
    if len(group.file_names) == 1:
        suffix = _safe_suffix_from_file_name(group.file_names[0])
        table_basename = f"{folder_safe_name}_{suffix}"
    else:
        table_basename = folder_safe_name
    staging_table = f"{dest_schema}.{table_basename}"

    # Human-readable source object name: folder name with underscores → spaces
    source_object = folder_name.replace("_", " ").lower()
    object_name = table_basename

    if csv_mapping_rows_only:
        print(
            _generate_mapping_csv_rows(
                object_name=object_name,
                source_object=source_object,
                data_columns=group.combined_profile,
                system_columns=sys_cols,
                pk_columns=destination_pk_cols,
                column_mapping=column_mapping,
                padding=padding,
            ),
            end="",
        )
        return

    print(f"\n{'─'*60}")
    print("-- " + "─" * 58)
    print(f"-- CREATE TABLE for new folder: {folder_name}")
    print(f"-- Group key:    {group.group_key}")
    print(f"-- Files in group ({len(group.file_names)}): {', '.join(group.file_names)}")
    print(f"-- Dest table:   {staging_table}")
    print(f"-- Type:         {'Excel (all-sheets merged)' if group.any_excel else 'CSV/Parquet'}")
    print(f"-- multi_file_ingest: {group.multi_file_ingest}")
    print(f"-- file_name_pattern: {group.file_name_pattern}")
    print("-- " + "─" * 58)
    print()
    print(
        _generate_create_table(
            schema=dest_schema,
            table_name=table_basename,
            data_columns=destination_profile,
            system_columns=sys_cols,
            pk_columns=destination_pk_cols,
            padding=padding,
        )
    )

    print()
    print("-- " + "─" * 58)
    print("-- PK Inference Evidence")
    print("-- " + "─" * 58)
    if destination_pk_cols:
        print(f"-- Suggested composite PK: {', '.join(destination_pk_cols)}")
    else:
        print("-- No reliable PK found — review manually, PK omitted")
    print(f"-- Rows scanned:   {pk_info['rows_scanned']:,}")
    if destination_pk_cols:
        print(f"-- Distinct key combos: {pk_info['distinct_count']:,}")
        print(f"-- Null key rows:  {pk_info['null_key_rows']}")
        print(f"-- Duplicate rows: {pk_info['duplicate_rows']}")
    print(f"-- Suggested load_strategy: {load_strategy}")
    print(f"-- Suggested merge_key_columns: {merge_key or '(none)'}")

    overlap = [n for n in all_file_names_in_folder if fnmatch.fnmatch(n, group.file_name_pattern)]
    outside_group = sorted(set(overlap) - set(group.file_names))
    if outside_group:
        print("-- WARNING: file_name_pattern overlaps files outside this group:")
        print(f"--          {', '.join(outside_group)}")
        print("--          Consider narrowing the pattern before applying config SQL.")
    print()

    print("-- " + "─" * 58)
    print("-- INSERT INTO [config].[sharepoint_ingestion]")
    print("-- " + "─" * 58)
    print(
        _generate_config_insert(
            sharepoint_base_url=default_base_url,
            sharepoint_process_folder=folder_server_relative_url,
            sharepoint_process_archive_folder=archive_folder,
            sharepoint_process_failed_folder=failed_folder,
            staging_table_name=staging_table,
            excel_tab_name=excel_tab_name_cfg,
            multi_file_ingest=group.multi_file_ingest,
            check_source_dest_columns=1,
            is_active=1,
            ingestion_scope="REAL",
            load_strategy=load_strategy,
            merge_key_columns=merge_key,
            column_mapping_json=column_mapping_json,
            file_name_pattern=group.file_name_pattern,
            error_notification_email_address=notification_to,
            error_notification_cc_email_address=notification_cc,
        )
    )
    print()


# ---------------------------------------------------------------------------
# Main discovery
# ---------------------------------------------------------------------------

def _build_sp_client(settings) -> tuple["SharePointClient", object]:
    """Return (SharePointClient, patched_settings) resolving credentials and site URL from KV."""
    from dataclasses import replace as _dc_replace

    env_name = str(getattr(settings, "env_name", "") or "")
    provider = maybe_build_provider(settings.key_vault)
    if provider is not None:
        print("  [auth] Fetching SharePoint credentials from Key Vault …")
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)
        # Resolve site URL from Key Vault.  This intentionally mirrors main.py:
        # Key Vault is authoritative; SHAREPOINT_SITE_URL_<ENV> is only an
        # emergency local-dev fallback when KV cannot be read.
        if settings.key_vault.site_url_secret_name:
            try:
                site_url = provider.get_secret(settings.key_vault.site_url_secret_name)
                settings = _dc_replace(
                    settings,
                    sharepoint=_dc_replace(settings.sharepoint, site_url=site_url),
                )
                print(f"  [auth] Resolved SharePoint site URL from Key Vault: {site_url}")
            except Exception as exc:
                print(f"  [WARN] Could not fetch site URL from Key Vault: {exc}")
    else:
        client_id = os.getenv("SHAREPOINT_CLIENT_ID", "")
        client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET", "")
        tenant_id = os.getenv("SHAREPOINT_TENANT_ID", "")
    if not (client_id and client_secret and tenant_id):
        raise RuntimeError(
            "SharePoint credentials not found.  Configure Key Vault or set "
            "SHAREPOINT_CLIENT_ID / SHAREPOINT_CLIENT_SECRET / SHAREPOINT_TENANT_ID."
        )
    return SharePointClient(
        site_url=settings.sharepoint.site_url,
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
    ), settings


def _norm(path: str) -> str:
    return path.strip().rstrip("/").lower()


def _configured_folder_keys(raw_folder: str, site_path: str) -> set[str]:
    """Return normalized comparable keys for a configured process folder.

    Handles both:
    - server-relative paths: /sites/<site>/Documents/<folder>
    - site-relative paths:   /Documents/<folder> (or Documents/<folder>)
    """
    value = str(raw_folder or "").strip()
    if not value:
        return set()

    keys = {_norm(value)}
    if not value.startswith("/"):
        keys.add(_norm(f"/{value}"))
    if value.startswith("/sites/") or value.startswith("/teams/"):
        return keys

    normalized_site = _norm(site_path or "")
    if not normalized_site:
        return keys

    value_with_leading_slash = value if value.startswith("/") else f"/{value}"
    keys.add(_norm(f"{normalized_site}{value_with_leading_slash}"))
    return keys


def _assert_dev_only(env_name: str) -> None:
    if str(env_name or "").strip().lower() != "dev":
        raise RuntimeError(
            "tools/discover_new_ingestion.py is DEV-only. "
            "Use --env dev and run against the dev environment only."
        )


def _list_folders_to_depth(sp: SharePointClient, root_folder: str, max_depth: int = 3) -> list:
    """Return folder items under *root_folder* up to ``max_depth`` levels deep.

    Depth semantics:
    - depth=1 → direct children of ``root_folder``
    - depth=2 → includes grandchildren
    - depth=3 → includes great-grandchildren
    """
    if max_depth <= 0:
        return []

    discovered: list = []
    frontier: list[tuple[str, int]] = [(root_folder, 1)]

    while frontier:
        current_folder, depth = frontier.pop(0)
        child_folders = sp.list_folders(current_folder)
        discovered.extend(child_folders)

        if depth < max_depth:
            frontier.extend((folder.server_relative_url, depth + 1) for folder in child_folders)

    return discovered


def discover(
    env: str | None = None,
    base_folder: str | None = None,
    dest_schema: str = "sharepoint",
    no_profile: bool = False,
    padding: float = 0.20,
    csv_mapping_rows_only: bool = False,
) -> None:
    def _status(*args, **kwargs) -> None:
        if not csv_mapping_rows_only:
            print(*args, **kwargs)

    sep = "=" * 70
    _status(sep)
    _status("  SharePoint New Ingestion Discovery Tool")
    _status(sep)

    # ------------------------------------------------------------------
    # 1. Settings + SQL
    # ------------------------------------------------------------------
    _status(f"\n[1/5] Loading settings (env={env or 'auto'}) …")
    settings = load_settings(env_override=env)
    _assert_dev_only(getattr(settings, "env_name", ""))

    # Resolve database names from Key Vault (same pattern as main.py).
    # load_settings() leaves sql.database blank — it must be injected from KV.
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    provider = maybe_build_provider(settings.key_vault)
    settings = _resolve_database_names(settings, provider, _logger)

    # Resolve SQL credentials for credential-based auth modes.
    resolved_sql = _resolve_sql_settings(settings, provider=provider)

    _status(f"      SQL:             {resolved_sql.host}/{resolved_sql.database}")
    _status(f"      SharePoint site: {settings.sharepoint.site_url}")
    sql = SqlClient(resolved_sql)
    sql.test_connection()
    _status("      SQL connection OK.")

    # ------------------------------------------------------------------
    # 2. SharePoint connection
    # ------------------------------------------------------------------
    _status("\n[2/5] Connecting to SharePoint …")
    sp, settings = _call_with_optional_stdout_suppressed(
        csv_mapping_rows_only,
        _build_sp_client,
        settings,
    )
    _status("      Connected.")
    _status(f"      SharePoint site: {settings.sharepoint.site_url}")

    # The generated config should use the authoritative environment SharePoint
    # base URL (resolved from Key Vault above), not copy an arbitrary existing
    # SQL config row which may be stale or from a previous environment.
    default_base_url = settings.sharepoint.site_url

    # ------------------------------------------------------------------
    # 3. Existing config rows
    # ------------------------------------------------------------------
    _status("\n[3/5] Loading existing [config].[sharepoint_ingestion] rows …")
    existing_rows = sql.query_rows(
        "SELECT * FROM [config].[sharepoint_ingestion] ORDER BY id"
    )
    _status(f"      Found {len(existing_rows)} row(s).")

    site_path = (urlparse(settings.sharepoint.site_url).path or "").rstrip("/")
    configured: set[str] = set()
    for row in existing_rows:
        configured.update(
            _configured_folder_keys(
                str(row.get("sharepoint_process_folder") or ""),
                site_path,
            )
        )
    _status(f"      Normalized configured folder key(s): {len(configured)}")

    first = existing_rows[0] if existing_rows else None
    first_folder = str(first.get("sharepoint_process_folder") or "") if first else ""
    default_root = first_folder.rsplit("/", 1)[0] if "/" in first_folder else first_folder
    # Do not suggest notification recipients for newly discovered REAL configs.
    # Operators can fill these in manually when applying the generated SQL.
    default_notification_to = ""
    default_notification_cc = ""

    scan_root = base_folder or default_root
    if not scan_root:
        _status("\n[ERROR] Cannot determine SharePoint root folder.  Pass --base-folder.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Discover new folders (metadata only)
    # ------------------------------------------------------------------
    _status(f"\n[4/5] Listing sub-folders under: {scan_root}")
    _status("      (metadata-only — no file downloads)")
    sp_folders = _list_folders_to_depth(sp, scan_root, max_depth=3)
    _status(f"      Found {len(sp_folders)} sub-folder(s) up to depth 3.")

    new_folders = [
        f for f in sp_folders
        if f.name.lower() not in _SKIP_FOLDER_NAMES
        and _norm(f.server_relative_url) not in configured
    ]

    if not new_folders:
        _status("\n  All folders are already in [config].[sharepoint_ingestion].  Nothing to do.")
        return

    _status(f"\n  {len(new_folders)} new (unconfigured) folder(s) found:")
    for f in new_folders:
        _status(f"    - {f.server_relative_url}")

    # ------------------------------------------------------------------
    # 5. File count + optional profiling
    # ------------------------------------------------------------------
    _status("\n[5/5] Checking file counts and profiling …")

    for sp_folder in new_folders:
        _status(f"\n  Folder: {sp_folder.server_relative_url}")
        files = sp.list_files(sp_folder.server_relative_url)
        _status(f"    Files found: {len(files)}")

        if len(files) < 1:
            _status("    SKIP — no files present.")
            continue

        for fi in files:
            _status(f"      - {fi.name}")

        folder_name = sp_folder.name
        safe_name = _snake_case_identifier_fragment(folder_name, fallback="folder")
        file_names = [fi.name for fi in files]
        excel_ingest = all(_is_excel(fi.name) for fi in files)

        _status(f"\n  {sep}")
        _status(f"  NEW INGESTION CANDIDATE: {folder_name}")
        _status(f"  Folder path:    {sp_folder.server_relative_url}")
        _status(f"  Files:          {len(files)}")
        _status(f"  Type:           {'Excel (all-sheets)' if excel_ingest else 'CSV/Parquet'}")
        _status("  Dest table:     (derived per discovered group)")
        _status(sep)

        if no_profile:
            _call_with_optional_stdout_suppressed(
                csv_mapping_rows_only,
                _print_stub,
                sp_folder=sp_folder,
                files=files,
                default_base_url=default_base_url,
                dest_schema=dest_schema,
                excel_ingest=excel_ingest,
                notification_to=default_notification_to,
                notification_cc=default_notification_cc,
            )
            continue

        _status(f"\n  Profiling {len(files)} file(s) …")
        candidates: list[_ProfileCandidate] = []
        for fi in files:
            _status(f"    Downloading: {fi.name}")
            cand = _call_with_optional_stdout_suppressed(
                csv_mapping_rows_only,
                _build_profile_candidate,
                sp,
                fi,
            )
            if cand is not None:
                candidates.append(cand)

        if not candidates:
            _status("    [WARN] No usable data found.  Skipping.")
            continue

        groups = _build_discovery_groups(candidates)
        _status(f"    Derived {len(groups)} ingestion recommendation group(s).")
        for g in groups:
            _print_group_sql(
                group=g,
                folder_name=folder_name,
                folder_safe_name=safe_name,
                folder_server_relative_url=sp_folder.server_relative_url,
                default_base_url=default_base_url,
                dest_schema=dest_schema,
                padding=padding,
                all_file_names_in_folder=file_names,
                notification_to=default_notification_to,
                notification_cc=default_notification_cc,
                csv_mapping_rows_only=csv_mapping_rows_only,
            )

    _status(f"\n{sep}")
    _status("  Discovery complete.")
    _status(sep)


def _print_stub(
    *,
    sp_folder,
    files,
    default_base_url,
    dest_schema,
    excel_ingest,
    notification_to: str,
    notification_cc: str,
):
    folder_name = sp_folder.name
    safe_name = _snake_case_identifier_fragment(folder_name, fallback="folder")
    staging_table = f"{dest_schema}.{safe_name}"
    archive_folder = f"{sp_folder.server_relative_url}/Processed"
    failed_folder = f"{sp_folder.server_relative_url}/Failed"
    excel_tab_name_cfg = "ALL_SHEETS" if excel_ingest else ""
    sys_cols = _SYSTEM_COLUMNS_EXCEL if excel_ingest else _SYSTEM_COLUMNS_PLAIN

    sys_col_defs = "\n".join(
        f"    {_q(c[0]):<45} {c[1]:<20}  {c[2]}"
        for c in sys_cols
    )

    print(f"""
-- --------------------------------------------------------
-- STUB: CREATE TABLE for new folder: {folder_name}
-- (Run without --no-profile to get typed business columns)
-- --------------------------------------------------------
CREATE TABLE [{dest_schema}].[{safe_name}] (
    -- TODO: add business columns here after profiling
{sys_col_defs}
);
""")
    file_names = [fi.name for fi in files]
    print(
        _generate_config_insert(
            sharepoint_base_url=default_base_url,
            sharepoint_process_folder=sp_folder.server_relative_url,
            sharepoint_process_archive_folder=archive_folder,
            sharepoint_process_failed_folder=failed_folder,
            staging_table_name=staging_table,
            excel_tab_name=excel_tab_name_cfg,
            file_name_pattern=_derive_file_pattern(file_names),
            error_notification_email_address=notification_to,
            error_notification_cc_email_address=notification_cc,
        )
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse(argv=None):
    p = argparse.ArgumentParser(
        description="Discover new SharePoint ingestion folders and suggest T-SQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--env", default="dev", choices=["dev"], help="DEV only (default: dev)")
    p.add_argument("--base-folder", default=None, dest="base_folder")
    p.add_argument("--dest-schema", default=_DEFAULT_DEST_SCHEMA, dest="dest_schema")
    p.add_argument("--no-profile", action="store_true", default=False, dest="no_profile")
    p.add_argument("--padding", type=float, default=0.20, dest="padding")
    p.add_argument(
        "--csv-mapping-rows",
        action="store_true",
        default=False,
        dest="csv_mapping_rows_only",
        help="Print only generated CSV mapping rows; omit normal SQL/recommendation output.",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse()
    discover(
        env=args.env,
        base_folder=args.base_folder,
        dest_schema=args.dest_schema,
        no_profile=args.no_profile,
        padding=args.padding,
        csv_mapping_rows_only=args.csv_mapping_rows_only,
    )
