from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from sqlalchemy import text

from sharepoint_ingest.config import load_settings
from sharepoint_ingest.logging_utils import configure_logging
from sharepoint_ingest.sql_client import SqlClient


def _split_sql_batches(script: str) -> list[str]:
    lines = script.splitlines()
    batches: list[list[str]] = [[]]

    for line in lines:
        if line.strip().upper() == "GO":
            batches.append([])
            continue
        batches[-1].append(line)

    return ["\n".join(batch).strip() for batch in batches if "\n".join(batch).strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap SQL schema for SharePoint ingestion")
    parser.add_argument("--env", default="prod", help="Environment to load (.env) settings")
    parser.add_argument("--script", default="sql/bootstrap.sql", help="Path to SQL bootstrap script")
    args = parser.parse_args()

    logger = configure_logging("INFO")
    settings = load_settings(env_override=args.env)

    # Some operational scripts (for example reset_and_prepare_dev_v2.sql) use
    # explicit USE <database> batches to move across multiple databases. The
    # normal runtime resolves the audit DB from Key Vault before constructing a
    # SqlClient, but bootstrap scripts may intentionally start without a DB name.
    # Connect to master in that case; the script can then select target DBs.
    sql_settings = settings.sql if settings.sql.database else replace(settings.sql, database="master")
    sql_client = SqlClient(sql_settings, logger=logger)

    script_path = Path(args.script)
    if not script_path.exists():
        raise FileNotFoundError(f"SQL script not found: {script_path}")

    content = script_path.read_text(encoding="utf-8")
    batches = _split_sql_batches(content)

    logger.info("Executing %s SQL batch(es) from %s", len(batches), script_path)
    # Keep all GO-delimited batches on the same physical connection so USE
    # statements persist across subsequent batches, matching sqlcmd semantics.
    with sql_client._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for idx, batch in enumerate(batches, start=1):
            logger.info("Executing batch %s/%s", idx, len(batches))
            conn.execute(text(batch))

    logger.info("SQL bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
