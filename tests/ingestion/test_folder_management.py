"""Tests for Processed/Failed folder creation on first run."""
from __future__ import annotations

import logging

import pytest

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    make_settings,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.models import IngestionConfig
from sharepoint_ingest.sharepoint_client import SharePointFileItem


def _config_with_folders(
    archive_folder: str | None = "/archive",
    failed_folder: str | None = "/failed",
    load_strategy: str = "TRUNCATE",
) -> IngestionConfig:
    return IngestionConfig(
        id=99,
        sharepoint_base_url="",
        sharepoint_process_folder="/folder",
        excel_tab_name="",
        sharepoint_process_archive_folder=archive_folder,
        sharepoint_process_failed_folder=failed_folder,
        process_frequency=None,
        header_skip_rows=0,
        check_source_dest_columns=False,
        multi_file_ingest=False,
        error_notification_email_address=None,
        process_id=None,
        workflow_id=None,
        staging_table_name="dbo.target",
        ingestion_scope="REAL",
        ingestion_domain=None,
        is_test_data=0,
        load_strategy=load_strategy,
        merge_key_columns="id",
    )


class _SingleFileSP(DummySharePointClient):
    def list_files(self, folder):
        return [SharePointFileItem(name="data.csv", server_relative_url=f"{folder}/data.csv")]


def _make_engine_and_sp(payload: bytes, *, existing_folders: set[str] | None = None, logger_name: str = "test.folders"):
    sp = _SingleFileSP(payload, existing_folders=existing_folders)
    sql = DummySqlClient()
    engine = IngestionEngine(make_settings(chunked=False), sql, sp, logging.getLogger(logger_name))
    return engine, sp, sql


def test_process_config_calls_ensure_folder_for_both_archive_and_failed() -> None:
    engine, sp, _ = _make_engine_and_sp(b"id,value\n1,a\n")
    engine._process_config(_config_with_folders(archive_folder="/archive", failed_folder="/failed"))

    ensured = [url for url, _ in sp.ensure_folder_calls]
    assert "/archive" in ensured
    assert "/failed" in ensured


def test_process_config_creates_folders_when_missing_on_first_run(caplog: pytest.LogCaptureFixture) -> None:
    engine, sp, _ = _make_engine_and_sp(b"id,value\n1,a\n", existing_folders=set())
    caplog.set_level(logging.INFO, logger="test.folders")
    engine._process_config(_config_with_folders(archive_folder="/archive", failed_folder="/failed"))

    created = {url for url, was_created in sp.ensure_folder_calls if was_created}
    assert "/archive" in created
    assert "/failed" in created

    messages = [r.getMessage() for r in caplog.records]
    assert any("created missing" in m and "archive/processed" in m for m in messages)
    assert any("created missing" in m and "failed" in m for m in messages)


def test_process_config_skips_create_when_folders_already_exist(caplog: pytest.LogCaptureFixture) -> None:
    engine, sp, _ = _make_engine_and_sp(
        b"id,value\n1,a\n", existing_folders={"/archive", "/failed"}
    )
    caplog.set_level(logging.INFO, logger="test.folders")
    engine._process_config(_config_with_folders(archive_folder="/archive", failed_folder="/failed"))

    ensured = [url for url, _ in sp.ensure_folder_calls]
    assert "/archive" in ensured
    assert "/failed" in ensured

    created = {url for url, was_created in sp.ensure_folder_calls if was_created}
    assert "/archive" not in created
    assert "/failed" not in created

    messages = [r.getMessage() for r in caplog.records]
    assert not any("created missing" in m for m in messages)


def test_process_config_skips_ensure_folder_when_no_folders_configured() -> None:
    engine, sp, _ = _make_engine_and_sp(b"id,value\n1,a\n")
    engine._process_config(_config_with_folders(archive_folder=None, failed_folder=None))

    assert sp.ensure_folder_calls == []


def test_process_config_continues_ingestion_when_ensure_folder_raises() -> None:
    class _FailingEnsureSP(_SingleFileSP):
        def ensure_folder(self, folder: str) -> bool:
            raise RuntimeError("Graph API 403 Forbidden")

    sp = _FailingEnsureSP(b"id,value\n1,a\n")
    sql = DummySqlClient()
    engine = IngestionEngine(
        make_settings(chunked=False), sql, sp, logging.getLogger("test.folders.error")
    )
    result = engine._process_config(_config_with_folders(archive_folder="/archive", failed_folder="/failed"))

    assert result.files_processed == 1
    assert result.files_failed == 0
