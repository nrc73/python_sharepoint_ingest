"""Excel worksheet parsing helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` to keep each module
focused.  Functions here are pure (no reference to engine instance state).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pandas as pd

from sharepoint_ingest.file_processors import (
    read_all_excel_sheets_from_bytes,
    read_excel_from_bytes,
)

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig


def attach_excel_tab_name_column(
    dataframe: pd.DataFrame, sheet_name: str
) -> pd.DataFrame:
    """Return a copy of *dataframe* with an ``excel_tab_name`` column set to
    *sheet_name*.  Reuses an existing case-insensitive column if found.
    """
    enriched = dataframe.copy()
    existing_column = next(
        (
            str(col)
            for col in enriched.columns
            if str(col).strip().lower() == "excel_tab_name"
        ),
        None,
    )
    target_column = existing_column or "excel_tab_name"
    enriched[target_column] = sheet_name
    return enriched


def parse_excel_payload(
    config: IngestionConfig,
    payload: bytes,
    file_name: str = "",
) -> pd.DataFrame:
    """Parse *payload* as an Excel workbook according to the tab selection
    rules in *config*.

    *file_name* is forwarded to the underlying Excel readers so that a warning
    is logged when a ``.xlsx``-named file is actually a legacy OLE2/BIFF
    ``.xls`` workbook.

    Supports:
    * blank / unset → first sheet
    * ``"*"`` / ``"ALL"`` / ``"ALL_SHEETS"`` → all sheets concatenated
    * ``"REGEX:<pattern>"`` → sheets matching the regex, concatenated
    * plain name → single named sheet
    """
    tab_name = (config.excel_tab_name or "").strip()

    if not tab_name:
        return read_excel_from_bytes(
            payload,
            sheet_name=None,
            header_skip_rows=config.header_skip_rows,
            file_name=file_name,
        )

    if tab_name.upper() in {"*", "ALL", "ALL_SHEETS"}:
        all_sheets = read_all_excel_sheets_from_bytes(
            payload,
            header_skip_rows=config.header_skip_rows,
            file_name=file_name,
        )
        if not all_sheets:
            return pd.DataFrame()
        ordered = [
            attach_excel_tab_name_column(all_sheets[name], name)
            for name in all_sheets
        ]
        return pd.concat(ordered, ignore_index=True)

    if tab_name.upper().startswith("REGEX:"):
        pattern = tab_name.split(":", 1)[1].strip()
        regex = re.compile(pattern)
        all_sheets = read_all_excel_sheets_from_bytes(
            payload,
            header_skip_rows=config.header_skip_rows,
            file_name=file_name,
        )
        matched = [
            attach_excel_tab_name_column(df, name)
            for name, df in all_sheets.items()
            if regex.search(name)
        ]
        if not matched:
            raise ValueError(f"No worksheet names matched regex pattern: {pattern}")
        return pd.concat(matched, ignore_index=True)

    return read_excel_from_bytes(
        payload,
        sheet_name=tab_name,
        header_skip_rows=config.header_skip_rows,
        file_name=file_name,
    )
