"""SQLAlchemy 2.x ORM models for the SHMAX schema.

Single source of truth for tables. Alembic migrations
should be generated from this file.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------
# Settings / tokens
# ---------------------------------------------------------------------
class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="owner", nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_login_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AdminSession(Base):
    """Server-side session store for admin / operator logins.

    The cookie carries an opaque session id; we verify it
    against this table on every request. Logout deletes the
    row, so a stolen cookie cannot outlive the operator's
    own logout. Sessions also have a hard expiry.
    """

    __tablename__ = "admin_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class SetupToken(Base):
    __tablename__ = "setup_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(64), default="bootstrap")


# ---------------------------------------------------------------------
# MAX side
# ---------------------------------------------------------------------
class MaxAccount(Base):
    __tablename__ = "max_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), default="primary", nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    session_path: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    transport: Mapped[str] = mapped_column(String(16), default="pymax", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="not_configured", nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class MaxChat(Base):
    __tablename__ = "max_chats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    last_event_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cached_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", name="uq_max_chats_account_chat"),
    )


# ---------------------------------------------------------------------
# Telegram side
# ---------------------------------------------------------------------
class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), default="primary", nullable=False)
    bot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    transport: Mapped[str] = mapped_column(
        String(16), default="bot_api", nullable=False
    )  # bot_api | mtproto
    status: Mapped[str] = mapped_column(String(32), default="not_configured", nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TelegramTarget(Base):
    __tablename__ = "telegram_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Audit P0 #3: validated facts from the REAL Telegram API —
    # never trust is_forum supplied by the frontend or a manual
    # chat-id entry. ``validated_at`` is set only after a live
    # getChat / getChatMember round-trip; ``missing_permissions``
    # is a JSON array of the exact rights SHMAX still lacks.
    # A target with a non-empty list is stored for diagnostics
    # but is NOT paired/ready.
    chat_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # "supergroup" | "group" | "channel" | "private"
    bot_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # "member" | "administrator" | ...
    missing_permissions: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # JSON list; NULL or "[]" = fully validated
    validated_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Each group is enabled independently. Pairing another group preserves it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_admin_hub: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_add_chats: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    show_source_chat: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_sender: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cached_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "account_id", "chat_id", name="uq_telegram_targets_account_chat"
        ),
    )


class TelegramUser(Base):
    """A Telegram user who has interacted with SHMAX.

    ``user_id`` is the authoritative principal — ACL decisions
    are keyed on it. ``display_name`` and ``username`` are
    best-effort copies from Telegram's own payload; ``nickname``
    is the operator-chosen /setname value that the bridge
    prefixes onto outbound MAX messages.

    ``approved`` defaults to False. A new user can register via
    /start but cannot send into MAX until the operator flips
    the flag in the web Users page. ``blocked`` is the inverse:
    a previously-approved user can be silenced without losing
    the registration.
    """

    __tablename__ = "telegram_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    nickname: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="read_only", nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Member-sync (Telegram MTProto group member synchronization):
    # membership state observed in the paired group ("member",
    # "admin", "owner", "left", "kicked"); ``is_admin`` mirrors
    # Telegram promoter/creator rights but NEVER grants SHMAX
    # admin; ``source`` records how the user was discovered
    # ("sync" = participant list, "message"/"event" = observed).
    membership: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )


class TelegramGroupAccess(Base):
    """Explicit per-group grants; discovery never grants permission."""
    __tablename__ = "telegram_group_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("telegram_targets.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("telegram_users.user_id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="read_only", nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    membership: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    __table_args__ = (UniqueConstraint("target_id", "user_id", name="uq_group_access_user"),)


# ---------------------------------------------------------------------
# Routing / ACL
# ---------------------------------------------------------------------
class Route(Base):
    __tablename__ = "routes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    max_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    max_account_id: Mapped[int] = mapped_column(
        ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=False
    )
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_thread_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    telegram_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "max_account_id",
            "max_chat_id",
            "telegram_account_id",
            "telegram_chat_id",
            name="uq_routes_max_destination",
        ),
        UniqueConstraint(
            "telegram_account_id",
            "telegram_chat_id",
            "telegram_thread_id",
            name="uq_routes_telegram",
        ),
        Index("ix_routes_enabled", "enabled"),
    )


class AclRule(Base):
    __tablename__ = "acl_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # global | chat | user
    scope_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    principal: Mapped[str] = mapped_column(String(64), nullable=False)  # role or user_id
    permission: Mapped[str] = mapped_column(String(64), nullable=False)
    allow: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


# ---------------------------------------------------------------------
# Message mapping + delivery queue
# ---------------------------------------------------------------------
class MessageMap(Base):
    """Persistent MAX <-> Telegram message id mapping.

    Both directions are stored in a single table keyed by the
    source message; lookups are O(1) via either side's unique
    composite index.
    """

    __tablename__ = "message_map"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    max_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=True
    )
    max_chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    max_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    telegram_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=True
    )
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    telegram_thread_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    telegram_message_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    outbound_marker: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "max_account_id",
            "max_chat_id",
            "max_message_id",
            "telegram_account_id",
            "telegram_chat_id",
            name="uq_msgmap_max_destination",
        ),
        UniqueConstraint(
            "telegram_account_id",
            "telegram_chat_id",
            "telegram_thread_id",
            "telegram_message_id",
            name="uq_msgmap_tg",
        ),
        Index("ix_msgmap_outbound", "outbound_marker", unique=True),
    )


class TelegramMessageAlias(Base):
    __tablename__ = "telegram_message_alias"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_map_id: Mapped[int] = mapped_column(ForeignKey("message_map.id", ondelete="CASCADE"), nullable=False)
    telegram_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("telegram_account_id", "telegram_chat_id", "telegram_thread_id", "telegram_message_id", name="uq_telegram_alias"),)


class DeliveryQueueItem(Base):
    """Durable outbox.

    A row is created the moment the Core decides to deliver
    something; the worker picks it up by status / ``next_attempt_at``
    and updates it through the state machine.

    V1 atomicity model:

    * ``claim_batch`` issues a single ``UPDATE`` guarded by
      status + lease state, then reads the rows it just
      claimed. Two workers cannot see the same row twice.
    * ``lease_until`` is set per claim; if the worker crashes
      before delivering, the next claim re-takes the row
      once the lease has expired.
    * ``idempotency_key`` (UNIQUE) prevents the bridge from
      enqueuing the same source/chat/message twice.
    """

    __tablename__ = "delivery_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    depends_on_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    target_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_thread_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    reply_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("message_map.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    # Atomic claim / lease.
    claimed_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_until: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Inbound idempotency: a stable key for (source,
    # chat_id, message_external_id, direction). UNIQUE.
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(160), nullable=True
    )
    __table_args__ = (
        Index("ix_dq_status_next", "status", "next_attempt_at"),
        Index("ix_dq_status_lease", "status", "lease_until"),
        Index("uq_dq_idempotency", "idempotency_key", unique=True),
    )


# ---------------------------------------------------------------------
# Audit / meta
# ---------------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ---------------------------------------------------------------------
class UpdateTransaction(Base):
    __tablename__ = "update_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    from_version: Mapped[str] = mapped_column(String(64), nullable=False)
    to_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    backup_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    old_schema: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    new_schema: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------
class PairingCodeRow(Base):
    """One-time pairing code for /shmax_connect (audit P0 #6).

    A code is issued from the wizard against a MAX chat the
    operator wants to bridge. The operator sends the code from
    the target Telegram supergroup; the adapter atomically marks
    the row consumed and the route is persisted.

    Properties:

    * ``code`` — 8 random URL-safe bytes, indexed unique.
    * ``expires_at`` — unix seconds; reject past this instant.
    * ``attempts`` — incrementing on every failed claim to
      defeat brute force; reject past ``max_attempts``.
    * ``consumed_at`` — set on success; reject if set.
    """

    __tablename__ = "pairing_codes"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    issued_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    max_chat_title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    telegram_chat_hint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


# ---------------------------------------------------------------------
class SentNative(Base):
    """Registry of OUR outbound native message ids (live A1).

    One logical delivery may produce SEVERAL native messages
    (media + separate follow-up text). Every native id is
    registered here at delivery time so an echo of ANY of them is
    suppressed internally — the visible-text marker is gone and
    must never come back.
    """

    __tablename__ = "sent_native"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)  # max | telegram
    chat_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    marker: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "transport",
            "chat_external_id",
            "message_external_id",
            name="uq_sent_native",
        ),
        Index("ix_sent_native_lookup", "transport", "chat_external_id", "message_external_id"),
    )
