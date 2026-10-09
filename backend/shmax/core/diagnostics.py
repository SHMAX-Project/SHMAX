"""Health / readiness diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import PathLayout


@dataclass(frozen=True)
class PersistentReport:
    """Snapshot of the persistent paths state."""

    root: Path
    writable: bool
    existing: list[str]
    missing: list[str]


def collect_persistent_report(layout: PathLayout) -> PersistentReport:
    """Inspect persistent paths and report which are present/missing.

    The check uses ``exists()`` and ``os.access(W_OK)`` on the root.
    Subdirectories are reported individually; the core does not
    auto-create them at startup — that happens in ``main.py`` via
    ``PathLayout.ensure()``.
    """
    wanted = {
        "db": layout.db,
        "config": layout.config,
        "secrets": layout.secrets,
        "sessions/max": layout.sessions_max,
        "sessions/telegram": layout.sessions_telegram,
        "state": layout.state,
        "backups": layout.backups,
        "cache": layout.cache,
    }
    existing = [name for name, p in wanted.items() if p.exists()]
    missing = [name for name, p in wanted.items() if not p.exists()]
    writable = bool(layout.root.exists() and _is_writable(layout.root))
    return PersistentReport(
        root=layout.root,
        writable=writable,
        existing=sorted(existing),
        missing=sorted(missing),
    )


def _is_writable(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)


__all__ = ["PersistentReport", "collect_persistent_report"]
