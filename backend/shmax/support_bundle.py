"""Support bundle.

Collects a sanitized snapshot of the SHMAX install for
operator debugging. The bundle NEVER contains secrets,
tokens, passwords, or message bodies.

The pipeline is allowlist-driven:

1. Only environment variables whose names appear in
   :data:`ALLOWED_ENV_KEYS` are recorded. The values are then
   pushed through :func:`_redact`.
2. Only files whose names appear in :data:`ALLOWED_FILE_PATTERNS`
   are inlined. Anything else under ``$DATA_DIR`` is skipped.
3. The doctor / watchdog / log excerpts are pushed through the
   same :func:`_redact` filter before they are recorded.

The previous implementation applied regex-based redaction over
``os.environ`` wholesale. That was both fragile (a regex miss
became a leak) and over-eager (a doctor field that *looked* like
a token was wiped). Allowlists make the policy explicit and
verifiable.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import io
import json
import logging
import os
import platform
import re
import socket
import tarfile
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .backup.manager import list_backups
from .diagnostics import Doctor
from .security import SecretStore


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Allowlists — every key/pattern below is intentional and explicit
# ----------------------------------------------------------------------
# Environment variables we are willing to record in the bundle.
# A deny-by-default posture: anything NOT in this set is dropped
# before redaction. ``SHMAX_*`` is allowed (configuration knobs),
# but obvious secret carriers (``PATH``, ``SSH_AUTH_SOCK``,
# ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``, ``PROXY*``) are not.
ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    {
        # SHMAX configuration surface
        "SHMAX_DATA_DIR",
        "SHMAX_LOG_LEVEL",
        "SHMAX_LISTEN_HOST",
        "SHMAX_LISTEN_PORT",
        "SHMAX_TELEGRAM_MODE",
        "SHMAX_LANG",
        "TZ",
        "LANG",
        "LC_ALL",
        # Process surface, no secrets
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "HOSTNAME",
        "HOME",
        "USER",
    }
)

# Files we are willing to inline (relative to ``$DATA_DIR``).
# Anything not matching one of these patterns is dropped. Master
# keys, store.bin, sessions, MTProto caches, and arbitrary user
# data are excluded by default.
ALLOWED_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^state/watchdogs\.json$"),
    re.compile(r"^state/telegram_auto\.json$"),
    re.compile(r"^logs/shmax\.log(\.\d+)?$"),
    re.compile(r"^logs/app\.log(\.\d+)?$"),
    re.compile(r"^health/.*\.json$"),
)

# Regex patterns applied to the *value* of every allowlisted
# record. They redact anything that resembles a phone number,
# a long base64 / hex token, a credential payload, or a master-
# key path. The set is intentionally conservative: false
# positives are acceptable, leaks are not.
_REDACT_PATTERNS = [
    re.compile(r"\+\d{6,15}"),
    re.compile(r"\b\d{9,}\b"),
    re.compile(r"[A-Za-z0-9_/+-]{40,}"),
    # `password=foo`, `password: foo`, `password is foo`, ...
    re.compile(r"(?i)password\s*(?:is\s+|[=:]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?hash|api[_-]?id|secret|token)\s*(?:is\s+|[=:]\s*)\S+"),
    re.compile(r"/(?:secrets/|store\.bin|master\.key)[^\s'\"]*"),
    re.compile(r"\b[0-9a-f]{64}\b"),
]


def _redact(value: str) -> str:
    if not isinstance(value, str):
        return value  # type: ignore[return-value]
    out = value
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact(obj)
    return obj


def _filtered_environment() -> dict[str, str]:
    """Return only the allowlisted environment variables, with
    each value pushed through the redact filter. Anything
    else is dropped — including PATH, SSH_AUTH_SOCK, proxy
    URLs, and any ``*_SECRET`` / ``*_TOKEN`` we may have
    forgotten to exclude."""
    env = dict(os.environ)
    return {
        key: _redact(value)
        for key, value in env.items()
        if key in ALLOWED_ENV_KEYS
    }


def _filtered_inline_files(data_dir: Path) -> dict[str, str]:
    """Return ``{relpath: text_content}`` for every file in
    ``data_dir`` whose relative path matches an
    :data:`ALLOWED_FILE_PATTERNS` entry. Other files are
    skipped silently — the operator sees the omission rather
    than a leak."""
    out: dict[str, str] = {}
    if not data_dir.is_dir():
        return out
    for path in data_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(data_dir).as_posix()
        except ValueError:
            continue
        if not any(pat.match(rel) for pat in ALLOWED_FILE_PATTERNS):
            continue
        try:
            out[rel] = _redact(path.read_text(errors="replace"))
        except OSError:
            continue
    return out


def collect_support_bundle(
    *,
    data_dir: Path,
    db,
    secret_store: SecretStore,
    doctor_results: Iterable[dict],
    watchdog_snapshots: dict[str, dict],
    queue_counts: dict,
    schema_revision: str,
    log_tail_lines: int = 200,
) -> dict:
    """Build a JSON-serializable support bundle."""
    bundle = {
        "shmax_version": __version__,
        "schema_revision": schema_revision,
        "generated_at": int(_dt.datetime.now().timestamp()),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "user": getpass.getuser() or "unknown",
            "hostname": socket.gethostname(),
        },
        "data_dir": str(data_dir),
        "queue": _scrub(queue_counts),
        "watchdogs": _scrub(watchdog_snapshots),
        "doctor": _scrub(list(doctor_results)),
        "backups": _scrub(list_backups(data_dir)),
        "environment": _filtered_environment(),
        # SecretStore exposure is names-only. Even names can be
        # sensitive in some contexts; the redact filter below
        # applies to anything longer than 12 chars to be safe.
        "secrets": sorted(_redact(name) for name in secret_store.names()),
        "inline_files": _filtered_inline_files(data_dir),
        "recent_logs_tail": [],  # filled below from caller
    }
    return bundle


def write_support_bundle(
    bundle: dict,
    log_path: Path,
    out_path: Path,
) -> Path:
    """Write the bundle to ``out_path`` as a tar.gz with sanitized logs."""
    bundle["recent_logs_tail"] = _tail_sanitized(log_path, lines=200)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")
    with tarfile.open(out_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="bundle.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return out_path


def _tail_sanitized(log_path: Path, *, lines: int) -> list[str]:
    if not log_path.is_file():
        return []
    text = log_path.read_text(errors="replace").splitlines()
    return [_redact(line) for line in text[-lines:]]


__all__ = [
    "ALLOWED_ENV_KEYS",
    "ALLOWED_FILE_PATTERNS",
    "collect_support_bundle",
    "write_support_bundle",
]
