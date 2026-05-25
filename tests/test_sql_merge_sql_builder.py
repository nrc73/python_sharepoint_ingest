from __future__ import annotations

from sharepoint_ingest.sql_client import SqlClient


def test_build_merge_sql_contains_expected_clauses() -> None:
    sql_text = SqlClient._build_merge_sql(
        schema="sharepoint",
        target_table="target_table",
        source_table="temp_table",
        source_columns=["business_key", "name", "amount"],
        merge_keys=["business_key"],
    )

    assert "MERGE [sharepoint].[target_table] AS target" in sql_text
    assert "USING [sharepoint].[temp_table] AS source" in sql_text
    assert "target.[business_key] = source.[business_key]" in sql_text
    assert "WHEN MATCHED THEN" in sql_text
    assert "WHEN NOT MATCHED BY TARGET THEN" in sql_text


def test_build_merge_sql_raises_for_missing_key_column() -> None:
    try:
        SqlClient._build_merge_sql(
            schema="sharepoint",
            target_table="target_table",
            source_table="temp_table",
            source_columns=["name", "amount"],
            merge_keys=["business_key"],
        )
        raised = False
    except ValueError:
        raised = True

    assert raised
