# SharePoint Ingestion Framework (Python)

A config-driven Python ingestion framework to replace interim Logic App steps for SharePoint-to-SQL ingestion.

This repository is being delivered in phases. The current phase includes repeatable sample data artifact generation and documentation updates to support end-to-end testing design.

---

## Updated processing objectives

### Daily data ingestion migration to SharePoint

- loop through all files for ingestion (`multi_file_ingest`)
- add `file_name` as a data column where configured
- skip header rows for CSV/Excel (`header_skip_rows`)
- move files to processed/archive and failed folders

### Data ingestion behavior

- convert source CamelCase columns to destination `snake_case`
- validate unacceptable source types vs destination table types
- support Excel multi-tab ingestion and multi-file Excel ingestion
- support CSV chunked ingestion for large files
- support Parquet ingestion (single-file and multi-file), with optional chunked processing

### Error and edge-case coverage

- multiple datasets in one worksheet
- mixed data types in a column (for example date values in numeric columns)
- invalid/mixed date formats including locale tags (for example `[$-en-AU]`, `[$-en-US]`)
- destination columns not found / source columns missing
- process not found / destination table not found / staging table not found
- configured Excel tabs not found
- invalid SharePoint base URL
- NOT NULL population issues
- schema changes (additional columns, type changes, column reordering)
- **APPEND reload primary key violations** — intra-file duplicate detection + SQL `IntegrityError` wrapping with dedicated remediation email

### Load strategies

| Strategy | Behaviour | PK risk |
|---|---|---|
| `TRUNCATE` (default) | Destination table is truncated before each load. Safe to reload any number of times. | None |
| `APPEND` | Rows are inserted without clearing the table. Re-processing the same file will cause a PK violation if the table has a unique/PK index. | **Yes — see below** |

### APPEND reload — primary key protection

When `load_strategy = APPEND` is used, the engine applies a **two-layer defence** against PK violations:

1. **Intra-file pre-flight check** — before any SQL write, the engine scans the incoming DataFrame for rows that share the same value(s) on `merge_key_columns`.  If duplicates are found, a `PRIMARY_KEY_VIOLATION` error is raised immediately and no insert is attempted.  For chunked CSV this check runs on the first chunk, preventing partial commits.

2. **SQL `IntegrityError` wrapper** — if the pre-flight check passes but the database still rejects the insert (because the same key already exists from a prior run), `append_load` catches the SQLAlchemy `IntegrityError` and re-raises it with the `PRIMARY_KEY_VIOLATION:` prefix.

In both cases the failure is routed to a **dedicated notification email** that includes the table name, key columns, duplicate count, sample key values, and two remediation options (FULL RELOAD / MANUAL CLEAN).

> See [`docs/PERFORMANCE_AND_RESOURCE_HANDLING.md`](docs/PERFORMANCE_AND_RESOURCE_HANDLING.md#append-reload--primary-key-violation-risks) for the full reload workflow and configuration guidance.

## Validation and notification reference

For a consolidated, single-source view of all validation families, notification routing,
and audit-log interpretation, see:

- [`docs/VALIDATION_AND_NOTIFICATION_REFERENCE.md`](docs/VALIDATION_AND_NOTIFICATION_REFERENCE.md)

## Testing reference

For detailed pytest usage guidance (what it covers, when to run it, scenario-based command matrix, and limitations), see:

- [`PYTEST_TESTING_GUIDE.md`](PYTEST_TESTING_GUIDE.md)

## Source → pandas → SQL destination type mapping (CSV / Excel / Parquet)

This is the simplified type reference used when validating source files against SQL destination table columns.

| Source value shape (CSV/Excel/Parquet) | Typical pandas dtype | Recommended SQL destination type(s) |
|---|---|---|
| Whole numbers (`1`, `200`, `-5`) | `int64` / `Int64` | `INT`, `BIGINT`, `SMALLINT` |
| Decimal numbers (`10.5`, `99.99`) | `float64` | `DECIMAL(p,s)`, `NUMERIC(p,s)`, `FLOAT` |
| True/False flags (`true/false`, `1/0`) | `bool` / `boolean` | `BIT` |
| Dates only (`2026-05-25`) | `datetime64[ns]` | `DATE` |
| Date + time (`2026-05-25 14:30:00`) | `datetime64[ns]` | `DATETIME`, `DATETIME2` |
| Text / codes (`AU`, `CUST001`, free text) | `object` / `string` | `VARCHAR(n)`, `NVARCHAR(n)`, `VARCHAR(MAX)` |
| UUID-like text | `object` / `string` | `UNIQUEIDENTIFIER` (if strictly UUID), else `VARCHAR` |
| Binary payloads (rare in these ingestions) | `object` / bytes-like | `VARBINARY` |

### Format notes by source type

- **CSV**: values are read as text first; numeric/date interpretation depends on parsing + normalization.
- **Excel**: dates may arrive as true Excel date cells *or* text-looking dates. Date-like text is validated/warned.
- **Parquet**: usually carries stronger native typing, so pandas dtypes are often closest to final SQL types.

### Destination system fields (not expected in source files)

The framework manages these destination fields automatically when present:

- `sp_ingest_load_dt` (`DATETIME`)
- `__$batch_id` (`INT NULL`)
- `__$job_instance_id` (`INT NULL`)

### CLI diagnostics objectives

- `--dry-run` should output issues to screen without email/table logging
- `--dry-run-all` should scan/report issues for all configured processes
- `--suggest-table-schema` should infer SQL column names/types from source data, including `file_name` / `sheet_name` where configured

---

## Project structure

```text
docs/                     # architecture and sequence documentation
sharepoint_setup/         # setup and verification scripts
sql/                      # SQL bootstrap scripts
sharepoint_ingest/        # ingestion framework source code
tests/                    # unit tests and sample artifacts
tools/                    # helper scripts (including sample artifact generation)
```

---

## Sample data artifacts (current phase)

Generate all valid/invalid sample CSV/XLSX artifacts:

```powershell
python tools/generate_sample_artifacts.py
```

Generate an optional large Parquet artifact (for example ~2GB, 20+ columns):

```powershell
python tools/generate_sample_artifacts.py --large-parquet --target-size-gb 2 --parquet-columns 20
```

Output location:

- `tests/sample_artifacts/valid/excel/`
- `tests/sample_artifacts/valid/csv/`
- `tests/sample_artifacts/valid/parquet/` (only when `--large-parquet` is used)
- `tests/sample_artifacts/invalid/excel/`
- `tests/sample_artifacts/invalid/csv/`

This includes three valid customer workbooks for multi-file Excel ingestion tests:

- `valid_customers_001.xlsx`
- `valid_customers_002.xlsx`
- `valid_customers_003.xlsx`

And multiple valid CSV files (including a large chunking file):

- `valid_transactions_001.csv`
- `valid_transactions_002.csv`
- `valid_transactions_large.csv`
- optional large Parquet output in `valid/parquet/` (for example `valid_transactions_large_2_0gb.parquet`)

See `tests/sample_artifacts/README.md` for detailed file-by-file intent.

---

## Quick start

1) Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2) Configure environment

```powershell
python tools\bootstrap_env.py
```

This bootstrap command will:

- create `.env` from `.env.example` when `.env` does not exist
- validate that `KEY_VAULT_URL` is present and non-empty
- leave existing `.env` unchanged

3) Ensure local SQL Server is running

- Use your desktop SQL Server instance (default or named instance).
- Recommended local host setting in `.env`: `SQL_SERVER_HOST=.`

4) Bootstrap SQL schema

```powershell
python sharepoint_setup\bootstrap_sql_schema.py --env prod
```

> Guard rails: `sql/bootstrap.sql` now enforces PROD-only execution and blocks
> insertion/update of `TEST` / `VALIDATION` / `PERF_TEST` / `sample_artifacts`
> config rows in `config.sharepoint_ingestion`.

5) Validate production guard rails (recommended DevOps preflight gate)

```powershell
python tools\validate_prod_guardrails.py --env prod
```

This command exits non-zero when test/sample config rows are present in prod.

6) Apply sample artifact config scripts in DEV only

```powershell
python sharepoint_setup\bootstrap_sql_schema.py --env dev --script sql/setup_ingest_dev_valid_artifacts.sql
python sharepoint_setup\bootstrap_sql_schema.py --env dev --script sql/setup_ingest_dev_invalid_artifacts.sql
```

Both scripts now fail fast if executed against any database other than
`ingest_dev`.

### DEV-only discovery helper

`tools/discover_new_ingestion.py` is intentionally **DEV-only** and now enforces this at runtime.

Use:

```powershell
python tools\discover_new_ingestion.py --env dev --base-folder "/sites/data_ingest_dev/Documents/<your_folder>"
```

Notes:

- `--env` only accepts `dev`
- running against non-dev environments is blocked with a fail-fast error
- for SharePoint paths, pass a folder under the `Documents` library (not a library name as a root)

7) Validate setup

```powershell
python sharepoint_setup\keyvault_secret_test.py --env prod
python sharepoint_setup\sql_connection_test.py --env prod
python sharepoint_setup\sharepoint_auth_test.py --env prod --folder "/sites/data_ingestion_prod/Documents"
```

---

## Logging and log retention

- Runtime logs are written to the local `logs/` folder.
- Log files use the pattern `sharepoint_ingestion_YYYYMMDD_HHMMSS.log`.
- On each process start (when logging is configured), automatic retention keeps only the latest **10** matching ingestion log files.
- Older matching log files are deleted automatically; unrelated files in `logs/` are not touched.

---

## Disable Python cache artifacts (`.pyc` / `__pycache__`)

If you do not want local cache files generated during normal runs, set:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
```

And prefer `python -B` for explicit commands, for example:

```powershell
python -B -m pytest -q
python -B -m sharepoint_ingest.main --env prod --dry-run
```

This repo also disables pytest's `.pytest_cache` plugin output by default.

---

## Configuration model target

Target control table for upcoming migration updates:

- `config.sharepoint_ingestion`

Including fields such as:

- `process_name`
- `sharepoint_base_url_prod`
- `sharepoint_base_url_dev`
- `sharepoint_process_folder`
- `excel_tab_names`
- `header_skip_rows`
- `multi_file_ingest`
- `staging_table_name`
- `file_type`
- `load_strategy` (`TRUNCATE` / `APPEND`)
- `merge_key_columns` (comma-separated key columns for APPEND duplicate detection and PK-violation diagnostics)
- `ingest_type` (`append` / `replace`)
- `add_columns` (`sheet_name` / `file_name`)

Retrospective compatibility fields remain in scope (`process_id`, `workflow_id`).

---

## SSIS execution target state

After standalone validation is complete, the Python runner can be executed from SSIS (Execute Process Task), for example:

```text
python -m sharepoint_ingest.main --env prod --ingestion-scope real --process-id <GUID>
```

To run sample/test artifact ingestions explicitly:

```text
python -m sharepoint_ingest.main --env dev --ingestion-scope test
```

Planned validation includes standalone Python runs first, then SSIS package integration tests (for example via Visual Studio Community + SSDT and SQL tooling).

---

## Delivery phases

1. **Phase 1 (current):** sample artifact generation + documentation update
2. **Phase 2:** config table population scripts + destination table DDL
3. **Phase 3:** ingestion engine updates for the new objectives/flags
4. **Phase 4:** standalone validation, then SSIS execution validation

---

## Security notes

- No credentials should be hardcoded in source files.
- Production use should prefer Azure Key Vault for SharePoint/SQL secrets.
- `.env` remains git-ignored.
