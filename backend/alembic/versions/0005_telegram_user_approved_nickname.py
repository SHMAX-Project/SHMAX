"""telegram_users: approved + nickname

Revision ID: 0005_telegram_user_approved_nickname
Revises: 0004_update_transactions
Create Date: 2026-10-05

Adds the audit-mandated columns:

  * approved  (Boolean, default False) — operator opt-in flag
    required before a Telegram user may post into MAX.
  * nickname  (String(64), nullable) — operator-chosen display
    name via /setname; persisted separately from Telegram's
    own ``display_name`` so a rename does not silently rebrand
    every outbound message.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_telegram_user_approved_nickname"
down_revision = "0004_update_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch:
        batch.add_column(
            sa.Column(
                "approved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "nickname",
                sa.String(length=64),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch:
        batch.drop_column("nickname")
        batch.drop_column("approved")