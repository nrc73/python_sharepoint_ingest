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
src/                      # ingestion framework source code
tests/                    # unit tests and sample artifacts
tools/                    # helper scripts (including sample artifact generation)
```

---

## Sample data artifacts (current phase)

Generate all valid/invalid sample CSV/XLSX artifacts:

```powershell
python tools/generate_sample_artifacts.py
```

Output location:

- `tests/sample_artifacts/valid/excel/`
- `tests/sample_artifacts/valid/csv/`
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
copy .env.example .env
```

3) Start local SQL container

```powershell
powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\create_sql_container.ps1
```

4) Bootstrap SQL schema

```powershell
python sharepoint_setup\bootstrap_sql_schema.py --env prod
```

5) Validate setup

```powershell
python sharepoint_setup\keyvault_secret_test.py --env prod
python sharepoint_setup\sql_connection_test.py --env prod
python sharepoint_setup\sharepoint_auth_test.py --env prod --folder "/sites/data_ingestion_prod/General/Input for ETL"
```

---

## Disable Python cache artifacts (`.pyc` / `__pycache__`)

If you do not want local cache files generated during normal runs, set:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
```

And prefer `python -B` for explicit commands, for example:

```powershell
python -B -m pytest -q
python -B -m src.main --env prod --dry-run
```

This repo also disables pytest's `.pytest_cache` plugin output by default.

---

## Configuration model target

Target control table for upcoming migration updates:

- `dbo.config_sharepoint_ingestion`

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
- `ingest_type` (`append` / `replace`)
- `add_columns` (`sheet_name` / `file_name`)

Retrospective compatibility fields remain in scope (`process_id`, `workflow_id`).

---

## SSIS execution target state

After standalone validation is complete, the Python runner can be executed from SSIS (Execute Process Task), for example:

```text
python -m src.main --env prod --process-id <GUID>
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
