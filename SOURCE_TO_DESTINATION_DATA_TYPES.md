# Source to Destination Data Type Mapping

This document explains how source file data types are interpreted and loaded into SQL Server destination columns in this ingestion framework, with a focus on **Parquet** behavior and common failure cases.

---

## 1) End-to-end type flow

| Step | What happens | Where implemented |
|---|---|---|
| Source file read | CSV/Excel/Parquet parsed into a Pandas DataFrame | `sharepoint_ingest/file_processors/*` |
| Optional rename | `column_mapping_json` renames source columns to destination names | `IngestionEngine._apply_column_mapping_if_present` |
| Metadata columns | `source_file_name` / `excel_tab_name` added when destination expects them | `IngestionEngine._apply_ingestion_metadata` |
| Normalization | String trim + destination datetime column coercion to datetime | `IngestionEngine._normalize_dataframe` |
| Validation | Source families checked against destination SQL families | `validate_source_against_destination` |
| SQL load | DataFrame loaded via SQLAlchemy/pyodbc (`to_sql`) | `SqlClient` |

---

## 2) Destination SQL type families used by validation

> Validation is based on **type families** (not exact SQL type equality).

| Destination family | SQL Server types included |
|---|---|
| Numeric | `int`, `bigint`, `smallint`, `tinyint`, `decimal`, `numeric`, `float`, `real`, `money`, `smallmoney` |
| Datetime | `date`, `datetime`, `datetime2`, `smalldatetime`, `datetimeoffset`, `time` |
| Bool | `bit` |
| String | `char`, `nchar`, `varchar`, `nvarchar`, `text`, `ntext`, `uniqueidentifier` |
| Binary | `binary`, `varbinary`, `image` |
| Other | Any other SQL type |

---

## 3) Source (Pandas) type families used by validation

| Pandas dtype pattern | Source family seen by validator |
|---|---|
| Integer dtype | Numeric |
| Float dtype | Numeric |
| Datetime dtype (`datetime64[...]`) | Datetime |
| Boolean dtype | Bool |
| Everything else (including most object/string columns) | String |

---

## 4) Compatibility matrix (what is accepted)

| Source family (after normalization) | Destination family | Outcome | Notes |
|---|---|---|---|
| Numeric | Numeric | ✅ Supported | `decimal`/`numeric` precision+scale limits still enforced. |
| Datetime | Datetime | ✅ Supported | Datetime conversion is attempted for destination datetime columns before validation. |
| Bool | Bool | ✅ Supported | Must be recognized as bool dtype by Pandas. |
| String | String | ✅ Supported | Max-length check enforced for bounded string columns (`varchar(n)`, `nvarchar(n)`, etc.). |
| String | Numeric | ❌ Blocking error | `TYPE_MISMATCH`. |
| String | Datetime | ❌ Blocking error (unless normalized first) | Datetime destination columns are normalized before validation; if conversion fails, processing fails. |
| String | Bool | ❌ Blocking error | `TYPE_MISMATCH`. |
| Numeric | Datetime | ❌ Blocking error* | `TYPE_MISMATCH` (except Excel serial date handling for datetime-targeted columns during normalization). |
| Any | Binary | ⚠️ Use caution | Binary family is classified, but validator compatibility is primarily enforced for numeric/datetime/bool families. |

\* Excel source has special datetime conversion behavior for serial-number date values.

---

## 5) Parquet-specific behavior

| Topic | Current behavior | User impact |
|---|---|---|
| Reader path | PyArrow Parquet → Pandas DataFrame (`to_pandas`) | Parquet logical/physical types are first mapped into Pandas dtypes. |
| Chunked processing | Row groups streamed and validated in chunks | Large files are processed without full in-memory download. |
| Validation timing | Issues are accumulated across chunks | Blocking errors fail before final destination swap. |
| SQL write path | Chunks loaded to transient temp table, then atomic swap/append | Destination table remains untouched when blocking validation errors occur. |

---

## 6) Common Parquet problem cases and what users will see

| Parquet/source scenario | Typical destination target | What users see | Validation/Error code |
|---|---|---|---|
| Text-like values in a numeric target column | `decimal`, `numeric`, `int`, etc. | Type mismatch failure | `TYPE_MISMATCH` |
| Decimal values exceed destination precision | `decimal(p,s)` | Precision overflow validation failure | `NUMERIC_PRECISION_EXCEEDED` |
| Decimal values exceed destination scale | `decimal(p,s)` | Scale overflow validation failure | `NUMERIC_SCALE_EXCEEDED` |
| Long text exceeds destination max length | `varchar(n)` / `nvarchar(n)` | Truncation-risk validation failure | `STRING_LENGTH_EXCEEDED` |
| Missing required destination columns | Any | Missing-column validation failure | `MISSING_DEST_COLUMNS_IN_SOURCE` |
| Additional source columns not in destination | Any | Warning only | `ADDITIONAL_SOURCE_COLUMNS` |
| Same columns, different order | Any | Warning only | `COLUMN_REORDERING_DETECTED` |

---

## 7) Validation code reference (quick lookup)

| Code | Severity | Meaning | Typical remediation |
|---|---|---|---|
| `TYPE_MISMATCH` | ERROR | Source family incompatible with destination family (numeric/datetime/bool checks) | Fix source type upstream or change destination SQL type. |
| `STRING_LENGTH_EXCEEDED` | ERROR | Source string length exceeds destination max length | Increase destination length or truncate/clean source. |
| `NUMERIC_PRECISION_EXCEEDED` | ERROR | Source total digits exceed destination precision | Increase destination precision or reduce source numeric size. |
| `NUMERIC_SCALE_EXCEEDED` | ERROR | Source fractional digits exceed destination scale | Increase destination scale or round source values. |
| `MISSING_DEST_COLUMNS_IN_SOURCE` | ERROR | Destination columns missing from source | Add/match required columns and mappings. |
| `ADDITIONAL_SOURCE_COLUMNS` | WARNING | Source has extra unmapped columns | Remove or ignore, or map explicitly. |
| `COLUMN_REORDERING_DETECTED` | WARNING | Source and destination column order differs | Usually safe; verify mapping and downstream expectations. |
| `HIGH_NULL_RATIO` | WARNING | Null ratio in a column exceeds configured threshold | Review source completeness/data quality. |

---

## 8) Practical guidance for users preparing Parquet files

| Recommendation | Why it helps |
|---|---|
| Keep source column names aligned to destination (or provide `column_mapping_json`) | Avoids missing/additional column issues. |
| Use explicit decimal precision/scale upstream when possible | Prevents precision/scale validation errors. |
| Keep string columns within destination max length | Prevents truncation-risk blocking errors. |
| Ensure datetime-targeted fields are true date/timestamp values where possible | Reduces parse/type mismatch risk. |
| Treat binary/complex/object-like columns as special cases | May require custom handling or destination redesign. |
