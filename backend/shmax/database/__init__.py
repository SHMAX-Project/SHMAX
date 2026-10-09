"""Database layer: SQLAlchemy 2.x async + Alembic.

Stage 1B introduces the real SQLite schema:

* settings        — generic key/value runtime configuration
* admin_users     — local administrator accounts (Argon2id)
* setup_tokens    — one-time bootstrap tokens
* max_accounts    — MAX accounts (eventually per-account)
* max_chats       — cached MAX chat descriptors
* telegram_accounts — Telegram bot identity records
* telegram_targets — Telegram chats used as bridge targets
* telegram_users  — known Telegram users for ACL
* routes          — MAX chat ↔ Telegram topic mappings
* acl_rules       — role-based access rules
* message_map     — persistent MAX ↔ Telegram message id mapping
* delivery_queue  — durable outbox with retry
* audit_log       — administrative events
* schema_meta     — Alembic version + ad-hoc meta

The Core uses SQLAlchemy ORM objects. Repositories expose
typed query helpers so the rest of SHMAX does not have to
build SQL by hand.
"""

from .engine import (
    Database,
    DatabaseSettings,
    build_engine,
    build_session_factory,
)
from .migrations import create_all, run_migrations_sync

__all__ = [
    "Database",
    "DatabaseSettings",
    "build_engine",
    "build_session_factory",
    "create_all",
    "run_migrations_sync",
]
