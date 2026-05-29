from __future__ import annotations

from io import BytesIO
from typing import Iterator

import pandas as pd


_CSV_READ_KWARGS = {
    "sep": ",",
    "quotechar": '"',
    "doublequote": True,
}


def read_csv_from_bytes(payload: bytes, header_skip_rows: int = 0) -> pd.DataFrame:
    """Read CSV content from bytes with practical encoding fallbacks."""
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return pd.read_csv(
                BytesIO(payload),
                skiprows=max(header_skip_rows, 0),
                encoding=encoding,
                **_CSV_READ_KWARGS,
            )
        except UnicodeDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    return pd.read_csv(
        BytesIO(payload),
        skiprows=max(header_skip_rows, 0),
        **_CSV_READ_KWARGS,
    )


def iter_csv_chunks_from_buffer(
    buffer: BytesIO,
    header_skip_rows: int = 0,
    chunk_size: int = 5000,
) -> Iterator[pd.DataFrame]:
    """Yield CSV content from an in-memory buffer in DataFrame chunks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            buffer.seek(0)
            for chunk_df in pd.read_csv(
                buffer,
                skiprows=max(header_skip_rows, 0),
                encoding=encoding,
                chunksize=chunk_size,
                **_CSV_READ_KWARGS,
            ):
                yield chunk_df
            return
        except UnicodeDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    buffer.seek(0)
    for chunk_df in pd.read_csv(
        buffer,
        skiprows=max(header_skip_rows, 0),
        chunksize=chunk_size,
        **_CSV_READ_KWARGS,
    ):
        yield chunk_df
