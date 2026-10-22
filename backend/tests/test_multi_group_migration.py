"""An upgrade preserves existing routes, aliases and explicit access grants."""
import sqlite3
import json
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from alembic import command
from shmax.database.migrations import _make_alembic_config


@pytest.mark.parametrize('foreign_keys',[False,True])
def test_upgrade_preserves_history_and_only_backfills_active_group(tmp_path,foreign_keys):
    album={'media_items':[{'kind':'photo','payload':{'photo_id':i,'base_url':f'https://cdn.max.ru/fixture-{i}'}} for i in range(3)],'album_caption':'caption','album_progress':{'next':1,'ids':['101'],'caption_sent':True},'max_chat_id':'max','max_message_id':'m1'}
    path=tmp_path/'migration.db'
    cfg=_make_alembic_config(f'sqlite:///{path}')
    command.upgrade(cfg,'0011_telegram_message_alias')
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO max_accounts(id,label,created_at,updated_at) VALUES(1,'primary',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_accounts(id,label,created_at,updated_at) VALUES(1,'primary',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_targets(id,account_id,chat_id,is_forum,is_active,cached_at) VALUES(1,1,'-1',1,1,CURRENT_TIMESTAMP),(2,1,'-2',1,0,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_users(user_id,role,approved,blocked,is_admin,first_seen_at) VALUES('alice','operator',1,0,0,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO routes(id,max_account_id,max_chat_id,telegram_account_id,telegram_chat_id,telegram_thread_id,enabled,created_at,updated_at) VALUES(7,1,'max',1,'-1','11',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO message_map(id,direction,max_account_id,max_chat_id,max_message_id,telegram_account_id,telegram_chat_id,telegram_thread_id,telegram_message_id,outbound_marker,created_at) VALUES(9,'max_to_telegram',1,'max','m1',1,'-1','11','101','marker',CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_message_alias(id,message_map_id,telegram_account_id,telegram_chat_id,telegram_thread_id,telegram_message_id) VALUES(12,9,1,'-1','11','102')")
        c.execute("INSERT INTO delivery_queue(id,direction,target_chat_id,payload,status,attempts,reply_mapping_id,next_attempt_at,created_at,updated_at,idempotency_key) VALUES(4,'max_to_telegram','-1',?,'retry',2,9,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'max:max:m1:max_to_telegram')",(json.dumps(album),))
    def enable_fk(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    if foreign_keys:event.listen(Engine,'connect',enable_fk)
    try:command.upgrade(cfg,'head')
    finally:
        if foreign_keys:event.remove(Engine,'connect',enable_fk)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT target_id,user_id,role,approved,blocked FROM telegram_group_access').fetchall()==[(1,'alice','operator',1,0)]
        assert c.execute('SELECT id,telegram_thread_id FROM routes').fetchall()==[(7,'11')]
        assert c.execute('SELECT id,message_map_id,telegram_message_id FROM telegram_message_alias').fetchall()==[(12,9,'102')]
        assert c.execute('SELECT id,max_message_id FROM message_map').fetchall()==[(9,'m1')]
        assert c.execute('SELECT id,status,attempts,depends_on_id,reply_mapping_id FROM delivery_queue').fetchall()==[(4,'retry',2,None,9)]
        assert c.execute('SELECT id,is_active,is_admin_hub,show_sender FROM telegram_targets ORDER BY id').fetchall()==[(1,1,0,1),(2,0,0,1)]
        assert json.loads(c.execute('SELECT payload FROM delivery_queue WHERE id=4').fetchone()[0])==album
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert c.execute('SELECT idempotency_key FROM delivery_queue').fetchall()==[('max:max:m1:max_to_telegram:-1',)]
        # Fanout can now persist a second independent mapping for the same MAX ID.
        c.execute("INSERT INTO message_map(direction,max_account_id,max_chat_id,max_message_id,telegram_account_id,telegram_chat_id,telegram_thread_id,telegram_message_id,outbound_marker,created_at) VALUES('max_to_telegram',1,'max','m1',1,'-2','22','201','marker2',CURRENT_TIMESTAMP)")
