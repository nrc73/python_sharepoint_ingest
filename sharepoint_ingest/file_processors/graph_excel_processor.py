from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pandas as pd

from sharepoint_ingest.ingestion._excel_utils import attach_excel_tab_name_column

if TYPE_CHECKING:
    from sharepoint_ingest.models import IngestionConfig


class GraphExcelExtractionError(ValueError):
    """Raised when Graph Excel workbook extraction cannot produce a DataFrame."""


def _normalise_header(value: Any, index: int) -> str:
    text = "" if value is None else str(value).strip()
    return text or f"Unnamed: {index}"


def _dedupe_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for column in columns:
        count = seen.get(column, 0)
        result.append(column if count == 0 else f"{column}.{count}")
        seen[column] = count + 1
    return result


def dataframe_from_used_range_values(
    values: list[list[Any]],
    *,
    header_skip_rows: int = 0,
) -> pd.DataFrame:
    """Convert a Graph Excel ``usedRange`` values array to a DataFrame.

    Graph returns a rectangular list-of-lists.  This mirrors the existing
    ``pandas.read_excel(..., skiprows=header_skip_rows)`` convention: skipped
    rows are ignored, the next row is treated as the header, and remaining rows
    become records.
    """
    rows = list(values or [])
    header_index = max(int(header_skip_rows or 0), 0)
    if len(rows) <= header_index:
        return pd.DataFrame()

    header_row = list(rows[header_index] or [])
    columns = _dedupe_columns(
        [_normalise_header(value, idx) for idx, value in enumerate(header_row)]
    )
    width = len(columns)
    if width == 0:
        return pd.DataFrame()

    records: list[list[Any]] = []
    for row in rows[header_index + 1 :]:
        current = list(row or [])
        if len(current) < width:
            current.extend([None] * (width - len(current)))
        records.append(current[:width])

    return pd.DataFrame(records, columns=columns)


def _worksheet_name(worksheet: dict) -> str:
    return str(worksheet.get("name") or "")


def _worksheet_id(worksheet: dict) -> str:
    value = worksheet.get("id") or worksheet.get("name")
    if not value:
        raise GraphExcelExtractionError("Worksheet metadata did not include id or name")
    return str(value)


def _select_worksheets(worksheets: list[dict], tab_name: str) -> tuple[list[dict], bool]:
    """Return selected worksheets and whether their names should be attached."""
    if not worksheets:
        raise GraphExcelExtractionError("Graph Excel workbook did not return any worksheets")

    resolved = (tab_name or "").strip()
    if not resolved:
        return [worksheets[0]], False

    if resolved.upper() in {"*", "ALL", "ALL_SHEETS"}:
        return worksheets, True

    if resolved.upper().startswith("REGEX:"):
        pattern = resolved.split(":", 1)[1].strip()
        regex = re.compile(pattern)
        matched = [ws for ws in worksheets if regex.search(_worksheet_name(ws))]
        if not matched:
            raise GraphExcelExtractionError(f"No worksheet names matched regex pattern: {pattern}")
        return matched, True

    for worksheet in worksheets:
        if _worksheet_name(worksheet) == resolved:
            return [worksheet], False

    raise GraphExcelExtractionError(f"Worksheet '{resolved}' was not found in Graph Excel workbook")


def read_excel_via_graph(
    sharepoint_client: Any,
    server_relative_url: str,
    config: IngestionConfig,
) -> pd.DataFrame:
    """Read an Excel workbook via Microsoft Graph workbook endpoints.

    This avoids downloading the encrypted workbook binary.  For sensitivity-label
    protected files, the Office Online/Graph workbook service performs the
    cloud-side open operation when the token and policy rights allow it.
    """
    session_id = sharepoint_client.create_excel_workbook_session(
        server_relative_url, persist_changes=False
    )
    try:
        worksheets = sharepoint_client.list_excel_worksheets(server_relative_url, session_id)
        selected, attach_names = _select_worksheets(worksheets, config.excel_tab_name)

        frames: list[pd.DataFrame] = []
        for worksheet in selected:
            used_range = sharepoint_client.get_excel_used_range(
                server_relative_url,
                session_id,
                _worksheet_id(worksheet),
                values_only=True,
            )
            dataframe = dataframe_from_used_range_values(
                list(used_range.get("values") or []),
                header_skip_rows=config.header_skip_rows,
            )
            if attach_names:
                dataframe = attach_excel_tab_name_column(dataframe, _worksheet_name(worksheet))
            frames.append(dataframe)

        if not frames:
            return pd.DataFrame()
        if len(frames) == 1:
            return frames[0]
        return pd.concat(frames, ignore_index=True)
    finally:
        sharepoint_client.close_excel_workbook_session(server_relative_url, session_id)