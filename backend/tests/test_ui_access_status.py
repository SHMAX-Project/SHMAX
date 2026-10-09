"""Auth/UI contract regression tests with disposable roots and tokens."""
import datetime as dt
import pytest
from sqlalchemy import select
from shmax.auth.setup_token import SetupTokenService
from shmax.database.models import SetupToken, AdminUser
from shmax.integration_status import evidence_state

async def mint(app):
    async with app.state.db.session() as s:
        return (await SetupTokenService(s).create(ttl=dt.timedelta(hours=1))).token

def create_owner(client):
    token = client.portal.call(mint, client.app)
    r = client.post('/api/setup/admin', json={'username':'test-owner','password':'disposable-password','token':token})
    assert r.status_code == 200, r.text
    return token, r.json()['csrf_token']

def test_bootstrap_owner_session_token_atomicity(client):
    assert client.get('/api/auth/session').json()['mode'] == 'BOOTSTRAP'
    assert client.get('/api/status').status_code == 401
    token, csrf = create_owner(client)
    session = client.get('/api/auth/session').json()
    assert session['mode'] == 'AUTHENTICATED'
    assert session['admin_exists'] and not session['setup_complete']
    assert session['csrf_token'] == csrf
    assert client.post('/api/setup/admin',json={'username':'second','password':'password-2','token':token}).status_code == 409
    async def check():
        async with client.app.state.db.session() as s:
            rows=(await s.execute(select(SetupToken))).scalars().all()
            assert rows[0].consumed_at is not None
            assert len((await s.execute(select(AdminUser))).scalars().all()) == 1
    client.portal.call(check)
    assert client.post('/api/setup/complete',json={'acknowledge_unconfigured':True}).status_code == 403
    assert client.post('/api/setup/complete',headers={'X-CSRF-Token':csrf},json={'acknowledge_unconfigured':True}).status_code == 200
    assert client.get('/api/auth/session').json()['setup_complete']
    cookie=client.cookies.get('shmax_session')
    client.post('/api/auth/logout')
    client.cookies.set('shmax_session',cookie)
    assert client.get('/api/auth/session').json()['mode'] == 'AUTH_REQUIRED'
    assert client.get('/api/status').status_code == 401

def test_unconfigured_status_never_inherits_fake_health(client):
    _, csrf = create_owner(client)
    body = client.get('/api/status').json()
    for key in ('max','telegram','bridge'):
        assert body[key]['state'] == 'NOT_CONFIGURED'
    for name in ('max','telegram'):
        r=client.get(f'/api/watchdogs/{name}')
        assert r.status_code == 200
        assert r.json()['state'] == body[name]['state']
        assert not body[name]['configured']
    assert client.post('/api/setup/complete',headers={'X-CSRF-Token':csrf},json={}).status_code == 409
    client.post('/api/auth/logout')
    for path in ('/api/status','/api/routes','/api/watchdogs/max','/api/storage'):
        assert client.get(path).status_code == 401

def test_invalid_setup_does_not_create_owner(client):
    assert client.post('/api/setup/admin',json={'username':'bad','password':'password123','token':'invalid'}).status_code == 400
    assert client.get('/api/auth/session').json()['mode']=='BOOTSTRAP'

def test_health_requires_real_current_successful_probe():
    from shmax.adapters.max.fake import FakeMaxAdapter
    adapter=object()
    probe={'adapter':adapter,'at':100,'verified':True,'health':{'connected':True,'authenticated':True}}
    assert evidence_state(False,adapter,probe,None,101)=='NOT_CONFIGURED'
    assert evidence_state(True,FakeMaxAdapter(),probe,None,101)=='CONFIGURED'
    assert evidence_state(True,adapter,None,None,101)=='CONNECTING'
    assert evidence_state(True,adapter,probe,None,101)=='HEALTHY'
    assert evidence_state(True,object(),probe,None,101)=='CONNECTING'
    assert evidence_state(True,adapter,probe,None,300)=='DEGRADED'
    probe['verified']=False
    assert evidence_state(True,adapter,probe,None,101)=='CONFIGURED'
