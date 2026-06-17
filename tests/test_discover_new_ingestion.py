from __future__ import annotations

import csv
import datetime
import io
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from sharepoint_ingest.config import (
    AppSettings,
    EmailSettings,
    KeyVaultSettings,
    SharePointSettings,
    SqlSettings,
)
from tools.discover_new_ingestion import (
    _DEFAULT_DEST_SCHEMA,
    _MAPPING_CSV_HEADER,
    _MAPPING_FIRST_ROW_COMMENT,
    _ProfileCandidate,
    _SYSTEM_COLUMNS_EXCEL,
    _SYSTEM_COLUMNS_PLAIN,
    _assert_dev_only,
    _build_discovery_groups,
    _col_type_with_nullability,
    _configured_folder_keys,
    _generate_config_insert,
    _generate_create_table,
    _generate_mapping_csv_rows,
    _list_folders_to_depth,
    _build_profile_candidate,
    _parse,
    _finalize_type,
    _infer_series,
    _merge_types,
    _print_group_sql,
    _read_file_sheets,
    _safe_suffix_from_file_name,
    _same_filename_family,
    _snake_case_identifier_fragment,
    _system_col_type_with_nullability,
)
import sharepoint_ingest.file_processors.excel_processor as excel_processor


def _candidate(file_name: str, *, cols: tuple[str, ...], kind: str = "excel") -> _ProfileCandidate:
    df = pd.DataFrame([{c: i for i, c in enumerate(cols, start=1)}])
    profile = {c: "INT" for c in cols}
    return _ProfileCandidate(
        file_name=file_name,
        server_relative_url=f"/dummy/{file_name}",
        file_kind=kind,
        normalized_business_columns=tuple(c.lower() for c in cols),
        combined_profile=profile,
        full_frames=[df],
        any_excel=(kind == "excel"),
    )


def test_same_filename_family_true_for_numbered_series() -> None:
    assert _same_filename_family(["file1.xlsx", "file2.xlsx"])
    assert _same_filename_family(["orders_202501.xlsx", "orders_202502.xlsx"])


def test_same_filename_family_false_for_unrelated_names() -> None:
    assert not _same_filename_family(["orders.xlsx", "items.xlsx"])


def test_build_groups_splits_same_layout_but_unrelated_names_to_single_file() -> None:
    c1 = _candidate("orders.xlsx", cols=("order_id", "amount"), kind="excel")
    c2 = _candidate("items.xlsx", cols=("order_id", "amount"), kind="excel")

    groups = _build_discovery_groups([c1, c2])

    assert len(groups) == 2
    assert all(g.multi_file_ingest == 0 for g in groups)
    assert sorted(g.file_name_pattern for g in groups) == ["items.xlsx", "orders.xlsx"]


def test_build_groups_keeps_multi_file_for_same_layout_same_family() -> None:
    c1 = _candidate("orders_202501.xlsx", cols=("order_id", "amount"), kind="excel")
    c2 = _candidate("orders_202502.xlsx", cols=("order_id", "amount"), kind="excel")

    groups = _build_discovery_groups([c1, c2])

    assert len(groups) == 1
    g = groups[0]
    assert g.multi_file_ingest == 1
    assert g.file_name_pattern == "orders_*.xlsx"


def test_build_groups_splits_different_layouts() -> None:
    c1 = _candidate("orders.xlsx", cols=("order_id", "amount"), kind="excel")
    c2 = _candidate("items.xlsx", cols=("item_id", "sku"), kind="excel")

    groups = _build_discovery_groups([c1, c2])

    assert len(groups) == 2
    assert all(g.multi_file_ingest == 0 for g in groups)


def test_read_file_sheets_skips_encrypted_excel_with_specific_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = excel_processor._OLE2_MAGIC + b"encrypted-placeholder"
    monkeypatch.setattr(
        excel_processor,
        "_ole2_stream_names",
        lambda _payload: {"encryptioninfo", "encryptedpackage"},
    )

    result = _read_file_sheets(payload, "protected.xlsx")

    assert result == {}
    output = capsys.readouterr().out
    assert "Skipping encrypted Excel file 'protected.xlsx'" in output


def test_read_file_sheets_skips_invalid_ole2_excel_with_specific_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = excel_processor._OLE2_MAGIC + b"invalid-placeholder"
    monkeypatch.setattr(
        excel_processor,
        "_ole2_stream_names",
        lambda _payload: {"worddocument"},
    )

    result = _read_file_sheets(payload, "bad.xlsx")

    assert result == {}
    output = capsys.readouterr().out
    assert "Skipping unreadable Excel file 'bad.xlsx'" in output
    assert "no BIFF Workbook/Book stream" in output


class _DiscoveryGraphStub:
    def __init__(self, payload: bytes, *, graph_excel_sheets: dict[str, list[list]] | None = None):
        self._payload = payload
        self.graph_excel_sheets = graph_excel_sheets or {}
        self.download_bytes_calls = 0
        self.graph_sessions_created = 0
        self.graph_sessions_closed: list[str] = []

    def download_file_to_bytes(self, server_relative_url: str) -> bytes:
        self.download_bytes_calls += 1
        return self._payload

    def create_excel_workbook_session(self, server_relative_url: str, *, persist_changes: bool = False) -> str:
        self.graph_sessions_created += 1
        return f"session-{self.graph_sessions_created}"

    def close_excel_workbook_session(self, server_relative_url: str, session_id: str) -> None:
        self.graph_sessions_closed.append(session_id)

    def list_excel_worksheets(self, server_relative_url: str, session_id: str) -> list[dict]:
        return [
            {"id": name, "name": name, "position": idx, "visibility": "Visible"}
            for idx, name in enumerate(self.graph_excel_sheets)
        ]

    def get_excel_used_range(
        self,
        server_relative_url: str,
        session_id: str,
        worksheet_id: str,
        *,
        values_only: bool = True,
    ) -> dict:
        return {"values": self.graph_excel_sheets[str(worksheet_id)]}


def test_build_profile_candidate_falls_back_to_graph_for_encrypted_excel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = excel_processor._OLE2_MAGIC + b"encrypted-placeholder"
    monkeypatch.setattr(
        excel_processor,
        "_ole2_stream_names",
        lambda _payload: {"encryptioninfo", "encryptedpackage"},
    )
    sp = _DiscoveryGraphStub(
        payload,
        graph_excel_sheets={
            "Sheet1": [["id", "value"], [1, "a"]],
            "Sheet2": [["id", "value"], [2, "b"]],
        },
    )
    fi = SimpleNamespace(name="protected.xlsx", server_relative_url="/folder/protected.xlsx")

    candidate = _build_profile_candidate(sp, fi)

    assert candidate is not None
    assert candidate.file_kind == "excel"
    assert candidate.any_excel is True
    assert candidate.combined_profile == {"id": "INT", "value": "VARCHAR:1"}
    assert sp.download_bytes_calls == 1
    assert sp.graph_sessions_created == 1
    assert sp.graph_sessions_closed == ["session-1"]
    output = capsys.readouterr().out
    assert "method=binary-download: starting" in output
    assert "method=binary-parse: detected encrypted/protected payload" in output
    assert "method=graph-workbook: attempting createSession" in output
    assert "method=graph-workbook: createSession success" in output
    assert "method=graph-workbook: success" in output


class _FakeGraphPermissionError(Exception):
    def __init__(self):
        super().__init__("403 Forbidden")
        self.response = SimpleNamespace(
            status_code=403,
            reason="Forbidden",
            text="{\"error\": {\"code\": \"accessDenied\"}}",
        )


class _ForbiddenGraphStub(_DiscoveryGraphStub):
    def create_excel_workbook_session(self, server_relative_url: str, *, persist_changes: bool = False) -> str:
        raise _FakeGraphPermissionError()


def test_read_file_sheets_prints_spn_permission_guidance_when_graph_fallback_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = excel_processor._OLE2_MAGIC + b"encrypted-placeholder"
    monkeypatch.setattr(
        excel_processor,
        "_ole2_stream_names",
        lambda _payload: {"encryptioninfo", "encryptedpackage"},
    )

    result = _read_file_sheets(
        payload,
        "protected.xlsx",
        sp=_ForbiddenGraphStub(payload),
        server_relative_url="/folder/protected.xlsx",
    )

    assert result == {}
    output = capsys.readouterr().out
    assert "Graph workbook extraction failed for 'protected.xlsx': HTTP 403" in output
    assert "SPN token is valid but is not authorised" in output
    assert "Graph application permissions/admin consent" in output
    assert "tools/diagnostics/graph_excel_probe.py --env dev" in output


@pytest.mark.parametrize("env_name", ["test", "prod", "", "DEVX"])
def test_assert_dev_only_rejects_non_dev(env_name: str) -> None:
    with pytest.raises(RuntimeError, match="DEV-only"):
        _assert_dev_only(env_name)


def test_assert_dev_only_accepts_dev_case_insensitive() -> None:
    _assert_dev_only("dev")
    _assert_dev_only("DEV")


def test_configured_folder_keys_keeps_server_relative_path() -> None:
    keys = _configured_folder_keys(
        "/sites/data_ingest_dev/Documents/valid_customers",
        "/sites/data_ingest_dev",
    )
    assert "/sites/data_ingest_dev/documents/valid_customers" in keys


def test_configured_folder_keys_expands_site_relative_path_with_site_prefix() -> None:
    keys = _configured_folder_keys("/Documents/valid_customers", "/sites/data_ingest_dev")
    assert "/documents/valid_customers" in keys
    assert "/sites/data_ingest_dev/documents/valid_customers" in keys


def test_configured_folder_keys_expands_relative_path_without_leading_slash() -> None:
    keys = _configured_folder_keys("Documents/valid_customers", "/sites/data_ingest_dev")
    assert "/documents/valid_customers" in keys
    assert "/sites/data_ingest_dev/documents/valid_customers" in keys


def test_generate_config_insert_includes_notification_to_and_cc_columns() -> None:
    sql = _generate_config_insert(
        sharepoint_base_url="https://example.sharepoint.com/sites/dev",
        sharepoint_process_folder="/Documents/source",
        sharepoint_process_archive_folder="/Documents/source/Processed",
        sharepoint_process_failed_folder="/Documents/source/Failed",
        staging_table_name="staging.dest_source",
        to_email_address="ops@example.com",
        cc_email_address="team@example.com;lead@example.com",
    )

    assert "[to_email_address]" in sql
    assert "[cc_email_address]" in sql
    assert "[column_mapping_json]" in sql
    assert "[integrated_table_name]" in sql
    assert "N'ops@example.com'" in sql
    assert "N'team@example.com;lead@example.com'" in sql
    assert "N'{}'" in sql


def test_generate_config_insert_renders_null_cc_when_blank() -> None:
    sql = _generate_config_insert(
        sharepoint_base_url="https://example.sharepoint.com/sites/dev",
        sharepoint_process_folder="/Documents/source",
        sharepoint_process_archive_folder="/Documents/source/Processed",
        sharepoint_process_failed_folder="/Documents/source/Failed",
        staging_table_name="staging.dest_source",
        to_email_address="ops@example.com",
        cc_email_address="",
    )

    assert "[cc_email_address]" in sql
    assert ",\n    NULL,\n" in sql


def test_generate_config_insert_defaults_blank_mapping_to_empty_json_object() -> None:
    sql = _generate_config_insert(
        sharepoint_base_url="https://example.sharepoint.com/sites/dev",
        sharepoint_process_folder="/Documents/source",
        sharepoint_process_archive_folder="/Documents/source/Processed",
        sharepoint_process_failed_folder="/Documents/source/Failed",
        staging_table_name="staging.dest_source",
        column_mapping_json="",
    )

    assert "[column_mapping_json]" in sql
    assert "N'{}'" in sql


def test_generate_config_insert_defaults_email_addresses_to_null() -> None:
    sql = _generate_config_insert(
        sharepoint_base_url="https://example.sharepoint.com/sites/dev",
        sharepoint_process_folder="/Documents/source",
        sharepoint_process_archive_folder="/Documents/source/Processed",
        sharepoint_process_failed_folder="/Documents/source/Failed",
        staging_table_name="staging.source",
    )

    assert "NathanChapman@company715.onmicrosoft.com" not in sql
    assert "N'{}',\n    NULL,\n    NULL," in sql


def test_snake_case_identifier_fragment_converts_camel_and_symbols() -> None:
    assert _snake_case_identifier_fragment("ValidCustomers", fallback="folder") == "valid_customers"
    assert _snake_case_identifier_fragment("Valid Customers - AU", fallback="folder") == "valid_customers_au"


def test_safe_suffix_from_file_name_returns_snake_case() -> None:
    assert _safe_suffix_from_file_name("CustomerTale.xlsx") == "customer_tale"
    assert _safe_suffix_from_file_name("Customer Tale - AU.xlsx") == "customer_tale_au"


class _FolderGraphStub:
    def __init__(self, graph: dict[str, list[str]]):
        self._graph = graph

    def list_folders(self, folder_server_relative_url: str):
        children = self._graph.get(folder_server_relative_url, [])
        return [
            SimpleNamespace(name=child.rsplit("/", 1)[-1], server_relative_url=child)
            for child in children
        ]


def test_list_folders_to_depth_1_only_direct_children() -> None:
    sp = _FolderGraphStub(
        {
            "/root": ["/root/a", "/root/b"],
            "/root/a": ["/root/a/a1"],
            "/root/b": ["/root/b/b1"],
        }
    )

    folders = _list_folders_to_depth(sp, "/root", max_depth=1)

    assert [f.server_relative_url for f in folders] == ["/root/a", "/root/b"]


def test_list_folders_to_depth_3_includes_great_grandchildren() -> None:
    sp = _FolderGraphStub(
        {
            "/root": ["/root/a"],
            "/root/a": ["/root/a/a1"],
            "/root/a/a1": ["/root/a/a1/a1x"],
            "/root/a/a1/a1x": ["/root/a/a1/a1x/too_deep"],
        }
    )

    folders = _list_folders_to_depth(sp, "/root", max_depth=3)

    assert [f.server_relative_url for f in folders] == [
        "/root/a",
        "/root/a/a1",
        "/root/a/a1/a1x",
    ]


def test_list_folders_to_depth_non_positive_returns_empty() -> None:
    sp = _FolderGraphStub({"/root": ["/root/a"]})

    assert _list_folders_to_depth(sp, "/root", max_depth=0) == []


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------


def test_infer_series_uses_decimal_for_simple_fractional_values() -> None:
    raw_type = _infer_series(pd.Series(["3.1", "3.2"]))

    assert raw_type == "DECIMAL:18:1"
    assert _finalize_type(raw_type) == "DECIMAL(18,1)"


def test_infer_series_uses_decimal_for_decimal_text_with_zero_fraction() -> None:
    raw_type = _infer_series(pd.Series(["1.0", "2.0"]))

    assert raw_type == "DECIMAL:18:1"
    assert _finalize_type(raw_type) == "DECIMAL(18,1)"


def test_infer_series_uses_decimal_for_float_values_with_zero_fraction() -> None:
    raw_type = _infer_series(pd.Series([1.0, 2.0]))

    assert raw_type == "DECIMAL:18:1"
    assert _finalize_type(raw_type) == "DECIMAL(18,1)"


def test_infer_series_uses_decimal_up_to_five_decimal_places() -> None:
    raw_type = _infer_series(pd.Series(["1.23456", "99.10000"]))

    assert raw_type == "DECIMAL:18:5"
    assert _finalize_type(raw_type) == "DECIMAL(18,5)"


def test_infer_series_uses_float_when_scale_exceeds_five_decimal_places() -> None:
    assert _infer_series(pd.Series(["1.234567", "2.000001"])) == "FLOAT"


def test_merge_types_promotes_int_and_decimal_to_decimal() -> None:
    raw_type = _merge_types("INT", "DECIMAL:18:2")

    assert raw_type == "DECIMAL:18:2"
    assert _finalize_type(raw_type) == "DECIMAL(18,2)"


def test_infer_series_detects_csv_datetime_text_with_double_space_and_ampm() -> None:
    raw_type = _infer_series(
        pd.Series(["4/1/2026  12:00:00 AM", "4/2/2026  01:30:00 PM"])
    )

    assert raw_type == "DATETIME2(3)"


def test_infer_series_detects_csv_date_only_text_as_date() -> None:
    raw_type = _infer_series(pd.Series(["4/1/2026", "4/2/2026"]))

    assert raw_type == "DATE"


def test_infer_series_detects_us_csv_datetime_hint_without_month_name_format() -> None:
    raw_type = _infer_series(
        pd.Series(["4/5/2026 0:00", "4/15/2026 0:00"], name="txn_date")
    )

    assert raw_type == "DATETIME2(3)"


def test_infer_series_force_all_us_dates_to_au_keeps_ambiguous_csv_dates_as_date() -> None:
    raw_type = _infer_series(
        pd.Series(["1/5/2026", "2/5/2026"], name="txn_date"),
        force_all_us_dates_to_au=True,
    )

    assert raw_type == "DATE"


def test_infer_series_detects_au_csv_date_hint_with_dash_separator() -> None:
    raw_type = _infer_series(pd.Series(["04-05-2026", "15-04-2026"], name="txn_date"))

    assert raw_type == "DATE"


def test_infer_series_keeps_mixed_free_text_date_column_as_varchar() -> None:
    raw_type = _infer_series(pd.Series(["4/15/2026", "about 4/16/2026"], name="notes"))

    assert raw_type.startswith("VARCHAR:")


def test_infer_series_keeps_conflicting_au_us_date_hints_as_varchar() -> None:
    raw_type = _infer_series(pd.Series(["15/04/2026", "4/16/2026"], name="txn_date"))

    assert raw_type.startswith("VARCHAR:")


def test_infer_series_keeps_name_like_columns_as_text_when_values_look_numeric() -> None:
    raw_type = _infer_series(pd.Series(["1.0", "2.0"], name="short_name"))

    assert raw_type == "VARCHAR:3"


# ---------------------------------------------------------------------------
# _col_type_with_nullability
# ---------------------------------------------------------------------------

def test_col_type_with_nullability_pk_is_not_null() -> None:
    result = _col_type_with_nullability("VARCHAR:80", is_pk=True, padding=0.20)
    assert result == "VARCHAR(100) NOT NULL"


def test_col_type_with_nullability_non_pk_is_null() -> None:
    result = _col_type_with_nullability("FLOAT", is_pk=False, padding=0.20)
    assert result == "FLOAT NULL"


def test_col_type_with_nullability_int_non_pk() -> None:
    result = _col_type_with_nullability("INT", is_pk=False, padding=0.20)
    assert result == "INT NULL"


# ---------------------------------------------------------------------------
# _system_col_type_with_nullability
# ---------------------------------------------------------------------------

def test_system_col_type_strips_default_expression() -> None:
    # The extra for sp_ingest_load_dt is "NOT NULL  DEFAULT SYSUTCDATETIME()"
    result = _system_col_type_with_nullability("DATETIME2(7)", "NOT NULL  DEFAULT SYSUTCDATETIME()")
    assert result == "DATETIME2(7) NOT NULL"


def test_system_col_type_null_extra() -> None:
    result = _system_col_type_with_nullability("VARCHAR(255)", "NULL")
    assert result == "VARCHAR(255) NULL"


def test_system_col_type_not_null_extra_only() -> None:
    result = _system_col_type_with_nullability("VARCHAR(100)", "NOT NULL")
    assert result == "VARCHAR(100) NOT NULL"


# ---------------------------------------------------------------------------
# _generate_mapping_csv_rows
# ---------------------------------------------------------------------------

_FIXED_DATE = datetime.date(2025, 3, 24)


def _parse_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def test_mapping_csv_has_correct_header() -> None:
    result = _generate_mapping_csv_rows(
        object_name="team_effort_register",
        source_object="team effort register",
        data_columns={"team_member": "VARCHAR:80"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    first_line = result.splitlines()[0]
    assert first_line == ",".join(_MAPPING_CSV_HEADER)


def test_mapping_csv_first_data_row_has_comment() -> None:
    result = _generate_mapping_csv_rows(
        object_name="team_effort_register",
        source_object="team effort register",
        data_columns={"team_member": "VARCHAR:80", "effort_spent": "FLOAT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    assert rows[0]["Comments"] == _MAPPING_FIRST_ROW_COMMENT
    assert rows[1]["Comments"] == ""


def test_mapping_csv_subsequent_rows_have_no_comment() -> None:
    result = _generate_mapping_csv_rows(
        object_name="my_object",
        source_object="my source",
        data_columns={"col_a": "INT", "col_b": "FLOAT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    # col_a → first business row → has comment; col_b → no comment; system cols → no comment
    assert rows[0]["Comments"] == _MAPPING_FIRST_ROW_COMMENT
    for row in rows[1:]:
        assert row["Comments"] == ""


def test_mapping_csv_object_and_source_object_in_every_row() -> None:
    result = _generate_mapping_csv_rows(
        object_name="team_effort_register",
        source_object="team effort register",
        data_columns={"col_a": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    for row in rows:
        assert row["Object name"] == "team_effort_register"
        assert row["Source object"] == "team effort register"


def test_mapping_csv_date_format_is_dd_mm_yyyy() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    assert rows[0]["Last update"] == "24/03/2025"


def test_mapping_csv_pk_column_marked_y() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"my_id": "INT", "name": "VARCHAR:50"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=["my_id"],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    pk_row = next(r for r in rows if r["Source column"] == "my_id")
    non_pk_row = next(r for r in rows if r["Source column"] == "name")
    assert pk_row["Primary key?"] == "Y"
    assert non_pk_row["Primary key?"] == ""


def test_mapping_csv_pk_column_has_not_null_type() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"my_id": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=["my_id"],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    pk_row = next(r for r in rows if r["Source column"] == "my_id")
    assert "NOT NULL" in pk_row["Staging type"]
    assert "NOT NULL" in pk_row["Integrated type"]


def test_mapping_csv_non_pk_column_has_null_type() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"description": "VARCHAR:50"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    row = next(r for r in rows if r["Source column"] == "description")
    assert "NULL" in row["Staging type"]
    assert "NOT NULL" not in row["Staging type"]


def test_mapping_csv_system_columns_appended_after_business_columns() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"biz_col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    # Business columns use Source column; system columns have Source column="" so use Staging column
    source_col_names = [r["Source column"] for r in rows]
    staging_col_names = [r["Staging column"] for r in rows]
    assert source_col_names.index("biz_col") < staging_col_names.index("source_file_name")
    assert "sp_ingest_load_dt" in staging_col_names
    assert "audit_id" in staging_col_names
    assert "__$batch_id" in staging_col_names
    assert "__$job_instance_id" in staging_col_names
    assert "sp_ingest_created_utc" not in staging_col_names
    assert "sp_ingest_modified_utc" not in staging_col_names


def test_mapping_csv_system_columns_strip_default_expression() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"biz_col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    # System columns have Source column="" — use Staging column to locate them
    load_dt_row = next(r for r in rows if r["Staging column"] == "sp_ingest_load_dt")
    # Source column must be empty (engine-managed, not present in source file)
    assert load_dt_row["Source column"] == ""
    # DEFAULT clause must NOT appear in the mapping type string
    assert "DEFAULT" not in load_dt_row["Staging type"]
    assert "DATETIME2(7) NOT NULL" == load_dt_row["Staging type"]


def test_mapping_csv_plain_system_columns_match_destination_conventions() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"biz_col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    by_staging_col = {r["Staging column"]: r for r in rows}

    assert by_staging_col["sp_ingest_load_dt"]["Staging type"] == "DATETIME2(7) NOT NULL"
    assert by_staging_col["audit_id"]["Staging type"] == "BIGINT NULL"
    assert by_staging_col["__$batch_id"]["Staging type"] == "INT NULL"
    assert by_staging_col["__$job_instance_id"]["Staging type"] == "INT NULL"
    assert "sp_ingest_created_utc" not in by_staging_col
    assert "sp_ingest_modified_utc" not in by_staging_col

    for col_name in ["sp_ingest_load_dt", "audit_id", "__$batch_id", "__$job_instance_id"]:
        assert by_staging_col[col_name]["Source column"] == ""


def test_mapping_csv_excel_system_columns_include_excel_tab_name() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"biz_col": "INT"},
        system_columns=_SYSTEM_COLUMNS_EXCEL,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    # System columns use Staging column (Source column is empty for engine-managed columns)
    staging_col_names = [r["Staging column"] for r in rows]
    assert "excel_tab_name" in staging_col_names


def test_default_dest_schema_is_sharepoint() -> None:
    assert _DEFAULT_DEST_SCHEMA == "sharepoint"


def test_parse_csv_mapping_rows_flag_defaults_false() -> None:
    args = _parse([])

    assert args.csv_mapping_rows_only is False


def test_parse_csv_mapping_rows_flag_enables_mapping_only_output() -> None:
    args = _parse(["--csv-mapping-rows"])

    assert args.csv_mapping_rows_only is True


def test_parse_force_all_us_dates_to_au_flag() -> None:
    args = _parse(["--force-all-us-dates-to-au"])

    assert args.force_all_us_dates_to_au is True


def test_build_profile_candidate_force_all_us_dates_to_au_profiles_csv_dates() -> None:
    payload = b"id,txn_date\n1,1/5/2026\n2,2/5/2026\n"
    sp = _DiscoveryGraphStub(payload)
    fi = SimpleNamespace(name="orders.csv", server_relative_url="/folder/orders.csv")

    candidate = _build_profile_candidate(
        sp,
        fi,
        force_all_us_dates_to_au=True,
    )

    assert candidate is not None
    assert candidate.file_kind == "csv"
    assert candidate.combined_profile["txn_date"] == "DATE"


def _print_group_sql_for_test(*, csv_mapping_rows_only: bool = False) -> None:
    group = _build_discovery_groups([
        _candidate("orders.xlsx", cols=("Order ID", "Order Amount"), kind="excel")
    ])[0]

    _print_group_sql(
        group=group,
        folder_name="Orders",
        folder_safe_name="orders",
        folder_server_relative_url="/sites/dev/Documents/Orders",
        default_base_url="https://example.sharepoint.com/sites/dev",
        dest_schema=_DEFAULT_DEST_SCHEMA,
        padding=0.20,
        all_file_names_in_folder=["orders.xlsx"],
        notification_to="",
        notification_cc="",
        csv_mapping_rows_only=csv_mapping_rows_only,
    )


def test_print_group_sql_default_omits_csv_mapping_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_group_sql_for_test()

    output = capsys.readouterr().out
    assert "CREATE TABLE" in output
    assert "INSERT INTO [config].[sharepoint_ingestion]" in output
    assert "PK Inference Evidence" in output
    assert "CSV mapping rows" not in output
    assert ",".join(_MAPPING_CSV_HEADER) not in output


def test_print_group_sql_uses_real_table_name_snake_case_columns_mapping_and_blank_emails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_group_sql_for_test()

    output = capsys.readouterr().out
    expected_mapping = json.dumps(
        {"Order ID": "order_id", "Order Amount": "order_amount"},
        separators=(",", ":"),
    )

    assert "-- Dest table:   sharepoint.orders_orders" in output
    assert "CREATE TABLE [sharepoint].[orders_orders]" in output
    assert "CONSTRAINT [PK_orders_orders] PRIMARY KEY CLUSTERED ([order_id])" in output
    assert "PK_dest_" not in output
    assert "sharepoint.dest_" not in output
    assert "[order_id]" in output
    assert "[order_amount]" in output
    assert "[Order ID]" not in output
    assert f"N'{expected_mapping}'" in output
    assert "-- Suggested merge_key_columns: order_id" in output
    assert f"N'{expected_mapping}',\n    NULL,\n    NULL," in output


def test_print_group_sql_csv_mapping_rows_flag_outputs_only_mapping_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_group_sql_for_test(csv_mapping_rows_only=True)

    output = capsys.readouterr().out
    assert output.startswith(",".join(_MAPPING_CSV_HEADER))
    assert "orders_orders" in output
    assert "Order ID" in output
    assert "order_id" in output
    assert "CREATE TABLE" not in output
    assert "INSERT INTO [config].[sharepoint_ingestion]" not in output
    assert "PK Inference Evidence" not in output
    assert "CSV mapping rows" not in output


def test_generate_create_table_uses_sharepoint_schema_and_managed_columns() -> None:
    sql = _generate_create_table(
        schema=_DEFAULT_DEST_SCHEMA,
        table_name="orders",
        data_columns={"order_id": "INT", "amount": "FLOAT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=["order_id"],
    )

    assert sql.startswith("CREATE TABLE [sharepoint].[orders] (")
    assert sql.endswith("\nWITH (DATA_COMPRESSION = PAGE);")
    assert "CONSTRAINT [PK_orders]" in sql
    assert "PK_dest_" not in sql
    assert "[sp_ingest_load_dt]" in sql
    assert "DATETIME2(7)" in sql
    assert "[audit_id]" in sql
    assert "BIGINT" in sql
    assert "[__$batch_id]" in sql
    assert "[__$job_instance_id]" in sql
    assert "[sp_ingest_created_utc]" not in sql
    assert "[sp_ingest_modified_utc]" not in sql


def test_mapping_csv_staging_and_integrated_types_are_identical() -> None:
    """Staging type and Integrated type should match for auto-generated rows."""
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"col_a": "VARCHAR:40", "col_b": "FLOAT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    for row in rows:
        assert row["Staging type"] == row["Integrated type"]


def test_mapping_csv_source_and_staging_column_names_are_identical() -> None:
    """Business columns: Source column == Staging column == Integrated column.
    System columns (engine-managed, not from source file): Source column is empty,
    Staging column == Integrated column (following the column_mapping_json pattern
    where system columns are never keys in the source→destination mapping).
    """
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"my_col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    sys_col_names_lower = {c[0].lower() for c in _SYSTEM_COLUMNS_PLAIN}
    for row in rows:
        if row["Staging column"].lower() in sys_col_names_lower:
            # System columns: no source-file key → Source column is blank
            assert row["Source column"] == ""
            assert row["Staging column"] == row["Integrated column"]
        else:
            # Business columns: identity source→dest mapping
            assert row["Source column"] == row["Staging column"] == row["Integrated column"]


def test_mapping_csv_transform_column_is_blank() -> None:
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    for row in rows:
        assert row["Transform"] == ""


def test_mapping_csv_defaults_date_to_today_when_not_supplied() -> None:
    """When as_of_date is omitted, today's date is used – just check the format."""
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns={"col": "INT"},
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
    )
    rows = _parse_csv(result)
    date_val = rows[0]["Last update"]
    # Must be DD/MM/YYYY (10 chars, two slashes at positions 2 and 5)
    assert len(date_val) == 10
    assert date_val[2] == "/" and date_val[5] == "/"


def test_mapping_csv_system_columns_not_duplicated_when_in_data_columns() -> None:
    """If a profiled data dict somehow contains a system column name, it must not
    appear twice – the system columns list takes precedence and the data entry
    is skipped.

    System columns have Source column="" so use Staging column for the uniqueness check.
    """
    data_with_sys = {
        "biz_col": "INT",
        "source_file_name": "VARCHAR:200",  # duplicate of a system column
    }
    result = _generate_mapping_csv_rows(
        object_name="obj",
        source_object="src",
        data_columns=data_with_sys,
        system_columns=_SYSTEM_COLUMNS_PLAIN,
        pk_columns=[],
        as_of_date=_FIXED_DATE,
    )
    rows = _parse_csv(result)
    # source_file_name is a system column → Source column is "", locate via Staging column
    source_file_rows = [r for r in rows if r["Staging column"] == "source_file_name"]
    assert len(source_file_rows) == 1, "source_file_name should appear exactly once"
    # Confirm Source column is empty (engine-managed, not a key in column_mapping_json)
    assert source_file_rows[0]["Source column"] == ""


# ---------------------------------------------------------------------------
# Regression: discover() must resolve DB names from KV before building SqlClient
# ---------------------------------------------------------------------------

def _make_sql(database: str = "") -> SqlSettings:
    return SqlSettings(
        host="localhost",
        port=1433,
        username="",
        password="",
        auth_mode="sspi",
        database=database,
        odbc_driver="ODBC Driver 18 for SQL Server",
        trust_server_certificate=True,
    )


def _make_app_settings(*, env_name: str = "dev", aud_db: str = "") -> AppSettings:
    kv = KeyVaultSettings(
        vault_name="kv-sp-ingest-dev",
        vault_url="https://kv-sp-ingest-dev.vault.azure.net/",
        client_id_secret_name="dm-sharepoint-dev-client-id",
        client_secret_secret_name="dm-sharepoint-dev-client-secret",
        tenant_id_secret_name="dm-sharepoint-dev-tenant-id",
        site_url_secret_name="dm-sharepoint-dev-site-url",
        sql_server_secret_name="dm-sql-dev-server",
        sql_int_database_secret_name="dm-sql-dev-int-database",
        sql_stg_database_secret_name="dm-sql-dev-stg-database",
        sql_aud_database_secret_name="dm-sql-dev-aud-database",
        sql_username_secret_name=None,
        sql_password_secret_name=None,
    )
    return AppSettings(
        env_name=env_name,
        log_level="INFO",
        allow_test_data_in_prod=False,
        default_load_strategy="TRUNCATE",
        default_file_pattern="*",
        null_alert_threshold=0.9,
        enable_chunked_csv=False,
        enable_chunked_parquet=True,
        ingest_chunk_size_rows=5000,
        azure_subscription_id=None,
        azure_resource_group=None,
        sql=_make_sql(database=aud_db),
        sql_stg=_make_sql(),
        sql_int=_make_sql(),
        key_vault=kv,
        sharepoint=SharePointSettings(
            site_url="https://mycompany.sharepoint.com/sites/data_ingest_dev",
            admin_url=None,
        ),
        email=EmailSettings(
            enabled=False,
            host=None,
            port=587,
            username=None,
            password=None,
            use_tls=True,
            from_address="test@example.com",
        ),
    )


def test_discover_resolves_database_before_sql_client_construction() -> None:
    """discover() must call _resolve_database_names before constructing SqlClient.

    Regression for the bug where load_settings() leaves sql.database="" and the
    tool attempted to connect with an empty database name, causing a driver error.
    """
    resolved_settings = _make_app_settings(aud_db="ingest_audit_dev")

    captured_sql_settings: list = []

    def _fake_sql_client(sql_settings, **kwargs):
        captured_sql_settings.append(sql_settings)
        mock = MagicMock()
        mock.test_connection.return_value = None
        mock.query_rows.return_value = []
        return mock

    import tools.discover_new_ingestion as _disc

    with (
        patch.object(_disc, "load_settings", return_value=_make_app_settings(aud_db="")),
        patch.object(_disc, "maybe_build_provider", return_value=None),
        patch.object(
            _disc,
            "_resolve_database_names",
            return_value=resolved_settings,
        ) as mock_resolve_db,
        patch.object(
            _disc,
            "_resolve_sql_settings",
            return_value=resolved_settings.sql,
        ),
        patch.object(_disc, "SqlClient", side_effect=_fake_sql_client),
        # _build_sp_client raises before SharePoint is touched — stop early
        patch.object(_disc, "_build_sp_client", side_effect=SystemExit(0)),
    ):
        try:
            _disc.discover(env="dev")
        except SystemExit:
            pass

    # _resolve_database_names must have been called (not skipped)
    mock_resolve_db.assert_called_once()

    # SqlClient must have been constructed with the *resolved* (non-empty) db name
    assert captured_sql_settings, "SqlClient was never constructed"
    assert captured_sql_settings[0].database == "ingest_audit_dev", (
        f"Expected 'ingest_audit_dev' but got '{captured_sql_settings[0].database}'. "
        "discover() appears to be building SqlClient before resolving DB names from KV."
    )


def test_discover_passes_resolved_credentials_to_sql_client() -> None:
    """For credential-based auth modes, _resolve_sql_settings must be called and
    its result (with injected credentials) must be used to build SqlClient."""
    base_settings = _make_app_settings(aud_db="")
    # Simulate load_settings returning settings without a database name (placeholder)
    cred_sql = replace(
        base_settings.sql,
        database="ingest_audit_dev",
        username="kv_user",
        password="kv_pass",
        auth_mode="sql_password",
    )

    captured_sql_settings: list = []

    def _fake_sql_client(sql_settings, **kwargs):
        captured_sql_settings.append(sql_settings)
        mock = MagicMock()
        mock.test_connection.return_value = None
        mock.query_rows.return_value = []
        return mock

    import tools.discover_new_ingestion as _disc

    resolved_settings = replace(base_settings, sql=cred_sql)

    with (
        patch.object(_disc, "load_settings", return_value=base_settings),
        patch.object(_disc, "maybe_build_provider", return_value=MagicMock()),
        patch.object(_disc, "_resolve_database_names", return_value=resolved_settings),
        patch.object(_disc, "_resolve_sql_settings", return_value=cred_sql),
        patch.object(_disc, "SqlClient", side_effect=_fake_sql_client),
        patch.object(_disc, "_build_sp_client", side_effect=SystemExit(0)),
    ):
        try:
            _disc.discover(env="dev")
        except SystemExit:
            pass

    assert captured_sql_settings, "SqlClient was never constructed"
    used = captured_sql_settings[0]
    assert used.username == "kv_user"
    assert used.password == "kv_pass"
    assert used.database == "ingest_audit_dev"


def test_build_sp_client_prefers_key_vault_site_url_over_env_fallback() -> None:
    """Discovery should mirror main.py: KV site-url beats SHAREPOINT_SITE_URL_DEV."""
    import tools.discover_new_ingestion as _disc

    settings = replace(
        _make_app_settings(aud_db="ingest_audit_dev"),
        sharepoint=SharePointSettings(
            site_url="https://wrong.example.sharepoint.com/sites/from-env",
            admin_url=None,
        ),
    )
    provider = MagicMock()
    provider.get_sharepoint_credentials.return_value = ("client-id", "client-secret", "tenant-id")
    provider.get_secret.return_value = "https://contoso.sharepoint.com/sites/data_ingest_dev"

    captured_site_urls: list[str] = []

    def _fake_sp_client(*, site_url: str, client_id: str, client_secret: str, tenant_id: str):
        captured_site_urls.append(site_url)
        return SimpleNamespace(site_url=site_url)

    with (
        patch.object(_disc, "maybe_build_provider", return_value=provider),
        patch.object(_disc, "SharePointClient", side_effect=_fake_sp_client),
    ):
        _sp, patched_settings = _disc._build_sp_client(settings)

    provider.get_secret.assert_called_once_with("dm-sharepoint-dev-site-url")
    assert patched_settings.sharepoint.site_url == "https://contoso.sharepoint.com/sites/data_ingest_dev"
    assert captured_site_urls == ["https://contoso.sharepoint.com/sites/data_ingest_dev"]


def test_discover_generated_insert_uses_resolved_site_url_not_first_config_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """New config SQL must not copy stale sharepoint_base_url from row id=1."""
    import tools.discover_new_ingestion as _disc

    resolved_site_url = "https://contoso.sharepoint.com/sites/data_ingest_dev"
    stale_sql_url = "https://wrong.example.sharepoint.com/sites/stale"

    settings = replace(
        _make_app_settings(aud_db="ingest_audit_dev"),
        sharepoint=SharePointSettings(site_url=resolved_site_url, admin_url=None),
    )

    sql_mock = MagicMock()
    sql_mock.test_connection.return_value = None
    sql_mock.query_rows.return_value = [
        {
            "id": 1,
            "sharepoint_base_url": stale_sql_url,
            "sharepoint_process_folder": "/sites/data_ingest_dev/Documents/Existing",
        }
    ]

    sp_mock = MagicMock()
    sp_mock.list_folders.return_value = [
        SimpleNamespace(
            name="New Folder",
            server_relative_url="/sites/data_ingest_dev/Documents/New Folder",
        )
    ]
    sp_mock.list_files.return_value = [
        SimpleNamespace(
            name="orders.csv",
            server_relative_url="/sites/data_ingest_dev/Documents/New Folder/orders.csv",
        )
    ]

    with (
        patch.object(_disc, "load_settings", return_value=settings),
        patch.object(_disc, "maybe_build_provider", return_value=MagicMock()),
        patch.object(_disc, "_resolve_database_names", return_value=settings),
        patch.object(_disc, "_resolve_sql_settings", return_value=settings.sql),
        patch.object(_disc, "SqlClient", return_value=sql_mock),
        patch.object(_disc, "_build_sp_client", return_value=(sp_mock, settings)),
        patch.object(
            _disc,
            "_build_profile_candidate",
            return_value=_candidate("orders.csv", cols=("order_id", "amount"), kind="csv"),
        ),
    ):
        _disc.discover(env="dev")

    output = capsys.readouterr().out
    assert f"N'{resolved_site_url}'" in output
    assert stale_sql_url not in output
