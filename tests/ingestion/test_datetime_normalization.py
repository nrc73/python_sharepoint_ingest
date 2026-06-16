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


def test_csv_datetime_column_infers_ambiguous_values_from_unambiguous_us_hints() -> None:
    engine = make_engine()
    source = pd.DataFrame({"txn_date": ["04/05/2026 0:00", "4/15/2026 0:00"]})
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    assert str(normalized.loc[0, "txn_date"]) == "2026-04-05 00:00:00"
    assert str(normalized.loc[1, "txn_date"]) == "2026-04-15 00:00:00"


def test_csv_datetime_column_rejects_conflicting_au_and_us_hints() -> None:
    engine = make_engine()
    source = pd.DataFrame({"txn_date": ["15/04/2026", "4/16/2026"]})
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    with pytest.raises(ValueError, match="Conflicting CSV date formats"):
        engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)


def test_bit_column_converts_strict_boolean_text_and_numbers() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {
            "flag": ["True", "false", "1", "0", "t", "F", 1, 0, None, ""],
            "description": ["Yes", "No", "Y", "N", "text", "other", "a", "b", "c", "d"],
        }
    )
    dest_cols = [
        {"column_name": "flag", "data_type": "bit"},
        {"column_name": "description", "data_type": "varchar"},
    ]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    assert normalized["flag"].tolist() == [
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        None,
        None,
    ]
    assert normalized["description"].tolist()[:4] == ["Yes", "No", "Y", "N"]


def test_bit_column_rejects_yes_no_text_with_clear_error() -> None:
    engine = make_engine()
    source = pd.DataFrame({"flag": ["Yes", "No"]})
    dest_cols = [{"column_name": "flag", "data_type": "bit"}]

    with pytest.raises(ValueError, match="Yes/No and Y/N are treated as strings"):
        engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)


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
        {"column_name": "sp_ingest_load_dt", "data_type": "datetime2"},
        {"column_name": "audit_id", "data_type": "bigint"},
        {"column_name": "__$job_instance_id", "data_type": "int"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    result = engine._destination_datetime_columns(dest_cols)

    assert "signup_date" in result
    assert "sp_ingest_load_dt" not in result


# ── Mixed text + date pass-through ───────────────────────────────────────────


def test_csv_datetime_column_with_mixed_free_text_is_passed_through_unchanged() -> None:
    """Column containing free-text rows must not be converted at all.

    'Withdrawal due 01-Jul-26 00:00' cannot be parsed as a date, so the entire
    column — including 'clean' rows like '4/1/2026 0:00' — must be left as
    original strings.  Partial conversion would silently lose the text rows.
    """
    engine = make_engine()
    source = pd.DataFrame(
        {
            "due_date": [
                "Withdrawal due 01-Jul-26 00:00",
                "4/1/2026 0:00",
                None,
                "Payment due 15-Aug-26",
            ],
            "amount": [100, 200, 300, 400],
        }
    )
    dest_cols = [
        {"column_name": "due_date", "data_type": "datetime"},
        {"column_name": "amount", "data_type": "decimal"},
    ]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    # Column must be completely unchanged — original strings preserved
    assert list(normalized["due_date"]) == list(source["due_date"])
    assert not pd.api.types.is_datetime64_any_dtype(normalized["due_date"])


def test_csv_datetime_column_with_only_free_text_is_passed_through_unchanged() -> None:
    """Column that is entirely free text (no parseable dates at all) passes through."""
    engine = make_engine()
    source = pd.DataFrame(
        {"note_date": ["Overdue since July", "TBC", "On demand"], "id": [1, 2, 3]}
    )
    dest_cols = [{"column_name": "note_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    assert list(normalized["note_date"]) == ["Overdue since July", "TBC", "On demand"]


def test_csv_datetime_column_with_all_parseable_dates_is_still_converted() -> None:
    """A column where every non-null value is a parseable date is still converted."""
    engine = make_engine()
    source = pd.DataFrame({"txn_date": ["15/04/2026", "16/04/2026", None]})
    dest_cols = [{"column_name": "txn_date", "data_type": "datetime"}]

    normalized = engine._normalize_dataframe(source, source_kind="csv", destination_columns=dest_cols)

    assert pd.api.types.is_datetime64_any_dtype(normalized["txn_date"])
    assert str(normalized.loc[0, "txn_date"].date()) == "2026-04-15"
    assert str(normalized.loc[1, "txn_date"].date()) == "2026-04-16"


def test_mixed_text_date_column_does_not_raise_during_csv_pre_scan() -> None:
    """The CSV date-order pre-scan must silently skip mixed text+date columns.

    Previously the pre-scan would raise ValueError ('Invalid or mixed CSV date
    values') for 'Withdrawal due 01-Jul-26 00:00' being an unrecognised format.
    The fix is to skip the entire column in the pre-scan.
    """
    from io import BytesIO

    from sharepoint_ingest.models import IngestionConfig

    engine = make_engine()
    csv_bytes = (
        b"due_date,amount\n"
        b"\"Withdrawal due 01-Jul-26 00:00\",100\n"
        b"4/1/2026 0:00,200\n"
    )
    buffer = BytesIO(csv_bytes)
    dest_cols = [{"column_name": "due_date", "data_type": "datetime"}]
    config = IngestionConfig(
        id=1,
        sharepoint_base_url="https://example.sharepoint.com",
        sharepoint_process_folder="/sites/test/Shared Documents/Inbox",
        excel_tab_name="Sheet1",
        sharepoint_process_archive_folder=None,
        sharepoint_process_failed_folder=None,
        process_frequency=None,
        header_skip_rows=0,
        check_source_dest_columns=False,
        multi_file_ingest=False,
        to_email_address=None,
        process_id="p1",
        workflow_id="wf1",
        staging_table_name="sharepoint.test_table",
    )

    # Must not raise; mixed text+date column must be absent from the hints dict
    hints = engine._infer_csv_datetime_order_hints(buffer, config, dest_cols)
    assert "due_date" not in hints


def test_detect_excel_datetime_stored_as_text_ignores_framework_managed_audit_fields() -> None:
    engine = make_engine()
    source = pd.DataFrame(
        {
            "sp_ingest_load_dt": ["01/01/2025", "31/01/2025"],
            "audit_id": [100, 101],
            "__$job_instance_id": [200, 201],
            "customer_id": ["C1", "C2"],
        }
    )
    dest_cols = [
        {"column_name": "sp_ingest_load_dt", "data_type": "datetime2"},
        {"column_name": "audit_id", "data_type": "bigint"},
        {"column_name": "__$job_instance_id", "data_type": "int"},
        {"column_name": "customer_id", "data_type": "varchar"},
    ]

    assert engine._detect_excel_datetime_text_issues(source, dest_cols) == []
