"""Old application versions and backup formats migrate without losing state."""
import io
import json
import sqlite3
import tarfile
from pathlib import Path
import pytest
from alembic import command
from shmax.database.migrations import _make_alembic_config
from shmax.backup import portable
from shmax.backup.legacy_import import convert
from shmax.backup.manager import create_backup,create_encrypted_backup
from shmax.security import SecretStore

VERSIONS=[('0.1.0','0010_member_sync'),('0.1.1','0011_telegram_message_alias'),('0.1.2','0011_telegram_message_alias'),('0.1.3','0011_telegram_message_alias')]


def source_appliance(tmp_path,revision):
    root=tmp_path/'old';(root/'db').mkdir(parents=True)
    cfg=_make_alembic_config('sqlite:///'+str(root/'db/shmax.db'));command.upgrade(cfg,revision)
    store=SecretStore(root/'secrets');store.set_secret('telegram_bot_token','123:compat-test-only')
    store.set_secret('max_phone','compat-test-only')
    session=root/'sessions/max/main.db';session.parent.mkdir(parents=True);session.write_bytes(b'SYNTHETIC_SAVED_SESSION')
    with sqlite3.connect(root/'db/shmax.db') as c:
        c.execute("INSERT INTO max_accounts(id,label,created_at,updated_at) VALUES(1,'fixture',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_accounts(id,label,created_at,updated_at) VALUES(1,'fixture',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO telegram_targets(id,account_id,chat_id,is_forum,is_active,cached_at,validated_at,missing_permissions) VALUES(1,1,'-1001',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'[]')")
        c.execute("INSERT INTO telegram_users(user_id,role,approved,blocked,nickname,first_seen_at,is_admin) VALUES('77','operator',1,0,'Test sender',CURRENT_TIMESTAMP,0)")
        c.execute("INSERT INTO routes(id,max_account_id,max_chat_id,telegram_account_id,telegram_chat_id,telegram_thread_id,enabled,created_at,updated_at) VALUES(1,1,'max-chat',1,'-1001','11',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO message_map(id,direction,max_account_id,max_chat_id,max_message_id,telegram_account_id,telegram_chat_id,telegram_thread_id,telegram_message_id,outbound_marker,created_at) VALUES(1,'max_to_telegram',1,'max-chat','max-1',1,'-1001','11','100','marker',CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO delivery_queue(direction,target_chat_id,payload,status,attempts,next_attempt_at,created_at,updated_at,reply_mapping_id) VALUES('max_to_telegram','-1001','{}','retry',2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1)")
        if revision=='0011_telegram_message_alias':c.execute("INSERT INTO telegram_message_alias(message_map_id,telegram_account_id,telegram_chat_id,telegram_thread_id,telegram_message_id) VALUES(1,1,'-1001','11','101')")
    return root


@pytest.mark.parametrize('version,revision',VERSIONS)
@pytest.mark.parametrize('encrypted',[False,True])
def test_legacy_import_preserves_state_from_each_published_version(tmp_path,version,revision,encrypted):
    root=source_appliance(tmp_path,revision)
    archive=tmp_path/'old.shmax';password='eight123'
    if encrypted:create_encrypted_backup(data_dir=root,out_path=archive,passphrase=password,shmax_version=version)
    else:create_backup(data_dir=root,out_path=archive,shmax_version=version)
    payload=tmp_path/'converted.tar.gz'
    converted=convert(archive,payload,password if encrypted else '')
    assert converted['application_version']==version and converted['schema_revision']==revision
    stage=tmp_path/'stage';manifest=portable.extract(payload,stage)
    portable.validate_stage(stage,manifest,migrate=True)
    assert SecretStore(stage/'secrets').get_secret('telegram_bot_token')=='123:compat-test-only'
    assert (stage/'sessions/max/main.db').read_bytes()==b'SYNTHETIC_SAVED_SESSION'
    with sqlite3.connect(stage/'db/shmax.db') as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT role,approved,nickname FROM telegram_users').fetchall()==[('operator',1,'Test sender')]
        assert c.execute('SELECT target_id,user_id,role,approved FROM telegram_group_access').fetchall()==[(1,'77','operator',1)]
        assert c.execute('SELECT max_chat_id,telegram_thread_id FROM routes').fetchall()==[('max-chat','11')]
        assert c.execute('SELECT status,attempts,reply_mapping_id FROM delivery_queue').fetchall()==[('retry',2,1)]
        assert c.execute('SELECT max_message_id,telegram_message_id FROM message_map').fetchall()==[('max-1','100')]
        assert c.execute('SELECT count(*) FROM telegram_message_alias').fetchone()[0]==(0 if revision=='0010_member_sync' else 1)
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_wrong_legacy_password_leaves_no_payload(tmp_path):
    root=source_appliance(tmp_path,'0010_member_sync');archive=tmp_path/'old.shmax'
    create_encrypted_backup(data_dir=root,out_path=archive,passphrase='eight123',shmax_version='0.1.0')
    output=tmp_path/'converted.tar.gz'
    with pytest.raises(portable.PortableError,match='Неверный пароль'):convert(archive,output,'wrong123')
    assert not output.exists() and not list(tmp_path.glob('legacy-*'))


def test_legacy_tampered_file_does_not_pass_manifest(tmp_path):
    root=source_appliance(tmp_path,'0010_member_sync');archive=tmp_path/'old.shmax'
    create_backup(data_dir=root,out_path=archive,shmax_version='0.1.0')
    changed=tmp_path/'tampered.gz'
    with tarfile.open(archive,'r:gz') as src,tarfile.open(changed,'w:gz') as dest:
        for member in src:
            content=src.extractfile(member).read()
            if member.name=='sessions/max/main.db':content=b'TAMPERED';member.size=len(content)
            dest.addfile(member,io.BytesIO(content))
    with pytest.raises(portable.PortableError,match='целостность'):convert(changed,tmp_path/'converted.gz','')
    assert not (tmp_path/'converted.gz').exists()

@pytest.mark.parametrize('kind',[tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.FIFOTYPE])
def test_legacy_links_devices_and_traversal_are_rejected(tmp_path,kind):
    source=tmp_path/'unsafe.gz'
    with tarfile.open(source,'w:gz') as tar:
        item=tarfile.TarInfo('secrets/key');item.type=kind;item.linkname='/tmp/outside';tar.addfile(item)
    with pytest.raises(portable.PortableError):convert(source,tmp_path/'result.gz','')
    assert not (tmp_path/'result.gz').exists()


def test_encrypted_legacy_stream_crosses_multiple_base64_blocks(tmp_path):
    import os
    root=source_appliance(tmp_path,'0010_member_sync')
    content=b'SYNTHETIC_SESSION_'+os.urandom(200_000)
    (root/'sessions/max/main.db').write_bytes(content)
    archive=tmp_path/'large.shmax.enc'
    create_encrypted_backup(data_dir=root,out_path=archive,passphrase='eight123',shmax_version='0.1.0')
    assert archive.stat().st_size>65536*3
    payload=tmp_path/'converted.gz';convert(archive,payload,'eight123')
    portable.extract(payload,tmp_path/'stage')
    assert (tmp_path/'stage/sessions/max/main.db').read_bytes()==content


def test_legacy_pax_header_is_bounded_before_parsing(tmp_path):
    import gzip
    source=tmp_path/'bomb.gz'
    item=tarfile.TarInfo('././@PaxHeader');item.type=tarfile.XHDTYPE;item.size=65537
    source.write_bytes(gzip.compress(item.tobuf()+b'\0'*1024))
    with pytest.raises(portable.PortableError):convert(source,tmp_path/'result.gz','')
    assert not (tmp_path/'result.gz').exists()


def test_legacy_parent_traversal_never_extracts_outside_stage(tmp_path):
    source=tmp_path/'unsafe.gz'
    with tarfile.open(source,'w:gz') as tar:
        item=tarfile.TarInfo('../../outside');item.size=6;tar.addfile(item,io.BytesIO(b'unsafe'))
    with pytest.raises(portable.PortableError):convert(source,tmp_path/'result.gz','')
    assert not (tmp_path/'outside').exists()
