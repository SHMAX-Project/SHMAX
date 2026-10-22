"""Owner/admin-controlled group settings, routes and scoped Telegram grants."""
from typing import Literal
from fastapi import Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..database.models import TelegramTarget, TelegramUser, TelegramGroupAccess, Route, MaxAccount
from ..routing.repository import RoutesRepository
from ..group_access import target_ready
from ..users.repository import TelegramUsersRepository


class GroupSettings(BaseModel):
    is_active: bool | None = None
    is_admin_hub: bool | None = None
    auto_add_chats: bool | None = None
    show_source_chat: bool | None = None
    show_sender: bool | None = None


class GroupGrant(BaseModel):
    approved: bool = False
    blocked: bool = False
    role: Literal['read_only','operator','admin','owner'] = 'read_only'


class GroupRoute(BaseModel):
    max_chat_id: str = Field(min_length=1,max_length=64)
    title: str = Field(default='',max_length=128)
    thread_id: str | None = Field(default=None,max_length=20,pattern=r'^[1-9][0-9]*$')


class RouteSettings(BaseModel):
    title: str | None = Field(default=None,max_length=128)
    thread_id: str | None = Field(default=None,max_length=20,pattern=r'^[1-9][0-9]*$')
    enabled: bool | None = None


def install_routes(app, *, db, require_group_admin, require_csrf):
    deps=[Depends(require_group_admin),Depends(require_csrf)]

    async def target_by_id(identity):
        async with db.session() as s:target=await s.get(TelegramTarget,identity)
        if not target:raise HTTPException(404,'Группа не найдена')
        return target

    @app.get('/api/telegram/groups',dependencies=deps)
    async def groups():
        async with db.session() as s:
            rows=(await s.scalars(select(TelegramTarget).order_by(TelegramTarget.id))).all()
        return {'groups':[dict(id=r.id,account_id=r.account_id,chat_id=r.chat_id,title=r.title,
            is_forum=r.is_forum,is_active=r.is_active,ready=target_ready(r),
            is_admin_hub=r.is_admin_hub,auto_add_chats=r.auto_add_chats,
            show_source_chat=r.show_source_chat,show_sender=r.show_sender) for r in rows]}

    @app.patch('/api/telegram/groups/{target_id}',dependencies=deps)
    async def settings(target_id:int,req:GroupSettings):
        async with db.session() as s:
            target=await s.get(TelegramTarget,target_id)
            if not target:raise HTTPException(404,'Группа не найдена')
            values=req.model_dump(exclude_none=True)
            if values.get('is_admin_hub',target.is_admin_hub) and not target.is_forum:
                raise HTTPException(400,'Админский обзор требует форум-группу с темами')
            if values.get('is_active') and not target.validated_at:raise HTTPException(400,'Сначала подтвердите группу через /shmax_connect')
            if values.get('auto_add_chats',target.auto_add_chats) and not values.get('is_admin_hub',target.is_admin_hub):
                raise HTTPException(400,'Автодобавление доступно только админскому обзору')
            for key,value in values.items():setattr(target,key,value)
            await s.commit()
        app.state.runtime.journal.emit('config.changed',component='bridge')
        return {'ok':True}

    @app.get('/api/telegram/groups/{target_id}/users',dependencies=deps)
    async def users(target_id:int):
        await target_by_id(target_id)
        async with db.session() as s:
            rows=(await s.execute(select(TelegramUser,TelegramGroupAccess).outerjoin(TelegramGroupAccess,
                (TelegramGroupAccess.user_id==TelegramUser.user_id)&(TelegramGroupAccess.target_id==target_id)).order_by(TelegramUser.first_seen_at.desc()))).all()
        return {'users':[dict(user_id=u.user_id,display_name=u.display_name,username=u.username,nickname=u.nickname,
            role=a.role if a else 'read_only',approved=bool(a and a.approved),blocked=bool(u.blocked or (a and a.blocked)),
            globally_blocked=u.blocked,membership=a.membership if a else None,is_admin=bool(a and a.is_admin),
            source=a.source if a else None,in_group=a is not None) for u,a in rows]}

    @app.put('/api/telegram/groups/{target_id}/users/{user_id}',dependencies=deps)
    async def grant(target_id:int,user_id:str,req:GroupGrant):
        await target_by_id(target_id)
        async with db.session() as s:
            user=await s.scalar(select(TelegramUser).where(TelegramUser.user_id==user_id))
            if not user:raise HTTPException(404,'Пользователь должен сначала написать боту /start')
            row=await s.scalar(select(TelegramGroupAccess).where(TelegramGroupAccess.target_id==target_id,TelegramGroupAccess.user_id==user_id))
            if not row:row=TelegramGroupAccess(target_id=target_id,user_id=user_id);s.add(row)
            row.approved=req.approved;row.blocked=req.blocked;row.role=req.role
            await s.commit()
        app.state.runtime.journal.emit('admin.action',component='bridge')
        return {'ok':True}

    @app.post('/api/telegram/groups/{target_id}/routes',dependencies=deps)
    async def add_route(target_id:int,req:GroupRoute):
        target=await target_by_id(target_id)
        if not target_ready(target):raise HTTPException(400,'Группа не подключена или не проверена')
        async with db.session() as s:
            existing=await RoutesRepository(s).find_by_max(max_account_id=1,max_chat_id=req.max_chat_id,telegram_chat_id=target.chat_id)
            if existing:raise HTTPException(409,'Этот чат MAX уже привязан к группе')
            if not target.is_forum and await s.scalar(select(Route.id).where(Route.telegram_chat_id==target.chat_id,Route.enabled.is_(True))):
                raise HTTPException(409,'Для нескольких чатов MAX включите темы в Telegram-группе')
        thread=req.thread_id
        if thread:
            async with db.session() as s:
                occupied=await RoutesRepository(s).find_by_telegram(telegram_account_id=target.account_id,
                    telegram_chat_id=target.chat_id,telegram_thread_id=thread)
                if occupied:raise HTTPException(409,'Эта тема уже привязана к другому чату MAX')
        if target.is_forum and not thread:
            try:thread=await app.state.runtime.telegram_adapter.create_topic(chat_id=target.chat_id,title=req.title or req.max_chat_id)
            except Exception:raise HTTPException(502,'Telegram не создал тему. Привязка не сохранена') from None
            if not thread:raise HTTPException(502,'Telegram не вернул тему')
        if not target.is_forum and thread:raise HTTPException(400,'Обычная группа не поддерживает темы')
        async with db.session() as s:
            account=await s.get(MaxAccount,1)
            if not account:s.add(MaxAccount(id=1,label='primary'));await s.flush()
            try:
                record=await RoutesRepository(s).upsert(max_account_id=1,max_chat_id=req.max_chat_id,telegram_account_id=target.account_id,
                    telegram_chat_id=target.chat_id,telegram_thread_id=str(thread) if thread else None,label=req.title or req.max_chat_id)
            except IntegrityError:raise HTTPException(409,'Назначение уже занято другим маршрутом') from None
        return {'id':record.id,'telegram_chat_id':target.chat_id,'telegram_thread_id':record.telegram_thread_id}

    @app.patch('/api/telegram/groups/{target_id}/routes/{route_id}',dependencies=deps)
    async def edit_route(target_id:int,route_id:int,req:RouteSettings):
        target=await target_by_id(target_id)
        if not target_ready(target):raise HTTPException(400,'Группа не подключена или не проверена')
        async with db.session() as s:
            row=await s.get(Route,route_id)
            if not row or row.telegram_account_id!=target.account_id or row.telegram_chat_id!=target.chat_id:
                raise HTTPException(404,'Привязка не найдена')
            values=req.model_dump(exclude_unset=True)
            if 'thread_id' in values:
                thread=values['thread_id'] or None
                if target.is_forum and not thread:raise HTTPException(400,'Укажите ID темы форум-группы')
                if not target.is_forum and thread:raise HTTPException(400,'Обычная группа не поддерживает темы')
                occupied=await RoutesRepository(s).find_by_telegram(telegram_account_id=target.account_id,
                    telegram_chat_id=target.chat_id,telegram_thread_id=thread)
                if occupied and occupied.id!=row.id:raise HTTPException(409,'Эта тема уже привязана к другому чату MAX')
                row.telegram_thread_id=thread
            if values.get('title') is not None:row.label=values['title']
            if values.get('enabled') is not None:row.enabled=values['enabled']
            try:await s.commit()
            except IntegrityError:raise HTTPException(409,'Назначение уже занято другим маршрутом') from None
        app.state.runtime.journal.emit('config.changed',component='bridge')
        return {'ok':True}

    @app.delete('/api/telegram/groups/{target_id}/routes/{route_id}',dependencies=deps)
    async def unlink(target_id:int,route_id:int):
        target=await target_by_id(target_id)
        async with db.session() as s:
            row=await s.get(Route,route_id)
            if not row or row.telegram_chat_id!=target.chat_id:raise HTTPException(404,'Привязка не найдена')
            # Preserve mappings and the historical route. Delayed outbox rows
            # revalidate this flag and cannot leak after revocation.
            row.enabled=False;await s.commit()
        return {'ok':True}
