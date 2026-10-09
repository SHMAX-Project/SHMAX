"""Log rotation / retention.

Stage 1B ships a simple size+time bounded log rotation.
``logging.handlers.RotatingFileHandler`` is sufficient; this
module wires it to ``$SHMAX_DATA_DIR/logs/shmax.log`` and
exposes a retention policy defaulting to 14 days.
"""

from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
from pathlib import Path


def configure_file_logging(logs_dir: Path, *, max_bytes: int = 5_000_000, backups: int = 5) -> logging.Handler:
    """Attach a rotating file handler to the root logger.

    Caller is responsible for having called ``configure_logging``
    first; this helper adds the file handler on top.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "shmax.log",
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    return handler


def purge_old_logs(logs_dir: Path, *, retention_days: int = 14) -> int:
    cutoff = _dt.datetime.now() - _dt.timedelta(days=retention_days)
    removed = 0
    if not logs_dir.is_dir():
        return 0
    for f in logs_dir.iterdir():
        if not f.is_file():
            continue
        mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


__all__ = ["configure_file_logging", "purge_old_logs"]
