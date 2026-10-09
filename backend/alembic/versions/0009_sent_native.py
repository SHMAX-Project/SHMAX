"""outbound native-id registry for echo suppression (live A1).

Revision ID: 0009_sent_native
Revises: 0008_tg_target_active
Create Date: 2026-10-08

One logical delivery may produce several native messages (media +
follow-up text). Every native id is registered in sent_native so
echoes are suppressed internally — the visible shmax: marker was
removed and must never return.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_sent_native"
down_revision = "0008_tg_target_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sent_native",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("chat_external_id", sa.String(length=128), nullable=False),
        sa.Column("message_external_id", sa.String(length=128), nullable=False),
        sa.Column("marker", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "transport",
            "chat_external_id",
            "message_external_id",
            name="uq_sent_native",
        ),
    )
    op.create_index(
        "ix_sent_native_lookup",
        "sent_native",
        ["transport", "chat_external_id", "message_external_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sent_native_lookup", table_name="sent_native")
    op.drop_table("sent_native")
