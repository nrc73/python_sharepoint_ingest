"""Central logging configuration for console and file handlers."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path


def _cleanup_old_logs(log_dir: str = "logs", max_files: int = 10) -> None:
    """Keep only the most recent SharePoint ingestion log files."""
    if max_files < 1:
        return

    log_path = Path(log_dir)
    log_files = [
        path
        for path in log_path.glob("sharepoint_ingestion_*.log")
        if path.is_file()
    ]
    log_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    for old_file in log_files[max_files:]:
        try:
            old_file.unlink()
        except OSError:
            # Best effort cleanup: do not fail ingestion logging setup.
            continue


def configure_logging(level: str = "INFO") -> logging.Logger:
    os.makedirs("logs", exist_ok=True)

    logger = logging.getLogger("sharepoint_ingestion")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(
        os.path.join("logs", f"sharepoint_ingestion_{timestamp}.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _cleanup_old_logs(log_dir="logs", max_files=10)

    return logger
