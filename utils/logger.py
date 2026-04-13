"""Rotating-file + console logger setup."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(
    name: str = "goldmind",
    log_file: str | Path = "logs/goldmind.log",
    level: str = "INFO",
    max_file_size_mb: int = 10,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure (idempotently) and return a project logger.

    Safe to call repeatedly — handlers are added only once per logger name.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fh = RotatingFileHandler(
        log_path,
        maxBytes=max_file_size_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.propagate = False
    return logger


def get_logger(name: str = "goldmind") -> logging.Logger:
    """Return an existing logger (does not configure handlers)."""
    return logging.getLogger(name)
