from __future__ import annotations

import logging
from io import BytesIO

import pandas as pd
import pytest
import xlwt

from sharepoint_ingest.file_processors.csv_processor import iter_csv_chunks_from_buffer, read_csv_from_bytes
from sharepoint_ingest.file_processors.excel_processor import (
    EncryptedExcelPayloadError,
    InvalidExcelPayloadError,
    classify_excel_payload_format,
    detect_excel_payload_format,
    read_all_excel_sheets_from_bytes,
    read_excel_from_bytes,
)
import sharepoint_ingest.file_processors.excel_processor as excel_processor
from sharepoint_ingest.file_processors.parquet_processor import iter_parquet_chunks_from_buffer, read_parquet_from_bytes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_xls_payload(sheets: dict[str, list[list]] | None = None) -> bytes:
    """Create a minimal legacy OLE2/BIFF .xls payload using xlwt.

    *sheets* is a mapping of {sheet_name: [[row0_col0, row0_col1, ...], ...]}
    Defaults to a single sheet "DATA" with a header + two data rows.
    """
    if sheets is None:
        sheets = {"DATA": [["col_a", "col_b"], [1, 2], [3, 4]]}
    wb = xlwt.Workbook()
    for sheet_name, rows in sheets.items():
        ws = wb.add_sheet(sheet_name)
        for row_idx, row in enumerate(rows):
            for col_idx, val in enumerate(row):
                ws.write(row_idx, col_idx, val)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


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


# ---------------------------------------------------------------------------
# Excel format detection tests
# ---------------------------------------------------------------------------


class TestDetectExcelPayloadFormat:
    """Unit tests for detect_excel_payload_format()."""

    def test_true_xlsx_detected_as_xlsx(self) -> None:
        out = BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            pd.DataFrame({"x": [1]}).to_excel(writer, index=False)
        assert detect_excel_payload_format(out.getvalue()) == "xlsx"

    def test_true_xls_detected_as_xls(self) -> None:
        payload = _make_xls_payload()
        assert detect_excel_payload_format(payload) == "xls"

    def test_xls_named_xlsx_detected_as_xls(self) -> None:
        """A legacy .xls payload disguised with a .xlsx extension is still detected as xls."""
        payload = _make_xls_payload()
        # The detection is payload-based — the file name is not consulted here.
        assert detect_excel_payload_format(payload) == "xls"

    def test_classify_html_payload(self) -> None:
        assert classify_excel_payload_format(b"  <html><body>login</body></html>") == "html_or_xml"

    def test_classify_empty_payload(self) -> None:
        assert classify_excel_payload_format(b"") == "empty"


class TestReadExcelFromBytesLegacyXls:
    """Integration tests: read_excel_from_bytes handles true .xls and disguised .xlsx files."""

    def test_reads_true_xls_single_sheet(self) -> None:
        payload = _make_xls_payload({"DATA": [["col_a", "col_b"], [10, 20], [30, 40]]})
        df = read_excel_from_bytes(payload, sheet_name="DATA", file_name="report.xls")
        assert list(df.columns) == ["col_a", "col_b"]
        assert list(df["col_a"]) == [10, 30]

    def test_reads_disguised_xls_as_xlsx_single_sheet(self, caplog: pytest.LogCaptureFixture) -> None:
        """A .xls payload with a .xlsx extension is read successfully and a warning is logged."""
        payload = _make_xls_payload({"DATA": [["col_a", "col_b"], [1, 2]]})
        with caplog.at_level(logging.WARNING, logger="sharepoint_ingest.file_processors.excel_processor"):
            df = read_excel_from_bytes(payload, sheet_name="DATA", file_name="report.xlsx")

        assert list(df.columns) == ["col_a", "col_b"]
        assert len(df) == 1
        assert any("format mismatch" in record.message.lower() for record in caplog.records)
        assert any("report.xlsx" in record.message for record in caplog.records)

    def test_reads_disguised_xls_as_xlsx_no_sheet_name(self) -> None:
        """First sheet is returned when sheet_name is omitted for a disguised .xls file."""
        payload = _make_xls_payload({"Sheet1": [["id", "val"], [99, "hello"]]})
        df = read_excel_from_bytes(payload, file_name="export.xlsx")
        assert list(df.columns) == ["id", "val"]
        assert list(df["id"]) == [99]

    def test_reads_disguised_xls_as_xlsm_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """The mismatch warning fires for .xlsm extension as well."""
        payload = _make_xls_payload()
        with caplog.at_level(logging.WARNING, logger="sharepoint_ingest.file_processors.excel_processor"):
            read_excel_from_bytes(payload, file_name="macro_report.xlsm")

        assert any("xlsm" in record.message for record in caplog.records)

    def test_true_xls_no_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """No warning is logged when the file name extension correctly matches the .xls payload."""
        payload = _make_xls_payload()
        with caplog.at_level(logging.WARNING, logger="sharepoint_ingest.file_processors.excel_processor"):
            read_excel_from_bytes(payload, file_name="report.xls")

        assert len(caplog.records) == 0

    def test_no_file_name_xls_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When no file_name is provided, .xls payload is read silently (extension unknown)."""
        payload = _make_xls_payload()
        with caplog.at_level(logging.WARNING, logger="sharepoint_ingest.file_processors.excel_processor"):
            df = read_excel_from_bytes(payload)

        assert len(df) > 0
        assert len(caplog.records) == 0

    def test_encrypted_ole2_payload_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Encrypted OOXML workbooks are OLE2 containers but are not readable by xlrd."""
        payload = excel_processor._OLE2_MAGIC + b"encrypted-placeholder"
        monkeypatch.setattr(
            excel_processor,
            "_ole2_stream_names",
            lambda _payload: {"encryptioninfo", "encryptedpackage"},
        )

        with pytest.raises(EncryptedExcelPayloadError, match="Encrypted Excel payload"):
            read_excel_from_bytes(payload, file_name="protected.xlsx")

    def test_invalid_ole2_payload_without_workbook_stream_raises_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload = excel_processor._OLE2_MAGIC + b"not-a-workbook"
        monkeypatch.setattr(
            excel_processor,
            "_ole2_stream_names",
            lambda _payload: {"worddocument"},
        )

        with pytest.raises(InvalidExcelPayloadError, match="no BIFF Workbook/Book stream"):
            read_excel_from_bytes(payload, file_name="not_excel.xlsx")

    def test_plain_text_payload_named_xlsx_raises_clear_error(self) -> None:
        with pytest.raises(InvalidExcelPayloadError, match="plain text"):
            read_excel_from_bytes(b"a,b\n1,2\n", file_name="export.xlsx")


class TestReadAllExcelSheetsLegacyXls:
    """Integration tests: read_all_excel_sheets_from_bytes handles .xls payloads."""

    def test_reads_true_xls_multiple_sheets(self) -> None:
        payload = _make_xls_payload({
            "Alpha": [["x"], [1], [2]],
            "Beta": [["y"], [3]],
        })
        sheets = read_all_excel_sheets_from_bytes(payload, file_name="data.xls")
        assert set(sheets.keys()) == {"Alpha", "Beta"}
        assert list(sheets["Alpha"]["x"]) == [1, 2]
        assert list(sheets["Beta"]["y"]) == [3]

    def test_reads_disguised_xls_multiple_sheets(self, caplog: pytest.LogCaptureFixture) -> None:
        """All sheets are returned from a .xls payload disguised as .xlsx."""
        payload = _make_xls_payload({
            "Sheet1": [["a"], [10]],
            "Sheet2": [["b"], [20]],
        })
        with caplog.at_level(logging.WARNING, logger="sharepoint_ingest.file_processors.excel_processor"):
            sheets = read_all_excel_sheets_from_bytes(payload, file_name="combined.xlsx")

        assert set(sheets.keys()) == {"Sheet1", "Sheet2"}
        assert any("format mismatch" in record.message.lower() for record in caplog.records)
