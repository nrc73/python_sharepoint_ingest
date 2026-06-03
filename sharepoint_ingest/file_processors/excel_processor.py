from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# OLE2 Compound Document signature — present in all legacy .xls (BIFF) files.
_OLE2_MAGIC: bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_excel_payload_format(payload: bytes) -> str:
    """Inspect the first bytes of *payload* to determine the actual Excel container format.

    Returns:
        ``"xls"``  — OLE2/BIFF legacy workbook (xlrd engine required).
        ``"xlsx"`` — OOXML ZIP-based workbook (openpyxl engine).
    """
    if payload[:8] == _OLE2_MAGIC:
        return "xls"
    return "xlsx"


def _choose_engine(payload: bytes, file_name: str = "") -> str:
    """Return the appropriate pandas Excel engine for *payload*.

    Logs a warning when the file extension disagrees with the actual payload
    format so that mis-labelled files are visible in the run log.
    """
    actual_format = detect_excel_payload_format(payload)

    if actual_format == "xls":
        ext = file_name.lower().rsplit(".", 1)[-1] if file_name else ""
        if ext in ("xlsx", "xlsm"):
            logger.warning(
                "Excel format mismatch detected for '%s': "
                "file extension suggests '%s' but payload is a legacy .xls (OLE2/BIFF) workbook. "
                "Reading with xlrd.",
                file_name or "<unknown>",
                ext,
            )
        return "xlrd"

    return "openpyxl"


def read_excel_from_bytes(
    payload: bytes,
    sheet_name: Optional[str] = None,
    header_skip_rows: int = 0,
    file_name: str = "",
) -> pd.DataFrame:
    """Read a single Excel worksheet from bytes.

    Automatically selects the correct engine based on the actual payload format:

    * OOXML ZIP payloads (true ``.xlsx`` / ``.xlsm``) → ``openpyxl``
    * OLE2/BIFF payloads (legacy ``.xls``, including mis-labelled ``.xlsx``) → ``xlrd``

    If *sheet_name* is ``None`` or empty, the first worksheet is used.
    """
    engine = _choose_engine(payload, file_name)
    resolved_sheet = sheet_name if sheet_name else 0
    return pd.read_excel(
        BytesIO(payload),
        sheet_name=resolved_sheet,
        skiprows=max(header_skip_rows, 0),
        engine=engine,
    )


def read_all_excel_sheets_from_bytes(
    payload: bytes,
    header_skip_rows: int = 0,
    file_name: str = "",
) -> dict[str, pd.DataFrame]:
    """Read all worksheets from an Excel payload.

    Automatically selects the correct engine based on the actual payload format.
    See :func:`read_excel_from_bytes` for details.
    """
    engine = _choose_engine(payload, file_name)
    return pd.read_excel(
        BytesIO(payload),
        sheet_name=None,
        skiprows=max(header_skip_rows, 0),
        engine=engine,
    )
