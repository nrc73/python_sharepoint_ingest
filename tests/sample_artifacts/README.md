# Sample data artifacts

This folder contains reusable sample files for ingestion testing.

## Folder layout

- `valid/excel/` – valid Excel files that should ingest cleanly
- `valid/csv/` – valid CSV files that should ingest cleanly
- `invalid/excel/` – Excel files intentionally containing data quality/schema issues
- `invalid/csv/` – CSV files intentionally containing data quality/schema issues

---

## Valid artifacts

### Multi-file Excel ingestion (3 files)

- `valid/excel/valid_customers_001.xlsx`
- `valid/excel/valid_customers_002.xlsx`
- `valid/excel/valid_customers_003.xlsx`

Each workbook includes tabs:

- `Customers_AU`
- `Customers_US`
- `Reference`

Use these for:

- multi-file Excel ingestion
- multi-tab ingestion
- CamelCase source column naming
- locale tag fields (`[$-en-AU]`, `[$-en-US]`) with valid values

### Multi-file and chunking CSV ingestion

- `valid/csv/valid_transactions_001.csv`
- `valid/csv/valid_transactions_002.csv`
- `valid/csv/valid_transactions_large.csv` (large dataset for chunking tests)

Use these for:

- multi-file CSV ingestion
- chunk-based ingestion validation

---

## Invalid artifacts

### CSV

- `invalid/csv/invalid_mixed_types.csv`
  - mixed types in numeric/date-related columns (type mismatch scenarios)

- `invalid/csv/invalid_not_null_and_missing_columns.csv`
  - missing key-like fields and blanks for NOT NULL validation scenarios

- `invalid/csv/invalid_datetime_stress.csv`
  - mixed/invalid datetime patterns and locale tags for datetime stress testing

### Excel

- `invalid/excel/invalid_customers_multiple_datasets.xlsx`
  - multiple datasets in the same sheet (repeated headers / section breaks)
  - invalid date and numeric values mixed into structured data

- `invalid/excel/invalid_missing_tabs.xlsx`
  - missing expected tabs (for configured tab-not-found checks)

- `invalid/excel/invalid_additional_unknown_columns.xlsx`
  - additional unknown columns not in destination
  - long text values (truncation-risk scenarios)
  - invalid values for type checks

---

## Notes on scenario coverage

These artifacts directly support source-data validation scenarios. Some requested scenarios are **configuration/runtime** scenarios and are typically tested via config rows and environment setup rather than data-only files, for example:

- process name not found in config
- destination/staging table not found
- invalid SharePoint base URL
- ingest mode differences (`append` / `replace`)

Those will be covered in the next phase with configuration seed scripts and destination table DDL.

---

## Regenerating artifacts

From repository root:

```powershell
python tools/generate_sample_artifacts.py
```
