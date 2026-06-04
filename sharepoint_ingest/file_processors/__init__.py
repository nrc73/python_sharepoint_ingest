"""File parsers for SharePoint ingestion payloads."""

from .csv_processor import iter_csv_chunks_from_buffer, read_csv_from_bytes
from .excel_processor import (
    EncryptedExcelPayloadError,
    ExcelPayloadError,
    InvalidExcelPayloadError,
    classify_excel_payload_format,
    detect_excel_payload_format,
    read_all_excel_sheets_from_bytes,
    read_excel_from_bytes,
)
from .graph_excel_processor import (
    GraphExcelExtractionError,
    dataframe_from_used_range_values,
    read_excel_via_graph,
)
from .parquet_processor import (
    SharePointRangeReader,
    iter_parquet_chunks_from_buffer,
    iter_parquet_chunks_from_file,
    open_parquet_from_range_reader,
    read_parquet_from_bytes,
)

__all__ = [
    "read_csv_from_bytes",
    "iter_csv_chunks_from_buffer",
    "ExcelPayloadError",
    "InvalidExcelPayloadError",
    "EncryptedExcelPayloadError",
    "detect_excel_payload_format",
    "classify_excel_payload_format",
    "read_excel_from_bytes",
    "read_all_excel_sheets_from_bytes",
    "GraphExcelExtractionError",
    "dataframe_from_used_range_values",
    "read_excel_via_graph",
    "read_parquet_from_bytes",
    "iter_parquet_chunks_from_buffer",
    # Streaming range-based Parquet access (large remote files)
    "SharePointRangeReader",
    "open_parquet_from_range_reader",
    "iter_parquet_chunks_from_file",
]
