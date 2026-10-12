"""Production service tests; only the external Telegram boundary is mocked."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace as NS
import pytest
from telethon.tl.types import ChannelParticipant, ChannelParticipantAdmin, ChannelParticipantCreator
from telethon.errors import FloodWaitError
from shmax.telegram_members.sync import MemberSyncService, observe_membership
from shmax.security.secret_store import SecretStore
from shmax.users.repository import TelegramUsersRepository
from shmax.database.models import TelegramAccount
from shmax.active_target import upsert_and_activate_target

class Client:
    def __init__(self):
        self.users = [NS(id=i, username=f'u{i}', first_name=f'Person {i}', last_name=None, bot=i in (1,2)) for i in range(1,254)]
        self.requests = []
        self.closed = False
        self.error = None
        self.pause = None
        self.identity = NS(id=1, bot=True)
        self.empty_page = False
    async def connect(self): pass
    async def sign_in(self, **kwargs):
        assert kwargs == {'bot_token': '1:test-token'}
        return self.identity
    async def get_input_entity(self, entity):
        assert entity == -100123
        return entity
    async def __call__(self, request):
        self.requests.append(request)
        if self.pause: await self.pause.wait()
        if self.error: raise self.error
        if self.empty_page: return NS(users=[],participants=[],count=253)
        users = self.users[request.offset:request.offset + request.limit]
        participants = [ChannelParticipant(user_id=u.id,date=None) for u in users]
        for i,u in enumerate(users):
            if u.id == 3: participants[i] = ChannelParticipantCreator(user_id=u.id, admin_rights=None)
            if u.id == 4: participants[i] = ChannelParticipantAdmin(user_id=u.id, inviter_id=1,promoted_by=1,date=None,admin_rights=None)
        return NS(users=users,participants=participants,count=len(self.users))
    async def disconnect(self): self.closed = True

def service(db,tmp_path,client):
    store=SecretStore(tmp_path/'secrets')
    store.set_secret('telegram_bot_token','1:test-token')
    svc=MemberSyncService(db=db,secret_store=store,client_factory=lambda:client)
    svc.save_credentials(123,'a'*32)
    return svc

@pytest.mark.asyncio
async def test_existing_silent_members_pagination_acl_and_idempotence(db,tmp_path):
    client=Client(); svc=service(db,tmp_path,client)
    stats=await svc.sync('-100123')
    assert (stats.status,stats.found,stats.new,stats.updated)==('ok',251,251,0)
    assert [r.offset for r in client.requests]==[0,200]
    assert all(r.limit==200 for r in client.requests)
    repo=TelegramUsersRepository(db)
    silent=await repo.get('253')
    assert silent and not silent.approved and not silent.blocked and silent.role=='read_only'
    assert await repo.get('1') is None and await repo.get('2') is None
    for uid in ('3','4'):
        admin=await repo.get(uid)
        assert admin.is_admin and admin.role=='read_only' and not admin.approved
    await repo.set_approval(user_id='3',approved=True,role='operator')
    await repo.set_nickname(user_id='3',nickname='Local')
    again=await svc.sync('-100123')
    assert (again.new,again.updated)==(0,0)
    client.users[-1].username='changed';client.users[-1].first_name='Changed Name'
    changed=await svc.sync('-100123')
    assert changed.updated==1 and changed.new==0
    owner=await repo.get('3')
    assert owner.approved and owner.role=='operator' and owner.nickname=='Local'
    assert client.closed and 'a'*32 not in json.dumps(await svc.state())
    assert not list(tmp_path.rglob('*.session'))

@pytest.mark.asyncio
async def test_not_configured_and_failure(db,tmp_path):
    svc=MemberSyncService(db=db,secret_store=SecretStore(tmp_path/'secrets'))
    assert (await svc.sync()).status=='not_configured'
    client=Client();svc=service(db,tmp_path,client)
    client.error=RuntimeError('sensitive error')
    assert (await svc.sync('-100123')).status=='telegram_error' and client.closed
    client.error=ImportError('missing')
    assert (await svc.sync('-100123')).status=='unavailable'

@pytest.mark.asyncio
async def test_identity_rejects_human_or_other_bot(db,tmp_path):
    client=Client();svc=service(db,tmp_path,client)
    for identity in (NS(id=1,bot=False),NS(id=99,bot=True)):
        client.identity=identity
        assert (await svc.sync('-100123')).status=='identity_mismatch'
        assert not client.requests and client.closed

@pytest.mark.asyncio
async def test_flood_wait_is_bounded(db,tmp_path,monkeypatch):
    client=Client();svc=service(db,tmp_path,client)
    client.error=FloodWaitError(request=None,capture=1)
    sleeps=[]
    async def sleep(seconds): sleeps.append(seconds)
    monkeypatch.setattr('shmax.telegram_members.sync.asyncio.sleep',sleep)
    assert (await svc.sync('-100123')).status=='rate_limited'
    assert sleeps==[1] and len(client.requests)==2 and client.closed
    client.requests.clear();client.error=FloodWaitError(request=None,capture=999)
    assert (await svc.sync('-100123')).status=='rate_limited' and len(client.requests)==1

@pytest.mark.asyncio
async def test_concurrency_cancellation_schedule(db,tmp_path):
    client=Client();svc=service(db,tmp_path,client);client.pause=asyncio.Event()
    task=asyncio.create_task(svc.sync('-100123'))
    while not client.requests: await asyncio.sleep(0)
    assert (await svc.sync('-100123')).status=='running'
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert client.closed
    svc.schedule(delay=10);scheduled=svc._task;svc.schedule(delay=10)
    assert svc._task is scheduled
    await svc.close();assert scheduled.done()

@pytest.mark.asyncio
@pytest.mark.parametrize('status,is_member,expected,admin',[
 ('member',False,'member',False),('left',False,'left',False),('kicked',False,'kicked',False),
 ('administrator',False,'admin',True),('creator',False,'owner',True),
 ('restricted',True,'member',False),('restricted',False,'left',False)])
async def test_live_membership_active_group_and_acl(db,status,is_member,expected,admin):
    async with db.session() as s:
        acc=TelegramAccount(label='primary');s.add(acc);await s.commit()
    await upsert_and_activate_target(db,account_id=acc.id,chat_id='-100123',title='Group',is_forum=True,chat_type='supergroup',bot_status='administrator')
    kwargs=dict(user_id='30',username='silent',display_name='Silent',status=status,is_member=is_member)
    await observe_membership(db,chat_id='other',**kwargs)
    repo=TelegramUsersRepository(db);assert await repo.get('30') is None
    await observe_membership(db,chat_id='-100123',**kwargs)
    user=await repo.get('30')
    assert user.membership==expected and user.is_admin==admin
    assert user.role=='read_only' and not user.approved and not user.blocked

@pytest.mark.parametrize('start',[None,'0009_sent_native'])
def test_migration_fresh_and_upgrade(tmp_path,start):
    from alembic import command
    from shmax.database.migrations import _make_alembic_config
    path=tmp_path/'db.sqlite';cfg=_make_alembic_config(f'sqlite:///{path}')
    if start:
        command.upgrade(cfg,start)
        with sqlite3.connect(path) as con: con.execute("INSERT INTO telegram_users(user_id,role,approved,blocked,first_seen_at) VALUES ('30','operator',1,0,CURRENT_TIMESTAMP)")
    command.upgrade(cfg,'head')
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0011_telegram_message_alias'
        assert {'membership','source','is_admin'} <= {r[1] for r in con.execute('PRAGMA table_info(telegram_users)')}
        assert 'membership' not in {r[1] for r in con.execute('PRAGMA table_info(admin_sessions)')}
        if start: assert con.execute("SELECT role,approved,is_admin FROM telegram_users WHERE user_id='30'").fetchone()==('operator',1,0)

@pytest.mark.asyncio
async def test_join_leave_promotion_preserves_explicit_acl(db):
    async with db.session() as s:
        acc=TelegramAccount(label='primary');s.add(acc);await s.commit()
    await upsert_and_activate_target(db,account_id=acc.id,chat_id='-100123',title='G',is_forum=True,chat_type='supergroup',bot_status='administrator')
    repo=TelegramUsersRepository(db)
    kwargs=dict(chat_id='-100123',user_id='30',username='silent',display_name='Silent')
    await observe_membership(db,status='member',**kwargs)
    await repo.set_approval(user_id='30',approved=True,role='operator')
    await repo.set_blocked(user_id='30',blocked=True)
    await repo.set_nickname(user_id='30',nickname='Local')
    for status,expected in [('administrator','admin'),('member','member'),('kicked','kicked'),('left','left')]:
        await observe_membership(db,status=status,**kwargs)
        user=await repo.get('30')
        assert user.membership==expected and user.role=='operator'
        assert user.approved and user.blocked and user.nickname=='Local'

@pytest.mark.asyncio
async def test_timeout_and_shutdown_disconnect_active_manual_run(db,tmp_path,monkeypatch):
    client=Client();svc=service(db,tmp_path,client);client.pause=asyncio.Event()
    monkeypatch.setattr('shmax.telegram_members.sync.SYNC_TIMEOUT_SECONDS',0.02)
    assert (await svc.sync('-100123')).status=='timeout' and client.closed
    monkeypatch.setattr('shmax.telegram_members.sync.SYNC_TIMEOUT_SECONDS',180)
    client.requests.clear();client.closed=False
    task=asyncio.create_task(svc.sync('-100123'))
    while not client.requests: await asyncio.sleep(0)
    await svc.close()
    assert task.cancelled() and client.closed

@pytest.mark.asyncio
async def test_total_limit_not_reported_as_complete(db,tmp_path,monkeypatch):
    client=Client();svc=service(db,tmp_path,client)
    monkeypatch.setattr('shmax.telegram_members.sync.MAX_PARTICIPANTS',200)
    assert (await svc.sync('-100123')).status=='participant_limit'
    assert await TelegramUsersRepository(db).list_all()==[]
    client.empty_page=True
    assert (await svc.sync("-100123")).status=="incomplete"


def test_api_secret_never_echoed_and_csrf_required(client,monkeypatch):
    import secrets
    sp=client.app.state.paths.root/'state'/'setup_secret'
    sp.parent.mkdir(parents=True,exist_ok=True);sp.write_text(secrets.token_urlsafe(24))
    token=client.post('/api/setup/token',headers={'X-Shmax-Setup-Secret':sp.read_text()}).json()['token']
    resp=client.post('/api/setup/admin',json={'token':token,'username':'owner','password':'owner-pass-12345'})
    csrf=resp.json()['csrf_token']
    assert client.put('/api/telegram/member-sync',json={'api_id':123,'api_hash':'a'*32}).status_code==403
    client.headers.update({'X-CSRF-Token':csrf})
    assert client.post('/api/telegram/member-sync').json()['status']=='not_configured'
    invalid='SECRET-INVALID-HASH'
    response=client.put('/api/telegram/member-sync',json={'api_id':0,'api_hash':invalid})
    assert response.status_code==400 and invalid not in response.text
    response=client.put('/api/telegram/member-sync',json={'api_id':123,'api_hash':'a'*32})
    assert response.status_code==200 and 'a'*32 not in response.text
    assert response.json()['api_hash_saved']
    assert client.app.state.secrets.get_secret('tg_member_sync_api_hash')=='a'*32
    assert 'a'*32 not in client.get('/api/telegram/member-sync').text
    assert client.put('/api/telegram/member-sync',json={'api_id':124,'api_hash':''}).status_code==200
    assert client.app.state.secrets.get_secret('tg_member_sync_api_hash')=='a'*32
    from shmax.adapters.telegram.base import TelegramBotInfo
    for flag in (None,False,True):
        async def probe(self):
            return TelegramBotInfo(bot_id='1',username='test_bot',can_join_groups=True,
                                   can_read_all_group_messages=flag)
        monkeypatch.setattr('shmax.adapters.telegram.bot_api.TelegramBotApiAdapter.probe',probe)
        response=client.post('/api/telegram/verify',json={'token':'1:test-token'})
        assert response.status_code==200
        assert bool(response.json()['privacy_warning'])==(flag is not True)

def test_production_client_uses_memory_session_and_existing_proxy(db,tmp_path,monkeypatch):
    from telethon.sessions import MemorySession
    captured=[]
    def factory(*args,**kwargs):
        captured.append((args,kwargs));return NS()
    monkeypatch.setattr('telethon.TelegramClient',factory)
    svc=service(db,tmp_path,Client())
    proxy=dict(scheme='socks5',host='127.0.0.1',port=10833,username='user',password='secret')
    svc._make_client({'proxy':proxy})
    args,kwargs=captured[0]
    assert isinstance(args[0],MemorySession) and args[1:]==(123,'a'*32)
    assert kwargs['receive_updates'] is False and kwargs['auto_reconnect'] is False
    assert kwargs['request_retries']==0 and kwargs['flood_sleep_threshold']==0
    assert kwargs['proxy']==dict(proxy_type='socks5',addr='127.0.0.1',port=10833,username='user',password='secret',rdns=True)
    assert not list(tmp_path.rglob('*.session'))
