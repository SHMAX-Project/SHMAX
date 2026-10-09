"""Configuration for the PyMax-backed MaxAdapter.

Construction-only — values are read from environment variables
or the caller-provided dataclass. The adapter never imports
PyMax types from here so this module is safe to import without
``maxapi-python`` being installed.

PyMax 2.4.1 device_type defaults to ``ANDROID``; we pin that
explicitly so a future PyMax release that flips the default to
``DESKTOP`` / ``WEB`` does not silently change our handshake.

Override via ``SHMAX_MAX_DEVICE_TYPE=android|ios|web|desktop``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MaxDeviceType = Literal["android", "ios", "web", "desktop"]


def _default_session_dir() -> Path:
    """Resolve the persistent MAX session directory.

    Resolution order:
      1. ``SHMAX_MAX_SESSION_DIR`` environment variable.
      2. ``$SHMAX_DATA_DIR/sessions/max`` (production layout).
      3. ``./.dev-data/sessions/max`` (developer fallback).
    """
    explicit = os.environ.get("SHMAX_MAX_SESSION_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()

    data_root = os.environ.get("SHMAX_DATA_DIR")
    if data_root:
        return Path(data_root).expanduser().resolve() / "sessions" / "max"

    return Path.cwd() / ".dev-data" / "sessions" / "max"


def _resolve_device_type() -> MaxDeviceType:
    raw = os.environ.get("SHMAX_MAX_DEVICE_TYPE", "android").strip().lower()
    if raw in ("android", "ios", "web", "desktop"):
        return raw  # type: ignore[return-value]
    # Defensive: refuse unknown values rather than silently falling
    # back — explicit opt-in is the audit's mandate.
    raise RuntimeError(
        f"SHMAX_MAX_DEVICE_TYPE={raw!r} is not a supported MAX device type; "
        "expected one of: android, ios, web, desktop"
    )


@dataclass(frozen=True)
class PyMaxAdapterConfig:
    """Static adapter configuration.

    ``phone`` is intentionally a plain string here; the adapter
    is responsible for keeping it out of logs and stack traces.
    ``None`` means "use the phone already stored in the local
    session file" — useful for repeated runs after the first login.

    ``device_type`` is the PyMax device class. We pin ``android``
    because PyMax 2.4.1 upstream behaviour matches that profile
    on production handsets, and the MAX server-side fingerprint
    validation accepts it.
    """

    phone: str | None
    session_dir: Path
    session_name: str = "main.db"
    app_version: str = "26.25.0"
    device_type: MaxDeviceType = "android"
    reconnect: bool = True
    log_level: str = "WARNING"

    @classmethod
    def from_env(cls) -> "PyMaxAdapterConfig":
        return cls(
            phone=os.environ.get("SHMAX_MAX_PHONE"),
            session_dir=_default_session_dir(),
            session_name=os.environ.get("SHMAX_MAX_SESSION_NAME", "main.db"),
            app_version=os.environ.get("SHMAX_MAX_APP_VERSION", "26.25.0"),
            device_type=_resolve_device_type(),
            reconnect=os.environ.get("SHMAX_MAX_RECONNECT", "1")
            not in {"0", "false", "False"},
            log_level=os.environ.get("SHMAX_LOG_LEVEL", "WARNING"),
        )
