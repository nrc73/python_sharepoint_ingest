from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# OLE2 Compound Document signature — present in all legacy .xls (BIFF) files.
_OLE2_MAGIC: bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC: bytes = b"PK\x03\x04"
_HTML_XML_MARKERS: tuple[bytes, ...] = (b"<html", b"<!doctype html", b"<table", b"<?xml")
_OLE2_WORKBOOK_STREAM_NAMES: set[str] = {"workbook", "book"}
_OLE2_ENCRYPTED_STREAM_NAMES: set[str] = {"encryptioninfo", "encryptedpackage"}


class ExcelPayloadError(ValueError):
    """Base class for Excel payload parsing/classification failures."""


class InvalidExcelPayloadError(ExcelPayloadError):
    """Raised when a payload is not a readable Excel workbook."""


class EncryptedExcelPayloadError(ExcelPayloadError):
    """Raised when an Excel payload appears to be encrypted/password-protected."""


def detect_excel_payload_format(payload: bytes) -> str:
    """Inspect the first bytes of *payload* to determine the actual Excel container format.

    Returns:
        ``"xls"``  — OLE2/BIFF legacy workbook (xlrd engine required).
        ``"xlsx"`` — OOXML ZIP-based workbook (openpyxl engine).
    """
    if payload[:8] == _OLE2_MAGIC:
        return "xls"
    return "xlsx"


def classify_excel_payload_format(payload: bytes) -> str:
    """Return a richer payload classification for diagnostics.

    This helper is intentionally conservative.  It does not decide whether an
    OLE2 document is definitely an Excel workbook; that requires stream
    inspection via :func:`_validate_ole2_excel_payload`.
    """
    if not payload:
        return "empty"
    if payload[:8] == _OLE2_MAGIC:
        return "xls_ole2"
    if payload.startswith(_ZIP_MAGIC):
        return "xlsx_zip"

    stripped = payload[:512].lstrip().lower()
    if any(stripped.startswith(marker) for marker in _HTML_XML_MARKERS):
        return "html_or_xml"
    if b"\x00" not in payload[:512]:
        return "plain_text"
    return "unknown_binary"


def _ole2_stream_names(payload: bytes) -> set[str] | None:
    """Return normalized OLE2 stream/storage names, or ``None`` if olefile is unavailable."""
    try:
        import olefile  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None

    with olefile.OleFileIO(BytesIO(payload)) as ole:
        entries = ole.listdir(streams=True, storages=True)

    names: set[str] = set()
    for entry in entries:
        joined = "/".join(str(part) for part in entry)
        leaf = str(entry[-1]) if entry else ""
        if joined:
            names.add(joined.strip().lower())
        if leaf:
            names.add(leaf.strip().lower())
    return names


def _validate_ole2_excel_payload(payload: bytes, file_name: str = "") -> None:
    """Validate that an OLE2 payload looks like a readable Excel workbook.

    Legacy BIFF ``.xls`` files contain a ``Workbook`` or ``Book`` stream.
    Password-protected/encrypted OOXML workbooks are also OLE2 containers but
    usually contain ``EncryptionInfo`` and ``EncryptedPackage`` streams instead.
    Those cannot be read by ``xlrd`` and are deliberately reported separately.
    """
    display_name = file_name or "<unknown>"

    try:
        stream_names = _ole2_stream_names(payload)
    except Exception as exc:
        raise InvalidExcelPayloadError(
            f"Invalid OLE2 Excel payload for '{display_name}': unable to inspect "
            f"the compound document streams ({type(exc).__name__}: {exc}). "
            "The file may be corrupt or truncated."
        ) from exc

    # If olefile is not installed, retain the previous behaviour: let xlrd try.
    if stream_names is None:
        return

    if stream_names & _OLE2_ENCRYPTED_STREAM_NAMES:
        raise EncryptedExcelPayloadError(
            f"Encrypted Excel payload for '{display_name}': payload is an OLE2 "
            "compound document containing EncryptionInfo/EncryptedPackage streams. "
            "The workbook appears password-protected or encrypted and cannot be "
            "read without decryption support."
        )

    if stream_names & _OLE2_WORKBOOK_STREAM_NAMES:
        return

    raise InvalidExcelPayloadError(
        f"Invalid OLE2 Excel payload for '{display_name}': no BIFF Workbook/Book "
        "stream was found in the compound document. The file may be corrupt, "
        "truncated, encrypted in an unsupported way, or not an Excel workbook."
    )


def _invalid_payload_error(payload_kind: str, file_name: str = "") -> InvalidExcelPayloadError:
    display_name = file_name or "<unknown>"
    hints = {
        "empty": "payload is empty",
        "html_or_xml": "payload looks like HTML/XML, possibly an error/login page or exported HTML table",
        "plain_text": "payload looks like plain text/CSV rather than an Excel workbook",
        "unknown_binary": "payload is not a ZIP/OpenXML workbook and not an OLE2 compound document",
    }
    hint = hints.get(payload_kind, f"unsupported payload kind '{payload_kind}'")
    return InvalidExcelPayloadError(f"Invalid Excel payload for '{display_name}': {hint}.")


def _choose_engine(payload: bytes, file_name: str = "") -> str:
    """Return the appropriate pandas Excel engine for *payload*.

    Logs a warning when the file extension disagrees with the actual payload
    format so that mis-labelled files are visible in the run log.
    """
    payload_kind = classify_excel_payload_format(payload)

    if payload_kind == "xls_ole2":
        _validate_ole2_excel_payload(payload, file_name=file_name)
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

    if payload_kind == "xlsx_zip":
        return "openpyxl"

    raise _invalid_payload_error(payload_kind, file_name=file_name)


def _read_excel_with_context(
    payload: bytes,
    *,
    sheet_name: str | int | None,
    header_skip_rows: int,
    file_name: str,
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    engine = _choose_engine(payload, file_name)
    try:
        return pd.read_excel(
            BytesIO(payload),
            sheet_name=sheet_name,
            skiprows=max(header_skip_rows, 0),
            engine=engine,
        )
    except ExcelPayloadError:
        raise
    except Exception as exc:
        payload_kind = classify_excel_payload_format(payload)
        display_name = file_name or "<unknown>"
        raise InvalidExcelPayloadError(
            f"Could not read Excel payload for '{display_name}' using engine='{engine}' "
            f"(detected payload kind='{payload_kind}'): {exc}"
        ) from exc


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
    resolved_sheet = sheet_name if sheet_name else 0
    result = _read_excel_with_context(
        payload,
        sheet_name=resolved_sheet,
        header_skip_rows=header_skip_rows,
        file_name=file_name,
    )
    return result  # type: ignore[return-value]


def read_all_excel_sheets_from_bytes(
    payload: bytes,
    header_skip_rows: int = 0,
    file_name: str = "",
) -> dict[str, pd.DataFrame]:
    """Read all worksheets from an Excel payload.

    Automatically selects the correct engine based on the actual payload format.
    See :func:`read_excel_from_bytes` for details.
    """
    result = _read_excel_with_context(
        payload,
        sheet_name=None,
        header_skip_rows=header_skip_rows,
        file_name=file_name,
    )
    return result  # type: ignore[return-value]
