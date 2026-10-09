"""Typed exceptions for MAX adapters.

These are thin shims over whatever the underlying transport raises.
The Core layer catches by category rather than by library-specific
class so the rest of SHMAX does not need to know whether the
adapter is implemented on top of PyMax or anything else.
"""

from __future__ import annotations


class MaxError(Exception):
    """Base class for every MAX-related failure."""


class MaxAuthError(MaxError):
    """SMS / QR / 2FA / token-related failure."""


class MaxConnectionError(MaxError):
    """TCP / WebSocket transport failure or unexpected disconnect."""


class MaxSessionError(MaxError):
    """Local session file is missing, corrupted, or unrecoverable."""


class MaxTransportError(MaxError):
    """Server returned an API error or unexpected payload shape."""


class MaxUnsupportedError(MaxError):
    """Operation is not supported by the current transport or build."""


__all__ = [
    "MaxError",
    "MaxAuthError",
    "MaxConnectionError",
    "MaxSessionError",
    "MaxTransportError",
    "MaxUnsupportedError",
]
