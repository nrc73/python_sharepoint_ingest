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
   - CREATE TABLE [schema].[dest_<folder>] with system columns matching existing
     conventions: source_file_name, sp_ingest_created_utc, sp_ingest_modified_utc,
     and excel_tab_name (for Excel ingestions).
   - INSERT INTO [config].[sharepoint_ingestion] ...

Usage (DEV only)
----------------
    python tools/discover_new_ingestion.py [--env dev]
                                           [--base-folder PATH]
                                           [--dest-schema sharepoint]
                                           [--no-profile]
                                           [--padding 0.20]
"""

from __future__ import annotations

import argparse
import fnmatch
import itertools
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from collections import defaultdict
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
from sharepoint_ingest.sharepoint_client import SharePointClient
from sharepoint_ingest.sql_client import SqlClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SYSTEM_COLUMNS_EXCEL = [
    ("excel_tab_name",          "VARCHAR(100)",  "NOT NULL"),
    ("source_file_name",        "VARCHAR(255)",  "NULL"),
    ("sp_ingest_created_utc",   "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
    ("sp_ingest_modified_utc",  "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
]
_SYSTEM_COLUMNS_PLAIN = [
    ("source_file_name",        "VARCHAR(255)",  "NULL"),
    ("sp_ingest_created_utc",   "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
    ("sp_ingest_modified_utc",  "DATETIME2(7)",  "NOT NULL  DEFAULT SYSUTCDATETIME()"),
]
_SKIP_FOLDER_NAMES = {"processed", "failed", "archive", "_archive", "_processed", "_failed"}
_DEFAULT_NOTIFICATION_TO = "NathanChapman@company715.onmicrosoft.com"
_DEFAULT_DEST_SCHEMA = "staging"

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
_BIT_VALUES = {"0", "1", "true", "false", "t", "f", "yes", "no", "y", "n"}


def _infer_series(series: pd.Series) -> str:
    """Return narrowest raw SQL type for a pandas Series.  VARCHAR returned as 'VARCHAR:<n>'."""
    non_null = series.dropna()
    if non_null.empty:
        return "VARCHAR(255)"

    dtype = series.dtype

    if pd.api.types.is_bool_dtype(dtype):
        return "BIT"
    if pd.api.types.is_integer_dtype(dtype):
        return "INT" if non_null.abs().max() <= 2_147_483_647 else "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        rounded = non_null.round(0)
        if (non_null - rounded).abs().max() < 1e-9:
            return "INT" if non_null.abs().max() <= 2_147_483_647 else "BIGINT"
        return "FLOAT"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        has_time = any(t.hour or t.minute or t.second for t in non_null.dt.time)
        return "DATETIME2(3)" if has_time else "DATE"

    # Object / string
    str_vals = non_null.astype(str)
    # Try datetime
    try:
        parsed = pd.to_datetime(str_vals, infer_datetime_format=True, errors="coerce")
        if parsed.notna().all():
            has_time = any(
                t.hour != 0 or t.minute != 0 or t.second != 0
                for t in parsed.dt.time
            )
            return "DATETIME2(3)" if has_time else "DATE"
    except Exception:
        pass
    # Try numeric
    try:
        nums = pd.to_numeric(str_vals, errors="coerce")
        if nums.notna().all():
            if (nums == nums.round(0)).all():
                return "INT" if nums.abs().max() <= 2_147_483_647 else "BIGINT"
            return "FLOAT"
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
        wa = la if la is not None else _fallback.get(a, 255)
        wb = lb if lb is not None else _fallback.get(b, 255)
        return f"VARCHAR:{max(wa, wb)}"
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


def _read_file_sheets(file_bytes: bytes, file_name: str) -> dict[str, pd.DataFrame]:
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
            return read_all_excel_sheets_from_bytes(file_bytes)
        print(f"    [SKIP] Unsupported file type: {file_name}")
        return {}
    except Exception as exc:
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

    _sys_excl = {"source_file_name", "excel_tab_name", "sp_ingest_created_utc", "sp_ingest_modified_utc"}

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
    to_email_address: str = _DEFAULT_NOTIFICATION_TO,
    cc_email_address: str = "",
    # Legacy aliases — forwarded to the new field names
    error_notification_email_address: str = "",
    error_notification_cc_email_address: str = "",
) -> str:
    def _s(v: str) -> str:
        v = str(v) if v else ""
        return "NULL" if not v else f"N'{v.replace(chr(39), chr(39)*2)}'"

    # Merge legacy aliases (caller may use either name)
    resolved_to = to_email_address or error_notification_email_address or _DEFAULT_NOTIFICATION_TO
    resolved_cc = cc_email_address or error_notification_cc_email_address
    resolved_column_mapping_json = (
        str(column_mapping_json).strip() if column_mapping_json is not None else ""
    )
    if not resolved_column_mapping_json:
        resolved_column_mapping_json = "{}"

    # For the integrated table, derive from staging if not supplied:
    # staging: staging.dest_X  →  integrated: staging.dest_X  (same schema/name, different DB)
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


def _build_profile_candidate(sp, fi) -> _ProfileCandidate | None:
    try:
        raw = sp.download_file_to_bytes(fi.server_relative_url)
    except Exception as exc:
        print(f"    [WARN] Download failed for '{fi.name}': {exc}")
        return None

    sheets = _read_file_sheets(raw, fi.name)
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
) -> None:
    archive_folder = f"{folder_server_relative_url}/Processed"
    failed_folder = f"{folder_server_relative_url}/Failed"

    if group.full_frames:
        try:
            full_df = pd.concat(group.full_frames, ignore_index=True)
            pk_info = _pk_inference(full_df, group.any_excel, col_raw_types=group.combined_profile)
        except Exception as exc:
            print(f"    [WARN] PK inference failed for group '{group.group_key}': {exc}")
            pk_info = _no_pk(0)
    else:
        pk_info = _no_pk(0)

    pk_cols = pk_info["pk_columns"]
    merge_key = ",".join(pk_cols)
    load_strategy = pk_info["strategy"]
    sys_cols = _SYSTEM_COLUMNS_EXCEL if group.any_excel else _SYSTEM_COLUMNS_PLAIN
    excel_tab_name_cfg = "ALL_SHEETS" if group.any_excel else ""

    # If we split by file, suffix destination table to avoid collisions.
    if len(group.file_names) == 1:
        suffix = _safe_suffix_from_file_name(group.file_names[0])
        table_basename = f"dest_{folder_safe_name}_{suffix}"
    else:
        table_basename = f"dest_{folder_safe_name}"
    staging_table = f"{dest_schema}.{table_basename}"

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
            data_columns=group.combined_profile,
            system_columns=sys_cols,
            pk_columns=pk_cols,
            padding=padding,
        )
    )

    print()
    print("-- " + "─" * 58)
    print("-- PK Inference Evidence")
    print("-- " + "─" * 58)
    if pk_cols:
        print(f"-- Suggested composite PK: {', '.join(pk_cols)}")
    else:
        print("-- No reliable PK found — review manually, PK omitted")
    print(f"-- Rows scanned:   {pk_info['rows_scanned']:,}")
    if pk_cols:
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
            file_name_pattern=group.file_name_pattern,
            error_notification_email_address=notification_to,
            error_notification_cc_email_address=notification_cc,
        )
    )
    print()


# ---------------------------------------------------------------------------
# Main discovery
# ---------------------------------------------------------------------------

def _build_sp_client(settings) -> SharePointClient:
    env_name = str(getattr(settings, "env_name", "") or "")
    provider = maybe_build_provider(settings.key_vault)
    if provider is not None:
        print("  [auth] Fetching SharePoint credentials from Key Vault …")
        client_id, client_secret, tenant_id = provider.get_sharepoint_credentials(env_name)
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
    )


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
) -> None:
    sep = "=" * 70
    print(sep)
    print("  SharePoint New Ingestion Discovery Tool")
    print(sep)

    # ------------------------------------------------------------------
    # 1. Settings + SQL
    # ------------------------------------------------------------------
    print(f"\n[1/5] Loading settings (env={env or 'auto'}) …")
    settings = load_settings(env_override=env)
    _assert_dev_only(getattr(settings, "env_name", ""))
    print(f"      SQL:             {settings.sql.host}/{settings.sql.database}")
    print(f"      SharePoint site: {settings.sharepoint.site_url}")
    sql = SqlClient(settings.sql)
    sql.test_connection()
    print("      SQL connection OK.")

    # ------------------------------------------------------------------
    # 2. Existing config rows
    # ------------------------------------------------------------------
    print("\n[2/5] Loading existing [config].[sharepoint_ingestion] rows …")
    existing_rows = sql.query_rows(
        "SELECT * FROM [config].[sharepoint_ingestion] ORDER BY id"
    )
    print(f"      Found {len(existing_rows)} row(s).")

    site_path = (urlparse(settings.sharepoint.site_url).path or "").rstrip("/")
    configured: set[str] = set()
    for row in existing_rows:
        configured.update(
            _configured_folder_keys(
                str(row.get("sharepoint_process_folder") or ""),
                site_path,
            )
        )
    print(f"      Normalized configured folder key(s): {len(configured)}")

    first = existing_rows[0] if existing_rows else None
    default_base_url = str(first.get("sharepoint_base_url") or "") if first else ""
    first_folder = str(first.get("sharepoint_process_folder") or "") if first else ""
    default_root = first_folder.rsplit("/", 1)[0] if "/" in first_folder else first_folder
    # Support both new (to_email_address/cc_email_address) and old column names
    default_notification_to = (
        str(first.get("to_email_address") or first.get("error_notification_email_address") or "").strip()
        if first
        else ""
    ) or _DEFAULT_NOTIFICATION_TO
    default_notification_cc = (
        str(first.get("cc_email_address") or first.get("error_notification_cc_email_address") or "").strip()
        if first
        else ""
    )

    # ------------------------------------------------------------------
    # 3. SharePoint connection
    # ------------------------------------------------------------------
    print("\n[3/5] Connecting to SharePoint …")
    sp = _build_sp_client(settings)
    print("      Connected.")

    scan_root = base_folder or default_root
    if not scan_root:
        print("\n[ERROR] Cannot determine SharePoint root folder.  Pass --base-folder.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Discover new folders (metadata only)
    # ------------------------------------------------------------------
    print(f"\n[4/5] Listing sub-folders under: {scan_root}")
    print("      (metadata-only — no file downloads)")
    sp_folders = _list_folders_to_depth(sp, scan_root, max_depth=3)
    print(f"      Found {len(sp_folders)} sub-folder(s) up to depth 3.")

    new_folders = [
        f for f in sp_folders
        if f.name.lower() not in _SKIP_FOLDER_NAMES
        and _norm(f.server_relative_url) not in configured
    ]

    if not new_folders:
        print("\n  All folders are already in [config].[sharepoint_ingestion].  Nothing to do.")
        return

    print(f"\n  {len(new_folders)} new (unconfigured) folder(s) found:")
    for f in new_folders:
        print(f"    - {f.server_relative_url}")

    # ------------------------------------------------------------------
    # 5. File count + optional profiling
    # ------------------------------------------------------------------
    print("\n[5/5] Checking file counts and profiling …")

    for sp_folder in new_folders:
        print(f"\n  Folder: {sp_folder.server_relative_url}")
        files = sp.list_files(sp_folder.server_relative_url)
        print(f"    Files found: {len(files)}")

        if len(files) < 1:
            print("    SKIP — no files present.")
            continue

        for fi in files:
            print(f"      - {fi.name}")

        folder_name = sp_folder.name
        safe_name = _snake_case_identifier_fragment(folder_name, fallback="folder")
        file_names = [fi.name for fi in files]
        excel_ingest = all(_is_excel(fi.name) for fi in files)

        print(f"\n  {sep}")
        print(f"  NEW INGESTION CANDIDATE: {folder_name}")
        print(f"  Folder path:    {sp_folder.server_relative_url}")
        print(f"  Files:          {len(files)}")
        print(f"  Type:           {'Excel (all-sheets)' if excel_ingest else 'CSV/Parquet'}")
        print("  Dest table:     (derived per discovered group)")
        print(sep)

        if no_profile:
            _print_stub(
                sp_folder=sp_folder,
                files=files,
                default_base_url=default_base_url,
                dest_schema=dest_schema,
                excel_ingest=excel_ingest,
                notification_to=default_notification_to,
                notification_cc=default_notification_cc,
            )
            continue

        print(f"\n  Profiling {len(files)} file(s) …")
        candidates: list[_ProfileCandidate] = []
        for fi in files:
            print(f"    Downloading: {fi.name}")
            cand = _build_profile_candidate(sp, fi)
            if cand is not None:
                candidates.append(cand)

        if not candidates:
            print("    [WARN] No usable data found.  Skipping.")
            continue

        groups = _build_discovery_groups(candidates)
        print(f"    Derived {len(groups)} ingestion recommendation group(s).")
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
            )

    print(f"\n{sep}")
    print("  Discovery complete.")
    print(sep)


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
    staging_table = f"{dest_schema}.dest_{safe_name}"
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
CREATE TABLE [{dest_schema}].[dest_{safe_name}] (
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
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse()
    discover(
        env=args.env,
        base_folder=args.base_folder,
        dest_schema=args.dest_schema,
        no_profile=args.no_profile,
        padding=args.padding,
    )




