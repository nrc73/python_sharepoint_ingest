"""CLI tests for staging-only ingestion mode."""
from __future__ import annotations

import pytest

from sharepoint_ingest.main import build_argument_parser, _validate_dry_run_destinations
from sharepoint_ingest.models import IngestionConfig


def _cfg(scope: str = "REAL", integrated_table_name: str | None = "sharepoint.integrated") -> IngestionConfig:
    return IngestionConfig(
        id=1,
        sharepoint_base_url="",
        sharepoint_process_folder="/folder",
        excel_tab_name="",
        sharepoint_process_archive_folder=None,
        sharepoint_process_failed_folder=None,
        process_frequency=None,
        header_skip_rows=0,
        check_source_dest_columns=False,
        multi_file_ingest=False,
        to_email_address=None,
        process_id=None,
        workflow_id="wf",
        staging_table_name="sharepoint.staging",
        ingestion_scope=scope,
        is_test_data=1 if scope == "TEST" else 0,
        integrated_table_name=integrated_table_name,
    )


class _TableSql:
    def __init__(self, table_columns: dict[str, list[dict]]):
        self.table_columns = table_columns
        self.requested: list[str] = []

    def get_table_columns(self, table_name: str):
        self.requested.append(table_name)
        return self.table_columns.get(table_name, [])


def test_parser_accepts_ingest_stg_only_flag() -> None:
    args = build_argument_parser().parse_args(["--ingest-stg-only"])
    assert args.ingest_stg_only is True


def test_parser_accepts_force_all_us_dates_to_au_flag() -> None:
    args = build_argument_parser().parse_args(["--force-all-us-dates-to-au"])
    assert args.force_all_us_dates_to_au is True


def test_parser_accepts_supress_warnings_flag() -> None:
    args = build_argument_parser().parse_args(["--supress-warnings"])
    assert args.supress_warnings is True


def test_parser_accepts_correctly_spelled_suppress_warnings_alias() -> None:
    args = build_argument_parser().parse_args(["--suppress-warnings"])
    assert args.supress_warnings is True


def test_parser_rejects_ingestion_scope_all() -> None:
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(["--ingestion-scope", "all"])


def test_dry_run_staging_only_ignores_missing_integrated_table_for_real_scope() -> None:
    stg = _TableSql({"sharepoint.staging": [{"column_name": "id"}]})
    integ = _TableSql({})

    _validate_dry_run_destinations([_cfg("REAL")], stg, integ, ingest_stg_only=True)

    assert stg.requested == ["sharepoint.staging"]
    assert integ.requested == []


def test_dry_run_staging_only_requires_staging_table_even_when_integrated_populated() -> None:
    stg = _TableSql({})
    integ = _TableSql({"sharepoint.integrated": [{"column_name": "id"}]})
    cfg = _cfg("REAL")
    cfg.staging_table_name = ""

    with pytest.raises(ValueError, match="blank staging_table_name"):
        _validate_dry_run_destinations([cfg], stg, integ, ingest_stg_only=True)

    assert stg.requested == []
    assert integ.requested == []


def test_dry_run_staging_only_ignores_missing_integrated_table_for_test_scope() -> None:
    stg = _TableSql({"sharepoint.staging": [{"column_name": "id"}]})
    integ = _TableSql({})

    _validate_dry_run_destinations([_cfg("TEST")], stg, integ, ingest_stg_only=True)

    assert stg.requested == ["sharepoint.staging"]
    assert integ.requested == []
