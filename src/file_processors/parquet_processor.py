"""Parquet file processing utilities for SharePoint ingestion.

Supports both in-memory (bytes/buffer) and streaming range-based access so that
large Parquet files are **never fully downloaded** before processing.  PyArrow's
``ParquetFile`` is opened against a :class:`SharePointRangeReader`, which
satisfies all seek/read calls via HTTP range requests — typically 2–3 requests
to read the footer, then one request per row group during iteration.
"""
from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Iterator

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from sharepoint_ingest.sharepoint_client import SharePointClient


# ---------------------------------------------------------------------------
# In-memory helpers (small files / tests)
# ---------------------------------------------------------------------------


def read_parquet_from_bytes(payload: bytes) -> pd.DataFrame:
    """Read an entire Parquet payload into a single DataFrame."""
    table = pq.read_table(BytesIO(payload))
    return table.to_pandas()


def iter_parquet_chunks_from_buffer(buffer: BytesIO, chunk_size: int = 5000) -> Iterator[pd.DataFrame]:
    """Yield Parquet content in row-based DataFrame chunks via Arrow record batches."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    buffer.seek(0)
    parquet_file = pq.ParquetFile(pa.BufferReader(buffer.getvalue()))

    for batch in parquet_file.iter_batches(batch_size=chunk_size):
        yield pa.Table.from_batches([batch]).to_pandas()


# ---------------------------------------------------------------------------
# Streaming range-based helpers (large remote files)
# ---------------------------------------------------------------------------


class SharePointRangeReader:
    """Seekable file-like object backed by Microsoft Graph HTTP range requests.

    Allows PyArrow's ``ParquetFile`` to stream a remote Parquet file without
    downloading it in full.  PyArrow fetches only the Parquet footer (a few KB
    at the file tail) on construction, then fetches each row group independently
    as iteration proceeds — **one HTTP range request per row group**.

    Parameters
    ----------
    sharepoint_client:
        A :class:`~sharepoint_ingest.sharepoint_client.SharePointClient` instance.
    server_relative_url:
        SharePoint server-relative URL of the Parquet file.
    file_size:
        Total file size in bytes (from the Graph item metadata ``size`` field).
    download_url:
        Pre-authenticated CDN URL (``@microsoft.graph.downloadUrl`` from the
        Graph item).  When supplied, Authorization headers are skipped and the
        CDN redirect is avoided — significantly reducing per-request overhead.
    """

    def __init__(
        self,
        sharepoint_client: "SharePointClient",
        server_relative_url: str,
        file_size: int,
        download_url: str | None = None,
    ) -> None:
        self._sp = sharepoint_client
        self._url = server_relative_url
        self._download_url = download_url
        self._size = file_size
        self._pos: int = 0

    # ── file-like interface ───────────────────────────────────────────────────

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self._size - self._pos
        if size == 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + size - 1, self._size - 1)
        data = self._sp.download_file_range_bytes(
            self._url, self._pos, end, download_url=self._download_url
        )
        self._pos += len(data)
        return data

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        elif whence == 2:
            self._pos = self._size + pos
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    # ── misc ─────────────────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        """PyArrow checks ``source.closed`` before wrapping in ``PythonFile``."""
        return False

    def close(self) -> None:
        pass

    def __enter__(self) -> "SharePointRangeReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_parquet_from_range_reader(reader: SharePointRangeReader) -> pq.ParquetFile:
    """Open a :class:`~pyarrow.parquet.ParquetFile` from a :class:`SharePointRangeReader`.

    Only the Parquet footer is fetched at this point (2–3 range requests).
    Row-group data is fetched lazily during
    :meth:`~pyarrow.parquet.ParquetFile.iter_batches`.

    The returned ``ParquetFile`` can be iterated **multiple times** (e.g.
    once for validation, once for loading) — each ``iter_batches`` call
    creates a new iterator that seeks back to the first row group.
    """
    return pq.ParquetFile(reader)


def iter_parquet_chunks_from_file(
    parquet_file: pq.ParquetFile,
    chunk_size: int = 5000,
) -> Iterator[pd.DataFrame]:
    """Yield DataFrames from an open :class:`~pyarrow.parquet.ParquetFile`.

    Each batch corresponds to at most *chunk_size* rows.  Row groups are
    fetched on demand — for a :class:`SharePointRangeReader`-backed file this
    means one HTTP range request per row group.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    for batch in parquet_file.iter_batches(batch_size=chunk_size):
        yield pa.Table.from_batches([batch]).to_pandas()
