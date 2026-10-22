"""Admin-only MAX network endpoints. Secrets never appear in responses."""
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,Field
from typing import Literal
from ..max_network import probe_max,sdk_proxy
from ..xray import state as xstate
from ..xray.subscription import parse_subscription

class NetworkConfig(BaseModel):
    mode:Literal['direct','socks5','http','xray']='direct'
    host:str|None=Field(default=None,max_length=253)
    port:int|None=Field(default=None,ge=1,le=65535)
    username:str|None=Field(default=None,max_length=256)
    password:str|None=Field(default=None,max_length=1024)


def install_routes(app,*,require_group_admin,require_csrf):
    deps=[Depends(require_group_admin),Depends(require_csrf)]
    def network():return app.state.runtime.max_network

    @app.get('/api/max/network',dependencies=deps)
    async def status():return await network().status()

    @app.put('/api/max/network',dependencies=deps)
    async def configure(request:Request):
        try:config=NetworkConfig.model_validate(await request.json()).model_dump()
        except Exception:raise HTTPException(400,'Проверьте параметры подключения MAX') from None
        try:return await network().configure(config)
        except ValueError:raise HTTPException(400,'Проверьте прокси или выбранный узел MAX; текущая авторизация должна быть завершена') from None
        except Exception:raise HTTPException(502,'Не удалось применить сеть MAX') from None

    @app.post('/api/max/network/probe',dependencies=deps)
    async def probe():
        net=network()
        async with net.mutation():
            try:
                await net.ensure()
                result=await probe_max(sdk_proxy(net.config(),net.manager.socks_port))
            except Exception:result={'ok':False,'detail':'Прокси MAX недоступен'}
            net.last_probe=result
            return result

    @app.post('/api/max/xray/subscription',dependencies=deps)
    async def subscription(request:Request):
        try:
            body=await request.json();url=body['url'].strip()
            if not url.lower().startswith(('https://','http://')):raise ValueError()
        except Exception:raise HTTPException(400,'Укажите HTTP(S) URL подписки MAX') from None
        net=network()
        async with net.mutation():
            try:parsed=parse_subscription(await xstate.fetch_subscription(url))
            except Exception:raise HTTPException(502,'Не удалось загрузить или разобрать подписку MAX') from None
            if not parsed.nodes:raise HTTPException(400,'Подписка MAX пуста')
            xstate.store_subscription(net.xsecrets,url,parsed.raw_links)
            await net.save_nodes([n.to_dict() for n in parsed.nodes])
        return await net.status()

    @app.post('/api/max/xray/select',dependencies=deps)
    async def select(request:Request):
        try:
            body=await request.json();node_id=body['node_id']
            if not isinstance(node_id,str) or not node_id or len(node_id)>128:raise ValueError()
        except Exception:raise HTTPException(400,'Выберите узел MAX') from None
        try:return await network().select(node_id)
        except ValueError:raise HTTPException(400,'Узел MAX не применён; предыдущие настройки сохранены') from None
        except Exception:raise HTTPException(502,'Не удалось запустить Xray MAX') from None
