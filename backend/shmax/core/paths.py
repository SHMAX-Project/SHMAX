"""Persistent data paths.

The SHMAX core code MUST NOT write user data anywhere except
under ``Settings.data_dir``. Application code may freely use
temporary scratch space (e.g. ``/tmp``) but anything that needs
to survive a container recreate goes here.
"""

from __future__ import annotations

from pathlib import Path

from ..config import PERSISTENT_SUBDIRS


class PathLayout:
    """Canonical layout under the persistent data root.

    ``cache`` is the only disposable subdirectory; everything
    else is treated as user-owned persistent state.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "db"
        self.config = root / "config"
        self.secrets = root / "secrets"
        self.sessions_max = root / "sessions" / "max"
        self.sessions_telegram = root / "sessions" / "telegram"
        self.state = root / "state"
        self.backups = root / "backups"
        self.cache = root / "cache"

    def ensure(self) -> list[Path]:
        """Create every subdirectory if it does not exist.

        Returns the list of directories that were created
        during this call (existed-already ones are not listed).
        """
        created: list[Path] = []
        for sub in (
            self.db,
            self.config,
            self.secrets,
            self.sessions_max,
            self.sessions_telegram,
            self.state,
            self.backups,
            self.cache,
        ):
            if not sub.exists():
                sub.mkdir(parents=True, exist_ok=True)
                created.append(sub)
        return created

    def all_persistent(self) -> list[Path]:
        """All directories under the persistent root that the
        layout owns. ``cache`` is included because it lives under
        the data root, but it is still treated as disposable from
        the application-contract side.
        """
        return [
            self.db,
            self.config,
            self.secrets,
            self.sessions_max,
            self.sessions_telegram,
            self.state,
            self.backups,
            self.cache,
        ]


def assert_under_data_root(path: Path, data_root: Path) -> bool:
    """Return True iff ``path`` resolves under ``data_root``.

    Defensive guard for any future code that builds paths from
    user input. Refuses to operate otherwise.
    """
    try:
        path.resolve().relative_to(data_root.resolve())
        return True
    except ValueError:
        return False


__all__ = ["PathLayout", "PERSISTENT_SUBDIRS", "assert_under_data_root"]
