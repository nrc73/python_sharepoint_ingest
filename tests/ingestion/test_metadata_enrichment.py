"""Tests for ingestion metadata column enrichment (source_file_name, excel_tab_name)."""
from __future__ import annotations

import logging

import pandas as pd

from .conftest import (
    DummySharePointClient,
    DummySqlClient,
    build_excel_payload,
    make_config,
    make_engine,
    make_settings,
)
from sharepoint_ingest.ingestion_engine import IngestionEngine


def test_apply_ingestion_metadata_sets_source_file_name_for_csv() -> None:
    engine = make_engine()
    config = make_config("APPEND")
    source = pd.DataFrame({"transaction_id": ["TXN000001"], "amount": [10.5]})
    dest_cols = [
        {"column_name": "transaction_id", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source, config, destination_columns=dest_cols,
        file_name="valid_transactions_001.csv", source_kind="csv",
    )

    assert "source_file_name" in enriched.columns
    assert enriched.loc[0, "source_file_name"] == "valid_transactions_001.csv"


def test_apply_ingestion_metadata_sets_excel_tab_name_for_excel() -> None:
    engine = make_engine()
    config = make_config("APPEND")
    config.excel_tab_name = "Customers_AU"
    source = pd.DataFrame({"customer_id": ["CUST00001"], "customer_name": ["Customer 1"]})
    dest_cols = [
        {"column_name": "customer_id", "data_type": "varchar"},
        {"column_name": "excel_tab_name", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source, config, destination_columns=dest_cols,
        file_name="valid_customers_001.xlsx", source_kind="excel",
    )

    assert "excel_tab_name" in enriched.columns
    assert "source_file_name" in enriched.columns
    assert enriched.loc[0, "excel_tab_name"] == "Customers_AU"
    assert enriched.loc[0, "source_file_name"] == "valid_customers_001.xlsx"


def test_parse_excel_payload_regex_adds_actual_sheet_name_column() -> None:
    engine = make_engine()
    config = make_config("APPEND")
    config.excel_tab_name = "REGEX:^Customers_(AU|US)$"
    payload = build_excel_payload(
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
    engine = make_engine()
    config = make_config("APPEND")
    config.excel_tab_name = "REGEX:^Customers_(AU|US)$"
    source = pd.DataFrame(
        {
            "customer_id": ["CUST00001", "CUST00002"],
            "excel_tab_name": ["Customers_AU", "Customers_US"],
        }
    )
    dest_cols = [
        {"column_name": "customer_id", "data_type": "varchar"},
        {"column_name": "excel_tab_name", "data_type": "varchar"},
        {"column_name": "source_file_name", "data_type": "varchar"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source, config, destination_columns=dest_cols,
        file_name="valid_customers_001.xlsx", source_kind="excel",
    )

    assert enriched["excel_tab_name"].tolist() == ["Customers_AU", "Customers_US"]


def test_apply_ingestion_metadata_sets_new_system_fields_when_present_in_destination() -> None:
    engine = make_engine()
    config = make_config("APPEND")
    source = pd.DataFrame({"transaction_id": ["TXN000001"], "amount": [10.5]})
    dest_cols = [
        {"column_name": "transaction_id", "data_type": "varchar"},
        {"column_name": "amount", "data_type": "decimal"},
        {"column_name": "sp_ingest_load_dt", "data_type": "datetime"},
        {"column_name": "audit_id", "data_type": "bigint"},
        {"column_name": "__$job_instance_id", "data_type": "int"},
    ]

    enriched = engine._apply_ingestion_metadata(
        source, config, destination_columns=dest_cols,
        file_name="valid_transactions_001.csv", source_kind="csv",
        audit_id=98765,
    )

    assert "sp_ingest_load_dt" in enriched.columns
    assert "audit_id" in enriched.columns
    assert "__$job_instance_id" in enriched.columns
    assert pd.notna(enriched.loc[0, "sp_ingest_load_dt"])
    assert enriched.loc[0, "audit_id"] == 98765
    assert enriched.loc[0, "__$job_instance_id"] is None
