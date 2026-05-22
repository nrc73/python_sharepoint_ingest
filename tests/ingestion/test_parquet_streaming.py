"""Tests for Parquet single-pass streaming ingestion path."""
from __future__ import annotations

import logging
from io import BytesIO

import pandas as pd
import pytest

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    make_config,
    make_settings,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine, MAX_PARQUET_FILE_SIZE_BYTES


def _make_parquet_bytes(rows: int = 5) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"id": list(range(rows)), "value": [f"v{i}" for i in range(rows)]})
    buf = BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


class _OversizedSharePointClient(DummySharePointClient):
    """Pretends every file is 550 MiB — above the 512 MiB hard cap."""
    REPORTED_SIZE = 550 * 1024 * 1024

    def get_file_item(self, server_relative_url: str) -> dict:
        return {"size": self.REPORTED_SIZE, "@microsoft.graph.downloadUrl": None}


def test_chunked_parquet_truncate_single_pass_uses_temp_then_swap() -> None:
    """Single-pass Parquet: chunks go into a temp table, then one atomic swap."""
    payload = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    settings.enable_chunked_parquet = True
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.parquet", "file.parquet")

    assert rows == 3
    assert sql.calls == [
        ("load_chunk_to_temp", 2),
        ("load_chunk_to_temp", 1),
        ("swap_temp_to_destination", "TRUNCATE"),
    ]
    assert sp.moved_to == [("/folder/file.parquet", "/archive")]


def test_non_chunked_parquet_append_strategy_loads_once() -> None:
    payload = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    settings = make_settings(chunked=False, chunk_size=2)
    settings.enable_chunked_parquet = False
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("APPEND"), "/folder/file.parquet", "file.parquet")

    assert rows == 3
    assert sql.calls == [("append_load", 3)]


def test_chunked_parquet_schema_validation_aborts_before_destination_swap() -> None:
    """Blocking schema errors prevent the atomic swap from firing."""
    payload = pd.DataFrame({"name": ["ok", "fit", "value-too-long-for-destination"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)

    class ValidatingSqlClient(DummySqlClient):
        def get_table_columns(self, table_name: str):
            return [{"column_name": "name", "data_type": "nvarchar", "character_maximum_length": 5}]

    sql = ValidatingSqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    settings.enable_chunked_parquet = True
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))
    config = make_config("TRUNCATE")
    config.check_source_dest_columns = True

    with pytest.raises(ValueError, match="Schema validation failed"):
        engine._process_single_file(config, "/folder/file.parquet", "file.parquet")

    assert not any(c[0] == "swap_temp_to_destination" for c in sql.calls)
    assert any(c[0] == "load_chunk_to_temp" for c in sql.calls)


def test_chunked_parquet_append_pk_dup_check_fires_before_destination_swap() -> None:
    """SQL-side PK duplicate check prevents the swap on APPEND strategy."""
    payload = pd.DataFrame({"id": [1, 1, 2], "value": ["a", "b", "c"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)

    class DupDetectSqlClient(DummySqlClient):
        def check_temp_table_for_pk_duplicates(self, temp_table, schema, key_columns):
            return 2, [{"id": 1}]

    sql = DupDetectSqlClient()
    settings = make_settings(chunked=True, chunk_size=10)
    settings.enable_chunked_parquet = True
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    with pytest.raises(ValueError, match="PRIMARY_KEY_VIOLATION"):
        engine._process_single_file(make_config("APPEND"), "/folder/file.parquet", "file.parquet")

    assert any(c[0] == "load_chunk_to_temp" for c in sql.calls)
    assert not any(c[0] == "swap_temp_to_destination" for c in sql.calls)


def test_parquet_size_limit_constant_is_512_mib() -> None:
    assert MAX_PARQUET_FILE_SIZE_BYTES == 512 * 1024 * 1024


def test_parquet_size_limit_raises() -> None:
    """Files reported as > 512 MiB must be rejected before any data is read."""
    payload = _make_parquet_bytes(5)
    sp = _OversizedSharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(make_settings(), sql, sp, logging.getLogger("test"))

    with pytest.raises(ValueError, match="PARQUET_FILE_SIZE_LIMIT_EXCEEDED"):
        engine._process_parquet_file_in_chunks(make_config("TRUNCATE"), "/folder/big.parquet", "big.parquet")

    assert not any(c[0] in ("load_chunk_to_temp", "swap_temp_to_destination") for c in sql.calls)


def test_parquet_file_within_limit_proceeds() -> None:
    payload = _make_parquet_bytes(3)
    assert len(payload) < MAX_PARQUET_FILE_SIZE_BYTES

    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(make_settings(), sql, sp, logging.getLogger("test"))

    rows = engine._process_parquet_file_in_chunks(make_config("TRUNCATE"), "/folder/ok.parquet", "ok.parquet")
    assert rows == 3
