"""Telegram group member sync fields (member-sync feature).

Revision ID: 0010_member_sync
Revises: 0009_sent_native
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010_member_sync"
down_revision = "0009_sent_native"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_users",
        sa.Column("membership", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "telegram_users",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "telegram_users",
        sa.Column("source", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_users", "source")
    op.drop_column("telegram_users", "is_admin")
    op.drop_column("telegram_users", "membership")
