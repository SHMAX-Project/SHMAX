"""Argon2id password hashing."""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerifyMismatchError,
)


class PasswordHasher:
    """Thin wrapper around argon2-cffi.

    The wrapper exists so the rest of the codebase depends only
    on our own names, and so test doubles can replace the
    underlying hasher cleanly.
    """

    def __init__(
        self,
        time_cost: int = 3,
        memory_cost: int = 64 * 1024,
        parallelism: int = 4,
    ) -> None:
        self._hasher = _Argon2PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
        )

    def hash(self, password: str) -> str:
        if not password:
            raise ValueError("password must not be empty")
        return self._hasher.hash(password)

    def verify(self, password: str, hashed: str) -> bool:
        try:
            self._hasher.verify(hashed, password)
            return True
        except (VerifyMismatchError, InvalidHashError):
            return False

    def needs_rehash(self, hashed: str) -> bool:
        return self._hasher.check_needs_rehash(hashed)


__all__ = ["PasswordHasher"]
