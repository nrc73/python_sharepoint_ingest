from __future__ import annotations

from datetime import datetime
from io import BytesIO
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

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
        default_load_strategy="TRUNCATE",
        null_alert_threshold=0.9,
        enable_chunked_csv=chunked,
        ingest_chunk_size_rows=chunk_size,
        env_name="test",
    )


def _settings_with_site(site_url: str = "https://mycompany715.sharepoint.com/sites/data_ingest_dev"):
    settings = _settings(chunked=False)
    settings.sharepoint = SimpleNamespace(site_url=site_url)
    return settings


def _build_excel_payload(sheet_frames: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, dataframe in sheet_frames.items():
            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


def _config(load_strategy: str = "TRUNCATE") -> IngestionConfig:
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

    rows = engine._process_single_file(_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("truncate_and_load", 2), ("append_load", 1)]
    assert sp.moved_to == [("/folder/file.csv", "/archive")]


def test_chunked_csv_append_strategy() -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(_config("APPEND"), "/folder/file.csv", "file.csv")

    assert rows == 3
    assert sql.calls == [("append_load", 2), ("append_load", 1)]


def test_chunked_csv_empty_file_truncate_reload_still_truncates() -> None:
    payload = b"id,value\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logging.getLogger("test"))

    rows = engine._process_single_file(_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    assert rows == 0
    assert sql.calls == [("truncate_and_load", 0)]


def test_excel_datetime_column_parses_ddmmyyyy_and_mmddyyyy_to_datetime() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    source = pd.DataFrame(
        {
            "txn_date": ["13/04/2026", "04/25/2026", "2026-05-01"],
            "value": [1, 2, 3],
        }
    )
    destination_columns = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="excel", destination_columns=destination_columns)

    assert pd.api.types.is_datetime64_any_dtype(normalized["txn_date"])
    assert str(normalized.loc[0, "txn_date"].date()) == "2026-04-13"
    assert str(normalized.loc[1, "txn_date"].date()) == "2026-04-25"
    assert str(normalized.loc[2, "txn_date"].date()) == "2026-05-01"


def test_datetime_column_rejects_ambiguous_slash_date_without_inference_hints() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    source = pd.DataFrame({"txn_date": ["03/04/2026"]})
    destination_columns = [{"column_name": "txn_date", "data_type": "datetime"}]

    with pytest.raises(ValueError, match="Ambiguous date values"):
        engine._normalize_dataframe(source, source_kind="excel", destination_columns=destination_columns)


def test_datetime_column_infers_ambiguous_values_from_unambiguous_dmy_hints() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    source = pd.DataFrame({"txn_date": ["13/04/2026", "03/04/2026"]})
    destination_columns = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=destination_columns)

    assert str(normalized.loc[0, "txn_date"].date()) == "2026-04-13"
    assert str(normalized.loc[1, "txn_date"].date()) == "2026-04-03"


def test_detect_excel_datetime_stored_as_text_warning_issue() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    source = pd.DataFrame(
        {
            "signup_date": ["01/01/2025", "31/01/2025", "2025-02-01", None],
            "customer_id": ["C1", "C2", "C3", "C4"],
        }
    )
    destination_columns = [
        {"column_name": "signup_date", "data_type": "datetime"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    issues = engine._detect_excel_datetime_text_issues(source, destination_columns)

    assert len(issues) == 1
    assert issues[0].code == "EXCEL_DATETIME_STORED_AS_TEXT"
    assert "count=3" in str(issues[0].details)


def test_detect_excel_datetime_stored_as_text_ignores_true_datetime_values() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    source = pd.DataFrame(
        {
            "signup_date": [datetime(2025, 1, 1), datetime(2025, 1, 2)],
            "customer_id": ["C1", "C2"],
        }
    )
    destination_columns = [
        {"column_name": "signup_date", "data_type": "datetime"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    issues = engine._detect_excel_datetime_text_issues(source, destination_columns)

    assert issues == []


def test_apply_ingestion_metadata_sets_source_file_name_for_csv() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    config = _config("APPEND")
    source = pd.DataFrame({"transaction_id": ["TXN000001"], "amount": [10.5]})
    destination_columns = [
        {"column_name": "transaction_id", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source,
        config,
        destination_columns=destination_columns,
        file_name="valid_transactions_001.csv",
        source_kind="csv",
    )

    assert "source_file_name" in enriched.columns
    assert enriched.loc[0, "source_file_name"] == "valid_transactions_001.csv"


def test_apply_ingestion_metadata_sets_excel_tab_name_for_excel() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    config = _config("APPEND")
    config.excel_tab_name = "Customers_AU"
    source = pd.DataFrame({"customer_id": ["CUST00001"], "customer_name": ["Customer 1"]})
    destination_columns = [
        {"column_name": "customer_id", "data_type": "varchar"},
        {"column_name": "excel_tab_name", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source,
        config,
        destination_columns=destination_columns,
        file_name="valid_customers_001.xlsx",
        source_kind="excel",
    )

    assert "excel_tab_name" in enriched.columns
    assert "source_file_name" in enriched.columns
    assert enriched.loc[0, "excel_tab_name"] == "Customers_AU"
    assert enriched.loc[0, "source_file_name"] == "valid_customers_001.xlsx"


def test_resolve_sharepoint_folder_prefixes_site_path_for_documents_relative_path() -> None:
    engine = IngestionEngine(_settings_with_site(), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))

    resolved = engine._resolve_sharepoint_folder("/Documents/valid_customers")

    assert resolved == "/sites/data_ingest_dev/Documents/valid_customers"


def test_resolve_sharepoint_folder_supports_env_site_path_placeholder() -> None:
    engine = IngestionEngine(_settings_with_site(), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))

    resolved = engine._resolve_sharepoint_folder("{env:site_path}/Documents/valid_customers/Processed")

    assert resolved == "/sites/data_ingest_dev/Documents/valid_customers/Processed"


def test_parse_excel_payload_regex_adds_actual_sheet_name_column() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    config = _config("APPEND")
    config.excel_tab_name = "REGEX:^Customers_(AU|US)$"
    payload = _build_excel_payload(
        {
            "Customers_AU": pd.DataFrame({"customer_id": ["CUST_AU_001"]}),
            "Customers_US": pd.DataFrame({"customer_id": ["CUST_US_001"]}),
            "Other": pd.DataFrame({"customer_id": ["CUST_OTH_001"]}),
        }
    )

    parsed = engine._parse_excel_payload(config, payload)

    assert "excel_tab_name" in parsed.columns
    assert sorted(parsed["excel_tab_name"].dropna().unique().tolist()) == ["Customers_AU", "Customers_US"]
    assert len(parsed) == 2


def test_apply_ingestion_metadata_preserves_existing_excel_tab_name_values() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))
    config = _config("APPEND")
    config.excel_tab_name = "REGEX:^Customers_(AU|US)$"
    source = pd.DataFrame(
        {
            "customer_id": ["CUST00001", "CUST00002"],
            "excel_tab_name": ["Customers_AU", "Customers_US"],
        }
    )
    destination_columns = [
        {"column_name": "customer_id", "data_type": "varchar"},
        {"column_name": "excel_tab_name", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source,
        config,
        destination_columns=destination_columns,
        file_name="valid_customers_001.xlsx",
        source_kind="excel",
    )

    assert enriched["excel_tab_name"].tolist() == ["Customers_AU", "Customers_US"]


def test_resolve_load_strategy_forces_append_for_multi_file_processing() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))

    resolved = engine._resolve_load_strategy("TRUNCATE", force_append=True)

    assert resolved == "APPEND"


def test_resolve_load_strategy_rejects_unsupported_merge_value() -> None:
    engine = IngestionEngine(_settings(chunked=False), DummySqlClient(), DummySharePointClient(b""), logging.getLogger("test"))

    with pytest.raises(ValueError, match="Unsupported load_strategy"):
        engine._resolve_load_strategy("merge")


def test_non_chunked_file_logs_validation_and_import_progress(caplog: pytest.LogCaptureFixture) -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    logger = logging.getLogger("test.progress.nonchunk")
    engine = IngestionEngine(_settings(chunked=False), sql, sp, logger)

    caplog.set_level(logging.INFO, logger=logger.name)
    engine._process_single_file(_config("APPEND"), "/folder/file.csv", "file.csv")

    messages = [record.getMessage() for record in caplog.records]
    assert any("validation progress: 100%" in m for m in messages)
    assert any("import progress: 100%" in m for m in messages)


def test_chunked_file_logs_validation_and_import_progress(caplog: pytest.LogCaptureFixture) -> None:
    payload = b"id,value\n1,a\n2,b\n3,c\n"
    sp = DummySharePointClient(payload)
    sql = DummySqlClient()
    logger = logging.getLogger("test.progress.chunked")
    engine = IngestionEngine(_settings(chunked=True, chunk_size=2), sql, sp, logger)

    caplog.set_level(logging.INFO, logger=logger.name)
    engine._process_single_file(_config("TRUNCATE"), "/folder/file.csv", "file.csv")

    messages = [record.getMessage() for record in caplog.records]
    assert any("validation progress: 100%" in m and "bytes" in m for m in messages)
    assert any("import progress: 100%" in m and "bytes" in m for m in messages)
