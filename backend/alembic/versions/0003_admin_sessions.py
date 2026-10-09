"""admin_sessions table for server-side logout / revocation

Revision ID: 0003_admin_sessions
Revises: 0002_queue_lease
Create Date: 2026-10-01 04:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_admin_sessions"
down_revision = "0002_queue_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
    )
    op.create_index("ix_admin_sessions_user", "admin_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_user", table_name="admin_sessions")
    op.drop_table("admin_sessions")
