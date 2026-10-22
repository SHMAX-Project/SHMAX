import pytest
from tests.test_backup_api import client
from shmax.backup.portable import MAGIC


def test_export_one_authenticated_encrypted_file(client):
    result=client.post('/api/portable/export',json={'password':'strong-password','confirmation':'strong-password'})
    assert result.status_code==200,result.text
    assert result.content.startswith(MAGIC)
    assert '.shmax' in result.headers['content-disposition']
    assert b'owner' not in result.content
    assert not list((client.app.state.paths.root/'backups/portable').iterdir())


def test_import_preview_then_fail_closed_without_hostd(client):
    exported=client.post('/api/portable/export',json={'password':'strong-password','confirmation':'strong-password'}).content
    upload=client.post('/api/portable/import',content=exported,headers={'Content-Type':'application/octet-stream'})
    assert upload.status_code==200,upload.text
    identity=upload.json()['job_id']
    assert client.post(f'/api/portable/{identity}/restore',json={}).status_code==400
    bad=client.post(f'/api/portable/{identity}/inspect',json={'password':'incorrect-password'})
    assert bad.status_code==400 and 'Неверный пароль' in bad.json()['detail']
    preview=client.post(f'/api/portable/{identity}/inspect',json={'password':'strong-password'})
    assert preview.status_code==200,preview.text
    assert preview.json()['compatible'] and 'secrets' in preview.json()['contents']
    result=client.post(f'/api/portable/{identity}/restore',json={})
    assert result.status_code==503
    assert client.get('/api/auth/session').json()['authenticated']


@pytest.mark.parametrize('path',['/api/portable/export','/api/portable/import','/api/portable/'+'a'*32+'/inspect','/api/portable/'+'a'*32+'/restore'])
def test_portable_requires_csrf(client,path):
    result=client.post(path,json={'password':'strong-password'},headers={'X-CSRF-Token':'invalid'})
    assert result.status_code==403


def test_portable_requires_privileged_role(client):
    import sqlite3
    with sqlite3.connect(client.app.state.paths.root/'db/shmax.db') as db:
        db.execute("UPDATE admin_users SET role='operator'");db.commit()
    result=client.post('/api/portable/export',json={'password':'strong-password','confirmation':'strong-password'})
    assert result.status_code==403


def test_portable_unauthenticated_denied(client):
    client.cookies.clear()
    assert client.post('/api/portable/import',content=b'x',headers={'Content-Type':'application/octet-stream'}).status_code==401

def test_password_validation_does_not_echo_secret(client):
    secret='PRIVATE'*50
    response=client.post('/api/portable/export',json={'password':secret,'confirmation':secret})
    assert response.status_code==422 and secret not in response.text


def test_import_size_cap_is_checked_before_writing(client):
    response=client.post('/api/portable/import',content=b'x',headers={'Content-Type':'application/octet-stream','Content-Length':str(600*1024*1024)})
    assert response.status_code==413
    root=client.app.state.paths.root/'backups/portable'
    assert not root.exists() or not list(root.iterdir())


def test_admin_can_export_but_cannot_restore(client):
    import sqlite3
    with sqlite3.connect(client.app.state.paths.root/'db/shmax.db') as db:
        db.execute("UPDATE admin_users SET role='admin'");db.commit()
    result=client.post('/api/portable/import',content=b'x'*64,headers={'Content-Type':'application/octet-stream'})
    assert result.status_code==403


def test_export_password_attempts_rate_limited(client):
    for _ in range(5):
        assert client.post('/api/portable/export',json={'password':'strong-password','confirmation':'different-password'}).status_code==400
    assert client.post('/api/portable/export',json={'password':'strong-password','confirmation':'strong-password'}).status_code==429

@pytest.mark.parametrize('encrypted',[False,True])
def test_import_legacy_backup_uses_same_owner_preview_gate(client,tmp_path,encrypted):
    from shmax.backup.manager import create_backup,create_encrypted_backup
    archive=tmp_path/'legacy.shmax'
    kwargs={'data_dir':client.app.state.paths.root,'out_path':archive,'shmax_version':'0.1.3'}
    if encrypted:create_encrypted_backup(**kwargs,passphrase='eight123')
    else:create_backup(**kwargs)
    uploaded=client.post('/api/portable/import',content=archive.read_bytes(),headers={'Content-Type':'application/octet-stream'})
    assert uploaded.status_code==200
    identity=uploaded.json()['job_id']
    response=client.post(f'/api/portable/{identity}/inspect',json={'password':'eight123' if encrypted else ''})
    assert response.status_code==200,response.text
    assert response.json()['compatible'] and response.json()['source_format']=='legacy-v1'
    assert client.get('/api/auth/session').json()['authenticated']
    # Preview never starts a restore or replaces the active database/session.
    assert not (client.app.state.paths.root/'backups/portable'/identity/'confirmed').exists()
