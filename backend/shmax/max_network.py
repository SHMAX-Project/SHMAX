"""Independent MAX transport, encrypted settings and a dedicated Xray owner.

No environment/global socket monkeypatches, no implicit direct fallback.
The SDK receives the same proxy for authentication, TCP and uploads; SHMAX
media downloads use it as well. Telegram settings are never modified here.
"""
from __future__ import annotations
import asyncio
import json
import os
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from .database.models import Setting
from .xray import state as xstate
from .xray.runtime import XrayRuntimeManager

CONFIG_KEY='max_network'
NODES_KEY='max_xray_nodes'

class PrefixedSecrets:
    def __init__(self,store):self.store=store
    def has_secret(self,key):return self.store.has_secret('max_'+key)
    def get_secret(self,key):return self.store.get_secret('max_'+key)
    def set_secret(self,key,value):return self.store.set_secret('max_'+key,value)
    def delete_secret(self,key):return self.store.delete_secret('max_'+key)


def build_manager():
    tg_port=int(os.environ.get('SHMAX_XRAY_SOCKS_PORT','10808'))
    explicit=os.environ.get('SHMAX_MAX_XRAY_SOCKS_PORT')
    port=int(explicit) if explicit else 10809
    if not explicit and port==tg_port:port+=1
    if not 1<=port<=65535 or port==tg_port:raise ValueError('MAX and Telegram Xray ports must be distinct')
    return XrayRuntimeManager(binary_path=os.environ.get('SHMAX_XRAY_BINARY','/usr/local/bin/xray'),
        socks_port=port,config_dir=Path('/run/shmax/max-xray'))


def proxy_url(config,port):
    mode=config.get('mode','direct')
    if mode=='direct':return None
    if mode=='xray':return f'http://127.0.0.1:{http_port(port)}'
    if mode not in {'socks5','http'}:raise ValueError('unsupported MAX network mode')
    host=config.get('host','').strip()
    number=config.get('port')
    if not host or any(c in host for c in '/@?#\\\r\n\t ') or not isinstance(number,int) or not 1<=number<=65535:
        raise ValueError('invalid proxy host or port')
    if ':' in host and not host.startswith('['):host=f'[{host}]'
    auth=''
    if config.get('username'):
        auth=quote(config['username'],safe='')+':'+quote(config.get('password') or '',safe='')+'@'
    return f'{mode}://{auth}{host}:{number}'


def http_port(socks_port):
    explicit=os.environ.get('SHMAX_MAX_XRAY_HTTP_PORT')
    port=int(explicit) if explicit else 10810
    occupied={socks_port,int(os.environ.get('SHMAX_XRAY_SOCKS_PORT','10808'))}
    if not explicit:
        while port in occupied:port+=1
    if not 1<=port<=65535 or port in occupied:
        raise ValueError('MAX Xray HTTP port must be distinct')
    return port


def with_http(config,socks_port):
    config['inbounds'].append({'tag':'max-http','listen':'127.0.0.1','port':http_port(socks_port),
        'protocol':'http','settings':{}})
    return config


def sdk_proxy(config,socks_port):
    # PyMax uploads use aiohttp's HTTP proxy, while its TCP socket supports
    # CONNECT too. For SOCKS, a dedicated local Xray bridges HTTP to SOCKS.
    if config.get('mode')=='socks5':return f'http://127.0.0.1:{http_port(socks_port)}'
    return proxy_url(config,socks_port)


def socks_bridge(config,socks_port):
    proxy_url(config,socks_port) # Validate before emitting an ephemeral config.
    server={'address':config['host'],'port':config['port']}
    if config.get('username'):server['users']=[{'user':config['username'],'pass':config.get('password') or ''}]
    return with_http({'log':{'loglevel':'none'},'inbounds':[{'listen':'127.0.0.1','port':socks_port,
        'protocol':'socks','settings':{'auth':'noauth','udp':False}}],
        'outbounds':[{'protocol':'socks','settings':{'servers':[server]}}]},socks_port)


async def probe_max(proxy=None,timeout=8):
    """TCP/TLS only: no login, SMS, profile request or test message."""
    writer=None;sock=None
    try:
        async with asyncio.timeout(timeout):
            ctx=ssl.create_default_context()
            from importlib import resources
            ctx.load_verify_locations(str(resources.files('pymax._data')/'rootca_ssl_rsa2022.crt'))
            kwargs={}
            if proxy:
                from python_socks.async_.asyncio import Proxy
                sock=await Proxy.from_url(proxy).connect(dest_host='api2.oneme.ru',dest_port=443)
                kwargs={'sock':sock}
            else:kwargs={'host':'api2.oneme.ru','port':443}
            _,writer=await asyncio.open_connection(**kwargs,ssl=ctx,server_hostname='api2.oneme.ru')
            return {'ok':True,'detail':'Сетевое соединение с MAX доступно; авторизация проверяется отдельно'}
    except Exception:
        return {'ok':False,'detail':'MAX недоступен через выбранное подключение'}
    finally:
        if writer:
            writer.close()
            try:await asyncio.wait_for(writer.wait_closed(),2)
            except Exception:pass
        elif sock:sock.close()


class MaxNetwork:
    def __init__(self,*,runtime,db,secrets,manager):
        self.runtime=runtime;self.db=db;self.secrets=secrets;self.manager=manager
        self.xsecrets=PrefixedSecrets(secrets);self._lock=asyncio.Lock()
        self.last_probe=None

    def config(self):
        if not self.secrets.has_secret(CONFIG_KEY):return {'mode':'direct'}
        value=json.loads(self.secrets.get_secret(CONFIG_KEY))
        proxy_url(value,self.manager.socks_port) # Corrupt config fails closed.
        return value

    async def nodes(self):
        async with self.db.session() as s:row=await s.get(Setting,NODES_KEY)
        return json.loads(row.value) if row else []

    async def save_nodes(self,nodes):
        async with self.db.session() as s:
            value=json.dumps(nodes,ensure_ascii=False)
            await s.execute(insert(Setting).values(key=NODES_KEY,value=value).on_conflict_do_update(index_elements=[Setting.key],set_={'value':value}))
            await s.commit()

    async def status(self):
        config=self.config()
        return {'mode':config.get('mode','direct'),'host':config.get('host'),'port':config.get('port'),
            'username':config.get('username'),'password_saved':bool(config.get('password')),
            'nodes':await self.nodes(),'selected_node_id':(xstate.load_selected(self.xsecrets) or (None,None))[0],
            'xray':self.manager.status().to_dict(),'last_probe':self.last_probe}

    @asynccontextmanager
    async def mutation(self):
        async with self._lock:
            recovery=self.runtime.max_recovery
            lock=recovery._lock if recovery else asyncio.Lock()
            async with lock:
                task=getattr(self.runtime.max_auth,'_auth_task',None)
                if task and not task.done():raise ValueError('Дождитесь завершения текущей авторизации MAX')
                yield

    async def ensure(self):
        config=self.config()
        if config.get('mode') in {'xray','socks5'} and not self.manager.is_running:
            outbound=(socks_bridge(config,self.manager.socks_port) if config['mode']=='socks5' else
                xstate.selected_node_config(self.xsecrets,local_socks_port=self.manager.socks_port))
            if outbound is None:raise ConnectionError('MAX Xray unavailable')
            if config['mode']=='xray':outbound=with_http(outbound,self.manager.socks_port)
            await self.manager.start(outbound)

    async def restore(self):
        # Set the proxy before any restored session may connect. A failed Xray
        # remains selected so the recovery loop cannot accidentally go direct.
        adapter=self.runtime.max_adapter
        try:config=self.config()
        except Exception:
            if hasattr(adapter,'configure_proxy'):adapter.configure_proxy('http://127.0.0.1:0')
            raise
        if hasattr(adapter,'configure_proxy'):adapter.configure_proxy(sdk_proxy(config,self.manager.socks_port))
        await self.ensure()

    async def apply(self,config):
        proxy=sdk_proxy(config,self.manager.socks_port)
        if config.get('mode')=='socks5':await self.manager.restart(socks_bridge(config,self.manager.socks_port))
        await self.runtime.stop_max_listener()
        await asyncio.wait_for(self.runtime.max_adapter.disconnect(),5)
        adapter=self.runtime.max_adapter
        if hasattr(adapter,'configure_proxy'):adapter.configure_proxy(proxy)
        elif self.runtime.runtime_mode.value=='production':raise RuntimeError('MAX adapter cannot apply proxy')
        self.secrets.set_secret(CONFIG_KEY,json.dumps(config))
        if config.get('mode') not in {'xray','socks5'}:await self.manager.stop()
        recovery=self.runtime.max_recovery
        if recovery:recovery.trigger()
        self.runtime.journal.emit('config.changed',component='max')

    async def configure(self,config):
        async with self.mutation():
            # Blank password retains a saved credential only for the same proxy.
            old=self.config()
            if config.get('mode') in {'socks5','http'} and config.get('password') is None:
                same=all(old.get(k)==config.get(k) for k in ('mode','host','port','username'))
                config['password']=old.get('password') if same else None
            if config.get('mode')=='xray' and not xstate.load_selected(self.xsecrets):
                raise ValueError('Сначала загрузите подписку и выберите узел MAX')
            await self.apply(config)
        return await self.status()

    async def select(self,node_id):
        from .xray.subscription import parse_subscription,build_xray_outbound
        async with self.mutation():
            links=xstate.load_subscription(self.xsecrets) or {}
            raw=links.get(node_id)
            if not raw:raise ValueError('Узел MAX не найден в подписке')
            parsed=parse_subscription(raw)
            node=next((n for n in parsed.nodes if n.node_id==node_id and n.supported),None)
            if node is None:raise ValueError('Узел MAX не поддерживается')
            old=self.config()
            previous=(socks_bridge(old,self.manager.socks_port) if old.get('mode')=='socks5' else
                xstate.selected_node_config(self.xsecrets,local_socks_port=self.manager.socks_port) if old.get('mode')=='xray' else None)
            if previous and old.get('mode')=='xray':previous=with_http(previous,self.manager.socks_port)
            try:
                await self.manager.restart(with_http(build_xray_outbound(node,raw,local_socks_port=self.manager.socks_port),self.manager.socks_port))
                result=await probe_max(f'http://127.0.0.1:{http_port(self.manager.socks_port)}')
                self.last_probe=result
                if not result['ok']:raise ConnectionError('MAX node unreachable')
            except Exception:
                if previous:await self.manager.restart(previous)
                else:await self.manager.stop()
                raise ValueError('Новый узел MAX недоступен; прежние настройки сохранены') from None
            xstate.store_selection(self.xsecrets,node_id,raw)
            await self.apply({'mode':'xray'})
        return await self.status()
