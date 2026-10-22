"""Group settings are scoped, admin controlled and reject occupied topics."""
from types import SimpleNamespace as NS
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from tests.test_multi_group import rt, seed
from shmax.api.groups import install_routes
from shmax.database.models import TelegramGroupAccess, TelegramTarget, Route

async def client_for(rt, *, admin=True):
    app=FastAPI()
    app.state.runtime=NS(journal=rt.journal,telegram_adapter=rt._tg)
    async def require_admin():
        if not admin:raise HTTPException(403,'admin required')
    async def csrf():return None
    install_routes(app,db=rt.db,require_group_admin=require_admin,require_csrf=csrf)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')

@pytest.mark.asyncio
async def test_group_grant_changes_only_selected_group(rt):
    await seed(rt)
    async with await client_for(rt) as c:
        response=await c.put('/api/telegram/groups/2/users/alice',json={'approved':True,'role':'operator'})
        assert response.status_code==200
        response=await c.put('/api/telegram/groups/1/users/alice',json={'approved':False,'role':'read_only'})
        assert response.status_code==200
    async with rt.db.session() as s:
        grants=(await s.scalars(select(TelegramGroupAccess).where(TelegramGroupAccess.user_id=='alice').order_by(TelegramGroupAccess.target_id))).all()
        assert [(g.target_id,g.approved) for g in grants]==[(1,False),(2,True)]

@pytest.mark.asyncio
async def test_group_mutations_require_admin(rt):
    await seed(rt)
    async with await client_for(rt,admin=False) as c:
        assert (await c.patch('/api/telegram/groups/1',json={'is_admin_hub':True})).status_code==403
        assert (await c.put('/api/telegram/groups/1/users/alice',json={'approved':True,'role':'owner'})).status_code==403
        assert (await c.post('/api/telegram/groups/1/routes',json={'max_chat_id':'secret'})).status_code==403

@pytest.mark.asyncio
async def test_route_edit_is_group_scoped_and_rejects_occupied_topic(rt):
    await seed(rt)
    async with await client_for(rt) as c:
        assert (await c.patch('/api/telegram/groups/2/routes/1',json={'thread_id':'23'})).status_code==404
        response=await c.post('/api/telegram/groups/1/routes',json={'max_chat_id':'another','thread_id':'11'})
        assert response.status_code==409 and rt._tg.topics==[]
        response=await c.patch('/api/telegram/groups/1/routes/1',json={'thread_id':'13','title':'Новая тема'})
        assert response.status_code==200
    async with rt.db.session() as s:
        row=await s.get(Route,1)
        assert row.telegram_thread_id=='13' and row.label=='Новая тема'

@pytest.mark.asyncio
async def test_hub_settings_are_independent_per_group(rt):
    await seed(rt)
    async with await client_for(rt) as c:
        assert (await c.patch('/api/telegram/groups/1',json={'is_admin_hub':True,'auto_add_chats':True,'show_sender':False})).status_code==200
        assert (await c.patch('/api/telegram/groups/2',json={'auto_add_chats':True})).status_code==400
    async with rt.db.session() as s:
        first=await s.get(TelegramTarget,1);other=await s.get(TelegramTarget,2)
        assert first.is_admin_hub and not first.show_sender
        assert not other.is_admin_hub and other.show_sender
