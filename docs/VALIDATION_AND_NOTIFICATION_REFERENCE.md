# Validation and Notification Reference

This page consolidates all current validation types, failure categories, and notification paths used by the SharePoint ingestion framework.

It is intended as a single handoff artifact for external documentation and operations teams.

---

## 1) Consolidated validation / notification families

| Family | What it covers | Typical outcome | Notification path |
|---|---|---|---|
| **A. Data file inconsistencies vs target table** | Source file shape/content does not align with destination metadata | Validation warnings or blocking validation errors | Validation notification (`build_validation_email_body`) for issue lists; generic failure email if run is blocked |
| **B. Configuration table inaccuracy** | `config.sharepoint_ingestion` values are missing, invalid, or inconsistent with runtime expectations | Runtime exception before or during processing | Generic failure notification (`build_failure_email_body`) |
| **C. Primary key / duplicate data** | APPEND reload conflicts, intra-file duplicate keys, DB uniqueness violations | `PRIMARY_KEY_VIOLATION` failure | Dedicated PK violation notification (`build_pk_violation_email_body`) |
| **D. Operational / platform instability** | Network/API timeout, transient disconnects, throttling, SQL contention/resource pressure | Retry (where implemented) or runtime failure | Generic failure notification with resource telemetry |

---

## 2) Family A — Data file inconsistencies vs target table

### Main code paths

- `src/schema_validator.py` → `validate_source_against_destination`
- `src/ingestion_engine.py` → `_run_schema_checks`, `_detect_excel_datetime_text_issues`, `_normalize_dataframe`, `_convert_series_to_datetime`

### Covered checks

> **System-managed audit fields**: `sp_ingest_created_utc` and
> `sp_ingest_modified_utc` are framework-managed ingestion fields and are excluded
> from source-vs-destination column checks and datetime/text warning generation.
> Generic business fields such as `created_date` and `modified_date` are treated
> as normal data columns and validated like any other field.

1. Missing expected destination columns in source (`MISSING_DEST_COLUMNS_IN_SOURCE`, ERROR)
2. Additional source columns not in destination (`ADDITIONAL_SOURCE_COLUMNS`, WARNING)
3. Source/destination column ordering drift (`COLUMN_REORDERING_DETECTED`, WARNING)
4. Type mismatches for numeric/datetime/bool targets (`TYPE_MISMATCH`, ERROR)
5. Potential string truncation (`STRING_LENGTH_EXCEEDED`, ERROR)
6. Numeric precision overflow risk (`NUMERIC_PRECISION_EXCEEDED`, ERROR)
7. Numeric scale overflow risk (`NUMERIC_SCALE_EXCEEDED`, ERROR)
8. High null ratio (`HIGH_NULL_RATIO`, WARNING)
9. Excel datetime stored as text (`EXCEL_DATETIME_STORED_AS_TEXT`, WARNING)
10. Ambiguous/unparseable date values (raises `ValueError`, blocking)

### Notification behavior

- Validation issue lists are aggregated and sent via `build_validation_email_body`.
- If blocking errors exist, the run fails and a failure audit record is written.

---

## 3) Family B — Configuration table inaccuracy

### Main code paths

- `src/sql_client.py` → `fetch_ingestion_configs`, `_to_config`
- `src/ingestion_engine.py` → strategy resolution, merge key resolution, folder/path resolution, parsing mode logic
- `src/main.py` → credential + environment resolution

### Typical configuration errors

1. Unsupported `load_strategy` (must resolve to `TRUNCATE` or `APPEND`; `TRUNCATE_RELOAD` normalized)
2. Invalid/missing `column_mapping_json`
3. Wrong SharePoint folder/base URL values
4. Invalid worksheet selection (`excel_tab_name` / regex matching no sheet)
5. Wrong destination table name / metadata lookup failures
6. Incomplete credentials (SharePoint or SQL)
7. Missing/incorrect `merge_key_columns` for APPEND duplicate-detection intent

### Notification behavior

- Routed through generic failure notification (`build_failure_email_body`).
- Logged in `log.sharepoint_ingestion_audit.message` with `status='FAILED'`.

---

## 4) Family C — Primary key / duplicate data

### Main code paths

- `src/ingestion_engine.py` → `_check_for_intra_file_duplicate_keys`, `_notify_pk_violation`
- `src/sql_client.py` → `append_load` catches SQLAlchemy `IntegrityError` and wraps it

### Detection layers

1. **Pre-flight in-memory duplicate detection** (before SQL write)
   - Runs for `APPEND` strategy.
   - Uses `merge_key_columns` (or PK/first-column fallback) to detect duplicate keys in incoming data.
   - Raises `ValueError` with `PRIMARY_KEY_VIOLATION:` prefix.

2. **Database constraint safety net**
   - If DB rejects append due to PK/unique constraint, `append_load` catches `IntegrityError` and re-raises as `PRIMARY_KEY_VIOLATION`.

### Notification behavior

- PK-specific subject/body via `build_pk_violation_email_body`.
- Includes table, key columns, duplicate count/sample values (when available), and remediation options:
  - FULL RELOAD
  - MANUAL CLEAN

---

## 5) Family D — Operational / platform instability

### Main code paths

- `src/sharepoint_client.py` → `_get_bytes`, `_is_transient_request_error`
- `src/ingestion_engine.py` → telemetry capture (`rows_scanned`, `memory_peak_mb`, `duration_seconds`)
- `docs/PERFORMANCE_AND_RESOURCE_HANDLING.md` → SQL contention and diagnostics guidance

### Covered behaviors

1. SharePoint transient request retries for timeout/connection/chunk errors
2. Retry on HTTP 408/409/425/429/500/502/503/504 for file download path
3. Runtime failure telemetry for downstream triage
4. Guidance for SQL blocking/log pressure and parallel workflow contention

### Notification behavior

- Generic failure email includes resource telemetry fields:
  - Rows scanned before failure
  - Peak process memory
  - Elapsed time

---

## 6) Notification routing map (single view)

| Trigger type | Raised in | Email builder | Email intent |
|---|---|---|---|
| Validation issue list (warnings/errors) | `IngestionEngine._publish_validation_issues` | `build_validation_email_body` | Data quality / schema mismatch summary |
| PK/duplicate failure (`PRIMARY_KEY_VIOLATION`) | `IngestionEngine._process_config` routed to `_notify_pk_violation` | `build_pk_violation_email_body` | Focused remediation for duplication/reload conflicts |
| Any other processing failure | `IngestionEngine._process_config` routed to `_notify_failure` | `build_failure_email_body` | General runtime failure with telemetry |

---

## 7) Audit log fields for external runbooks

Audit records are written via `SqlClient.insert_audit_record` to `log.sharepoint_ingestion_audit`.

Key fields to include in external documentation:

- `status` (`SUCCESS` / `FAILED`)
- `records_loaded`
- `rows_scanned`
- `validation_error_count`
- `memory_peak_mb`
- `duration_seconds`
- `message`

Interpretation pattern:

- `rows_scanned = 0` + short duration → early parse/config/connectivity failure
- `rows_scanned > 0` + failed status → partial processing/failure during load path
- high `memory_peak_mb` + long duration → possible capacity/performance bottleneck

---

## 8) Suggested external documentation structure

For Confluence / runbook handoff, copy the sections in this order:

1. Consolidated family matrix (Section 1)
2. Notification routing map (Section 6)
3. Audit-field interpretation (Section 7)
4. Family-specific remediation details (Sections 2–5)

This keeps stakeholder docs concise while still preserving engineering traceability to source modules.
