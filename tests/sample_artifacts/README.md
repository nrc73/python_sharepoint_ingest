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

Use these for:

- multi-file Excel ingestion
- multi-tab ingestion
- CamelCase source column naming
- worksheet-level locale date display formatting:
  - `Customers_AU` uses `d/mm/yyyy;@`
  - `Customers_US` uses `m/d/yyyy;@`

### Multi-file and chunking CSV ingestion

- `valid/csv/valid_transactions_001.csv`
- `valid/csv/valid_transactions_002.csv`
- `valid/csv/valid_transactions_large.csv` (1,000,000 rows, 20 columns for boundary/chunking tests)

Use these for:

- multi-file CSV ingestion
- chunk-based ingestion validation
- 1 million row boundary testing
- extended column-mapping validation (20 source columns)

---

## Invalid artifacts

### CSV

- `invalid/csv/invalid_mixed_types.csv`
  - mixed types in numeric/date-related columns (type mismatch scenarios)

- `invalid/csv/invalid_not_null_and_missing_columns.csv`
  - missing key-like fields and blanks for NOT NULL validation scenarios

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

- `invalid/excel/invalid_datetime_stress.xlsx`
  - datetime stress scenarios moved to Excel
  - AU/US worksheet date display formatting aligned with valid Excel files
  - includes intentionally invalid datetime strings for parser/type validation

- `invalid/excel/invalid_date_as_text.xlsx`
  - subtle datetime issue: values look like real dates (e.g. `01/01/2025`) but are stored/formatted as **text** (`@`) in Excel
  - used to validate detection + notification when datetime destination columns are sourced from Excel text cells

- `invalid/excel/invalid_numeric_overflow.xlsx`
  - numeric values that exceed typical `DECIMAL(p,s)` precision/scale limits
  - includes both precision-overflow and scale-overflow examples
  - used to validate numeric exceeded detection (`NUMERIC_PRECISION_EXCEEDED`, `NUMERIC_SCALE_EXCEEDED`)

---

## Notes on scenario coverage

These artifacts directly support source-data validation scenarios. Some requested scenarios are **configuration/runtime** scenarios and are typically tested via config rows and environment setup rather than data-only files, for example:

- process name not found in config
- destination/staging table not found
- invalid SharePoint base URL
- ingest mode differences (`append` / `replace`)

Those will be covered in the next phase with configuration seed scripts and destination table DDL.

---

## Excel/pandas to SQL destination type mapping (validation reference)

The ingestion pipeline parses Excel with pandas/openpyxl, then validates source columns against SQL destination metadata. The simplified type-family mapping used by validation logic is:

| Source (Excel/pandas observed) | Validator source family | SQL destination types accepted as matching family |
|---|---|---|
| Integer numeric cells | `numeric` | `int`, `bigint`, `smallint`, `tinyint`, `decimal`, `numeric`, `float`, `real`, `money`, `smallmoney` |
| Decimal/float numeric cells | `numeric` | `int`, `bigint`, `smallint`, `tinyint`, `decimal`, `numeric`, `float`, `real`, `money`, `smallmoney` |
| Date/datetime cells (`datetime64`/Timestamp) | `datetime` | `date`, `datetime`, `datetime2`, `smalldatetime`, `datetimeoffset`, `time` |
| Boolean cells | `bool` | `bit` |
| Text/object cells | `string` | `char`, `nchar`, `varchar`, `nvarchar`, `text`, `ntext`, `uniqueidentifier` |

Additional validation checks used by these artifacts:

- `MISSING_DEST_COLUMNS_IN_SOURCE` (error)
- `ADDITIONAL_SOURCE_COLUMNS` (warning)
- `TYPE_MISMATCH` (error for numeric/datetime/bool families)
- `STRING_LENGTH_EXCEEDED` (warning)
- `NUMERIC_PRECISION_EXCEEDED` / `NUMERIC_SCALE_EXCEEDED` (warning)
- `EXCEL_DATETIME_STORED_AS_TEXT` (warning, precheck for date-like text in Excel)

---

## Regenerating artifacts

From repository root:

```powershell
python tools/generate_sample_artifacts.py
```
