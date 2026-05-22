"""Shared stubs and factory helpers for ingestion engine tests.

Import these in individual test modules rather than duplicating the definitions.
"""
from __future__ import annotations

import logging
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest

from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.models import IngestionConfig


# ---------------------------------------------------------------------------
# Stub clients
# ---------------------------------------------------------------------------

class DummySharePointClient:
    """In-memory SharePoint stub — serves payload bytes, tracks moves."""

    def __init__(self, payload: bytes, *, existing_folders: set[str] | None = None):
        self._payload = payload
        self.moved_to: list[tuple[str, str]] = []
        self.ensure_folder_calls: list[tuple[str, bool]] = []
        self._existing_folders: set[str] = existing_folders or set()

    def download_file_to_buffer(self, server_relative_url: str):
        return BytesIO(self._payload)

    def download_file_to_bytes(self, server_relative_url: str) -> bytes:
        return self._payload

    def get_file_item(self, server_relative_url: str) -> dict:
        return {"size": len(self._payload), "@microsoft.graph.downloadUrl": None}

    def download_file_range_bytes(
        self,
        server_relative_url: str,
        start: int,
        end: int,
        download_url: str | None = None,
    ) -> bytes:
        return self._payload[start : end + 1]

    def ensure_folder(self, folder_server_relative_url: str) -> bool:
        normalized = folder_server_relative_url.rstrip("/")
        created = normalized not in self._existing_folders
        if created:
            self._existing_folders.add(normalized)
        self.ensure_folder_calls.append((normalized, created))
        return created

    def move_file(self, src: str, dest_folder: str) -> str:
        self.moved_to.append((src, dest_folder))
        return f"{dest_folder.rstrip('/')}/moved.csv"


class DummySqlClient:
    """SQL stub — records all calls, no real DB connection."""

    def __init__(self):
        self.calls: list[tuple] = []

    def truncate_and_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("truncate_and_load", len(df)))

    def append_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("append_load", len(df)))

    def merge_load(self, df: pd.DataFrame, table_name: str, merge_keys: list[str]) -> None:
        self.calls.append(("merge_load", len(df)))

    def load_chunk_to_temp(self, df: pd.DataFrame, temp_table: str, schema: str, first_chunk: bool) -> None:
        self.calls.append(("load_chunk_to_temp", len(df)))

    def check_temp_table_for_pk_duplicates(self, temp_table: str, schema: str, key_columns: list) -> tuple:
        return 0, []

    def swap_temp_to_destination(self, temp_table: str, schema: str, dest_table: str, load_strategy: str) -> None:
        self.calls.append(("swap_temp_to_destination", load_strategy))

    def drop_temp_table(self, temp_table: str, schema: str) -> None:
        pass

    def get_table_columns(self, table_name: str):
        return []

    def get_primary_key_columns(self, table_name: str):
        return ["id"]

    def fetch_ingestion_configs(self, process_id=None, workflow_id=None, ingestion_scope=None, active_only=True):
        return []

    def insert_audit_record(self, **kwargs):
        return None


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def make_settings(chunked: bool = True, chunk_size: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
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
        enable_chunked_csv=chunked,
        enable_chunked_parquet=True,
        ingest_chunk_size_rows=chunk_size,
        env_name="test",
    )


def make_settings_with_site(
    site_url: str = "https://mycompany715.sharepoint.com/sites/data_ingest_dev",
) -> SimpleNamespace:
    settings = make_settings(chunked=False)
    settings.sharepoint = SimpleNamespace(site_url=site_url)
    return settings


def make_config(load_strategy: str = "TRUNCATE") -> IngestionConfig:
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
        ingestion_scope="REAL",
        ingestion_domain=None,
        is_test_data=0,
        load_strategy=load_strategy,
        merge_key_columns="id",
    )


def build_excel_payload(sheet_frames: dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in sheet_frames.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    return buf.getvalue()


def make_engine(
    payload: bytes = b"",
    *,
    chunked: bool = False,
    chunk_size: int = 2,
    sql: DummySqlClient | None = None,
    sp: DummySharePointClient | None = None,
    logger_name: str = "test",
) -> IngestionEngine:
    return IngestionEngine(
        make_settings(chunked=chunked, chunk_size=chunk_size),
        sql or DummySqlClient(),
        sp or DummySharePointClient(payload),
        logging.getLogger(logger_name),
    )
