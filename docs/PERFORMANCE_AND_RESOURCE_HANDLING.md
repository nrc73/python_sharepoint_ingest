# Performance and Resource Handling

This document describes performance characteristics, known resource pressures, and
operational guidance for the SharePoint ingestion pipeline — in particular for
production environments where multiple workflow instances may run in parallel.

For a consolidated catalog of validation families and notification routing, see
[`VALIDATION_AND_NOTIFICATION_REFERENCE.md`](VALIDATION_AND_NOTIFICATION_REFERENCE.md).

---

## Table of contents

1. [SQL Server insert performance](#sql-server-insert-performance)
2. [Parallel ingestion considerations](#parallel-ingestion-considerations)
3. [APPEND reload — primary key violation risks](#append-reload--primary-key-violation-risks)
4. [Memory and process footprint](#memory-and-process-footprint)
5. [SharePoint download throughput](#sharepoint-download-throughput)
6. [Parquet range-read streaming](#parquet-range-read-streaming)
7. [Failure email — resource telemetry](#failure-email--resource-telemetry)
8. [Diagnosing slow or hung runs](#diagnosing-slow-or-hung-runs)
9. [Configuration reference](#configuration-reference)

---

## SQL Server insert performance

### What changed (v1.x → current)

| Setting | Old | Current | Effect |
|---|---|---|---|
| `pandas.to_sql` method | `"multi"` | `None` (executemany) | Enables pyodbc `fast_executemany` path |
| `chunksize` | `2099 // num_cols` (~99 for customers) | `10 000` | Fewer round-trips, larger vectorised batches |

`method="multi"` generates a large `INSERT INTO … VALUES (…),(…),(…)` string per chunk
and **bypasses** pyodbc's `fast_executemany=True` engine setting.  Switching to
`method=None` lets SQLAlchemy call `cursor.executemany()` instead, which pyodbc
vectorises into a single server-side batch per chunk.

### Observed impact (dev environment — SQL Server in local Docker)

| File | Rows | Old duration | Current duration | Peak RSS |
|---|---|---|---|---|
| `valid_transactions_large.csv` | 1 000 000 | ~850 s (~14 min) | ~572 s (~9.5 min) | 719 MB |

> The 33% improvement is due purely to the `fast_executemany` path change; the dominant
> remaining cost is local Docker disk I/O and SQL transaction log writes.  
> On production-grade SQL Server hardware (SSD-backed, properly sized log file, Simple
> recovery model) expect **60–180 s** for the same 1M-row file.  
> Enable `ENABLE_CHUNKED_CSV=true` (and `ENABLE_CHUNKED_PARQUET=true` for parquet flows)
> to reduce peak RSS from ~720 MB to ~150 MB
> regardless of file size, at a small throughput cost.

### Notes on dormant merge implementation

`SqlClient.merge_load` exists in the codebase but is not part of the active ingestion
runtime path. Current ingestion strategy resolution allows `TRUNCATE` and `APPEND`.
If `MERGE` is reintroduced in future, re-evaluate bulk insert settings for that path
before enabling it in production workflows.

---

## Parallel ingestion considerations

In production, multiple workflow trigger schedules may overlap.  Common contention
scenarios and mitigations:

### 1 — Two workflows writing to the same destination table

If `wf-a` TRUNCATEs `dbo.dest_transactions` while `wf-b` is still inserting into it,
`wf-b` will receive a lock wait or deadlock error.

**Mitigation options:**
- Schedule workflows with non-overlapping windows in SSIS / SQL Agent.
- Use `load_strategy = APPEND` + a surrogate `batch_id` column so concurrent inserts
  never conflict.
- Use a staging table per workflow and merge into the final table in a post-step.

### 2 — SQL Server transaction log pressure

`TRUNCATE TABLE` is minimally logged, but a million-row `INSERT` generates significant
transaction log growth.  If the log file reaches its auto-growth limit, the insert will
block until auto-growth completes.

**Indicators:** `wait_type = WRITELOG`, high `used_log_space_percent` from
`DBCC SQLPERF(LOGSPACE)`.

**Mitigation options:**
- Pre-size the log file (`ALTER DATABASE … MODIFY FILE`) to avoid frequent auto-growth.
- Set the recovery model to `SIMPLE` for staging/ingest databases (acceptable for
  reload-safe pipelines).
- Use chunked CSV ingestion (`ENABLE_CHUNKED_CSV=true`) — each chunk is a smaller
  transaction.

### 3 — Blocking from a killed/timed-out Python process

If a Python process is killed mid-insert (e.g. by a scheduler timeout), its SQL
transaction remains open until SQL Server detects the network drop (typically 30–60 s).
Until then, other sessions trying to `TRUNCATE` the same table will block.

**Diagnosis:**
```sql
-- Find blocking sessions
SELECT session_id, blocking_session_id, wait_type, wait_time, status, command
FROM sys.dm_exec_requests
WHERE database_id = DB_ID('ingest_dev');

-- Find locks on a specific table
SELECT request_session_id, resource_type, request_mode, request_status
FROM sys.dm_tran_locks
WHERE resource_database_id   = DB_ID('ingest_dev')
  AND resource_associated_entity_id = OBJECT_ID('dbo.dest_transactions_large');

-- Kill the blocking session (replace 57 with actual session_id)
-- KILL 57;
```

**Mitigation options:**
- Set `LOCK_TIMEOUT` on the ingestion SQL connection (`SET LOCK_TIMEOUT 30000`).
- Use a dedicated schema/table per workflow so a blocked workflow never blocks others.

### 4 — Memory pressure (multiple large files in parallel)

Each ingestion process loads an entire file into pandas before writing to SQL
(unless `ENABLE_CHUNKED_CSV=true`).  For a 178 MB / 1M-row CSV the peak RSS is
~300–500 MB per process.  Running 4 large-file workflows in parallel can exhaust
available memory on a small VM/container.

**Mitigation options:**
- Enable chunked CSV: `ENABLE_CHUNKED_CSV=true` in `.env`.  Chunks default to 50 000
  rows and keep peak RSS below ~150 MB regardless of file size.
- Set container/VM memory limits and schedule large workflows off-peak.
- Monitor `memory_peak_mb` in `log.sharepoint_ingestion_audit`.

---

## APPEND reload — primary key violation risks

When a config uses `load_strategy = APPEND`, each run **adds** rows to the destination table
rather than replacing them.  Re-processing the same file (or a corrected version of it) can
therefore cause a **primary key constraint violation** if the destination table has a unique
or primary key index on any of the ingested columns.

### Two-layer defence

The engine protects against PK violations in two complementary stages:

#### 1 — Intra-file pre-flight check (Python, before any SQL)

Before calling `append_load`, `_check_for_intra_file_duplicate_keys` scans the incoming
DataFrame for rows that share the same value(s) on the configured `merge_key_columns`.

- Runs on the full DataFrame for non-chunked files.
- Runs on the **first chunk only** for chunked CSV processing, which is sufficient to
  catch duplicates within a single reload of the same file.
- Raises `ValueError: PRIMARY_KEY_VIOLATION: …` immediately if duplicates are found,
  **before any SQL write is attempted**.  This prevents partial inserts in chunked runs.

#### 2 — SQL `IntegrityError` wrapper (SQLAlchemy, as a safety net)

If the intra-file check passes but a PK constraint violation still occurs at the database
level (e.g. because the same key already exists from a prior run), `SqlClient.append_load`
catches the SQLAlchemy `IntegrityError` and re-raises it as
`ValueError: PRIMARY_KEY_VIOLATION: Appending … failed due to a primary key / unique …`.

This ensures the engine's `_process_config` exception handler can identify PK violations
by string prefix and route them to `_notify_pk_violation` rather than the generic
`_notify_failure` handler.

### Dedicated PK violation email

When a PK violation is caught, a purpose-built notification email is sent that includes:

- File name and destination table
- Configured key column(s)
- Duplicate row count and up to 5 sample key values (from the intra-file check)
- Resource telemetry (rows scanned, peak memory, elapsed time)
- Two remediation options:

  | Option | When to use |
  |---|---|
  | **FULL RELOAD** | Switch config to `TRUNCATE` strategy, re-upload the corrected file and rerun. |
  | **MANUAL CLEAN** | Delete the duplicate rows from the destination table and re-upload only the delta. |

### Reload workflow for APPEND configs

```
1. Identify duplicate keys from the notification email (key column(s), sample values).
2. Choose a remediation option:
   a) FULL RELOAD:
      - Set load_strategy = TRUNCATE in the config row.
      - Re-upload the corrected file to the SharePoint process folder.
      - Rerun the workflow.
      - Reset load_strategy = APPEND after the successful run.
   b) MANUAL CLEAN:
      - Run a DELETE against the destination table for the affected key values.
      - Re-upload only the new/corrected rows.
      - Rerun with load_strategy = APPEND unchanged.
3. Verify the audit log entry shows status = SUCCESS and the expected records_loaded count.
```

### Key column configuration

`merge_key_columns` (comma-separated) defines the key columns used for APPEND duplicate
detection and PK-violation diagnostics. If left blank, the engine falls back to:

1. Primary key columns discovered from `INFORMATION_SCHEMA.KEY_COLUMN_USAGE`
2. The first destination column (last resort)

Always configure `merge_key_columns` explicitly for APPEND configs to ensure accurate
pre-flight detection.

---

## Memory and process footprint

The ingestion engine captures peak RSS memory at key checkpoints using `psutil` (or a
`ctypes`/`Psapi.dll` fallback on Windows):

- Before file download
- After file download
- After DataFrame parse
- After normalisation
- After SQL load

Peak values are written to `log.sharepoint_ingestion_audit.memory_peak_mb` and
included in failure notification emails.

---

## SharePoint download throughput

### CSV and Excel files

- Files are downloaded in a single `requests` call via the Graph/REST API into an
  in-memory buffer before parsing.
- Typical throughput: 5–30 MB/s depending on tenant throttling and proximity.
- For very large files (>100 MB), the download itself may take 30–60 s and appear to
  hang.  Check the audit log `duration_seconds` vs. `rows_scanned` to determine whether
  the bottleneck is download or SQL insert.
- Microsoft 429 (throttle) responses are retried automatically by the SharePoint client;
  excessive throttling can add minutes to large runs.

### Parquet files

Parquet files use **HTTP range-read streaming** — the file is **never fully downloaded**.
See [Parquet range-read streaming](#parquet-range-read-streaming) for full details.

---

## Parquet range-read streaming (single-pass)

Parquet files use a `SharePointRangeReader` backed by Microsoft Graph HTTP range
requests.  The engine calls `get_file_item` once to obtain the file size and a
pre-authenticated CDN URL, then PyArrow's `ParquetFile` fetches only the data it needs.

### Single-pass pipeline (current design)

Each row group is fetched **once** and runs through the full pipeline
(transform → validate → stage) before the next row group is requested:

| Step | HTTP requests | Data transferred |
|---|---|---|
| Open `ParquetFile` (footer read) | 2–3 | ~8–32 KB |
| Per row group — transform + validate + `load_chunk_to_temp` | **1** | ~row-group size |
| Final `swap_temp_to_destination` (SQL only) | 0 | — |

For a 1 GB Parquet file with 478 row groups the full run makes **~481 range requests**
and transfers the file data **once**.  The file is never held entirely in memory.

**Compared to the previous two-pass design** (validate-all → then reload-all):

| Metric | Old (two-pass) | Current (single-pass) |
|---|---|---|
| Range requests per row group | 2 | **1** |
| Total data transferred | 2× file size | **1× file size** |
| Per-row CPU (column-map + normalise) | 2× | **1×** |
| Destination writes visible during run | Incremental (mid-run rows visible) | **Atomic (all-or-nothing)** |
| Typical wall-clock saving (>100 MB) | — | **40–55 %** |

The destination table is never written until the final `swap_temp_to_destination`
transaction, so a validation failure or PK violation at any point leaves the destination
completely unchanged.

### Memory profile

| Mode | Peak RSS |
|---|---|
| Non-chunked (all rows in one batch) | ~row-group size × parallelism |
| Chunked (`ENABLE_CHUNKED_PARQUET=true`) | < `INGEST_CHUNK_SIZE_ROWS` × row width |

With the default 5 000-row chunk size, peak RSS for a 1 GB Parquet file stays below
**~150 MB** regardless of total row count.

### Request rate and throttling

Each range request is an authenticated Graph API call.  For very large files:

- **Range request rate** is bounded by the row-group iteration speed, typically
  1–5 requests/second for a standard SharePoint tenant.
- **Microsoft 429 throttling** is handled by the SharePoint client with exponential
  back-off; expect 5–15 s delays per throttled request.
- If the CDN pre-auth URL (`@microsoft.graph.downloadUrl`) is available in the Graph
  item metadata, the engine uses it directly — skipping re-authentication on each
  request and significantly reducing per-request overhead.

### Diagnosing slow Parquet runs

| Symptom | Likely cause |
|---|---|
| Validation progress stalls at a fixed percentage | 429 throttle back-off on a large row group |
| `duration_seconds` high, `memory_peak_mb` low | Normal for chunked range reads on large files |
| Progress advances then stops completely | Network timeout; check SharePoint connectivity |
| "I/O operation on closed file" error | `SharePointRangeReader` missing `closed` property — should not occur after v2.1 |

---

## Failure email — resource telemetry

From v2.0, failure emails include a **Resource telemetry** section:

```
Ingestion failure for process: config_id=4, workflow_id=wf-valid-transactions-large, ...
File: valid_transactions_large.csv

Error:
Config 4 failed for file valid_transactions_large.csv: <error detail>

Resource telemetry:
  Rows scanned before failure : 750000
  Peak memory (process)       : 423.2 MB
  Elapsed time                : 312.4s
  Host / runner               : (optional)

NOTE: If multiple ingestion workflows run in parallel, check
log.sharepoint_ingestion_audit and sys.dm_exec_requests on the SQL Server
for blocking sessions, high log usage, or contention on the destination table
before reprocessing this file.
```

### How to read the telemetry

| Field | What it tells you |
|---|---|
| Rows scanned = 0, short elapsed time | Early failure — file parse, download, or schema error. No SQL impact. |
| Rows scanned = N (some rows), moderate elapsed time | Failure mid-load. Destination table may be partially populated; check audit log. |
| Rows scanned = full row count, long elapsed time | Failure during SQL commit/write phase — check for log pressure or blockers. |
| High memory + failure | Possible OOM. Enable chunked CSV or increase container memory. |

---

## Diagnosing slow or hung runs

Use `_diag_sql_blockers.py` in the project root:

```bash
python tools/diagnostics/_diag_sql_blockers.py
```

This script reports:
- Active requests on `ingest_dev` with blocking session IDs and wait types
- Locks held on `dbo.dest_transactions_large`
- Open transactions on `ingest_dev`
- Current destination table row count

For a stuck process (Python alive, 0 CPU, not progressing):

1. Check for orphaned processes: `tasklist | findstr python.exe`
2. Check SQL blocking with the script above
3. If no blocker is found, the process is likely mid-download or mid-sort (pandas
   internal).  Allow 2–3 minutes for 1M-row files after validation completes.
4. If the process is still alive after 10 minutes with 0 CPU and 0 SQL activity,
   kill and rerun — the table will be TRUNCATEd on the next `TRUNCATE` strategy run.

---

## Configuration reference

| `.env` variable | Default | Purpose |
|---|---|---|
| `ENABLE_CHUNKED_CSV` | `false` | Stream CSV in row chunks instead of loading the whole file into memory |
| `ENABLE_CHUNKED_PARQUET` | `true` | Stream Parquet via Arrow record batches instead of loading full file into memory |
| `INGEST_CHUNK_SIZE_ROWS` | `5000` | Rows per chunk (applies when chunked CSV/Parquet is enabled) |
| `DEFAULT_LOAD_STRATEGY` | `TRUNCATE` | `TRUNCATE` or `APPEND` |
| `NULL_ALERT_THRESHOLD` | `0.5` | Fraction of NULLs in a column before a validation warning is raised |

---

*Last updated: 2026-05-15*
