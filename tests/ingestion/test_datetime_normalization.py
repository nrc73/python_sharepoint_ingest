"""Tests for datetime column normalization and Excel datetime-stored-as-text detection."""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytest

from .conftest import DummySharePointClient, DummySqlClient, make_engine


def test_excel_datetime_column_parses_ddmmyyyy_and_mmddyyyy_to_datetime() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {"txn_date": ["13/04/2026", "04/25/2026", "2026-05-01"], "value": [1, 2, 3]}
    )
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="excel", destination_columns=dest_cols)

    assert pd.api.types.is_datetime64_any_dtype(normalized["txn_date"])
    assert str(normalized.loc[0, "txn_date"].date()) == "2026-04-13"
    assert str(normalized.loc[1, "txn_date"].date()) == "2026-04-25"
    assert str(normalized.loc[2, "txn_date"].date()) == "2026-05-01"


def test_datetime_column_rejects_ambiguous_slash_date_without_inference_hints() -> None:
    engine = make_engine()
    source = pd.DataFrame({"txn_date": ["03/04/2026"]})
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    with pytest.raises(ValueError, match="Ambiguous date values"):
        engine._normalize_dataframe(source, source_kind="excel", destination_columns=dest_cols)


def test_datetime_column_infers_ambiguous_values_from_unambiguous_dmy_hints() -> None:
    engine = make_engine()
    source = pd.DataFrame({"txn_date": ["13/04/2026", "03/04/2026"]})
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    assert str(normalized.loc[0, "txn_date"].date()) == "2026-04-13"
    assert str(normalized.loc[1, "txn_date"].date()) == "2026-04-03"


def test_detect_excel_datetime_stored_as_text_warning_issue() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {
            "signup_date": ["01/01/2025", "31/01/2025", "2025-02-01", None],
            "customer_id": ["C1", "C2", "C3", "C4"],
        }
    )
    dest_cols = [
        {"column_name": "signup_date", "data_type": "datetime"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    issues = engine._detect_excel_datetime_text_issues(source, dest_cols)

    assert len(issues) == 1
    assert issues[0].code == "EXCEL_DATETIME_STORED_AS_TEXT"
    assert "count=3" in str(issues[0].details)


def test_detect_excel_datetime_stored_as_text_ignores_true_datetime_values() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {
            "signup_date": [datetime(2025, 1, 1), datetime(2025, 1, 2)],
            "customer_id": ["C1", "C2"],
        }
    )
    dest_cols = [
        {"column_name": "signup_date", "data_type": "datetime"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    assert engine._detect_excel_datetime_text_issues(source, dest_cols) == []


def test_destination_datetime_columns_excludes_framework_managed_audit_fields() -> None:
    engine = make_engine()
    dest_cols = [
        {"column_name": "signup_date", "data_type": "datetime2"},
        {"column_name": "load_datetime", "data_type": "datetime2"},
        {"column_name": "__$batch_id", "data_type": "int"},
        {"column_name": "__$job_instance_id", "data_type": "int"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    result = engine._destination_datetime_columns(dest_cols)

    assert "signup_date" in result
    assert "load_datetime" not in result


def test_detect_excel_datetime_stored_as_text_ignores_framework_managed_audit_fields() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {
            "load_datetime": ["01/01/2025", "31/01/2025"],
            "__$batch_id": [100, 101],
            "__$job_instance_id": [200, 201],
            "customer_id": ["C1", "C2"],
        }
    )
    dest_cols = [
        {"column_name": "load_datetime", "data_type": "datetime2"},
        {"column_name": "__$batch_id", "data_type": "int"},
        {"column_name": "__$job_instance_id", "data_type": "int"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    assert engine._detect_excel_datetime_text_issues(source, dest_cols) == []
