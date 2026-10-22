import json
from types import SimpleNamespace as NS
import pytest
from tests.test_media_retry_lifetime import rt
from shmax.max_network import MaxNetwork,PrefixedSecrets,proxy_url,sdk_proxy,with_http
from shmax.xray import state as xs
from shmax.xray.subscription import parse_subscription

class Manager:
    socks_port=10809
    def __init__(self):self.is_running=False;self.configs=[]
    async def start(self,config):self.is_running=True;self.configs.append(config);return self.status()
    async def restart(self,config):return await self.start(config)
    async def stop(self):self.is_running=False
    def status(self):return NS(to_dict=lambda:{'state':'running' if self.is_running else 'stopped'})

class Adapter:
    def __init__(self):self.proxy=None;self.disconnects=0
    def configure_proxy(self,value):self.proxy=value
    async def disconnect(self):self.disconnects+=1

async def network(rt):
    rt._max=Adapter();manager=Manager()
    return MaxNetwork(runtime=rt,db=rt.db,secrets=rt.secrets,manager=manager)

@pytest.mark.asyncio
async def test_max_proxy_is_encrypted_redacted_and_independent_of_telegram(rt):
    net=await network(rt)
    rt.secrets.set_secret('xray_selected_node_id','telegram-original')
    await net.configure({'mode':'http','host':'proxy.example','port':3128,'username':'user','password':'hidden-secret'})
    assert rt._max.proxy=='http://user:hidden-secret@proxy.example:3128'
    assert rt.secrets.get_secret('xray_selected_node_id')=='telegram-original'
    state=await net.status()
    assert state['password_saved'] and 'hidden-secret' not in json.dumps(state)
    assert 'hidden-secret' not in (rt.layout.root/'secrets'/'store.bin').read_bytes().decode(errors='ignore')
    await net.configure({'mode':'direct'})
    assert rt._max.proxy is None and rt._max.disconnects==2

@pytest.mark.asyncio
async def test_socks_proxy_uses_local_http_bridge_for_sdk_uploads(rt):
    net=await network(rt)
    await net.configure({'mode':'socks5','host':'proxy.example','port':1080,'username':'u','password':'p'})
    assert rt._max.proxy=='http://127.0.0.1:10810'
    cfg=net.manager.configs[-1]
    assert all(i['listen']=='127.0.0.1' for i in cfg['inbounds'])
    assert {i['protocol'] for i in cfg['inbounds']}=={'socks','http'}
    assert cfg['outbounds'][0]['settings']['servers'][0]['users']==[{'user':'u','pass':'p'}]
    net.manager.is_running=False
    await net.restore()
    assert net.manager.is_running and rt._max.proxy=='http://127.0.0.1:10810'

@pytest.mark.asyncio
async def test_selected_xray_restores_from_separate_encrypted_cache(rt):
    net=await network(rt)
    raw='vless://00000000-0000-0000-0000-000000000001@example.com:443?security=tls&type=tcp#MAX'
    parsed=parse_subscription(raw);identity=parsed.nodes[0].node_id
    xs.store_selection(net.xsecrets,identity,raw)
    rt.secrets.set_secret('max_network',json.dumps({'mode':'xray'}))
    await net.restore()
    assert rt._max.proxy=='http://127.0.0.1:10810' and net.manager.is_running
    assert not rt.secrets.has_secret('xray_selected_node_id')
    assert rt.secrets.has_secret('max_xray_selected_node_id')

@pytest.mark.asyncio
async def test_unreachable_new_node_restores_old_selection_without_direct_fallback(rt,monkeypatch):
    net=await network(rt)
    old='vless://00000000-0000-0000-0000-000000000001@old.example:443?security=tls&type=tcp#OLD'
    new='vless://00000000-0000-0000-0000-000000000002@new.example:443?security=tls&type=tcp#NEW'
    old_id=parse_subscription(old).nodes[0].node_id;new_id=parse_subscription(new).nodes[0].node_id
    xs.store_subscription(net.xsecrets,'https://subscription.example',{old_id:old,new_id:new})
    xs.store_selection(net.xsecrets,old_id,old)
    rt.secrets.set_secret('max_network',json.dumps({'mode':'xray'}));await net.restore()
    async def unavailable(*args,**kwargs):return {'ok':False,'detail':'unavailable'}
    monkeypatch.setattr('shmax.max_network.probe_max',unavailable)
    with pytest.raises(ValueError):await net.select(new_id)
    assert xs.load_selected(net.xsecrets)[0]==old_id
    assert net.config()['mode']=='xray' and rt._max.proxy=='http://127.0.0.1:10810'
    assert 'old.example' in json.dumps(net.manager.configs[-1])

@pytest.mark.asyncio
async def test_proxy_change_refuses_to_interrupt_ongoing_auth(rt):
    import asyncio
    net=await network(rt)
    rt.max_auth._auth_task=asyncio.create_task(asyncio.sleep(10))
    try:
        with pytest.raises(ValueError):await net.configure({'mode':'direct'})
        assert rt._max.disconnects==0
    finally:
        rt.max_auth._auth_task.cancel()
        await asyncio.gather(rt.max_auth._auth_task,return_exceptions=True)


def test_new_max_defaults_preserve_old_custom_telegram_ports(monkeypatch):
    from shmax.max_network import build_manager,http_port
    monkeypatch.setenv('SHMAX_XRAY_SOCKS_PORT','10809')
    monkeypatch.delenv('SHMAX_MAX_XRAY_SOCKS_PORT',raising=False)
    monkeypatch.delenv('SHMAX_MAX_XRAY_HTTP_PORT',raising=False)
    manager=build_manager()
    assert manager.socks_port==10810 and http_port(manager.socks_port)==10811
    monkeypatch.setenv('SHMAX_MAX_XRAY_SOCKS_PORT','10809')
    with pytest.raises(ValueError):build_manager()


def test_sdk_proxy_log_redacts_password_even_at_debug(caplog):
    import logging
    from shmax.adapters.max.pymax_adapter import _ProxyLogFilter
    logger=logging.getLogger('pymax.transport.tcp')
    with caplog.at_level(logging.DEBUG,logger=logger.name):
        logger.debug('tcp connecting via proxy %s','http://user:private-test-pass@example.com:3128')
    assert 'private-test-pass' not in caplog.text and 'MAX proxy transport event' in caplog.text

@pytest.mark.asyncio
async def test_actual_sdk_receives_same_proxy_for_tcp_and_uploads_and_retains_it_during_auth(tmp_path,monkeypatch):
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
    from shmax.adapters.max.config import PyMaxAdapterConfig
    proxy='http://127.0.0.1:10810'
    adapter=PyMaxMaxAdapter(PyMaxAdapterConfig(phone=None,session_dir=tmp_path,device_type='ios',proxy_url=proxy))
    client=adapter._build_client(saved_only=True)
    await client._ensure_runtime() # Build SDK runtime locally; no connect/login.
    assert client.extra_config.proxy==proxy
    assert client._connection.transport._proxy==proxy
    assert client._app.api.uploads.app.config.proxy==proxy
    identities=[]
    async def actual_sender(identity):
        identities.append(identity)
        return NS(names=[NS(name='Ирина')])
    monkeypatch.setattr(client._app.api.users,'get_user',actual_sender)
    assert await adapter.resolve_sender_name('123')=='Ирина'
    assert identities==[123]
    async def no_network():pass
    monkeypatch.setattr(adapter,'connect',no_network)
    await adapter.prepare_sms(phone='+79990000000')
    assert adapter._config.proxy_url==proxy and adapter._config.device_type=='ios'
    await adapter.disconnect()
