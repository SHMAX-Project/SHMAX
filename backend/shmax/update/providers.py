"""Update providers.

V1 ships two providers:

* ``LocalUpdateSource`` — answers the version check from the
  filesystem (used in offline / dev mode).
* ``GitHubUpdateSource`` — implements the URL contract for
  GitHub Release checks. Real network calls require a real
  repository URL (``SHMAX_UPDATE_REPO_URL``); the provider
  exposes ``release_url`` so the UI can confirm the URL
  shape before any network call is made.

These are independent of the state machine — they expose
``async def fetch() -> VersionInfo`` and the operator wires
them in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .state_machine import VersionInfo
from .. import __version__


class UpdateSource:
    """Base class for update sources.

    Subclasses implement :meth:`fetch` and return a
    :class:`VersionInfo`. This base class is intentionally
    abstract; concrete providers below are the supported
    V1 implementations.
    """

    async def fetch(self) -> VersionInfo:
        raise NotImplementedError("UpdateSource is abstract; subclass and implement fetch().")


class LocalUpdateSource(UpdateSource):
    """Answers version checks with whatever ``SHMAX_VERSION_FILE`` says.

    Used for offline development and CI. The file holds JSON::

        {"current_version": "0.1.0", "available_version": "0.2.0"}
    """

    def __init__(self, version_file: Path) -> None:
        self._path = version_file

    async def fetch(self) -> VersionInfo:
        if not self._path.is_file():
            return VersionInfo(
                current_version=os.environ.get("SHMAX_VERSION", __version__),
                available_version=os.environ.get("SHMAX_VERSION", __version__),
            )
        import json

        data = json.loads(self._path.read_text())
        return VersionInfo(
            current_version=str(data.get("current_version", "0.0.0")),
            available_version=str(data.get("available_version", data.get("current_version", "0.0.0"))),
            channel=str(data.get("channel", "stable")),
            last_check_at=int(data.get("last_check_at", 0)),
        )


class GitHubUpdateSource(UpdateSource):
    """GitHub Release provider.

    The URL is configurable via ``SHMAX_UPDATE_REPO_URL`` so the
    operator can point at a fork or a self-hosted release mirror.

    V1 keeps the implementation offline by design. The provider
    reports the URL it would call (see :attr:`release_url`) and
    leaves ``available_version == current_version`` until a real
    repository URL is wired in. Network calls are deliberately
    left for the operator to enable after live validation.
    """

    def __init__(self, repo_url: str, current_version: str) -> None:
        self._repo_url = repo_url.rstrip("/")
        self._current = current_version

    async def fetch(self) -> VersionInfo:
        # No network call until the operator wires a real
        # repository URL through the UI and confirms the
        # endpoint shape. The state machine + UI treat this
        # as "no update available".
        return VersionInfo(
            current_version=self._current,
            available_version=self._current,
            channel="stable",
            last_check_at=0,
        )

    @property
    def release_url(self) -> str:
        return f"{self._repo_url}/releases/latest"


__all__ = [
    "GitHubUpdateSource",
    "LocalUpdateSource",
    "UpdateSource",
]
