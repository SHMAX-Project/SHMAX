"""Saved SOCKS startup recovery and exactly one polling consumer; no live credentials."""
import asyncio
from types import SimpleNamespace as NS
import pytest
import pytest_asyncio
from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
from shmax.adapters.telegram.config import ProxyConfig, TransportConfig, TransportMode as Mode
from shmax.adapters.telegram.manager import TelegramTransportManager as Manager, TransportSelectionError
from shmax.config import RuntimeMode
from shmax.core.paths import PathLayout
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.security import SecretStore
from shmax.startup_restore import persist_telegram_transport, restore_telegram
from shmax.telegram_recovery import TelegramRecovery, failure_kind

class Session:
    def __init__(self,s):self.closed=False;s.sessions.append(self)
    async def close(self):self.closed=True
class Bot:
    def __init__(self,s):self.s=s;self.session=Session(s)
    async def get_me(self):
        self.s.calls+=1
        if self.s.errors:
            e=self.s.errors.pop(0)
            if e:raise e
        return NS(id=424242,username='fixture_bot')
class Dispatcher:
    def __init__(self,s):self.s=s;self.stop=asyncio.Event()
    def message(self):return lambda f:f
    def chat_member(self):return lambda f:f
    async def start_polling(self,*a,**kw):
        self.s.pollers+=1;self.s.maximum=max(self.s.maximum,self.s.pollers)
        try:await self.stop.wait()
        finally:self.s.pollers-=1
    async def stop_polling(self):self.stop.set()
async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():await asyncio.sleep(.005)
@pytest_asyncio.fixture
async def recovered(tmp_path,monkeypatch):
    settings=DatabaseSettings(data_dir=tmp_path);run_migrations_sync(settings.url)
    db=Database(settings);secrets=SecretStore(tmp_path/'secrets');secrets.set_secret('telegram_bot_token','123:fixture-only')
    await persist_telegram_transport(db,secrets,mode=Mode.BOT_API_SOCKS5,proxy=ProxyConfig('socks5','127.0.0.1',10808),api_id=None,api_hash=None,mtproxy=None)
    rt=AppRuntime(database=db,secret_store=secrets,layout=PathLayout(tmp_path),config=RuntimeConfig(start_workers=False),runtime_mode=RuntimeMode.TEST)
    await rt.start()
    s=NS(ready=True,calls=0,errors=[],pollers=0,maximum=0,sessions=[],configs=[])
    def factory(cfg):
        s.configs.append(cfg)
        return TelegramBotApiAdapter(cfg,bot_factory=lambda:Bot(s),dispatcher_factory=lambda:Dispatcher(s))
    monkeypatch.setattr('shmax.adapters.telegram.manager.TelegramBotApiAdapter',factory)
    async def dependency(proxy,**kw):
        assert proxy==ProxyConfig('socks5','127.0.0.1',10808)
        if not s.ready:raise ConnectionRefusedError('fixture unavailable')
    monkeypatch.setattr('shmax.telegram_recovery.proxy_accepting',dependency)
    async def restore():return await restore_telegram(rt,db=db,secret_store=secrets,session_dir=str(tmp_path/'sessions'))
    rec=TelegramRecovery(rt,restore=restore,initial_delay=.02,retry_cap=.04,health_interval=.03,permanent_delay=.25,jitter=lambda d:d);rt.telegram_recovery=rec
    yield rt,rec,s
    await rt.stop();await db.dispose()
    assert s.pollers==0
    assert all(x.closed for x in s.sessions)
@pytest.mark.asyncio
async def test_delayed_proxy_recovers_without_save(recovered):
    rt,rec,s=recovered;s.ready=False;rec.start()
    await eventually(lambda:rec.status['state']=='DEGRADED');assert not rt.telegram_adapter.is_real()
    maximum=rt.max_adapter;s.ready=True;rec.trigger()
    await eventually(lambda:rec.status['state']=='HEALTHY')
    assert rt.max_adapter is maximum and s.pollers==s.maximum==1
    assert all(c.mode==Mode.BOT_API_SOCKS5 and c.proxy.scheme=='socks5' for c in s.configs)
@pytest.mark.asyncio
async def test_first_getme_timeout_retries(recovered):
    rt,rec,s=recovered;s.errors=[TimeoutError()];rec.start()
    await eventually(lambda:rec.status['state']=='HEALTHY')
    assert rec.status['attempts']>=2 and s.maximum==1
    assert all(x.closed for x in s.sessions[:-1])
@pytest.mark.asyncio
async def test_concurrent_triggers_one_install_one_consumer(recovered):
    rt,rec,s=recovered
    await asyncio.gather(*(rec.attempt() for _ in range(10)))
    assert len(s.configs)==1 and s.pollers==s.maximum==1
@pytest.mark.asyncio
async def test_dead_polling_task_recovered(recovered):
    rt,rec,s=recovered;rec.start();await eventually(lambda:rec.status['state']=='HEALTHY')
    old=rt.telegram_adapter;old._polling_task.cancel();await asyncio.gather(old._polling_task,return_exceptions=True);rec.trigger()
    await eventually(lambda:rt.telegram_adapter is not old and s.pollers==1)
    assert s.maximum==1 and old._bot is None
@pytest.mark.asyncio
async def test_health_probe_failure_keeps_existing_consumer(recovered):
    rt,rec,s=recovered;await rec.attempt();old=rt.telegram_adapter;s.errors=[TimeoutError()]
    assert (await rec.attempt())['state']=='DEGRADED'
    assert rt.telegram_adapter is old and s.pollers==1
    assert (await rec.attempt())['restored'] and rt.telegram_adapter is old
@pytest.mark.asyncio
async def test_max_connection_is_untouched(recovered):
    rt,rec,s=recovered;await rt.max_adapter.connect();maximum=rt.max_adapter;before=await maximum.health()
    await rec.attempt();assert rt.max_adapter is maximum and await maximum.health()==before
@pytest.mark.asyncio
async def test_invalid_credentials_cooldown_and_redaction(recovered):
    class TelegramUnauthorizedError(Exception):pass
    rt,rec,s=recovered;s.errors=[TelegramUnauthorizedError('fixture secret must not appear')]*10;rec.start()
    await eventually(lambda:rec.status['state']=='AUTH_REQUIRED');await asyncio.sleep(.07)
    assert s.calls==1 and 'fixture secret' not in str(rec.status) and s.pollers==0
    assert rt.secrets.has_secret('telegram_bot_token')
@pytest.mark.asyncio
async def test_cancel_hanging_probe_closes_session(recovered,monkeypatch):
    rt,rec,s=recovered;entered=asyncio.Event()
    async def hanging(self):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(Bot,'get_me',hanging);rec.start();await entered.wait();await asyncio.wait_for(rt.stop(),1)
    assert rec.task is None and all(x.closed for x in s.sessions)
@pytest.mark.asyncio
async def test_restart_restores_saved_token_and_proxy(recovered):
    rt,rec,s=recovered;await rec.attempt();first=rt.telegram_adapter;await first.disconnect();rt._telegram_failure=(False,'transport_failure')
    assert (await rec.attempt())['restored']
    assert rt.telegram_adapter is not first and rt.telegram_manager.config.bot_token=='123:fixture-only'
    assert rt.telegram_manager.config.proxy==ProxyConfig('socks5','127.0.0.1',10808) and s.maximum==1
@pytest.mark.asyncio
async def test_explicit_missing_proxy_never_falls_back():
    m=Manager(TransportConfig(mode=Mode.BOT_API_SOCKS5,bot_token='fixture'))
    with pytest.raises(TransportSelectionError,match='explicit transport bot_api_socks5') as caught:await m.select_probed()
    assert caught.value.permanent and 'AUTO' not in str(caught.value)
def test_auto_skips_absent_and_wrong_scheme_proxy():
    assert Manager(TransportConfig(mode=Mode.AUTO,bot_token='fixture')).auto_iter()==[Mode.BOT_API_DIRECT]
    m=Manager(TransportConfig(mode=Mode.AUTO,bot_token='fixture',proxy=ProxyConfig('http','127.0.0.1',8080)))
    assert m.auto_iter()==[Mode.BOT_API_DIRECT,Mode.BOT_API_HTTP]
def test_proxy_credentials_classification_is_sanitized():
    class ProxyError(Exception):pass
    assert failure_kind(ProxyError('authentication rejected fixture-password'))==(True,'proxy_credentials_rejected')

@pytest.mark.asyncio
async def test_real_web_start_delayed_dependency_and_second_boot(recovered,tmp_path):
    import time
    from fastapi.testclient import TestClient
    from shmax.api.app import create_app
    from shmax.config import Settings
    rt,rec,s=recovered
    s.ready=False
    def boots():
        settings=Settings(host="127.0.0.1",port=0,data_dir=tmp_path,runtime_mode=RuntimeMode.TEST,log_level='ERROR')
        with TestClient(create_app(settings)) as client:
            assert client.get('/health').status_code==200
            actual=client.app.state.runtime
            assert client.app.state.restore_report['telegram']['state']=='DEGRADED'
            original_max=actual.max_adapter
            s.ready=True;actual.telegram_recovery.trigger()
            limit=time.monotonic()+4
            while actual.telegram_recovery.status['state']!='HEALTHY' and time.monotonic()<limit:time.sleep(.01)
            assert actual.telegram_recovery.status['state']=='HEALTHY'
            assert actual.max_adapter is original_max
        with TestClient(create_app(settings)) as client:
            assert client.get('/health').status_code==200
            actual=client.app.state.runtime
            assert client.app.state.restore_report['telegram']['restored']
            assert actual.telegram_manager.config.mode==Mode.BOT_API_SOCKS5
        assert s.pollers==0 and s.maximum==1
    await asyncio.to_thread(boots)

@pytest.mark.asyncio
@pytest.mark.parametrize('cycle',range(3))
async def test_repeated_lifecycle_cancels_owned_tasks(recovered,cycle):
    rt,rec,s=recovered;rec.start();await eventually(lambda:rec.status['state']=='HEALTHY')
    task=rec.task;await rt.stop()
    assert task.done() and rec.task is None and s.pollers==0
    assert all(x.closed for x in s.sessions)

@pytest.mark.asyncio
async def test_xray_running_socks_and_telegram_health_are_distinct(tmp_path):
    from shmax.xray.runtime import XrayRuntimeManager
    from shmax.api.app import _xray_telegram_reachable
    import time
    async def accept(reader,writer):
        writer.close();await writer.wait_closed()
    server=await asyncio.start_server(accept,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    manager=XrayRuntimeManager(binary_path='/not/installed',socks_port=port)
    manager._process=NS(returncode=None,pid=4242)
    try:
        status=await manager.health()
        assert status.state=='running' and manager.is_running
        adapter=NS(_config=TransportConfig(mode=Mode.BOT_API_SOCKS5,proxy=ProxyConfig('socks5','127.0.0.1',port)))
        runtime=NS(telegram_adapter=adapter,_integration_probes={'telegram':{'adapter':adapter,'at':time.monotonic(),'health':{'connected':False,'authenticated':False}}})
        assert _xray_telegram_reachable(runtime,manager) is False
        runtime._integration_probes['telegram']['health']={'connected':True,'authenticated':True}
        assert _xray_telegram_reachable(runtime,manager) is True
    finally:
        manager._process=None;server.close();await server.wait_closed()

@pytest.mark.asyncio
async def test_managed_xray_not_restarted_when_listening_and_recovers_dead_readiness(recovered,monkeypatch):
    from shmax.startup_restore import restore_integrations
    rt,rec,s=recovered
    class Managed:
        is_running=True
        socks_port=10808
        listening=True
        restarts=0
        async def health(self):return NS(state='running' if self.listening else 'failed')
        async def restart(self,config):self.restarts+=1;self.listening=True
    manager=Managed()
    rt.secrets.set_secret('xray_managed','1')
    monkeypatch.setattr('shmax.xray.state.selected_node_config',lambda *a,**kw:{'fixture':True})
    rt.telegram_recovery=None
    await restore_integrations(rt,db=rt.db,secret_store=rt.secrets,session_dir=str(rt.layout.root/'sessions'),xray_manager=manager)
    recovery=rt.telegram_recovery
    await recovery.close()
    await recovery.prepare();await recovery.prepare()
    assert manager.restarts==0
    manager.listening=False
    await recovery.prepare();await recovery.prepare()
    assert manager.restarts==0
    await recovery.prepare()
    assert manager.restarts==1 and manager.listening
    assert s.maximum==1

@pytest.mark.asyncio
async def test_invalid_saved_proxy_is_permanent_without_network_attempt(recovered,monkeypatch):
    rt,rec,s=recovered
    await persist_telegram_transport(rt.db,rt.secrets,mode=Mode.BOT_API_SOCKS5,proxy=ProxyConfig('socks5','',0),api_id=None,api_hash=None,mtproxy=None)
    async def tripwire(*a,**kw):raise AssertionError('invalid proxy must not be probed')
    monkeypatch.setattr('shmax.telegram_recovery.proxy_accepting',tripwire)
    result=await restore_telegram(rt,db=rt.db,secret_store=rt.secrets,session_dir=str(rt.layout.root/'sessions'))
    assert result['state']=='AUTH_REQUIRED' and result['detail']=='invalid_configuration'
    assert s.calls==0
