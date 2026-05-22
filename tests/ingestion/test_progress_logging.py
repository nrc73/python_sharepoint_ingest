"""Tests for phase-progress log emission (validation / import milestones)."""
from __future__ import annotations

import logging

import pytest

from .conftest import DummySharePointClient, DummySqlClient, make_config, make_settings
from sharepoint_ingest.ingestion_engine import IngestionEngine


def test_non_chunked_file_logs_validation_and_import_progress(caplog: pytest.LogCaptureFixture) -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    logger = logging.getLogger("test.progress.nonchunk")
    engine = IngestionEngine(make_settings(chunked=False), sql, sp, logger)

    caplog.set_level(logging.INFO, logger=logger.name)
    engine._process_single_file(make_config("APPEND"), "/folder/file.csv", "file.csv")

    messages = [r.getMessage() for r in caplog.records]
    assert any("validation progress: 100%" in m for m in messages)
    assert any("sql-ingestion progress: 100%" in m for m in messages)


def test_chunked_file_logs_validation_and_import_progress(caplog: pytest.LogCaptureFixture) -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    logger = logging.getLogger("test.progress.chunked")
    engine = IngestionEngine(make_settings(chunked=True, chunk_size=2), sql, sp, logger)

    caplog.set_level(logging.INFO, logger=logger.name)
    engine._process_single_file(make_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    messages = [r.getMessage() for r in caplog.records]
    assert any("validation progress: 100%" in m and "bytes" in m for m in messages)
    assert any("import progress: 100%" in m and "bytes" in m for m in messages)
