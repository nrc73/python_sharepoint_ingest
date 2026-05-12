from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "tests" / "sample_artifacts"


def _ensure_dirs() -> dict[str, Path]:
    paths = {
        "valid_excel": OUTPUT_ROOT / "valid" / "excel",
        "valid_csv": OUTPUT_ROOT / "valid" / "csv",
        "invalid_excel": OUTPUT_ROOT / "invalid" / "excel",
        "invalid_csv": OUTPUT_ROOT / "invalid" / "csv",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def _build_customers_rows(file_seq: int, start_id: int, count: int, region: str) -> list[dict]:
    rows: list[dict] = []
    base_date = datetime(2024, 1, 1) + timedelta(days=file_seq * 7)

    for i in range(count):
        idx = start_id + i
        signup_date = base_date + timedelta(days=i)
        rows.append(
            {
                "CustomerId": f"CUST{idx:05d}",
                "CustomerName": f"Customer {idx}",
                "SignupDate": signup_date.date(),
                "CreditLimit": round(1500 + (i * 35.75) + (file_seq * 50), 2),
                "IsActive": "Y" if i % 5 != 0 else "N",
                "RegionCode": region,
                "SourceSystem": "CRM",
            }
        )
    return rows


def _apply_signup_date_number_format(writer: pd.ExcelWriter, sheet_name: str, format_code: str) -> None:
    ws = writer.book[sheet_name]
    headers = [cell.value for cell in ws[1]]
    if "SignupDate" not in headers:
        return

    signup_col_idx = headers.index("SignupDate") + 1
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=signup_col_idx).number_format = format_code


def generate_valid_excel_files(valid_excel_dir: Path) -> None:
    for n in (1, 2, 3):
        file_path = valid_excel_dir / f"valid_customers_{n:03d}.xlsx"

        au_rows = _build_customers_rows(file_seq=n, start_id=1000 + (n * 100), count=8, region="AU")
        us_rows = _build_customers_rows(file_seq=n, start_id=2000 + (n * 100), count=8, region="US")

        with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
            pd.DataFrame(au_rows).to_excel(writer, sheet_name="Customers_AU", index=False)
            pd.DataFrame(us_rows).to_excel(writer, sheet_name="Customers_US", index=False)

            # Explicit display formats per worksheet while preserving true Excel date values.
            _apply_signup_date_number_format(writer, "Customers_AU", "d/mm/yyyy;@")
            _apply_signup_date_number_format(writer, "Customers_US", "m/d/yyyy;@")


def generate_valid_csv_files(valid_csv_dir: Path) -> None:
    file_1 = valid_csv_dir / "valid_transactions_001.csv"
    file_2 = valid_csv_dir / "valid_transactions_002.csv"
    file_large = valid_csv_dir / "valid_transactions_large.csv"

    def _rows(offset: int, count: int) -> list[dict]:
        rows: list[dict] = []
        for i in range(count):
            idx = offset + i
            rows.append(
                {
                    "TransactionId": f"TXN{idx:06d}",
                    "CustomerId": f"CUST{1000 + (idx % 50):05d}",
                    "TransactionDate": (datetime(2025, 1, 1) + timedelta(days=i % 90)).date().isoformat(),
                    "Amount": round(20 + (i * 1.87), 2),
                    "Currency": "AUD" if i % 2 == 0 else "USD",
                    "Status": "COMPLETE",
                }
            )
        return rows

    pd.DataFrame(_rows(offset=1, count=25)).to_csv(file_1, index=False)
    pd.DataFrame(_rows(offset=1000, count=25)).to_csv(file_2, index=False)

    # Large CSV test fixture for chunking + header skip behavior:
    # - triple prior row volume (12,000 -> 36,000)
    # - add two leading non-data rows before the header
    large_df = pd.DataFrame(_rows(offset=100000, count=36000))
    with file_large.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# preamble row 1 for skip-rows testing\n")
        fh.write("# preamble row 2 for skip-rows testing\n")
        large_df.to_csv(fh, index=False)


def generate_invalid_csv_files(invalid_csv_dir: Path) -> None:
    mixed_types = pd.DataFrame(
        [
            {"RecordId": "R001", "Amount": 10.50, "EffectiveDate": "2025-01-31", "Quantity": 1},
            {"RecordId": "R002", "Amount": "ABC", "EffectiveDate": "31/01/2025", "Quantity": 2},
            {"RecordId": "R003", "Amount": "2025-03-01", "EffectiveDate": "not-a-date", "Quantity": "x"},
        ]
    )
    mixed_types.to_csv(invalid_csv_dir / "invalid_mixed_types.csv", index=False)

    not_null_and_missing = pd.DataFrame(
        [
            {"CustomerName": "No ID Customer", "SignupDate": "2025-01-01", "CreditLimit": 1000.0},
            {"CustomerName": "", "SignupDate": "2025-01-02", "CreditLimit": 1001.0},
            {"CustomerName": "Null Date Customer", "SignupDate": "", "CreditLimit": 1002.0},
        ]
    )
    not_null_and_missing.to_csv(invalid_csv_dir / "invalid_not_null_and_missing_columns.csv", index=False)

    datetime_stress = pd.DataFrame(
        [
            {"EventId": "E001", "EventDate": "2025-01-31", "LocaleTag": "[$-en-AU]"},
            {"EventId": "E002", "EventDate": "31/01/2025", "LocaleTag": "[$-en-AU]"},
            {"EventId": "E003", "EventDate": "01/31/2025", "LocaleTag": "[$-en-US]"},
            {"EventId": "E004", "EventDate": "31-01-25", "LocaleTag": "[$-en-AU]"},
            {"EventId": "E005", "EventDate": "2025/31/01", "LocaleTag": "[$-en-US]"},
            {"EventId": "E006", "EventDate": "2025-13-01", "LocaleTag": "[$-en-AU]"},
        ]
    )
    datetime_stress.to_csv(invalid_csv_dir / "invalid_datetime_stress.csv", index=False)


def generate_invalid_excel_files(invalid_excel_dir: Path) -> None:
    # 1) multiple datasets in same sheet + mixed type issues in numeric/date fields
    file_1 = invalid_excel_dir / "invalid_customers_multiple_datasets.xlsx"
    sheet_rows = pd.DataFrame(
        [
            {"CustomerId": "CUST90001", "CustomerName": "Bad Data 1", "SignupDate": "2025-01-01", "CreditLimit": 2500.0},
            {"CustomerId": "CUST90002", "CustomerName": "Bad Data 2", "SignupDate": "01/02/2025", "CreditLimit": "2025-03-05"},
            {"CustomerId": None, "CustomerName": None, "SignupDate": None, "CreditLimit": None},
            {"CustomerId": "CustomerId", "CustomerName": "CustomerName", "SignupDate": "SignupDate", "CreditLimit": "CreditLimit"},
            {"CustomerId": "CUST99991", "CustomerName": "Second Dataset 1", "SignupDate": "31/13/2025", "CreditLimit": 1500.0},
            {"CustomerId": "CUST99992", "CustomerName": "Second Dataset 2", "SignupDate": "text", "CreditLimit": "ABC"},
        ]
    )
    with pd.ExcelWriter(file_1, engine="openpyxl") as writer:
        sheet_rows.to_excel(writer, sheet_name="DATA", index=False)

    # 2) workbook intentionally missing expected tabs like Customers_AU / Customers_US
    file_2 = invalid_excel_dir / "invalid_missing_tabs.xlsx"
    with pd.ExcelWriter(file_2, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                {"CustomerId": "CUST81001", "CustomerName": "Only One Tab", "SignupDate": "2025-02-01", "CreditLimit": 1000.0}
            ]
        ).to_excel(writer, sheet_name="OnlySheet", index=False)

    # 3) additional unknown columns + likely truncation values
    file_3 = invalid_excel_dir / "invalid_additional_unknown_columns.xlsx"
    with pd.ExcelWriter(file_3, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                {
                    "CustomerId": "CUST82001",
                    "CustomerName": "X" * 400,
                    "SignupDate": "2025-02-01",
                    "CreditLimit": 1500.25,
                    "UnexpectedCode": "UNMAPPED_001",
                    "ProcessName": "does_not_exist",
                },
                {
                    "CustomerId": "",
                    "CustomerName": "Missing Key",
                    "SignupDate": "not-a-date",
                    "CreditLimit": "BAD_NUMBER",
                    "UnexpectedCode": "UNMAPPED_002",
                    "ProcessName": "does_not_exist",
                },
            ]
        ).to_excel(writer, sheet_name="Customers_AU", index=False)


def main() -> None:
    paths = _ensure_dirs()
    generate_valid_excel_files(paths["valid_excel"])
    generate_valid_csv_files(paths["valid_csv"])
    generate_invalid_csv_files(paths["invalid_csv"])
    generate_invalid_excel_files(paths["invalid_excel"])

    print(f"Sample artifacts generated under: {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
