"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30 23:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "setup_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purpose", sa.String(64), nullable=False, server_default="bootstrap"),
    )

    op.create_table(
        "max_accounts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False, server_default="primary"),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("session_path", sa.String(256), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("transport", sa.String(16), nullable=False, server_default="pymax"),
        sa.Column("status", sa.String(32), nullable=False, server_default="not_configured"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "max_chats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cached_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "chat_id", name="uq_max_chats_account_chat"),
    )

    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False, server_default="primary"),
        sa.Column("bot_id", sa.String(64), nullable=True),
        sa.Column("bot_username", sa.String(64), nullable=True),
        sa.Column("transport", sa.String(16), nullable=False, server_default="bot_api"),
        sa.Column("status", sa.String(32), nullable=False, server_default="not_configured"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "telegram_targets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("is_forum", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("cached_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "chat_id", name="uq_telegram_targets_account_chat"),
    )
    op.create_table(
        "telegram_users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="read_only"),
        sa.Column("blocked", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "routes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("max_chat_id", sa.String(64), nullable=False),
        sa.Column("max_account_id", sa.Integer, sa.ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_chat_id", sa.String(64), nullable=False),
        sa.Column("telegram_thread_id", sa.String(64), nullable=True),
        sa.Column("telegram_account_id", sa.Integer, sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("max_account_id", "max_chat_id", name="uq_routes_max"),
        sa.UniqueConstraint(
            "telegram_account_id", "telegram_chat_id", "telegram_thread_id",
            name="uq_routes_telegram",
        ),
    )
    op.create_index("ix_routes_enabled", "routes", ["enabled"])

    op.create_table(
        "acl_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("scope_ref", sa.String(64), nullable=True),
        sa.Column("principal", sa.String(64), nullable=False),
        sa.Column("permission", sa.String(64), nullable=False),
        sa.Column("allow", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "message_map",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("max_account_id", sa.Integer, sa.ForeignKey("max_accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("max_chat_id", sa.String(64), nullable=True),
        sa.Column("max_message_id", sa.String(64), nullable=True),
        sa.Column("telegram_account_id", sa.Integer, sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("telegram_chat_id", sa.String(64), nullable=True),
        sa.Column("telegram_thread_id", sa.String(64), nullable=True),
        sa.Column("telegram_message_id", sa.String(64), nullable=True),
        sa.Column("outbound_marker", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("max_account_id", "max_chat_id", "max_message_id", name="uq_msgmap_max"),
        sa.UniqueConstraint(
            "telegram_account_id", "telegram_chat_id", "telegram_thread_id", "telegram_message_id",
            name="uq_msgmap_tg",
        ),
    )
    op.create_index("ix_msgmap_outbound", "message_map", ["outbound_marker"], unique=True)

    op.create_table(
        "delivery_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("target_chat_id", sa.String(64), nullable=False),
        sa.Column("target_thread_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("reply_mapping_id", sa.Integer, sa.ForeignKey("message_map.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dq_status_next", "delivery_queue", ["status", "next_attempt_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("target", sa.String(128), nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "schema_meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "schema_meta",
        "audit_log",
        "delivery_queue",
        "message_map",
        "acl_rules",
        "routes",
        "telegram_users",
        "telegram_targets",
        "telegram_accounts",
        "max_chats",
        "max_accounts",
        "setup_tokens",
        "admin_users",
        "settings",
    ):
        op.drop_table(table)
