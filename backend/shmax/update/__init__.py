"""Updater subsystem.

* State machine: CHECK -> PRECHECK -> BACKUP -> FETCH -> STOP ->
  SWITCH_VERSION -> MIGRATE -> START -> HEALTHCHECK -> DONE / ROLLBACK.
* Providers: LocalUpdateSource (offline, fully implemented) and
  GitHubUpdateSource (URL-only; real network calls require the
  operator-supplied repository URL).
* ``update.cli_update`` runs the transaction with recording
  hooks so tests can drive the state machine deterministically.
"""

from .providers import (
    GitHubUpdateSource,
    LocalUpdateSource,
    UpdateSource,
)
from .state_machine import (
    RollbackRecord,
    STAGE_ORDER,
    UpdateContext,
    UpdateStage,
    UpdateStateError,
    UpdateTransaction,
    VersionInfo,
)

__all__ = [
    "GitHubUpdateSource",
    "LocalUpdateSource",
    "RollbackRecord",
    "STAGE_ORDER",
    "UpdateContext",
    "UpdateSource",
    "UpdateStage",
    "UpdateStateError",
    "UpdateTransaction",
    "VersionInfo",
]
