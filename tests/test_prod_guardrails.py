from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.main import _validate_prod_guard_rails
from sharepoint_ingest.models import IngestionConfig


def _make_settings(env_name: str, allow_test_data_in_prod: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        env_name=env_name,
        allow_test_data_in_prod=allow_test_data_in_prod,
        email=SimpleNamespace(
            enabled=False,
            host=None,
            port=25,
            username=None,
            password=None,
            use_tls=False,
            from_address="noreply@example.com",
        ),
        default_file_pattern="*",
        default_load_strategy="TRUNCATE",
        null_alert_threshold=0.9,
        enable_chunked_csv=False,
        enable_chunked_parquet=True,
        ingest_chunk_size_rows=1000,
        sharepoint=SimpleNamespace(site_url="https://example.sharepoint.com/sites/prod"),
    )


def _cfg(*, scope: str = "REAL", is_test_data: int = 0, workflow_id: str = "wf") -> IngestionConfig:
    return IngestionConfig(
        id=1,
        sharepoint_base_url="",
        sharepoint_process_folder="/folder",
        excel_tab_name="",
        sharepoint_process_archive_folder="/archive",
        sharepoint_process_failed_folder="/failed",
        process_frequency=None,
        header_skip_rows=0,
        check_source_dest_columns=False,
        multi_file_ingest=True,
        error_notification_email_address=None,
        process_id=None,
        workflow_id=workflow_id,
        staging_table_name="sharepoint.target",
        is_active="1",
        ingestion_scope=scope,
        ingestion_domain=None,
        is_test_data=is_test_data,
        file_name_pattern="*.csv",
        load_strategy="TRUNCATE",
        merge_key_columns="id",
        column_mapping_json=None,
    )


class _DummySql:
    def __init__(self, configs):
        self._configs = configs

    def fetch_ingestion_configs(self, **kwargs):
        return self._configs


class _DummySp:
    site_url = "https://example.sharepoint.com/sites/prod"

    def ensure_folder(self, folder_server_relative_url: str) -> bool:
        return False

    def list_files(self, folder_server_relative_url: str):
        return []


def test_validate_prod_guard_rails_blocks_non_real_scope_in_prod() -> None:
    settings = _make_settings("prod", allow_test_data_in_prod=False)
    with pytest.raises(ValueError, match="Guard rail violation"):
        _validate_prod_guard_rails(settings, "test")


def test_validate_prod_guard_rails_allows_real_scope_in_prod() -> None:
    settings = _make_settings("prod", allow_test_data_in_prod=False)
    _validate_prod_guard_rails(settings, "real")


def test_validate_prod_guard_rails_allows_dev_test_scope() -> None:
    settings = _make_settings("dev", allow_test_data_in_prod=False)
    _validate_prod_guard_rails(settings, "test")


def test_ingestion_engine_blocks_test_rows_in_prod() -> None:
    settings = _make_settings("prod", allow_test_data_in_prod=False)
    sql = _DummySql(configs=[_cfg(scope="TEST", is_test_data=1, workflow_id="wf-test")])
    engine = IngestionEngine(settings, sql, _DummySp(), logger=logging.getLogger("test.prod.guard"))

    with pytest.raises(ValueError, match="Guard rail violation"):
        engine.run(ingestion_scope="real")


def test_ingestion_engine_allows_real_rows_in_prod() -> None:
    settings = _make_settings("prod", allow_test_data_in_prod=False)
    sql = _DummySql(configs=[_cfg(scope="REAL", is_test_data=0, workflow_id="wf-real")])
    engine = IngestionEngine(settings, sql, _DummySp(), logger=logging.getLogger("test.prod.guard.real"))

    summary = engine.run(ingestion_scope="real")
    assert summary.files_processed == 0
    assert summary.files_failed == 0

