"""Logging for both processes: the console (seen by Docker / journalctl), plus an
optional rotating file in LOG_DIR."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

CALLBACK_PATH = "/auth/callback"


class HideLoginCode(logging.Filter):
    """uvicorn logs every URL. Hide the one-time ?code=&state= of the Discord login."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client, method, path, http_version, status)
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and args[2].startswith(CALLBACK_PATH + "?")
        ):
            record.args = (*args[:2], CALLBACK_PATH + "?[hidden]", *args[3:])
        return True


def setup_logging(process_name: str, level: str = "INFO", log_dir: str = "logs") -> None:
    """Send logs to stdout, and also to <log_dir>/<process_name>.log unless log_dir is empty."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    handlers: list[logging.Handler] = [console]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(
            Path(log_dir) / f"{process_name}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file.setFormatter(fmt)
        handlers.append(file)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = handlers
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HideLoginCode) for f in access.filters):
        access.addFilter(HideLoginCode())
