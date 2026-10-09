"""Runtime configuration for SHMAX.

All values come from environment variables.
No secrets are loaded at import time. Secrets live in
``$SHMAX_DATA_DIR/secrets`` and will be handled by the
secure-storage layer (Stage 1+).

The single source of truth for the persistent data root is
``SHMAX_DATA_DIR``. On Debian production this is ``/var/lib/shmax``.
On developer machines it can be ``./.dev-data`` (gitignored).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _default_data_dir() -> Path:
    """Return the default persistent data directory.

    Order of precedence:
      1. ``SHMAX_DATA_DIR`` environment variable.
      2. ``/var/lib/shmax`` (production Debian).
    """
    raw = os.environ.get("SHMAX_DATA_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path("/var/lib/shmax")


class RuntimeMode(str, Enum):
    """Operational mode of the SHMAX runtime.

    The mode is selected via the ``SHMAX_RUNTIME_MODE`` environment
    variable. The default is :py:attr:`PRODUCTION`.

    Invariants:

    * In ``PRODUCTION`` mode, no ``Fake*`` adapter may be selected.
      Startup refuses with a clear error if the adapter factory
      cannot provide a real implementation.
    * In ``DEVELOPMENT`` mode, fakes are allowed but the runtime
      logs a loud warning on every selection. Useful for
      workstation exploration.
    * In ``TEST`` mode, fakes are permitted silently. Pytest
      fixtures and the local integration suite set
      ``SHMAX_RUNTIME_MODE=test`` explicitly.
    """

    PRODUCTION = "production"
    DEVELOPMENT = "development"
    TEST = "test"


def resolve_runtime_mode() -> RuntimeMode:
    """Resolve the active runtime mode from the environment.

    Default is :py:attr:`RuntimeMode.PRODUCTION`. The
    environment value is matched case-insensitively.
    """
    raw = os.environ.get("SHMAX_RUNTIME_MODE", "production").strip().lower()
    for member in RuntimeMode:
        if member.value == raw:
            return member
    raise RuntimeError(
        f"SHMAX_RUNTIME_MODE={raw!r} is not a valid mode; "
        f"expected one of {[m.value for m in RuntimeMode]}"
    )


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings.

    Instances are cheap to construct; build once at startup.
    """

    host: str
    port: int
    data_dir: Path
    log_level: str
    runtime_mode: RuntimeMode
    enable_max_qr_auth: bool = False
    debug_max_pymax: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        # Audit-mandated WAN-safe default: SHMAX does not
        # publish to every interface unless the operator
        # explicitly opts in via SHMAX_HOST=0.0.0.0. The
        # default 127.0.0.1 keeps the ODROID LAN-only by
        # default; installers that bridge via the hostd Unix
        # socket are unaffected.
        host_env = os.environ.get("SHMAX_HOST")
        if host_env is None:
            host = "127.0.0.1"
        else:
            host = host_env
        return cls(
            host=host,
            port=int(os.environ.get("SHMAX_PORT", "8787")),
            data_dir=_default_data_dir(),
            log_level=os.environ.get("SHMAX_LOG_LEVEL", "INFO").upper(),
            runtime_mode=resolve_runtime_mode(),
            # QR auth via WebClient is gated behind an explicit
            # opt-in. Until an owner has round-trip proven the
            # flow end-to-end on the ODROID, the wizard MUST
            # NOT surface a "QR-code" button by default — an
            # unfinished path is worse than no path.
            enable_max_qr_auth=os.environ.get(
                "SHMAX_ENABLE_QR_AUTH", "0"
            ) in {"1", "true", "yes", "True"},
            # Debug PyMax internals at DEBUG level. Off by
            # default to avoid log noise.
            debug_max_pymax=os.environ.get(
                "SHMAX_DEBUG_MAX_PYMAX", "0"
            ) in {"1", "true", "yes", "True"},
        )


# Persistent subdirectories that MUST exist on disk.
PERSISTENT_SUBDIRS = (
    "db",
    "config",
    "secrets",
    "sessions/max",
    "sessions/telegram",
    "state",
    "backups",
    "cache",
)
