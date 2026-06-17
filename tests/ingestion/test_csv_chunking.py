"""Tests for CSV chunked ingestion path."""
from __future__ import annotations

import logging

import pytest

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    make_config,
    make_engine,
    make_settings,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine


def test_chunked_csv_truncate_then_append() -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("truncate_and_load", 2), ("append_load", 1)]
    assert sp.moved_to == [("/folder/file.csv", "/archive")]


def test_chunked_csv_append_strategy() -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(make_config("APPEND"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("append_load", 2), ("append_load", 1)]


def test_chunked_csv_handles_mixed_quoted_and_unquoted_fields() -> None:
    payload = (
        b"id,name,description\n"
        b"1,alpha,simple\n"
        b"2,beta,\"long string, with comma and \"\"quoted\"\" text\"\n"
        b"3,gamma,after\n"
    )
    sp = DummySharePointClient(payload)

    class CapturingSqlClient(DummySqlClient):
        def __init__(self):
            super().__init__()
            self.loaded_chunks = []

        def truncate_and_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().truncate_and_load(df, table_name)

        def append_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().append_load(df, table_name)

    sql = CapturingSqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("truncate_and_load", 2), ("append_load", 1)]
    first_chunk = sql.loaded_chunks[0]
    assert first_chunk.loc[1, "description"] == 'long string, with comma and "quoted" text'


def test_chunked_csv_datetime_uses_full_file_us_hint_before_first_chunk_loads() -> None:
    payload = (
        b"id,txn_date\n"
        b"1,04/05/2026 0:00\n"  # ambiguous: US hint later should make this 5-Apr, not 4-May
        b"2,4/15/2026 0:00\n"
    )
    sp = DummySharePointClient(payload)

    class CapturingSqlClient(DummySqlClient):
        def __init__(self):
            super().__init__()
            self.loaded_chunks = []

        def get_table_columns(self, table_name: str):
            return [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "txn_date", "data_type": "datetime"},
            ]

        def truncate_and_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().truncate_and_load(df, table_name)

        def append_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().append_load(df, table_name)

    sql = CapturingSqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=1), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 2
    assert str(sql.loaded_chunks[0].loc[0, "txn_date"]) == "2026-04-05 00:00:00"
    assert str(sql.loaded_chunks[1].loc[1, "txn_date"]) == "2026-04-15 00:00:00"


def test_chunked_csv_force_all_us_dates_to_au_applies_before_first_chunk_loads() -> None:
    payload = (
        b"id,txn_date\n"
        b"1,1/5/2026\n"
        b"2,2/5/2026\n"
    )
    sp = DummySharePointClient(payload)

    class CapturingSqlClient(DummySqlClient):
        def __init__(self):
            super().__init__()
            self.loaded_chunks = []

        def get_table_columns(self, table_name: str):
            return [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "txn_date", "data_type": "datetime"},
            ]

        def truncate_and_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().truncate_and_load(df, table_name)

        def append_load(self, df, table_name: str) -> None:
            self.loaded_chunks.append(df.copy())
            super().append_load(df, table_name)

    sql = CapturingSqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=1), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(
        make_config("TRUNCATE"),
        "/folder/file.csv",
        "file.csv",
        force_all_us_dates_to_au=True,
    )

    assert rows == 2
    assert str(sql.loaded_chunks[0].loc[0, "txn_date"].date()) == "2026-01-05"
    assert str(sql.loaded_chunks[1].loc[1, "txn_date"].date()) == "2026-02-05"


def test_chunked_csv_empty_file_truncate_reload_still_truncates() -> None:
    payload = b"id,value\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(
        make_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test")
    )

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 0
    assert sql.calls == [("truncate_and_load", 0)]


# ── Per-chunk schema validation ──────────────────────────────────────────────

def test_chunked_csv_schema_error_in_chunk2_aborts_after_chunk1_loads() -> None:
    """Schema validation runs on every chunk.

    Chunk 1 passes validation and is loaded immediately.  Chunk 2 contains a
    value that exceeds the destination column length, so validation fails and
    the chunk is NOT loaded — mirroring the Parquet single-pass behavior.
    """
    # chunk_size=2: chunk-0 = ["ok", "fit"] (pass), chunk-1 = ["value-too-long"] (fail)
    payload = b"name\nok\nfit\nvalue-too-long-for-destination\n"
    sp = DummySharePointClient(payload)

    class ValidatingSqlClient(DummySqlClient):
        def get_table_columns(self, table_name: str):
            return [{"column_name": "name", "data_type": "nvarchar", "character_maximum_length": 5}]

    sql = ValidatingSqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))
    config = make_config("TRUNCATE")
    config.check_source_dest_columns = True

    with pytest.raises(ValueError, match="Schema validation failed"):
        engine._process_single_file(config, "/folder/file.csv", "file.csv")

    load_calls = [(c[0], c[1]) for c in sql.calls if c[0] in ("truncate_and_load", "append_load")]
    # Chunk 1 (2 rows) is loaded; chunk 2 is blocked before its load call.
    assert len(load_calls) == 1
    assert load_calls[0] == ("truncate_and_load", 2)


def test_chunked_csv_schema_error_in_chunk1_aborts_before_any_load() -> None:
    """A schema error on the very first chunk prevents any SQL write."""
    payload = b"name\nvalue-too-long-for-destination\nok\n"
    sp = DummySharePointClient(payload)

    class ValidatingSqlClient(DummySqlClient):
        def get_table_columns(self, table_name: str):
            return [{"column_name": "name", "data_type": "nvarchar", "character_maximum_length": 5}]

    sql = ValidatingSqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))
    config = make_config("TRUNCATE")
    config.check_source_dest_columns = True

    with pytest.raises(ValueError, match="Schema validation failed"):
        engine._process_single_file(config, "/folder/file.csv", "file.csv")

    load_calls = [c[0] for c in sql.calls if c[0] in ("truncate_and_load", "append_load")]
    assert load_calls == []


def test_chunked_csv_schema_valid_all_chunks_loads_all() -> None:
    """When all chunks pass schema validation, all rows are loaded."""
    payload = b"name\nok\nfit\nhi\nno\n"
    sp = DummySharePointClient(payload)

    class ValidatingSqlClient(DummySqlClient):
        def get_table_columns(self, table_name: str):
            return [{"column_name": "name", "data_type": "nvarchar", "character_maximum_length": 5}]

    sql = ValidatingSqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))
    config = make_config("TRUNCATE")
    config.check_source_dest_columns = True

    rows = engine._process_single_file(config, "/folder/file.csv", "file.csv")

    assert rows == 4
    load_calls = [(c[0], c[1]) for c in sql.calls if c[0] in ("truncate_and_load", "append_load")]
    assert load_calls == [("truncate_and_load", 2), ("append_load", 2)]
