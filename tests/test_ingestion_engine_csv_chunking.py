from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd

from src.ingestion_engine import IngestionEngine
from src.models import IngestionConfig


class DummySharePointClient:
    def __init__(self, payload: bytes):
        self._payload = payload
        self.moved_to: list[tuple[str, str]] = []

    def download_file_to_buffer(self, server_relative_url: str):
        from io import BytesIO

        return BytesIO(self._payload)

    def download_file_to_bytes(self, server_relative_url: str) -> bytes:
        return self._payload

    def move_file(self, src_server_relative_url: str, dest_folder_server_relative_url: str) -> str:
        self.moved_to.append((src_server_relative_url, dest_folder_server_relative_url))
        return f"{dest_folder_server_relative_url.rstrip('/')}/moved.csv"


class DummySqlClient:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def truncate_and_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("truncate_and_load", len(df)))

    def append_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("append_load", len(df)))

    def merge_load(self, df: pd.DataFrame, table_name: str, merge_keys: list[str]) -> None:
        self.calls.append(("merge_load", len(df)))

    def get_table_columns(self, table_name: str):
        return []

    def get_primary_key_columns(self, table_name: str):
        return ["id"]


def _settings(chunked: bool = True, chunk_size: int = 2):
    return SimpleNamespace(
        email=SimpleNamespace(enabled=False, host=None, port=25, username=None, password=None, use_tls=False, from_address="noreply@example.com"),
        default_file_pattern="*",
        default_load_strategy="truncate_reload",
        null_alert_threshold=0.9,
        enable_chunked_csv=chunked,
        ingest_chunk_size_rows=chunk_size,
        env_name="test",
    )


def _config(load_strategy: str = "truncate_reload") -> IngestionConfig:
    return IngestionConfig(
        id=1,
        sharepoint_base_url="",
        sharepoint_process_folder="/folder",
        excel_tab_name="",
        sharepoint_process_archive_folder="/archive",
        sharepoint_process_failed_folder=None,
        process_frequency=None,
        header_skip_rows=0,
        check_source_dest_columns=False,
        multi_file_ingest=True,
        error_notification_email_address=None,
        process_id=None,
        workflow_id=None,
        staging_table_name="dbo.target",
        load_strategy=load_strategy,
        merge_key_columns="id",
    )


def test_chunked_csv_truncate_then_append() -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(_config("truncate_reload"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("truncate_and_load", 2), ("append_load", 1)]
    assert sp.moved_to == [("/folder/file.csv", "/archive")]


def test_chunked_csv_append_strategy() -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(_config("append"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("append_load", 2), ("append_load", 1)]


def test_chunked_csv_empty_file_truncate_reload_still_truncates() -> None:
    payload = b"id,value\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(_config("truncate_reload"), "/folder/file.csv", "file.csv")

    assert rows == 0
    assert sql.calls == [("truncate_and_load", 0)]
