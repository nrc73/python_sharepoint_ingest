"""Tests for primary-key violation detection and notification."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError as SqlIntegrityError

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    make_config,
    make_engine,
    make_settings,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.notifications import build_pk_violation_email_body
from sharepoint_ingest.sql_client import SqlClient


def test_append_load_raises_pk_violation_value_error_on_integrity_error() -> None:
    """SqlClient.append_load wraps SQLAlchemy IntegrityError as a ValueError with
    PRIMARY_KEY_VIOLATION: prefix so the engine can route it specifically."""
    mock_settings = MagicMock()
    mock_settings.odbc_driver = "ODBC Driver 17 for SQL Server"
    mock_settings.trust_server_certificate = True
    mock_settings.auth_mode = "sql_password"
    mock_settings.username = "user"
    mock_settings.password = "pass"
    mock_settings.host = "localhost"
    mock_settings.port = 1433
    mock_settings.database = "db"

    with patch("sharepoint_ingest.sql_client.create_engine") as mock_create_engine:
        mock_create_engine.return_value = MagicMock()
        sql = SqlClient(mock_settings)

    df = pd.DataFrame({"id": [1], "value": ["a"]})
    orig_exc = SqlIntegrityError("stmt", "params", Exception("Violation of PRIMARY KEY constraint"))

    with patch("pandas.DataFrame.to_sql", side_effect=orig_exc):
        with pytest.raises(ValueError, match="PRIMARY_KEY_VIOLATION"):
            sql.append_load(df, "dbo.target")


def test_intra_file_duplicate_keys_raise_before_sql_on_append() -> None:
    """Engine detects intra-file duplicate PK values BEFORE any SQL insert."""
    payload = b"id,value\n1,a\n1,b\n2,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(make_settings(chunked=False), sql, sp, logging.getLogger("test"))
    config = make_config("APPEND")

    with pytest.raises(ValueError, match="PRIMARY_KEY_VIOLATION"):
        engine._process_single_file(config, "/folder/file.csv", "file.csv")

    assert not any(call[0] == "append_load" for call in sql.calls)


def test_chunked_csv_intra_file_duplicate_keys_caught_on_first_chunk() -> None:
    """Duplicate key detection fires on the first chunk, preventing any SQL write."""
    payload = b"id,value\n1,a\n1,b\n2,c\n3,d\n4,e\n"
    sql = DummySqlClient()
    sp = DummySharePointClient(payload)
    engine = IngestionEngine(make_settings(chunked=True, chunk_size=5), sql, sp, logging.getLogger("test"))

    with pytest.raises(ValueError, match="PRIMARY_KEY_VIOLATION"):
        engine._process_single_file(make_config("APPEND"), "/folder/file.csv", "file.csv")

    assert not any(call[0] in ("append_load", "truncate_and_load") for call in sql.calls)


def test_notify_pk_violation_builds_dedicated_subject_and_remediation_body() -> None:
    """_notify_pk_violation sends an email with PK-specific subject and remediation guidance."""
    sent_calls: list[tuple] = []

    class CapturingNotifier:
        def send(self, to_address, subject, body):
            sent_calls.append((to_address, subject, body))
            return True

    engine = make_engine()
    engine.notifier = CapturingNotifier()
    config = make_config("APPEND")
    config.error_notification_email_address = "ops@example.com"

    error_msg = (
        "Config 1 failed for file test.csv: PRIMARY_KEY_VIOLATION: File contains 4 rows "
        "with duplicate values on key column(s) ['id'] for table 'dbo.target'."
    )
    engine._notify_pk_violation(config, error_msg, file_name="test.csv", rows_scanned=4)

    assert len(sent_calls) == 1
    to_addr, subject, body = sent_calls[0]
    assert to_addr == "ops@example.com"
    assert "PRIMARY KEY VIOLATION" in subject
    assert "dbo.target" in body
    assert "Remediation options" in body
    assert "FULL RELOAD" in body
    assert "MANUAL CLEAN" in body


def test_pk_violation_email_body_contains_full_context() -> None:
    """build_pk_violation_email_body includes all expected fields."""
    body = build_pk_violation_email_body(
        process_name="config_id=1, workflow_id=wf-test",
        error_message="PRIMARY_KEY_VIOLATION: duplicate keys on business_key",
        file_name="reload_test.csv",
        table_name="dbo.sample_ingestion_target",
        key_columns=["business_key"],
        duplicate_count=6,
        sample_values=[{"business_key": "BK001"}, {"business_key": "BK002"}],
        rows_scanned=100,
        memory_peak_mb=45.2,
        duration_seconds=1.3,
    )

    assert "PRIMARY KEY VIOLATION" in body
    assert "reload_test.csv" in body
    assert "dbo.sample_ingestion_target" in body
    assert "business_key" in body
    assert "6" in body
    assert "BK001" in body
    assert "FULL RELOAD" in body
    assert "MANUAL CLEAN" in body
    assert "100" in body
    assert "45.2 MB" in body
