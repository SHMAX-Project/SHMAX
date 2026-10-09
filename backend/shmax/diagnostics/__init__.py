"""Diagnostics (doctor) module.

Runs offline self-checks against the SHMAX data directory,
database, and config. External integrations (MAX, Telegram,
proxies) report ``NOT_CONFIGURED`` / ``NOT_TESTED`` rather
than ``FAIL`` until live validation has wired them up.
"""

from .doctor import (
    CheckResult,
    CheckStatus,
    Doctor,
    DoctorReport,
)

__all__ = [
    "CheckResult",
    "CheckStatus",
    "Doctor",
    "DoctorReport",
]
