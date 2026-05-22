# LLM Context Guide

> **Purpose** — helps AI coding assistants orient quickly without needing to
> load the entire codebase into context.  Read this first, then open only the
> module(s) relevant to your task.

---

## Quick-reference: where things live

| What you're working on | Start here |
|---|---|
| Main business logic | `sharepoint_ingest/ingestion_engine.py` |
| Date/datetime parsing | `sharepoint_ingest/ingestion/_datetime_utils.py` |
| Excel tab selection | `sharepoint_ingest/ingestion/_excel_utils.py` |
| Load strategy (TRUNCATE/APPEND) | `sharepoint_ingest/ingestion/_load_strategy.py` |
| SharePoint path normalisation | `sharepoint_ingest/ingestion/_sharepoint_paths.py` |
| Metadata column enrichment | `sharepoint_ingest/ingestion/_metadata.py` |
| Issue/notification formatting | `sharepoint_ingest/ingestion/_notification_helpers.py` |
| Failure / PK / validation emails | `sharepoint_ingest/ingestion/_engine_notifications.py` |
| PK duplicate detection | `sharepoint_ingest/ingestion/_pk_checks.py` |
| Process memory sampling | `sharepoint_ingest/ingestion/_telemetry.py` |
| File-type resolution | `sharepoint_ingest/ingestion/_file_parsing.py` |
| CSV/Parquet/Excel readers | `sharepoint_ingest/file_processors/` |
| SharePoint Graph API client | `sharepoint_ingest/sharepoint_client.py` |
| SQL merge/load helpers | `sharepoint_ingest/sql_client.py` |
| Schema validation rules | `sharepoint_ingest/schema_validator.py` |
| Config dataclasses | `sharepoint_ingest/config.py` |
| DB model dataclasses | `sharepoint_ingest/models.py` |
| Email notification builders | `sharepoint_ingest/notifications.py` |
| Azure Key Vault client | `sharepoint_ingest/keyvault_client.py` |
| Ingestion tests | `tests/ingestion/` (split by concern — CSV, Parquet, datetime, PK, folders, etc.) |
| SQL merge builder tests | `tests/test_sql_merge_sql_builder.py` |
| File processor tests | `tests/data_processing/` |

---

## Ignore these when reasoning about business logic

| Path | Why |
|---|---|
| `src/` | Thin compatibility shims — re-export from `sharepoint_ingest/` |
| `.venv/` | Virtual environment |
| `__pycache__/` | Byte-code |
| `tests/sample_artifacts/` | Generated binary test fixtures |
| `docs/manual-spn-sharepoint-graph-setup-guide.html` | Large HTML doc |
| `sharepoint_setup/` | One-time provisioning scripts, not business logic |

---

## Architecture in one paragraph

The entry point is `sharepoint_ingest.main` → `IngestionEngine.run()`.  The
engine fetches ingestion configs from SQL, iterates over matching SharePoint
files, and routes each file to a specialised path (non-chunked, CSV-chunked,
or Parquet single-pass streaming).  The Parquet path uses HTTP range requests
via `SharePointRangeReader` to avoid loading the whole file into memory, loads
row-groups into a SQL temp table, validates in-flight, then atomically swaps
the temp table to the destination.  The `sharepoint_ingest/ingestion/`
sub-package holds extracted pure-function helpers so the engine file itself
stays under ~900 lines.

---

## Key invariants

* **`MANAGED_DESTINATION_COLUMNS`** (`schema_validator.py`) — columns
  auto-populated by the framework (`sp_ingest_created_utc` etc.); never
  validated or overwritten.
* **`MAX_PARQUET_FILE_SIZE_BYTES`** (`ingestion_engine.py`) — hard file-size
  cap before any data is read.
* **Load strategy** — only `"TRUNCATE"` and `"APPEND"` are supported; `"MERGE"`
  is not implemented and will raise `ValueError`.
* **PK violation** — detected *before* any SQL write (intra-file duplicate
  check) and again server-side after streaming to temp table (APPEND only).
