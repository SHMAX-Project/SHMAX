"""Structured logging setup for SHMAX.

Per-area loggers (``shmax.system``, ``shmax.max``, ``shmax.telegram``,
``shmax.routing``, ``shmax.queue``, ``shmax.auth``, ``shmax.update``,
``shmax.backup``). The default format is plain text; downstream
code may swap in a JSON formatter by setting
``SHMAX_LOG_FORMAT=json``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


@dataclass(frozen=True)
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once at startup."""
    level_name = (level or os.environ.get("SHMAX_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    # Idempotent: clear handlers if already configured.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    fmt = os.environ.get("SHMAX_LOG_FORMAT", "plain").lower()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level_name)
    # Quiet down chatty libraries by default; the operator can
    # raise them via SHMAX_LOG_LEVEL=DEBUG.
    for name in ("aiogram", "telethon", "pymax", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def get_area_logger(area: str) -> logging.Logger:
    """Return the per-area logger (e.g. ``shmax.queue``)."""
    return logging.getLogger(f"shmax.{area}")


__all__ = ["configure_logging", "get_area_logger"]
