"""MAX auth state machine and provider wiring."""

from ...adapters.max.providers import CredentialFuture
from .service import (
    MaxAuthService,
    MaxAuthSnapshot,
    MaxAuthState,
    MaxAuthStateStore,
    build_providers,
)

__all__ = [
    "CredentialFuture",
    "MaxAuthService",
    "MaxAuthSnapshot",
    "MaxAuthState",
    "MaxAuthStateStore",
    "build_providers",
]