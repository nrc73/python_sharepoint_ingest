"""Tests for SharePoint path/URL resolution on IngestionEngine."""
from __future__ import annotations

from .conftest import DummySharePointClient, DummySqlClient, make_settings_with_site
from sharepoint_ingest.ingestion_engine import IngestionEngine
import logging


def test_resolve_sharepoint_folder_prefixes_site_path_for_documents_relative_path() -> None:
    engine = IngestionEngine(
        make_settings_with_site(), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test")
    )

    resolved = engine._resolve_sharepoint_folder("/Documents/valid_customers")

    assert resolved == "/sites/data_ingest_dev/Documents/valid_customers"


def test_resolve_sharepoint_folder_supports_env_site_path_placeholder() -> None:
    engine = IngestionEngine(
        make_settings_with_site(), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test")
    )

    resolved = engine._resolve_sharepoint_folder("{env:site_path}/Documents/valid_customers/Processed")

    assert resolved == "/sites/data_ingest_dev/Documents/valid_customers/Processed"
