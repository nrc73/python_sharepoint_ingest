from __future__ import annotations

import pandas as pd

from src.schema_validator import validate_source_against_destination


def test_schema_validator_detects_missing_and_additional_columns() -> None:
    source = pd.DataFrame(
        {
            "business_key": ["A", "B"],
            "name": ["foo", "bar"],
            "extra_column": [1, 2],
        }
    )

    destination_columns = [
        {
            "column_name": "business_key",
            "data_type": "nvarchar",
            "character_maximum_length": 50,
        },
        {
            "column_name": "name",
            "data_type": "nvarchar",
            "character_maximum_length": 50,
        },
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
        },
    ]

    issues = validate_source_against_destination(source, destination_columns)
    codes = {i.code for i in issues}

    assert "MISSING_DEST_COLUMNS_IN_SOURCE" in codes
    assert "ADDITIONAL_SOURCE_COLUMNS" in codes


def test_schema_validator_detects_type_mismatch() -> None:
    source = pd.DataFrame({"amount": ["ABC", "XYZ"]})
    destination_columns = [
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
        }
    ]

    issues = validate_source_against_destination(source, destination_columns)
    assert any(i.code == "TYPE_MISMATCH" for i in issues)


def test_schema_validator_detects_numeric_precision_exceeded() -> None:
    source = pd.DataFrame({"amount": [123456.78, 12.34]})
    destination_columns = [
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
            "numeric_precision": 5,
            "numeric_scale": 2,
        }
    ]

    issues = validate_source_against_destination(source, destination_columns)
    assert any(i.code == "NUMERIC_PRECISION_EXCEEDED" for i in issues)


def test_schema_validator_detects_numeric_scale_exceeded() -> None:
    source = pd.DataFrame({"amount": [123.456, 10.12]})
    destination_columns = [
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
            "numeric_precision": 8,
            "numeric_scale": 2,
        }
    ]

    issues = validate_source_against_destination(source, destination_columns)
    assert any(i.code == "NUMERIC_SCALE_EXCEEDED" for i in issues)


def test_schema_validator_numeric_within_precision_scale_has_no_numeric_exceeded_issues() -> None:
    source = pd.DataFrame({"amount": [99.99, 10.12]})
    destination_columns = [
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
            "numeric_precision": 5,
            "numeric_scale": 2,
        }
    ]

    issues = validate_source_against_destination(source, destination_columns)
    codes = {i.code for i in issues}
    assert "NUMERIC_PRECISION_EXCEEDED" not in codes
    assert "NUMERIC_SCALE_EXCEEDED" not in codes


def test_schema_validator_ignores_managed_destination_columns_for_missing_check() -> None:
    source = pd.DataFrame(
        {
            "transaction_id": ["TXN000001"],
            "amount": [10.5],
        }
    )

    destination_columns = [
        {
            "column_name": "transaction_id",
            "data_type": "varchar",
            "character_maximum_length": 20,
        },
        {
            "column_name": "amount",
            "data_type": "decimal",
            "character_maximum_length": None,
            "numeric_precision": 18,
            "numeric_scale": 2,
        },
        {
            "column_name": "created_date",
            "data_type": "datetime2",
            "character_maximum_length": None,
        },
        {
            "column_name": "modified_date",
            "data_type": "datetime2",
            "character_maximum_length": None,
        },
    ]

    issues = validate_source_against_destination(source, destination_columns)
    codes = {i.code for i in issues}

    assert "MISSING_DEST_COLUMNS_IN_SOURCE" not in codes
