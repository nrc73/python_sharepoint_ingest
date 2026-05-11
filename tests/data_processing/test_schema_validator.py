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
