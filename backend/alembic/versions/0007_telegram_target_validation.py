"""telegram_targets validation facts (audit P0 #3).

Revision ID: 0007_tg_target_validation
Revises: 0006_pairing_codes
Create Date: 2026-10-05

Adds the columns that hold facts validated against the REAL
Telegram API (getChat / getChatMember). A target whose
``missing_permissions`` list is non-empty is persisted for
diagnostics but is not considered paired.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_tg_target_validation"
down_revision = "0006_pairing_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_targets",
        sa.Column("chat_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "telegram_targets",
        sa.Column("bot_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "telegram_targets",
        sa.Column("missing_permissions", sa.Text(), nullable=True),
    )
    op.add_column(
        "telegram_targets",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_targets", "validated_at")
    op.drop_column("telegram_targets", "missing_permissions")
    op.drop_column("telegram_targets", "bot_status")
    op.drop_column("telegram_targets", "chat_type")
