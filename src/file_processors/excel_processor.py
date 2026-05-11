from __future__ import annotations

from io import BytesIO
from typing import Optional

import pandas as pd


def read_excel_from_bytes(
    payload: bytes,
    sheet_name: Optional[str] = None,
    header_skip_rows: int = 0,
) -> pd.DataFrame:
    """
    Read a single Excel worksheet from bytes.

    If sheet_name is None or empty, the first worksheet is used.
    """
    resolved_sheet = sheet_name if sheet_name else 0
    return pd.read_excel(
        BytesIO(payload),
        sheet_name=resolved_sheet,
        skiprows=max(header_skip_rows, 0),
        engine="openpyxl",
    )


def read_all_excel_sheets_from_bytes(payload: bytes, header_skip_rows: int = 0) -> dict[str, pd.DataFrame]:
    """Read all worksheets from an Excel payload."""
    return pd.read_excel(
        BytesIO(payload),
        sheet_name=None,
        skiprows=max(header_skip_rows, 0),
        engine="openpyxl",
    )
