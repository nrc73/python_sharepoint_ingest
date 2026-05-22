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

Usage
-----
    python tools/discover_new_ingestion.py [--env dev|test|prod]
                                           [--base-folder PATH]
                                           [--dest-schema dbo]
                                           [--no-profile]
                                           [--padding 0.20]
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import re
import sys
import uuid
from collections import defaultdict
from io import BytesIO
from typing import Any

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
    excel_tab_name: str = "",
    process_frequency: str = "DAILY",
    header_skip_rows: int = 0,
    multi_file_ingest: int = 1,
    check_source_dest_columns: int = 1,
    is_active: int = 1,
    ingestion_scope: str = "REAL",
    load_strategy: str = "TRUNCATE",
    merge_key_columns: str = "",
    file_name_pattern: str = "",
    ingestion_domain: str = "",
) -> str:
    def _s(v: str) -> str:
        v = str(v) if v else ""
        return "NULL" if not v else f"N'{v.replace(chr(39), chr(39)*2)}'"

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
    [is_active],
    [ingestion_scope],
    [ingestion_domain],
    [file_name_pattern],
    [load_strategy],
    [merge_key_columns],
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
    {is_active},
    {_s(ingestion_scope)},
    {_s(ingestion_domain)},
    {_s(file_name_pattern)},
    {_s(load_strategy)},
    {_s(merge_key_columns)},
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


def discover(
    env: str | None = None,
    base_folder: str | None = None,
    dest_schema: str = "dbo",
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

    configured = {
        _norm(str(r.get("sharepoint_process_folder") or ""))
        for r in existing_rows
        if str(r.get("sharepoint_process_folder") or "").strip()
    }

    first = existing_rows[0] if existing_rows else None
    default_base_url = str(first.get("sharepoint_base_url") or "") if first else ""
    first_folder = str(first.get("sharepoint_process_folder") or "") if first else ""
    default_root = first_folder.rsplit("/", 1)[0] if "/" in first_folder else first_folder

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
    sp_folders = sp.list_folders(scan_root)
    print(f"      Found {len(sp_folders)} sub-folder(s).")

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
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", folder_name)
        staging_table = f"{dest_schema}.dest_{safe_name}"
        archive_folder = f"{sp_folder.server_relative_url}/Processed"
        failed_folder = f"{sp_folder.server_relative_url}/Failed"
        file_names = [fi.name for fi in files]
        file_pattern = _derive_file_pattern(file_names)
        excel_ingest = all(_is_excel(fi.name) for fi in files)

        print(f"\n  {sep}")
        print(f"  NEW INGESTION CANDIDATE: {folder_name}")
        print(f"  Folder path:    {sp_folder.server_relative_url}")
        print(f"  Files:          {len(files)}")
        print(f"  Type:           {'Excel (all-sheets)' if excel_ingest else 'CSV/Parquet'}")
        print(f"  Dest table:     {staging_table}")
        print(sep)

        if no_profile:
            _print_stub(
                sp_folder=sp_folder,
                files=files,
                default_base_url=default_base_url,
                dest_schema=dest_schema,
                excel_ingest=excel_ingest,
            )
            continue

        # ------------------------------------------------------------------
        # Download + profile
        # ------------------------------------------------------------------
        # combined_profile: {col_name: raw_type}
        combined_profile: dict[str, str] = {}
        # full_df: concatenated dataframe of ALL rows from ALL files/sheets
        full_frames: list[pd.DataFrame] = []
        any_excel = False

        print(f"\n  Profiling {len(files)} file(s) …")
        for fi in files:
            print(f"    Downloading: {fi.name}")
            try:
                raw = sp.download_file_to_bytes(fi.server_relative_url)
            except Exception as exc:
                print(f"    [WARN] Download failed for '{fi.name}': {exc}")
                continue

            sheets = _read_file_sheets(raw, fi.name)
            if not sheets:
                continue

            is_this_excel = _is_excel(fi.name)
            if is_this_excel:
                any_excel = True

            for sheet_key, df in sheets.items():
                # Attach excel_tab_name column for full-data PK testing
                if is_this_excel and len(sheets) > 1:
                    df = df.copy()
                    df["excel_tab_name"] = sheet_key
                elif is_this_excel and len(sheets) == 1:
                    df = df.copy()
                    df["excel_tab_name"] = sheet_key

                # Attach source_file_name for PK testing
                df = df.copy()
                df["source_file_name"] = fi.name

                full_frames.append(df)
                prof = _profile_df(df.drop(columns=["source_file_name", "excel_tab_name"],
                                           errors="ignore"))
                combined_profile = _merge_profiles(combined_profile, prof)

        if not combined_profile and not full_frames:
            print("    [WARN] No usable data found.  Skipping.")
            continue

        # ------------------------------------------------------------------
        # Concatenate all data for PK inference
        # ------------------------------------------------------------------
        if full_frames:
            print(f"    Building combined dataset for PK analysis …")
            try:
                full_df = pd.concat(full_frames, ignore_index=True)
                total_rows = len(full_df)
                print(f"    Total rows scanned: {total_rows:,}")
                pk_info = _pk_inference(full_df, any_excel, col_raw_types=combined_profile)
            except Exception as exc:
                print(f"    [WARN] PK inference failed: {exc}")
                pk_info = _no_pk(0)
        else:
            pk_info = _no_pk(0)

        # ------------------------------------------------------------------
        # Determine system columns
        # ------------------------------------------------------------------
        sys_cols = _SYSTEM_COLUMNS_EXCEL if any_excel else _SYSTEM_COLUMNS_PLAIN
        excel_tab_name_cfg = "ALL_SHEETS" if any_excel else ""

        # ------------------------------------------------------------------
        # Print output
        # ------------------------------------------------------------------
        pk_cols = pk_info["pk_columns"]
        merge_key = ",".join(pk_cols)
        load_strategy = pk_info["strategy"]

        print(f"\n{'─'*60}")
        print("-- " + "─" * 58)
        print(f"-- CREATE TABLE for new folder: {folder_name}")
        print(f"-- Dest table:  {staging_table}")
        print(f"-- Type:        {'Excel (all-sheets merged)' if any_excel else 'CSV/Parquet'}")
        print("-- " + "─" * 58)
        print()
        print(
            _generate_create_table(
                schema=dest_schema,
                table_name=f"dest_{safe_name}",
                data_columns=combined_profile,
                system_columns=sys_cols,
                pk_columns=pk_cols,
                padding=padding,
            )
        )

        # PK evidence comment
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
        print()

        print("-- " + "─" * 58)
        print("-- INSERT INTO [config].[sharepoint_ingestion]")
        print("-- " + "─" * 58)
        print(
            _generate_config_insert(
                sharepoint_base_url=default_base_url,
                sharepoint_process_folder=sp_folder.server_relative_url,
                sharepoint_process_archive_folder=archive_folder,
                sharepoint_process_failed_folder=failed_folder,
                staging_table_name=staging_table,
                excel_tab_name=excel_tab_name_cfg,
                multi_file_ingest=1 if len(files) > 1 else 1,
                check_source_dest_columns=1,
                is_active=1,
                ingestion_scope="REAL",
                load_strategy=load_strategy,
                merge_key_columns=merge_key,
                file_name_pattern=file_pattern,
            )
        )
        print()

    print(f"\n{sep}")
    print("  Discovery complete.")
    print(sep)


def _print_stub(*, sp_folder, files, default_base_url, dest_schema, excel_ingest):
    folder_name = sp_folder.name
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", folder_name)
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
    p.add_argument("--env", default=None)
    p.add_argument("--base-folder", default=None, dest="base_folder")
    p.add_argument("--dest-schema", default="dbo", dest="dest_schema")
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
