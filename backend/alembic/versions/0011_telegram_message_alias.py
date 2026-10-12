"""Additional Telegram IDs for one canonical MAX message mapping."""
from alembic import op
import sqlalchemy as sa
revision = '0011_telegram_message_alias'
down_revision = '0010_member_sync'
branch_labels = depends_on = None


def upgrade():
    op.create_table('telegram_message_alias',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('message_map_id', sa.Integer(), sa.ForeignKey('message_map.id', ondelete='CASCADE'), nullable=False),
        sa.Column('telegram_account_id', sa.Integer(), nullable=False),
        sa.Column('telegram_chat_id', sa.String(64), nullable=False),
        sa.Column('telegram_thread_id', sa.String(64), nullable=False),
        sa.Column('telegram_message_id', sa.String(64), nullable=False),
        sa.UniqueConstraint('telegram_account_id','telegram_chat_id','telegram_thread_id','telegram_message_id',name='uq_telegram_alias'))


def downgrade():
    op.drop_table('telegram_message_alias')
