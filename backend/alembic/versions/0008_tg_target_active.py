"""single active Telegram target (surgical P1 #5).

Revision ID: 0008_tg_target_active
Revises: 0007_tg_target_validation
Create Date: 2026-10-07

Adds ``is_active`` to telegram_targets. EXACTLY ONE row is active;
pairing swaps the flag inside one serialized transaction. Backfill:
the most recently validated row (fallback: newest cached) becomes
active.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_tg_target_active"
down_revision = "0007_tg_target_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_targets",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        """
        UPDATE telegram_targets SET is_active = true
        WHERE id = (
            SELECT id FROM telegram_targets AS t
            WHERE t.validated_at IS NOT NULL
            ORDER BY t.validated_at DESC, t.cached_at DESC
            LIMIT 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column("telegram_targets", "is_active")
