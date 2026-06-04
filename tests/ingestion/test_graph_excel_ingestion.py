from __future__ import annotations

import logging

import pandas as pd
import pytest

from sharepoint_ingest.file_processors.excel_processor import _OLE2_MAGIC
from sharepoint_ingest.file_processors.graph_excel_processor import (
    dataframe_from_used_range_values,
    read_excel_via_graph,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    build_excel_payload,
    make_config,
    make_settings,
)


def test_dataframe_from_used_range_values_honors_skiprows_and_dedupes_headers() -> None:
    values = [
        ["ignored", "ignored"],
        ["id", "name", "name", ""],
        [1, "alpha", "a2", "blank-header"],
    ]

    df = dataframe_from_used_range_values(values, header_skip_rows=1)

    assert list(df.columns) == ["id", "name", "name.1", "Unnamed: 3"]
    assert df.to_dict(orient="records") == [
        {"id": 1, "name": "alpha", "name.1": "a2", "Unnamed: 3": "blank-header"}
    ]


def test_read_excel_via_graph_all_sheets_adds_excel_tab_name_and_closes_session() -> None:
    sp = DummySharePointClient(
        b"",
        graph_excel_sheets={
            "AU": [["id", "value"], [1, "a"]],
            "US": [["id", "value"], [2, "b"]],
        },
    )
    config = make_config()
    config.excel_tab_name = "ALL"

    df = read_excel_via_graph(sp, "/folder/protected.xlsx", config)

    assert list(df.columns) == ["id", "value", "excel_tab_name"]
    assert df.to_dict(orient="records") == [
        {"id": 1, "value": "a", "excel_tab_name": "AU"},
        {"id": 2, "value": "b", "excel_tab_name": "US"},
    ]
    assert sp.graph_sessions_created == 1
    assert sp.graph_sessions_closed == ["session-1"]


def test_read_excel_via_graph_regex_selects_matching_sheets() -> None:
    sp = DummySharePointClient(
        b"",
        graph_excel_sheets={
            "Customers_AU": [["id"], [1]],
            "Ignore": [["id"], [99]],
            "Customers_US": [["id"], [2]],
        },
    )
    config = make_config()
    config.excel_tab_name = "REGEX:^Customers_"

    df = read_excel_via_graph(sp, "/folder/protected.xlsx", config)

    assert list(df["id"]) == [1, 2]
    assert list(df["excel_tab_name"]) == ["Customers_AU", "Customers_US"]


def test_cloud_excel_only_skips_binary_download_and_loads_graph_dataframe() -> None:
    sp = DummySharePointClient(
        b"not-used",
        graph_excel_sheets={"Sheet1": [["id", "value"], [1, "a"], [2, "b"]]},
    )
    sql = DummySqlClient()
    settings = make_settings(chunked=False)
    settings.graph_excel_extraction_mode = "cloud_excel_only"
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/protected.xlsx", "protected.xlsx")

    assert rows == 2
    assert sp.download_bytes_calls == 0
    assert sp.graph_sessions_created == 1
    assert sp.graph_sessions_closed == ["session-1"]
    assert sql.calls == [("truncate_and_load", 2)]
    assert sp.moved_to == [("/folder/protected.xlsx", "/archive")]


def test_protected_auto_falls_back_to_graph_when_binary_payload_is_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sharepoint_ingest.file_processors.excel_processor as excel_processor

    monkeypatch.setattr(
        excel_processor,
        "_ole2_stream_names",
        lambda _payload: {"encryptioninfo", "encryptedpackage", "dataspaces/dataspaceinfo"},
    )
    sp = DummySharePointClient(
        _OLE2_MAGIC + b"encrypted-placeholder",
        graph_excel_sheets={"Sheet1": [["id"], [42]]},
    )
    sql = DummySqlClient()
    settings = make_settings(chunked=False)
    settings.graph_excel_extraction_mode = "protected_auto"
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/protected.xlsx", "protected.xlsx")

    assert rows == 1
    assert sp.download_bytes_calls == 1
    assert sp.graph_sessions_created == 1
    assert sql.calls == [("truncate_and_load", 1)]


def test_protected_auto_preserves_existing_binary_path_for_normal_workbooks() -> None:
    payload = build_excel_payload({"Sheet1": pd.DataFrame({"id": [1], "value": ["normal"]})})
    sp = DummySharePointClient(
        payload,
        graph_excel_sheets={"Sheet1": [["id"], [999]]},
    )
    sql = DummySqlClient()
    settings = make_settings(chunked=False)
    settings.graph_excel_extraction_mode = "protected_auto"
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/normal.xlsx", "normal.xlsx")

    assert rows == 1
    assert sp.download_bytes_calls == 1
    assert sp.graph_sessions_created == 0
    assert sql.calls == [("truncate_and_load", 1)]


def test_invalid_graph_excel_mode_raises_clear_error() -> None:
    settings = make_settings(chunked=False)
    settings.graph_excel_extraction_mode = "bad-mode"
    engine = IngestionEngine(settings, DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))

    with pytest.raises(ValueError, match="Invalid GRAPH_EXCEL_EXTRACTION_MODE"):
        engine._graph_excel_extraction_mode()