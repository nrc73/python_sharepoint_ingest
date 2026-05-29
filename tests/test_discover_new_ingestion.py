from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from tools.discover_new_ingestion import (
    _ProfileCandidate,
    _assert_dev_only,
    _build_discovery_groups,
    _configured_folder_keys,
    _generate_config_insert,
    _list_folders_to_depth,
    _safe_suffix_from_file_name,
    _snake_case_identifier_fragment,
    _same_filename_family,
)


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




