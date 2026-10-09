"""update_transactions

Revision ID: 0004_update_transactions
Revises: 0003_admin_sessions
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op as _op


# revision identifiers, used by Alembic.
revision = "0004_update_transactions"
down_revision = "0003_admin_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _op.create_table(
        "update_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("transaction_id", sa.String(), nullable=False, unique=True),
        sa.Column("from_version", sa.String(), nullable=False),
        sa.Column("to_version", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("backup_id", sa.String(), nullable=True),
        sa.Column("old_schema", sa.String(), nullable=True),
        sa.Column("new_schema", sa.String(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    _op.create_index(
        "ix_update_transactions_transaction_id",
        "update_transactions",
        ["transaction_id"],
        unique=True,
    )


def downgrade() -> None:
    _op.drop_index("ix_update_transactions_transaction_id", table_name="update_transactions")
    _op.drop_table("update_transactions")