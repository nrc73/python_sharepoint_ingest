import os
import zipfile
import traceback
from pathlib import Path


# Optional but useful:
# pip install olefile xlrd openpyxl
try:
    import olefile
except ImportError:
    olefile = None

try:
    import xlrd
except ImportError:
    xlrd = None

try:
    import openpyxl
except ImportError:
    openpyxl = None


OLE2_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
ZIP_MAGIC = b"PK\x03\x04"
HTML_MARKERS = [b"<html", b"<!doctype html", b"<table", b"<?xml"]
CSV_MARKERS = [b",", b";", b"\t"]


def hexdump(data: bytes, width=16):
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{i:08X}  {hex_part:<{width * 3}}  {ascii_part}")
    return "\n".join(lines)


def diagnose_excel_file(filepath):
    path = Path(filepath)

    print("=" * 80)
    print(f"File: {path}")
    print("=" * 80)

    # Basic filesystem checks
    print("\n[Filesystem]")
    print(f"Exists: {path.exists()}")

    if not path.exists():
        return

    print(f"Is file: {path.is_file()}")
    print(f"Suffix: {path.suffix}")
    print(f"Name starts with ~$ lock marker: {path.name.startswith('~$')}")

    try:
        size = path.stat().st_size
        print(f"Size bytes: {size}")
    except Exception as e:
        print(f"[ERROR] Could not stat file: {e}")
        return

    if size == 0:
        print("[FAIL] File is empty.")
        return

    if size < 512:
        print("[WARN] File is very small; likely not a valid Excel workbook.")

    # Read header/sample
    try:
        with open(path, "rb") as f:
            header = f.read(4096)
    except Exception as e:
        print(f"[ERROR] Could not open file: {e}")
        return

    print("\n[First bytes]")
    print(hexdump(header[:128]))

    lower_header = header.lower().lstrip()

    # Signature detection
    is_zip = header.startswith(ZIP_MAGIC)
    is_ole2 = header.startswith(OLE2_MAGIC)
    looks_html = any(lower_header.startswith(marker) for marker in HTML_MARKERS)
    looks_text = b"\x00" not in header[:512] and not is_zip and not is_ole2

    print("\n[Format signatures]")
    print(f"ZIP signature: {is_zip}")
    print(f"OLE2/Compound File signature: {is_ole2}")
    print(f"Looks like HTML/XML: {looks_html}")
    print(f"Looks like plain text: {looks_text}")
    print(f"zipfile.is_zipfile: {zipfile.is_zipfile(path)}")

    ext = path.suffix.lower()

    if ext == ".xlsx" and is_ole2:
        print("[MISMATCH] Extension is .xlsx but payload is OLE2 legacy format.")
    elif ext == ".xls" and is_zip:
        print("[MISMATCH] Extension is .xls but payload is ZIP/OpenXML format.")
    elif ext in [".xlsx", ".xlsm", ".xltx", ".xltm"] and not is_zip:
        print("[MISMATCH] Extension suggests OpenXML, but file is not ZIP-based.")
    elif ext == ".xls" and not is_ole2:
        print("[MISMATCH] Extension suggests legacy .xls, but file is not OLE2.")

    if looks_html:
        print("[LIKELY ISSUE] File appears to be HTML/XML, possibly an error page, login page, or exported HTML table.")

    if looks_text:
        sample = header[:500].decode("utf-8", errors="replace")
        print("[INFO] Text sample:")
        print(sample)

    # ZIP / XLSX diagnostics
    if is_zip or zipfile.is_zipfile(path):
        print("\n[ZIP/OpenXML inspection]")

        try:
            with zipfile.ZipFile(path, "r") as z:
                bad_member = z.testzip()
                names = z.namelist()

                print(f"ZIP entries: {len(names)}")
                print(f"ZIP test result: {'OK' if bad_member is None else f'BAD MEMBER: {bad_member}'}")

                required_xlsx_parts = [
                    "[Content_Types].xml",
                    "_rels/.rels",
                    "xl/workbook.xml",
                ]

                for part in required_xlsx_parts:
                    print(f"Contains {part}: {part in names}")

                suspicious = [
                    name for name in names
                    if name.lower().endswith((".html", ".htm", ".txt"))
                ]

                if suspicious:
                    print(f"[WARN] Suspicious text/html entries: {suspicious[:10]}")

                print("\nFirst ZIP entries:")
                for name in names[:20]:
                    print(f"  - {name}")

        except zipfile.BadZipFile as e:
            print(f"[FAIL] Bad ZIP file: {e}")
        except Exception as e:
            print(f"[ERROR] ZIP inspection failed: {e}")
            print(traceback.format_exc())

    # OLE2 / XLS diagnostics
    if is_ole2:
        print("\n[OLE2 inspection]")

        if olefile is None:
            print("[INFO] olefile is not installed. Install with: pip install olefile")
        else:
            try:
                is_ole = olefile.isOleFile(str(path))
                print(f"olefile.isOleFile: {is_ole}")

                with olefile.OleFileIO(str(path)) as ole:
                    streams = ole.listdir(streams=True, storages=True)

                    print(f"OLE directory entries: {len(streams)}")
                    print("First OLE entries:")
                    for entry in streams[:30]:
                        print(f"  - {'/'.join(entry)}")

                    stream_names = {"/".join(entry) for entry in streams}

                    workbook_candidates = [
                        "Workbook",
                        "Book",
                        "WORKBOOK",
                        "BOOK",
                    ]

                    found_workbook_streams = [
                        s for s in stream_names
                        if s.split("/")[-1] in workbook_candidates
                    ]

                    print(f"Workbook stream candidates: {found_workbook_streams}")

                    if not found_workbook_streams:
                        print("[FAIL] OLE2 container does not appear to contain a BIFF Workbook/Book stream.")
                        print("       This matches xlrd's: Can't find workbook in OLE2 compound document.")

                    # Common non-workbook OLE streams
                    non_excel_markers = [
                        "WordDocument",
                        "PowerPoint Document",
                        "VisioDocument",
                        "\x05SummaryInformation",
                    ]

                    for marker in non_excel_markers:
                        matches = [s for s in stream_names if marker in s]
                        if matches:
                            print(f"[INFO] Found possible non-Excel OLE marker '{marker}': {matches[:5]}")

            except Exception as e:
                print(f"[FAIL] OLE2 inspection failed: {e}")
                print(traceback.format_exc())

    # Try reading as XLSX
    print("\n[Read attempts]")

    if openpyxl is None:
        print("[INFO] openpyxl not installed. Install with: pip install openpyxl")
    else:
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
            print("[OK] openpyxl read succeeded.")
            print(f"Sheets: {wb.sheetnames}")
            wb.close()
        except Exception as e:
            print(f"[FAIL] openpyxl read failed: {type(e).__name__}: {e}")

    # Try reading as XLS
    if xlrd is None:
        print("[INFO] xlrd not installed. Install with: pip install xlrd")
    else:
        try:
            book = xlrd.open_workbook(str(path), on_demand=True)
            print("[OK] xlrd read succeeded.")
            print(f"Sheets: {book.sheet_names()}")
            book.release_resources()
        except Exception as e:
            print(f"[FAIL] xlrd read failed: {type(e).__name__}: {e}")

    print("\n[Likely conclusion]")

    if path.name.startswith("~$"):
        print("- This looks like an Office temporary/lock file, not the workbook.")
    elif is_ole2 and ext == ".xlsx":
        print("- File is named .xlsx but is actually OLE2.")
        print("- If no Workbook/Book stream was found, it is probably not a readable Excel .xls workbook.")
    elif is_zip and ext == ".xlsx":
        print("- File is structurally ZIP/OpenXML. Check required XLSX parts above.")
    elif looks_html:
        print("- File is probably an HTML/XML response saved with an Excel extension.")
    elif looks_text:
        print("- File is probably CSV/text saved with an Excel extension.")
    else:
        print("- See failures above; file may be corrupted, truncated, encrypted, or not an Excel workbook.")


# Example
excel_filepath = r"C:\Users\chapmann\Downloads\2603-QPMBI-31032026.xlsx"   # --> most recent qtr file.  --> File opened by Jason manually and saved as .xlsx -> correct format
# excel_filepath = r"C:\Users\chapmann\Downloads\2512-QPMBI-20251231.xlsx" --> File opened by Jason manually and saved as .xlsx -> correct format
# excel_filepath = r"C:\Users\chapmann\Downloads\2512-QPMBI-20251231 - Copy.xlsx" --> Original File before by Jason manually opened and saved as 2512-QPMBI-20251231.xlsx -> file is actually OLE2 not .xls or xlsx
# excel_filepath = r"C:\Users\chapmann\Downloads\01-03-2026 1 - _Complaints_Management - Defence Health.xlsx" -> file is actually OLE2 not .xls or xlsx
diagnose_excel_file(excel_filepath)