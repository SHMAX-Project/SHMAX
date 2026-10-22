"""Isolated group routing and a durable, bidirectional administrator overview.

Each destination owns an independent outbox row, mapping and retry state.
A Telegram mirror depends on a successfully delivered Telegram→MAX row.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import shutil
import tempfile
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from sqlalchemy import select
from .database.models import TelegramTarget, Route, DeliveryQueueItem
from .routing.repository import RoutesRepository
from .bridge.engine import BridgeConfig, BridgeEngine
from .bridge.message_map import MessageMapRepository, outbound_marker
from .queue.repository import DeliveryQueueRepository, DuplicateEnqueueError
from .users.repository import TelegramUsersRepository
from .group_access import target_ready, can_send
from .domain.models import Attachment
from .domain.enums import AttachmentKind
from .delivery_errors import InvalidPayload


async def destinations(db, max_chat_id, *, title=None):
    title=title or 'Чат MAX '+hashlib.sha256(str(max_chat_id).encode()).hexdigest()[:8]
    async with db.session() as session:
        routes=await RoutesRepository(session).list_by_max(max_account_id=1,max_chat_id=max_chat_id)
        targets=(await session.scalars(select(TelegramTarget).where(TelegramTarget.account_id==1))).all()
    result=[]
    for target in targets:
        if not target_ready(target):continue
        route=next((r for r in routes if r.telegram_chat_id==target.chat_id),None)
        if route is None and target.is_admin_hub and target.auto_add_chats:
            route=NS(id=None,max_chat_id=max_chat_id,telegram_chat_id=target.chat_id,
                     telegram_thread_id=None,label=title,enabled=True)
        if route:result.append((target,route))
    return result


async def handle_inbound(runtime, source, event):
    if source=='max':
        targets=await destinations(runtime.db,event.chat.external_id,title=event.chat.title)
        if not targets:return False
        # Optional identity resolution cannot log out, authenticate or block
        # forwarding indefinitely. Only the actual sender ID is looked up.
        if any(t.show_sender for t,_ in targets) and event.sender and not event.sender.display_name and hasattr(runtime._max,'resolve_sender_name'):
            try:
                name=await asyncio.wait_for(runtime._max.resolve_sender_name(event.sender.external_id),2)
                if name:event=replace(event,sender=replace(event.sender,display_name=name))
            except (Exception,):pass
    else:
        from .group_access import find_target
        target=await find_target(runtime.db,event.chat.external_id)
        targets=[(target,None)] if target_ready(target) else []
    accepted=False
    for target,route in targets:
        async with runtime.db.session() as session:
            engine=BridgeEngine(config=BridgeConfig(max_account_id=1,telegram_account_id=1,
                telegram_target_id=target.id,telegram_target_chat_id=target.chat_id,
                telegram_target_forum=target.is_forum,group_mode=True,route_override=route,
                show_source_chat=target.show_source_chat,show_sender=target.show_sender),
                routes=RoutesRepository(session),mapping=MessageMapRepository(session),
                queue=DeliveryQueueRepository(session),max_adapter=runtime._max,telegram_adapter=runtime._tg,
                acl=None,users=TelegramUsersRepository(runtime.db),on_enqueue=runtime.wakeup,
                media_fetcher=runtime._fetch_media,db=runtime.db,journal=runtime.journal)
            try:
                result=await (engine.handle_max_event(event) if source=='max' else engine.handle_telegram_event(event))
                accepted=accepted or result
            except DuplicateEnqueueError:
                runtime.journal.emit('message.duplicate',direction='max_to_telegram' if source=='max' else 'telegram_to_max')
            except Exception as exc:
                runtime.journal.emit('message.processing_failed',component='bridge',
                    direction='max_to_telegram' if source=='max' else 'telegram_to_max',error_class=type(exc).__name__)
                # Isolate a failed destination from other groups. A pre-queue
                # failure remains a visible error; queued jobs retry durably.
                import logging
                logging.getLogger(__name__).exception('group inbound processing failed')
    return accepted


async def enqueue_mirrors(engine,event,route,parent,parent_id):
    targets=await destinations(engine._db,route.max_chat_id,title=route.label)
    for target,destination in targets:
        if target.chat_id==event.chat.external_id:continue
        payload=copy.deepcopy(parent)
        payload.update(direction='max_to_telegram',group_version=2,route_id=destination.id,
            auto_admin_route=destination.id is None,max_chat_id=route.max_chat_id,
            max_message_id='pending-telegram-mirror',target_chat_id=target.chat_id,
            target_thread_id=destination.telegram_thread_id,marker=outbound_marker(),
            source_chat_title=destination.label or route.label or 'Чат MAX',
            mirror_from_telegram=True,media_origin='telegram',depends_on_id=parent_id,
            reply_to_telegram_message_id=None)
        payload['mirror_reply_to_max_message_id']=payload.pop('reply_to_max_message_id',None)
        from .bridge.provenance import safe_label
        from .bridge.attribution import attribute_text
        chat=safe_label(destination.label or route.label or 'Чат MAX') if target.show_source_chat else ''
        sender=parent.get('author_label') if target.show_sender else ''
        header=' · '.join(x for x in (chat,sender) if x)
        body='\n'.join(x for x in (parent.get('mirror_body'),parent.get('mirror_fallback')) if x)
        caption=parent.get('mirror_caption') or ''
        payload['text']=attribute_text(text=body,label=header) if body else ''
        if payload.get('media_items'):
            combined='\n'.join(x for x in (payload['text'],caption) if x)
            if not combined:combined=header
            fits=len(combined.encode('utf-16-le'))//2<=1024
            payload['album_caption']=combined if fits else header
            payload['text']='' if fits else combined
        elif payload.get('lazy_attachment') or payload.get('media_ref'):
            payload['caption']=attribute_text(text=caption,label=header) if not body else caption
        await engine._queue.enqueue(direction='max_to_telegram',target_chat_id=target.chat_id,
            target_thread_id=destination.telegram_thread_id,payload=payload,depends_on_id=parent_id,
            idempotency_key=f'mirror:{event.chat.external_id}:{event.external_id}:{target.chat_id}',commit=False)


async def prepare_destination(runtime, rec):
    """Validate the current route/grant again just before an external send."""
    p=rec.payload
    if p.get('group_version')!=2:
        # Old TG jobs do not identify the sending principal. They cannot be
        # authorized against scoped grants after migration; keep them visible
        # for manual review rather than guessing an identity or replaying.
        if runtime.runtime_mode.value=='production':
            if rec.direction=='telegram_to_max':raise InvalidPayload('legacy delivery requires review')
            async with runtime.db.session() as session:
                target=await session.scalar(select(TelegramTarget).where(
                    TelegramTarget.chat_id==rec.target_chat_id,
                    TelegramTarget.account_id==p.get('telegram_account_id')))
                route=await RoutesRepository(session).find_by_max(
                    max_account_id=p.get('max_account_id'),max_chat_id=p.get('max_chat_id'),
                    telegram_chat_id=rec.target_chat_id)
                if not target_ready(target):raise InvalidPayload('target disabled')
                if not route or not route.enabled or (route.telegram_thread_id or '')!=(rec.target_thread_id or ''):
                    raise InvalidPayload('legacy delivery requires review')
        return
    async with runtime.db.session() as session:
        if rec.direction=='telegram_to_max':
            route=await session.get(Route,p.get('route_id'))
            target=await session.scalar(select(TelegramTarget).where(TelegramTarget.chat_id==p['source_telegram_chat_id'],TelegramTarget.account_id==p['telegram_account_id']))
            if (not route or not route.enabled or not target_ready(target)
                or route.telegram_chat_id!=p['source_telegram_chat_id']
                or (route.telegram_thread_id or '')!=(p.get('source_telegram_thread_id') or '')
                or route.max_chat_id!=rec.target_chat_id):raise InvalidPayload('route changed')
            target_id=target.id
        else:
            target=await session.scalar(select(TelegramTarget).where(TelegramTarget.chat_id==rec.target_chat_id,TelegramTarget.account_id==p['telegram_account_id']))
            if not target_ready(target):raise InvalidPayload('target disabled')
            target_id=target.id
    if rec.direction=='telegram_to_max':
        if not await can_send(runtime.db,target_id,p.get('sender_telegram_user_id')):raise InvalidPayload('group grant revoked')
        return
    if p.get('auto_admin_route') and not p.get('route_id'):
        if not target.is_admin_hub or not target.auto_add_chats or not target.is_forum:raise InvalidPayload('automatic overview disabled')
        if not hasattr(runtime,'_group_topic_locks'):runtime._group_topic_locks=weakref.WeakValueDictionary()
        key=(target.id,p['max_chat_id']);lock=runtime._group_topic_locks.get(key)
        if lock is None:lock=asyncio.Lock();runtime._group_topic_locks[key]=lock
        async with lock:
            async with runtime.db.session() as session:
                route=await RoutesRepository(session).find_by_max(max_account_id=p['max_account_id'],max_chat_id=p['max_chat_id'],telegram_chat_id=target.chat_id)
            if route is None:
                from .bridge.provenance import safe_label
                topic=await runtime._tg.create_topic(chat_id=target.chat_id,title=safe_label(p.get('source_chat_title')) or 'Новый чат MAX')
                if not topic:raise RuntimeError('topic creation returned no ID')
                async with runtime.db.session() as session:
                    route=await RoutesRepository(session).upsert(max_account_id=p['max_account_id'],max_chat_id=p['max_chat_id'],telegram_account_id=target.account_id,
                        telegram_chat_id=target.chat_id,telegram_thread_id=str(topic),label=p.get('source_chat_title') or 'Чат MAX')
                runtime.journal.emit('routing.topic_created',component='bridge')
            if not route.enabled:raise InvalidPayload('route disabled')
            p.update(route_id=route.id,target_thread_id=route.telegram_thread_id)
    async with runtime.db.session() as session:
        route=await session.get(Route,p.get('route_id'))
        if (not route or not route.enabled or route.telegram_chat_id!=rec.target_chat_id
            or route.max_chat_id!=p['max_chat_id']
            or (route.telegram_thread_id or '')!=(p.get('target_thread_id') or '')):raise InvalidPayload('route changed')
        if p.get('mirror_from_telegram'):
            mapped=await MessageMapRepository(session).find_by_telegram(telegram_account_id=p['telegram_account_id'],
                telegram_chat_id=p['source_telegram_chat_id'],telegram_thread_id=p.get('source_telegram_thread_id'),telegram_message_id=p['source_telegram_message_id'])
            if mapped is None:raise InvalidPayload('mirror has no confirmed MAX mapping')
            p['max_message_id']=mapped.max_message_id
            # Resolve the original reply in the receiving group's own map.
            reply_id=rec.payload.get('mirror_reply_to_max_message_id')
            if reply_id:
                reply=await MessageMapRepository(session).find_by_max(
                    max_account_id=p['max_account_id'],max_chat_id=p['max_chat_id'],
                    max_message_id=reply_id,telegram_chat_id=rec.target_chat_id)
                if reply and (reply.telegram_thread_id or '')==(p.get('target_thread_id') or ''):
                    p['reply_to_telegram_message_id']=reply.telegram_message_id
        await DeliveryQueueRepository(session).checkpoint(queue_id=rec.id,payload=p,worker_id=rec.worker_id)


async def owned_media(runtime,rec,source,attachment):
    stored=await runtime._fetch_media(source,attachment)
    if stored is None:return None
    directory=runtime.layout.cache/'delivery-owned';directory.mkdir(parents=True,exist_ok=True)
    suffix=Path(stored.media_ref.partition(':')[2]).suffix
    fd,name=tempfile.mkstemp(prefix=f'{rec.id}-',suffix=suffix,dir=directory)
    import os
    os.close(fd)
    try:shutil.copyfile(stored.media_ref.partition(':')[2],name)
    except BaseException:
        Path(name).unlink(missing_ok=True);raise
    return NS(media_ref=stored.kind+':'+name,kind=stored.kind,path=Path(name),filename=getattr(stored,'filename','media'),
              mime=getattr(stored,'mime',None),size=getattr(stored,'size',None),sha256=getattr(stored,'sha256',None))


async def prepare_lazy_media(runtime,rec):
    p=rec.payload
    item=p.get('lazy_attachment')
    if not item:return
    ref=p.get('owned_media_ref')
    if ref and Path(ref.partition(':')[2]).is_file():p['media_ref']=ref;return
    stored=await owned_media(runtime,rec,p.get('media_origin','max'),Attachment(kind=AttachmentKind(item['kind']),payload=item['payload'],caption=item.get('caption')))
    if stored is None:raise RuntimeError('media temporarily unavailable')
    p.update(media_ref=stored.media_ref,owned_media_ref=stored.media_ref,media_path=str(stored.path),media_kind=stored.kind,
             media_filename=stored.filename,media_mime=stored.mime,media_size=stored.size,media_sha256=stored.sha256)
    async with runtime.db.session() as session:
        await DeliveryQueueRepository(session).checkpoint(queue_id=rec.id,payload=p,worker_id=rec.worker_id)
