from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_settings
from src.logging_utils import configure_logging
from src.sql_client import SqlClient


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
    sql_client = SqlClient(settings.sql, logger=logger)

    script_path = Path(args.script)
    if not script_path.exists():
        raise FileNotFoundError(f"SQL script not found: {script_path}")

    content = script_path.read_text(encoding="utf-8")
    batches = _split_sql_batches(content)

    logger.info("Executing %s SQL batch(es) from %s", len(batches), script_path)
    for idx, batch in enumerate(batches, start=1):
        logger.info("Executing batch %s/%s", idx, len(batches))
        sql_client.execute(batch)

    logger.info("SQL bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
