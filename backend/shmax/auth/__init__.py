"""Authentication primitives.

* :class:`PasswordHasher` — Argon2id wrapper.
* :class:`AdminRepository` — admin user CRUD.
* :class:`SetupTokenService` — one-time bootstrap tokens.
* :class:`RateLimiter` — login brute-force protection.
* :class:`SessionSigner` — signed session cookies.
"""

from .passwords import PasswordHasher
from .pairing import (
    AclDecision,
    AclService,
    DEFAULT_PERMISSIONS,
    PairingCode,
    PairingCodeService,
)
from .rate_limit import RateLimiter
from .sessions import SessionSigner
from .setup_token import SetupTokenService

__all__ = [
    "AclDecision",
    "AclService",
    "DEFAULT_PERMISSIONS",
    "PairingCode",
    "PairingCodeService",
    "PasswordHasher",
    "RateLimiter",
    "SessionSigner",
    "SetupTokenService",
]
