"""Regression tests for --ingest-stg-only runtime semantics."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd

from .conftest import DummySharePointClient, make_config, make_settings
from sharepoint_ingest.ingestion_engine import IngestionEngine
from sharepoint_ingest.sharepoint_client import SharePointFileItem


class _AuditSql:
    def __init__(self):
        self.records: list[tuple[str, dict]] = []
        self._next_audit_id = 1

    def fetch_ingestion_configs(self, **kwargs):
        return []

    def insert_audit_record(self, **kwargs):
        self.records.append(("insert", kwargs))
        audit_id = self._next_audit_id
        self._next_audit_id += 1
        return audit_id

    def update_audit_record(self, **kwargs):
        self.records.append(("update", kwargs))
        return True


class _DataSql:
    def __init__(self, database: str):
        self._settings = SimpleNamespace(database=database)
        self.calls: list[tuple] = []
        self.primary_key_columns = ["id"]
        self.table_columns = [
            {"column_name": "id", "data_type": "int", "ordinal_position": 1},
            {"column_name": "value", "data_type": "varchar", "character_maximum_length": 100, "ordinal_position": 2},
        ]

    def get_table_columns(self, table_name: str):
        self.calls.append(("get_table_columns", table_name))
        return self.table_columns

    def get_primary_key_columns(self, table_name: str):
        self.calls.append(("get_primary_key_columns", table_name))
        return self.primary_key_columns

    def truncate_and_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("truncate_and_load", table_name, len(df)))

    def append_load(self, df: pd.DataFrame, table_name: str) -> None:
        self.calls.append(("append_load", table_name, len(df)))

    def copy_stg_to_int(self, *, stg_table_name: str, int_table_name: str, int_database: str, load_strategy: str) -> int:
        self.calls.append(("copy_stg_to_int", stg_table_name, int_database, int_table_name, load_strategy))
        return 123


class _MultiFileSP(DummySharePointClient):
    def __init__(self, payload_by_url: dict[str, bytes]):
        first_payload = next(iter(payload_by_url.values()))
        super().__init__(first_payload)
        self._payload_by_url = payload_by_url

    def list_files(self, folder):
        return [
            SharePointFileItem(name=name, server_relative_url=url)
            for url, name in [
                ("/folder/a.csv", "a.csv"),
                ("/folder/b.csv", "b.csv"),
            ]
        ]

    def download_file_to_bytes(self, server_relative_url: str) -> bytes:
        return self._payload_by_url[server_relative_url]


def _make_engine(payload_by_url: dict[str, bytes]) -> tuple[IngestionEngine, _AuditSql, _DataSql, _DataSql]:
    audit_sql = _AuditSql()
    stg_sql = _DataSql("ingest_stg_dev")
    int_sql = _DataSql("ingest_int_dev")
    engine = IngestionEngine(
        make_settings(chunked=False),
        audit_sql,
        _MultiFileSP(payload_by_url),
        logging.getLogger("test.stg.only"),
        stg_sql_client=stg_sql,
        int_sql_client=int_sql,
    )
    return engine, audit_sql, stg_sql, int_sql


def test_staging_only_skips_integrated_promotion_and_audits_staging_destination() -> None:
    engine, audit_sql, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n", "/folder/b.csv": b"id,value\n2,b\n"})
    config = make_config("APPEND")
    config.multi_file_ingest = False
    config.integrated_table_name = "sharepoint.integrated_target"

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 1
    assert not any(call[0] == "copy_stg_to_int" for call in stg_sql.calls)
    update_records = [kwargs for action, kwargs in audit_sql.records if action == "update"]
    assert update_records
    assert update_records[-1]["destination_database"] == "ingest_stg_dev"
    assert update_records[-1]["destination_table"] == "staging.target"


def test_staging_only_multifile_truncates_once_then_appends_subsequent_files() -> None:
    engine, _, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n", "/folder/b.csv": b"id,value\n2,b\n"})
    config = make_config("TRUNCATE")
    config.multi_file_ingest = True
    config.file_name_pattern = "*.csv"
    config.integrated_table_name = "sharepoint.integrated_target"

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 2
    load_calls = [call for call in stg_sql.calls if call[0] in {"truncate_and_load", "append_load"}]
    assert load_calls == [
        ("truncate_and_load", "staging.target", 1),
        ("append_load", "staging.target", 1),
    ]
    assert not any(call[0] == "copy_stg_to_int" for call in stg_sql.calls)


def test_staging_only_overrides_append_config_to_truncate_reload() -> None:
    engine, _, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n", "/folder/b.csv": b"id,value\n2,b\n"})
    config = make_config("APPEND")
    config.multi_file_ingest = False
    config.integrated_table_name = "sharepoint.integrated_target"

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 1
    load_calls = [call for call in stg_sql.calls if call[0] in {"truncate_and_load", "append_load"}]
    assert load_calls == [("truncate_and_load", "staging.target", 1)]


def test_test_scope_uses_normal_promotion_even_when_staging_only_flag_is_set() -> None:
    engine, audit_sql, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n", "/folder/b.csv": b"id,value\n2,b\n"})
    config = make_config("TRUNCATE")
    config.multi_file_ingest = False
    config.ingestion_scope = "TEST"
    config.is_test_data = 1
    config.integrated_table_name = "sharepoint.integrated_target"

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 1
    assert any(call[0] == "copy_stg_to_int" for call in stg_sql.calls)
    update_records = [kwargs for action, kwargs in audit_sql.records if action == "update"]
    assert update_records[-1]["destination_database"] == "ingest_int_dev"
    assert update_records[-1]["destination_table"] == "sharepoint.integrated_target"


def test_staging_only_truncate_reload_still_checks_duplicate_primary_keys() -> None:
    engine, _, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n1,b\n", "/folder/b.csv": b"id,value\n2,b\n"})
    config = make_config("TRUNCATE")
    config.multi_file_ingest = False

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_failed == 1
    assert result.errors
    assert "PRIMARY_KEY_VIOLATION" in result.errors[0]
    assert not any(call[0] in {"truncate_and_load", "append_load"} for call in stg_sql.calls)


def test_staging_only_duplicate_check_uses_staging_pk_not_merge_key_columns() -> None:
    engine, _, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n2,a\n"})
    config = make_config("TRUNCATE")
    config.multi_file_ingest = False
    config.merge_key_columns = "value"
    stg_sql.primary_key_columns = ["id"]

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 1
    assert result.files_failed == 0
    assert ("truncate_and_load", "staging.target", 2) in stg_sql.calls


def test_staging_only_fails_fast_when_staging_table_name_blank_even_with_integrated_table() -> None:
    engine, audit_sql, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n"})
    config = make_config("TRUNCATE")
    config.staging_table_name = ""
    config.integrated_table_name = "sharepoint.integrated_target"

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 0
    assert result.files_failed == 1
    assert "blank staging_table_name" in result.errors[0]
    assert not any(call[0] in {"truncate_and_load", "append_load"} for call in stg_sql.calls)
    assert not engine.sharepoint_client.download_bytes_calls
    assert any(kwargs["status"] == "FAILED" for action, kwargs in audit_sql.records if action == "insert")


def test_staging_only_fails_fast_when_staging_table_missing() -> None:
    engine, _, stg_sql, _ = _make_engine({"/folder/a.csv": b"id,value\n1,a\n"})
    config = make_config("TRUNCATE")
    stg_sql.table_columns = []

    result = engine._process_config(config, ingest_stg_only=True)

    assert result.files_processed == 0
    assert result.files_failed == 1
    assert "was not found" in result.errors[0]
    assert not any(call[0] in {"truncate_and_load", "append_load"} for call in stg_sql.calls)
