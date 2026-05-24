from __future__ import annotations

import pandas as pd
import pytest

from tools.discover_new_ingestion import (
    _ProfileCandidate,
    _assert_dev_only,
    _build_discovery_groups,
    _configured_folder_keys,
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
