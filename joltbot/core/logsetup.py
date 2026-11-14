"""Logging for both processes: the console (seen by Docker / journalctl), plus an
optional rotating file in LOG_DIR. Lines are plain text by default, or one JSON
object per line with LOG_FORMAT=json."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .alerts import install_alerts

CALLBACK_PATH = "/auth/callback"
# Each process keeps its current log file plus this many older ones, 2 MB each.
LOG_FILE_MAX_BYTES = 2_000_000
LOG_FILE_BACKUPS = 5

# Attributes every LogRecord has. Anything else on a record came from `extra=`.
# color_message is uvicorn's copy of the message with terminal colour codes; never useful here.
_STANDARD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "color_message"}


class HideLoginCode(logging.Filter):
    """uvicorn logs every URL. Hide the one-time ?code=&state= of the Discord login."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Blank out the query string of the login callback URL. The record is still logged."""
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


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log tools that filter by field.

    Always has ``ts`` (UTC, ISO 8601), ``level``, ``logger`` and ``message``.
    Fields passed with ``extra=`` (e.g. ``guild_id``) are added as they are, and
    a traceback goes in ``exception``. Values JSON can't encode become strings,
    so a log call can never fail because of what it was given.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a single line of JSON."""
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


def setup_logging(
    process_name: str,
    level: str = "INFO",
    log_dir: str = "logs",
    log_format: str = "text",
    alert_webhook_url: str = "",
) -> None:
    """Send logs to stdout, and also to <log_dir>/<process_name>.log unless log_dir is empty.

    Args:
        process_name: ``"bot"`` or ``"web"``; names the log file and labels alerts.
        level: Minimum level to log.
        log_dir: Folder for the rotating log file. Empty = console only.
        log_format: ``"text"`` (readable lines) or ``"json"`` (see ``JsonFormatter``).
        alert_webhook_url: Discord webhook that receives ERROR and above
            (see ``core/alerts.py``). Empty = no alerts.
    """
    fmt: logging.Formatter
    if log_format == "json":
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    handlers: list[logging.Handler] = [console]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(
            Path(log_dir) / f"{process_name}.log",
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file.setFormatter(fmt)
        handlers.append(file)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = handlers
    # Added last, after the console is set up, so a problem with the URL is visible there.
    if (alerts := install_alerts(alert_webhook_url, process_name)) is not None:
        root.addHandler(alerts)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HideLoginCode) for f in access.filters):
        access.addFilter(HideLoginCode())
