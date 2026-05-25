# Pytest Testing Guide

This document explains what `pytest` covers in this repository, when to run it, and which test commands to use for common change scenarios.

---

## 1) Purpose of pytest in this project

Use `pytest` for **code-level regression testing** of ingestion logic and guardrail behavior.

It is best for:

- validating Python logic changes quickly
- preventing regressions before commit/PR
- running focused test slices after targeted edits

It is **not** a replacement for live environment checks (Azure/SharePoint/SQL connectivity).

---

## 2) Install test dependencies

```powershell
pip install -r requirements-dev.txt
```

---

## 3) Standard pytest commands

| Scenario | Command |
|---|---|
| Run full suite | `python -m pytest -q` |
| Run full suite without writing bytecode caches | `python -B -m pytest -q` |
| Run a test folder | `python -m pytest tests/ingestion -q` |
| Run specific test files | `python -m pytest tests/data_processing/test_schema_validator.py tests/ingestion/test_datetime_normalization.py -q` |
| Verbose/debug output | `python -m pytest -vv` |

---

## 4) When pytest should be run

| When | Why |
|---|---|
| Before committing code | Catch regressions early |
| Before opening a PR | Ensure branch is stable |
| After changing ingestion logic | Verify parsing/normalization/load behavior |
| After changing schema/type validation logic | Verify type-family and validation codes |
| After changing SQL merge/load logic | Verify SQL generation + PK/strategy behavior |
| Before release/deploy handoff | Baseline code confidence |

Minimum recommendation:

1. run focused tests for the area you changed
2. run full `python -m pytest -q` before finalizing

---

## 5) Change-type → test command matrix

| If you changed... | Run... |
|---|---|
| metadata/system fields | `python -m pytest tests/ingestion/test_metadata_enrichment.py -q` |
| datetime parsing/normalization | `python -m pytest tests/ingestion/test_datetime_normalization.py -q` |
| schema/type checks | `python -m pytest tests/data_processing/test_schema_validator.py -q` |
| load strategies / PK behavior | `python -m pytest tests/ingestion/test_load_strategy.py tests/ingestion/test_pk_violation.py -q` |
| parquet flow | `python -m pytest tests/ingestion/test_parquet_streaming.py -q` |
| SQL merge SQL builder | `python -m pytest tests/test_sql_merge_sql_builder.py -q` |
| SQL auth mode logic | `python -m pytest tests/test_sql_auth_modes.py -q` |
| discovery/prod guardrails | `python -m pytest tests/test_discover_new_ingestion.py tests/test_prod_guardrails.py -q` |
| broad/multi-area changes | `python -m pytest -q` |

---

## 6) What pytest does NOT validate

`pytest` does not prove live external setup is correct. It does not validate:

- live Azure Key Vault RBAC/context
- real SharePoint app permissions/listing
- actual SQL Server connectivity in target environments
- Database Mail profile/send behavior

For those, run setup validation scripts in `sharepoint_setup/README.md`, for example:

- `python sharepoint_setup/sql_connection_test.py --env prod`
- `python sharepoint_setup/keyvault_secret_test.py --env prod`
- `python sharepoint_setup/sharepoint_auth_test.py --env prod --folder "..."`

---

## 7) Pytest config notes in this repository

Current config exists in two places:

- `pytest.ini`
- `pyproject.toml` (`[tool.pytest.ini_options]`)

Current behavior note:

- `pytest.ini` is actively used by the current local runs
- cache provider is disabled via `-p no:cacheprovider`

---

## 8) Practical workflow recommendation

1. Make code change.
2. Run focused tests from section 5.
3. Run full suite: `python -m pytest -q`.
4. If relevant, run live setup validators (Key Vault/SQL/SharePoint).
5. Finalize only when both code-level and environment-level checks are green.
