"""SHMAX security primitives.

Only the ``SecretStore`` lives here for now. Authentication and
session management live under :mod:`shmax.auth`.
"""

from .secret_store import (
    MasterKeyError,
    SecretNotFound,
    SecretStore,
    SecretStoreError,
)

__all__ = [
    "MasterKeyError",
    "SecretNotFound",
    "SecretStore",
    "SecretStoreError",
]
