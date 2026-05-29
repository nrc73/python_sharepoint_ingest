from __future__ import annotations

from io import BytesIO

import pandas as pd

from sharepoint_ingest.file_processors.csv_processor import iter_csv_chunks_from_buffer, read_csv_from_bytes
from sharepoint_ingest.file_processors.excel_processor import read_all_excel_sheets_from_bytes, read_excel_from_bytes
from sharepoint_ingest.file_processors.parquet_processor import iter_parquet_chunks_from_buffer, read_parquet_from_bytes


def test_read_csv_from_bytes() -> None:
    payload = b"col1,col2\n1,2\n3,4\n"
    df = read_csv_from_bytes(payload)
    assert list(df.columns) == ["col1", "col2"]
    assert len(df) == 2


def test_iter_csv_chunks_from_buffer() -> None:
    payload = b"col1,col2\n1,2\n3,4\n5,6\n"
    chunks = list(iter_csv_chunks_from_buffer(BytesIO(payload), chunk_size=2))
    assert len(chunks) == 2
    assert list(chunks[0]["col1"]) == [1, 3]
    assert list(chunks[1]["col1"]) == [5]


def test_iter_csv_chunks_from_buffer_honors_skiprows() -> None:
    payload = b"ignore,this\ncol1,col2\n1,2\n3,4\n"
    chunks = list(iter_csv_chunks_from_buffer(BytesIO(payload), header_skip_rows=1, chunk_size=1))
    assert len(chunks) == 2
    assert list(chunks[0].columns) == ["col1", "col2"]
    assert list(chunks[0]["col1"]) == [1]


def test_read_csv_from_bytes_handles_mixed_quoted_and_unquoted_fields() -> None:
    payload = (
        b"id,name,description\n"
        b"1,alpha,simple\n"
        b"2,beta,\"long string, with comma and \"\"quoted\"\" text\"\n"
        b"3,gamma,after\n"
    )

    df = read_csv_from_bytes(payload)

    assert len(df) == 3
    assert df.loc[1, "description"] == 'long string, with comma and "quoted" text'


def test_iter_csv_chunks_from_buffer_handles_mixed_quoted_and_unquoted_fields() -> None:
    payload = (
        b"id,name,description\n"
        b"1,alpha,simple\n"
        b"2,beta,\"long string, with comma and \"\"quoted\"\" text\"\n"
        b"3,gamma,after\n"
    )

    chunks = list(iter_csv_chunks_from_buffer(BytesIO(payload), chunk_size=2))

    assert len(chunks) == 2
    assert list(chunks[0]["id"]) == [1, 2]
    assert chunks[0].loc[1, "description"] == 'long string, with comma and "quoted" text'
    assert list(chunks[1]["id"]) == [3]


def test_read_excel_single_sheet_from_bytes() -> None:
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1, 2]}).to_excel(writer, sheet_name="DATA", index=False)
    payload = out.getvalue()

    df = read_excel_from_bytes(payload, sheet_name="DATA")
    assert list(df.columns) == ["a"]
    assert len(df) == 2


def test_read_excel_all_sheets_from_bytes() -> None:
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="S1", index=False)
        pd.DataFrame({"a": [2]}).to_excel(writer, sheet_name="S2", index=False)
    payload = out.getvalue()

    all_sheets = read_all_excel_sheets_from_bytes(payload)
    assert set(all_sheets.keys()) == {"S1", "S2"}


def test_read_parquet_from_bytes() -> None:
    source = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    payload = source.to_parquet(index=False)

    parsed = read_parquet_from_bytes(payload)

    assert list(parsed.columns) == ["id", "value"]
    assert len(parsed) == 2


def test_iter_parquet_chunks_from_buffer() -> None:
    source = pd.DataFrame({"id": [1, 2, 3, 4, 5], "value": ["a", "b", "c", "d", "e"]})
    payload = source.to_parquet(index=False)

    chunks = list(iter_parquet_chunks_from_buffer(BytesIO(payload), chunk_size=2))

    assert len(chunks) == 3
    assert list(chunks[0]["id"]) == [1, 2]
    assert list(chunks[1]["id"]) == [3, 4]
    assert list(chunks[2]["id"]) == [5]
