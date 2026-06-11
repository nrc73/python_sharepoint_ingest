# Staging-only ingestion mode — Q&A record

This document captures the key questions and answers from the implementation
discussion for `python -m sharepoint_ingest.main --ingest-stg-only`.

## Original feature request

> I want `python -m sharepoint_ingest.main` to have additional option
> `--ingest-stg-only`. In this case regardless of whether the
> `[staging_table_name]` & `[integrated_table_name]` are populated in
> `[config].[sharepoint_ingestion]` I only want the CSV, Excel and Parquet data
> ingested into the stg database destination table. Note that this does not
> apply to the `[ingestion_scope]='TEST'` ingestions. Where `--ingest-stg-only`
> is used, it's strictly a truncate and reload data ingestion.
>
> Where possible, all the same primary key checks, data type checks, config
> table checks and all other email notifications should still apply when
> `--ingest-stg-only` is used. Can you investigate any potential issues or
> confusions with the validations applied to the destination staging tables in
> contrast to the integrated tables?

## Clarification questions and answers

### 1. CLI spelling

**Question:** You requested `--ingest-stg-only` with an underscore, but the
current parser has `--ingest-stg-only` with hyphens. Should I support both
aliases so existing tests/usage keep working, or replace the hyphenated form?

**Answer:** Use `--ingest-stg-only`.

### 2. Blank staging table with populated integrated table

**Question:** Staging-only still needs a destination staging table. If
`staging_table_name` is blank but `integrated_table_name` is populated, should
the run fail fast, or should it load into `integrated_table_name` as a fallback
destination in the staging DB?

**Answer:** If `staging_table_name` is blank but `integrated_table_name` is
populated, fail fast.

### 3. Strict truncate/reload semantics

**Question:** Current code still honours `config.load_strategy`; an `APPEND`
config can append in staging-only mode. Should `--ingest-stg-only` forcibly
override non-TEST configs to `TRUNCATE` for the first selected file, then append
only subsequent files within the same config run so multi-file runs form one
full reload set?

**Answer:** For multiple files or sheets, truncate at the start of the process
and load all files and sheets into the staging table.

### 4. Integrated table validation

**Question:** In staging-only mode, should all integrated-table checks be
skipped entirely for non-TEST configs, even if `integrated_table_name` exists but
is missing/invalid, because the integrated table is irrelevant to the run?

**Answer:** Yes.

### 5. Schema/data type check destination

**Question:** Currently schema validation uses `staging_table_name` metadata. Is
that intended for staging-only mode, or do you also want an optional comparison
against integrated-table metadata to catch staging-vs-integrated drift before a
future normal run?

**Answer:** Currently schema validation uses `staging_table_name` metadata. Is
that intended for staging-only mode — yes.

### 6. Primary key source of truth

**Question:** For staging-only, should duplicate-key checks resolve keys from
`merge_key_columns` / staging table PK only, or should they prefer integrated
table PK when configured? Current code uses staging table metadata.

**Answer:** For staging-only, duplicate-key checks resolve keys from staging
table PK only.

### 7. Existing data conflict checks

**Question:** Because staging-only is a truncate/reload, should we avoid checking
whether incoming keys already exist in the current staging table and only check
duplicates within the incoming file/run?

**Answer:** Yes.

### 8. TEST scope exception and `all`

**Question:** Should `--ingest-stg-only --ingestion-scope all` eventually run
TEST configs through normal staging→integrated promotion while
REAL/VALIDATION/PERF_TEST configs go staging-only, or do you want to keep the
current CLI choices that do not allow `all`?

**Answer:** Remove `--ingestion-scope all`.

### 9. SharePoint file movement

**Question:** After a successful staging-only load, should files still be moved
to the configured Processed/archive folder exactly like normal ingestion?

**Answer:** Yes.

### 10. Audit/email wording

**Question:** Should audit rows and notification bodies explicitly say
`mode=STAGING_ONLY` and destination = staging DB/table, as current partial code
does, or do you want a separate audit/status marker/column for this mode?

**Answer:** Don’t say `mode=STAGING_ONLY`, but clearly state the destination
table, including database name.

### 11. Validation warnings vs hard failures

**Question:** Should non-blocking warnings (additional source columns, column
reorder, high null ratio) continue to send emails but allow the staging-only load
to succeed?

**Answer:** Yes.

### 12. Documentation/schema updates

**Question:** Do you want README/runbook updates only, or also SQL
comments/migrations if we add no new DB columns?

**Answer:** Both.

### 13. Backward compatibility

**Question:** Should this feature preserve current normal ingestion behavior
byte-for-byte unless the new flag is supplied?

**Answer:** Yes.

### 14. Fail-fast config validation

**Question:** Should a non-TEST staging-only config fail before downloading files
if `staging_table_name` is blank or missing in the staging DB, rather than
failing later at metadata/load time?

**Answer:** Yes.

### 15. Naming in code/tests

**Question:** Do you prefer internal Python names to stay `ingest_stg_only`
(valid identifier) even if the CLI option includes an underscore?

**Answer:** Yes.

## Resulting implementation decisions

- CLI uses `--ingest-stg-only`.
- Non-`TEST` staging-only runs require a populated, valid `staging_table_name`.
- `integrated_table_name` is not used as fallback and integrated-table metadata
  is ignored for non-`TEST` staging-only runs.
- Staging-only mode forces a truncate/reload into the staging DB: first load unit
  truncates, later files/chunks in the same run append.
- TEST configs keep the normal staging→integrated behavior even when the flag is
  supplied.
- Schema/type validation uses staging table metadata.
- Duplicate-key prechecks use staging-table primary key columns only.
- Validation warnings still notify without blocking the load; blocking errors
  still fail.
- Successful files are still moved to the configured processed/archive folder.
- Audit/notification context identifies the actual destination database/table
  without using `mode=STAGING_ONLY` wording.