"""pairing_codes table for /shmax_connect (audit P0 #6).

Revision ID: 0006_pairing_codes
Revises: 0005_telegram_user_approved_nickname
Create Date: 2026-10-05

Adds the ``pairing_codes`` table that the wizard writes to and
``/shmax_connect`` atomically consumes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_pairing_codes"
down_revision = "0005_telegram_user_approved_nickname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pairing_codes",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_chat_id", sa.String(length=128), nullable=False),
        sa.Column("max_chat_title", sa.String(length=256), nullable=True),
        sa.Column("telegram_chat_hint", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(
        "ix_pairing_codes_expires_at",
        "pairing_codes",
        ["expires_at"],
    )
    op.create_index(
        "ix_pairing_codes_consumed_at",
        "pairing_codes",
        ["consumed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pairing_codes_consumed_at", table_name="pairing_codes")
    op.drop_index("ix_pairing_codes_expires_at", table_name="pairing_codes")
    op.drop_table("pairing_codes")