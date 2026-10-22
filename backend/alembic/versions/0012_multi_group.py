"""Per-group grants, administrator fanout and durable dependent deliveries.

Preserve the current group and its grants; previously deactivated groups stay
inactive. Preserve message aliases while rebuilding SQLite unique constraints.
"""
import sqlalchemy as sa
from alembic import op
revision = '0012_multi_group'
down_revision = '0011_telegram_message_alias'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('telegram_targets', sa.Column('is_admin_hub', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('telegram_targets', sa.Column('auto_add_chats', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('telegram_targets', sa.Column('show_source_chat', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('telegram_targets', sa.Column('show_sender', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table('telegram_group_access',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('target_id',sa.Integer(),sa.ForeignKey('telegram_targets.id',ondelete='CASCADE'),nullable=False),
        sa.Column('user_id',sa.String(64),sa.ForeignKey('telegram_users.user_id',ondelete='CASCADE'),nullable=False),
        sa.Column('role',sa.String(16),nullable=False,server_default='read_only'),
        sa.Column('approved',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('blocked',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('membership',sa.String(16)),sa.Column('is_admin',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('source',sa.String(16)),sa.UniqueConstraint('target_id','user_id',name='uq_group_access_user'))
    op.execute('''INSERT INTO telegram_group_access(target_id,user_id,role,approved,blocked,membership,is_admin,source)
        SELECT t.id,u.user_id,u.role,u.approved,u.blocked,u.membership,u.is_admin,u.source
        FROM telegram_users u CROSS JOIN telegram_targets t WHERE t.is_active = true''')
    with op.batch_alter_table('routes') as batch:
        batch.drop_constraint('uq_routes_max', type_='unique')
        batch.create_unique_constraint('uq_routes_max_destination', ['max_account_id','max_chat_id','telegram_account_id','telegram_chat_id'])
    # ON DELETE CASCADE can remove aliases during SQLite table recreation.
    # Save and restore them explicitly; IDs and foreign keys remain unchanged.
    op.execute('CREATE TABLE shmax_migration_aliases AS SELECT * FROM telegram_message_alias')
    op.execute('CREATE TABLE shmax_migration_queue_refs AS SELECT id,reply_mapping_id FROM delivery_queue WHERE reply_mapping_id IS NOT NULL')
    with op.batch_alter_table('message_map') as batch:
        batch.drop_constraint('uq_msgmap_max',type_='unique')
        batch.create_unique_constraint('uq_msgmap_max_destination',['max_account_id','max_chat_id','max_message_id','telegram_account_id','telegram_chat_id'])
    op.execute('DELETE FROM telegram_message_alias')
    op.execute('INSERT INTO telegram_message_alias SELECT * FROM shmax_migration_aliases')
    op.drop_table('shmax_migration_aliases')
    op.execute('UPDATE delivery_queue SET reply_mapping_id=(SELECT reply_mapping_id FROM shmax_migration_queue_refs WHERE shmax_migration_queue_refs.id=delivery_queue.id) WHERE id IN (SELECT id FROM shmax_migration_queue_refs)')
    op.drop_table('shmax_migration_queue_refs')
    op.add_column('delivery_queue',sa.Column('depends_on_id',sa.Integer(),nullable=True))
    op.create_index('ix_delivery_queue_depends_on_id','delivery_queue',['depends_on_id'])
    # Replayed MAX updates must collide with their pre-upgrade outbox row,
    # including rows not yet mapped because their send is still pending.
    op.execute(sa.text("UPDATE delivery_queue SET idempotency_key=idempotency_key||':'||target_chat_id WHERE direction='max_to_telegram' AND substr(idempotency_key,1,4)='max:' AND substr(idempotency_key,-16)=:suffix").bindparams(suffix=':max_to_telegram'))


def downgrade():
    # Collapsing fanout would discard mappings. Require an operator-managed
    # cold rollback instead of silently deleting rows in a schema downgrade.
    raise RuntimeError('v0.2.0 multi-group downgrade requires restoring its verified pre-upgrade cold backup')
