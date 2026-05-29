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


def test_chunked_parquet_truncate_single_pass_loads_directly_to_staging() -> None:
    """Single-pass Parquet: chunks go directly to the staging table (first
    chunk truncates, subsequent chunks append).  No temp table is used."""
    payload = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    settings.enable_chunked_parquet = True
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(make_config("TRUNCATE"), "/folder/file.parquet", "file.parquet")

    assert rows == 3
    # First chunk: truncate_and_load (2 rows), second chunk: append_load (1 row)
    assert sql.calls == [
        ("truncate_and_load", 2),
        ("append_load", 1),
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


def test_chunked_parquet_schema_validation_aborts_mid_stream() -> None:
    """Blocking schema errors abort the stream — no swap completes.

    In the direct-to-staging flow (no temp buffer), chunks that pass validation
    are written before the blocking error fires on a later chunk.  The key
    guarantee is that the stream stops (ValueError is raised) and NO append of
    the offending chunk occurs after the error.
    """
    # chunk_size=2:  chunk-0 = ["ok", "fit"] (pass),  chunk-1 = ["value-too-long-…"] (fail)
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

    # The first passing chunk is loaded (truncate_and_load with 2 rows).
    # The second chunk triggers the error before it can be loaded.
    load_calls = [(c[0], c[1]) for c in sql.calls if c[0] in ("truncate_and_load", "append_load")]
    assert len(load_calls) == 1, f"Expected only 1 load call before abort; got {load_calls}"
    assert load_calls[0][0] == "truncate_and_load"
    assert load_calls[0][1] == 2   # 2 rows from the first (passing) chunk


def test_chunked_parquet_no_temp_table_methods_called() -> None:
    """The parquet streaming path must NOT call any temp-table SQL methods."""
    payload = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}).to_parquet(index=False)
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    settings = make_settings(chunked=True, chunk_size=2)
    settings.enable_chunked_parquet = True
    engine = IngestionEngine(settings, sql, sp, logging.getLogger("test"))

    engine._process_single_file(make_config("TRUNCATE"), "/folder/file.parquet", "file.parquet")

    temp_methods = {"load_chunk_to_temp", "swap_temp_to_destination",
                    "drop_temp_table", "check_temp_table_for_pk_duplicates"}
    used = {c[0] for c in sql.calls}
    assert used.isdisjoint(temp_methods), (
        f"Unexpected temp-table methods called: {used & temp_methods}"
    )


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

    # No data I/O should have occurred
    load_calls = [c[0] for c in sql.calls if c[0] in
                  ("truncate_and_load", "append_load", "load_chunk_to_temp", "swap_temp_to_destination")]
    assert load_calls == []


def test_parquet_file_within_limit_proceeds() -> None:
    payload = _make_parquet_bytes(3)
    assert len(payload) < MAX_PARQUET_FILE_SIZE_BYTES

    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(make_settings(), sql, sp, logging.getLogger("test"))

    rows = engine._process_parquet_file_in_chunks(make_config("TRUNCATE"), "/folder/ok.parquet", "ok.parquet")
    assert rows == 3
