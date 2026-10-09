"""queue lease and idempotency

Adds:
* ``delivery_queue.claimed_at`` + ``lease_until`` so that the
  worker can recover from crashes (expired leases are
  reclaimed before re-claim).
* ``delivery_queue.idempotency_key`` (UNIQUE) so a duplicate
  inbound event from the same source/chat/message/direction
  cannot create a second queue item.

Revision ID: 0002_queue_lease
Revises: 0001_initial
Create Date: 2026-10-01 04:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_queue_lease"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_queue",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delivery_queue",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delivery_queue",
        sa.Column("worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "delivery_queue",
        sa.Column("idempotency_key", sa.String(160), nullable=True),
    )
    # The UNIQUE constraint is created with nullable=True
    # because SQLite (and Alembic) do not allow adding a NOT
    # NULL UNIQUE column without a default. We populate the
    # column for legacy rows below.
    op.create_index(
        "uq_dq_idempotency",
        "delivery_queue",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_dq_status_lease",
        "delivery_queue",
        ["status", "lease_until"],
    )
    # Backfill idempotency_key for legacy rows so the unique
    # constraint does not block future inserts. Existing rows
    # get a stable key derived from their primary id.
    op.execute(
        "UPDATE delivery_queue SET idempotency_key = 'legacy:' || id "
        "WHERE idempotency_key IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_dq_status_lease", table_name="delivery_queue")
    op.drop_index("uq_dq_idempotency", table_name="delivery_queue")
    op.drop_column("delivery_queue", "idempotency_key")
    op.drop_column("delivery_queue", "worker_id")
    op.drop_column("delivery_queue", "lease_until")
    op.drop_column("delivery_queue", "claimed_at")
