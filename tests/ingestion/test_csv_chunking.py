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
