import tarfile
import json
import sqlite3
from pathlib import Path
import pytest
from shmax.backup.portable import snapshot,encrypt,decrypt,extract,rebind_owner,PortableError,BAD_PASSWORD
from shmax.database.migrations import run_migrations_sync
from shmax.security import SecretStore

@pytest.fixture
def appliance(tmp_path):
    root=tmp_path/'source';(root/'db').mkdir(parents=True)
    run_migrations_sync('sqlite:///'+str(root/'db/shmax.db'))
    store=SecretStore(root/'secrets');store.set_secret('telegram_bot_token','123:TOP_SECRET');store.set_secret('xray_subscription','https://secret.example/UUID')
    for name in ['sessions/max/session','config/xray.json','cache/photo.jpg','logs/runtime.log']:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'PRIVATE_SESSION')
    with sqlite3.connect(root/'db/shmax.db') as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('setup_completed','true',CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO telegram_users(user_id,role,approved,blocked,nickname,first_seen_at,is_admin) VALUES('77','operator',1,0,'Роман',CURRENT_TIMESTAMP,0)")
        db.commit()
        yield root


def package(root,tmp_path):
    payload=tmp_path/'payload.tar.gz'; manifest=snapshot(root,payload)
    encrypted=tmp_path/'backup.shmax';encrypt(payload,encrypted,'strong-password')
    return payload,encrypted,manifest


def test_portable_snapshot_secrets_sessions_wal_inventory(appliance,tmp_path):
    payload,encrypted,manifest=package(appliance,tmp_path)
    contents=encrypted.read_bytes()
    assert b'TOP_SECRET' not in contents and b'PRIVATE_SESSION' not in contents
    assert b'UUID' not in contents and b'manifest' not in contents
    names={e['path'] for e in manifest['entries']}
    assert {'db/shmax.db','sessions/max/session','config/xray.json'}.issubset(names)
    assert not any(n.startswith(('cache/','logs/')) for n in names)
    decrypted=tmp_path/'decrypted';decrypt(encrypted,decrypted,'strong-password')
    stage=tmp_path/'stage';read=extract(decrypted,stage)
    assert read==manifest
    assert SecretStore(stage/'secrets').get_secret('telegram_bot_token')=='123:TOP_SECRET'
    with sqlite3.connect(stage/'db/shmax.db') as db:
        assert db.execute('SELECT nickname FROM telegram_users').fetchone()[0]=='Роман'
    assert (stage/'sessions/max/session').read_bytes()==b'PRIVATE_SESSION'


@pytest.mark.parametrize('failure',['wrong','tamper','truncate','append','header'])
def test_authenticated_failure_leaves_no_partial_payload(appliance,tmp_path,failure):
    _,encrypted,_=package(appliance,tmp_path)
    raw=bytearray(encrypted.read_bytes())
    if failure=='tamper':raw[len(raw)//2]^=1
    if failure=='truncate':raw=raw[:-20]
    if failure=='append':raw+=b'junk'
    if failure=='header':raw[0]=0
    encrypted.write_bytes(raw)
    with pytest.raises(PortableError,match=BAD_PASSWORD):
        decrypt(encrypted,tmp_path/'result','wrong-password' if failure=='wrong' else 'strong-password')
    assert not (tmp_path/'result').exists()


def test_fresh_salt_nonce_each_export(appliance,tmp_path):
    payload,a,_=package(appliance,tmp_path);b=tmp_path/'second.shmax';encrypt(payload,b,'strong-password')
    assert a.read_bytes()!=b.read_bytes()


def test_rebind_destination_owner_and_revoke_imported_sessions(appliance,tmp_path):
    payload,_,manifest=package(appliance,tmp_path);stage=tmp_path/'stage';extract(payload,stage)
    rebind_owner(stage,{'username':'new-owner','password_hash':'destination-hash'})
    with sqlite3.connect(stage/'db/shmax.db') as db:
        assert db.execute('SELECT username,password_hash,role FROM admin_users').fetchall()==[('new-owner','destination-hash','owner')]
        assert db.execute('SELECT count(*) FROM admin_sessions').fetchone()[0]==0
        assert db.execute('SELECT nickname,approved,role FROM telegram_users').fetchone()==('Роман',1,'operator')


@pytest.mark.parametrize('name',['../escape','/absolute','secrets/link','config/x'])
def test_unsafe_archive_rejected(tmp_path,name):
    import tarfile,io
    payload=tmp_path/'bad.tar.gz'
    with tarfile.open(payload,'w:gz') as tar:
        info=tarfile.TarInfo(name)
        if name=='secrets/link':info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
        else:info.size=4
        tar.addfile(info,io.BytesIO(b'evil'))
    with pytest.raises(PortableError):extract(payload,tmp_path/'stage')
    assert not (tmp_path/'stage').exists() and not (tmp_path/'escape').exists()

@pytest.mark.parametrize('change',['new_version','new_schema','missing_key','malformed','bad_digest','missing_db'])
def test_manifest_schema_and_missing_recovery_material_rejected(appliance,tmp_path,change):
    import io,tarfile
    payload=tmp_path/'payload';snapshot(appliance,payload)
    with tarfile.open(payload,'r:gz') as tar:
        files={m.name:tar.extractfile(m).read() for m in tar if m.isfile()}
    manifest=json.loads(files['manifest.json'])
    if change=='new_version':manifest['application_version']='99.0.0'
    if change=='new_schema':manifest['schema_revision']='future_revision'
    if change=='bad_digest':manifest['entries'][0]['sha256']='0'*64
    if change in {'missing_key','missing_db'}:
        name='db/shmax.db' if change=='missing_db' else next(n for n in files if n.endswith('.key'))
        del files[name];manifest['entries']=[e for e in manifest['entries'] if e['path']!=name]
    files['manifest.json']=b'invalid' if change=='malformed' else json.dumps(manifest).encode()
    corrupt=tmp_path/'corrupt'
    with tarfile.open(corrupt,'w:gz') as tar:
        for name,data in files.items():
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    with pytest.raises(PortableError):extract(corrupt,tmp_path/'stage')
    assert not (tmp_path/'stage').exists()


def test_decompression_bomb_rejected_before_body(tmp_path):
    import tarfile,gzip
    from shmax.backup.portable import MAX_FILE
    header=tarfile.TarInfo('config/bomb');header.size=MAX_FILE+1
    payload=tmp_path/'bomb';payload.write_bytes(gzip.compress(header.tobuf()+b'\0'*1024))
    with pytest.raises(PortableError):extract(payload,tmp_path/'stage')


def test_compatible_previous_schema_migrates_forward(appliance,tmp_path):
    from alembic import command
    from shmax.database.migrations import _make_alembic_config,ALEMBIC_DIR
    from shmax.backup.portable import validate_stage,schema_revision
    cfg=_make_alembic_config('sqlite:///'+str(appliance/'db/shmax.db'));cfg.set_main_option('script_location',str(ALEMBIC_DIR))
    command.downgrade(cfg,'0009_sent_native')
    payload=tmp_path/'previous';snapshot(appliance,payload)
    stage=tmp_path/'stage';manifest=extract(payload,stage)
    assert manifest['schema_revision']=='0009_sent_native'
    validate_stage(stage,manifest,migrate=True)
    assert schema_revision(stage/'db/shmax.db')=='0010_member_sync'


def test_source_binaries_and_deployment_binding_excluded(appliance,tmp_path):
    (appliance/'state').mkdir(exist_ok=True)
    (appliance/'state/hardware').write_bytes(b'\x7fELFbinary')
    (appliance/'config/native.so').write_bytes(b'binary')
    payload=tmp_path/'portable';manifest=snapshot(appliance,payload)
    paths=[e['path'] for e in manifest['entries']]
    assert 'state/hardware' not in paths and 'config/native.so' not in paths

@pytest.mark.parametrize('kind',[tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_LONGLINK])
def test_extended_tar_metadata_rejected_before_large_allocation(tmp_path,kind):
    import gzip
    from shmax.backup.portable import extract,PortableError
    info=tarfile.TarInfo('metadata');info.type=kind;info.size=2*1024*1024*1024
    payload=tmp_path/'bomb.gz'
    with gzip.open(payload,'wb') as stream:stream.write(info.tobuf(format=tarfile.USTAR_FORMAT))
    with pytest.raises(PortableError):extract(payload,tmp_path/'stage')
    assert not (tmp_path/'stage').exists()
