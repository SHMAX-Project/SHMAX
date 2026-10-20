"""Real PyMax 2.4.1 models and durable album progress; no live credentials."""
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pymax.types.domain.attachments.photo import PhotoAttachment
from pymax.types.domain.message import Message
from shmax.adapters.max.base import MaxEvent
from shmax.adapters.normalize import normalize_max
from shmax.bridge.engine import BridgeEngine
from shmax.domain.models import ReplyRef
from shmax.media.album import deliver_album


def event(count, chat=0):
    raw=Message(id=123,chat_id=chat,sender=42,text='album caption',time=1,type='USER',cid=None,
                attaches=[PhotoAttachment(type='PHOTO',photo_token='fixture',photo_id=i+1,base_url=f'https://cdn.max.ru/fixture-{i}',width=128,height=128) for i in range(count)])
    return normalize_max(MaxEvent(event_id='123',chat_id=str(chat),sender_id='42',text=raw.text,timestamp=1,raw=raw))


class Destination:
    def __init__(self):self.calls=[];self.fail=False;self.counter=0
    async def send_media_group(self,**kw):
        if self.fail:raise TimeoutError('fixture upload timeout')
        self.calls.append(('group',kw));ids=[]
        for ref in kw['media_refs']:self.counter+=1;ids.append(str(self.counter))
        return NS(external_message_id=ids[0],all_external_ids=ids)
    async def send_media(self,**kw):
        self.calls.append(('single',kw));self.counter+=1;return NS(external_message_id=str(self.counter))
    async def send_message(self,**kw):
        self.calls.append(('text',kw));self.counter+=1;return NS(external_message_id=str(self.counter))
    async def send_reply(self,**kw):return await self.send_message(**kw)


async def setup(count,tmp_path):
    n=event(count);engine=object.__new__(BridgeEngine)
    media,fallback=await engine._prepare_media(n,'max')
    assert fallback is None
    media['album_caption']='Sender\nalbum caption';media['text']=''
    dest=Destination();saved=[]
    async def fetch(source,a):
        p=tmp_path/f'{a.payload["photo_id"]}.jpg';p.write_bytes(b'fixture-image')
        return NS(media_ref='photo:'+str(p),kind='photo')
    async def save(payload):saved.append(copy.deepcopy(payload))
    return n,media,dest,fetch,save,saved


@pytest.mark.parametrize('count',[1,2,6,10])
def test_real_pymax_normalization_preserves_every_photo_and_saved_messages(count):
    n=event(count)
    assert n.chat.external_id=='0'
    assert len(n.attachments)==count
    assert [a.payload['photo_id'] for a in n.attachments]==list(range(1,count+1))
    assert all(a.kind.value=='photo' for a in n.attachments)


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[2,6,10,11,21])
async def test_all_photos_correct_forum_order_caption_once(count,tmp_path):
    n,p,dest,fetch,save,saved=await setup(count,tmp_path)
    result=await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=dest,fetch=fetch,save=save)
    refs=[ref for kind,kw in dest.calls for ref in (kw['media_refs'] if kind=='group' else [kw['media_ref']])]
    assert [Path(r.partition(':')[2]).stem for r in refs]==[str(i) for i in range(1,count+1)]
    assert len(result.all_external_ids)==count
    assert all(kw['thread_id']=='52' and kw['chat_id']=='-100777' for kind,kw in dest.calls)
    assert sum(bool(kw.get('caption')) for kind,kw in dest.calls)==1
    assert all(2<=len(kw['media_refs'])<=10 for kind,kw in dest.calls if kind=='group')
    assert all('Неподдерживаемое' not in str(kw) for kind,kw in dest.calls)
    assert saved[-1]['album_progress']['next']==count


@pytest.mark.asyncio
async def test_reply_parameter_preserved(tmp_path):
    n,p,d,f,s,states=await setup(6,tmp_path);reply=ReplyRef('telegram','-100777','300','52')
    await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=reply,adapter=d,fetch=f,save=s)
    assert d.calls[0][1]['reply_to'] is reply


@pytest.mark.asyncio
async def test_upload_retry_before_confirmation(tmp_path):
    n,p,d,f,s,states=await setup(6,tmp_path);d.fail=True
    with pytest.raises(TimeoutError):await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=f,save=s)
    assert not states and p['album_progress']['next']==0
    d.fail=False
    await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=f,save=s)
    assert len(d.calls)==1


@pytest.mark.asyncio
async def test_download_failure_partial_success_restart_retry_no_duplicates(tmp_path):
    n,p,d,fetch,save,states=await setup(6,tmp_path)
    async def failure(source,a):
        if a.payload['photo_id']==4:raise TimeoutError('fixture download')
        return await fetch(source,a)
    with pytest.raises(TimeoutError):await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=failure,save=save)
    assert states[-1]['album_progress']['next']==3
    # Restart reconstructs solely from the durable payload.
    restored=copy.deepcopy(states[-1])
    result=await deliver_album(payload=restored,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=fetch,save=save)
    assert len(result.all_external_ids)==6
    assert [len(kw['media_refs']) for kind,kw in d.calls]==[3,3]
    assert sum(bool(kw['caption']) for kind,kw in d.calls)==1


@pytest.mark.asyncio
async def test_terminal_unavailable_photo_does_not_lose_other_valid_photos(tmp_path):
    n,p,d,fetch,save,states=await setup(6,tmp_path)
    async def failure(source,a):
        if a.payload['photo_id']==4:raise TimeoutError('fixture download')
        return await fetch(source,a)
    result=await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=failure,save=save,terminal=True)
    assert result.state=='failed' and len(result.all_external_ids)==5
    refs=[r for kind,kw in d.calls for r in kw['media_refs']]
    assert [Path(r.partition(':')[2]).stem for r in refs]==['1','2','3','5','6']


@pytest.mark.asyncio
async def test_mixed_document_unknown_keeps_all_supported_items(tmp_path):
    from shmax.domain.models import Attachment
    from shmax.domain.enums import AttachmentKind
    from dataclasses import replace
    n=event(2);n=replace(n,attachments=(n.attachments[0],Attachment(kind=AttachmentKind.OTHER,payload={}),Attachment(kind=AttachmentKind.DOCUMENT,payload={'file_id':7}),n.attachments[1]))
    engine=object.__new__(BridgeEngine);media,fallback=await engine._prepare_media(n,'max')
    assert [i['kind'] for i in media['media_items']]==['photo','document','photo']
    assert fallback.count('Неподдерживаемое')==1


@pytest.mark.asyncio
async def test_native_bot_api_album_returns_all_ids_forum_reply_and_escaped_caption(tmp_path):
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.manager import TransportConfig, TransportMode
    adapter=TelegramBotApiAdapter(TransportConfig(mode=TransportMode.BOT_API_DIRECT, bot_token='123:fixture'))
    calls=[]
    class Bot:
        async def send_media_group(self,**kw):
            calls.append(kw)
            return [NS(message_id=i,chat=NS(id=-100777)) for i in range(1,7)]
    adapter._started=True;adapter._bot=Bot()
    reply=ReplyRef('telegram','-100777','300','52')
    result=await adapter.send_media_group(chat_id='-100777',media_refs=['photo:'+str(tmp_path/f'{i}.jpg') for i in range(6)],thread_id='52',caption='Sender <safe>',reply_to=reply)
    assert result.all_external_ids==['1','2','3','4','5','6']
    assert calls[0]['message_thread_id']==52 and calls[0]['reply_parameters'].message_id==300
    assert [m.caption for m in calls[0]['media']]==['Sender &lt;safe&gt;',None,None,None,None,None]


from tests.test_media_retry_lifetime import rt


@pytest.mark.asyncio
async def test_durable_queue_aliases_duplicate_event_and_restart(rt,tmp_path):
    from shmax.database.models import MaxAccount,TelegramAccount
    from shmax.queue.repository import DeliveryQueueRepository,DuplicateEnqueueError
    from shmax.bridge.message_map import MessageMapRepository
    async with rt.db.session() as session:
        session.add_all([MaxAccount(id=1,label='fixture'),TelegramAccount(id=1,label='fixture')]);await session.commit()
    n,p,dest,fetch,save,states=await setup(6,tmp_path)
    p.update(direction='max_to_telegram',max_account_id=1,max_chat_id='0',max_message_id='123',telegram_account_id=1,marker='fixture-album-marker',target_chat_id='-100777')
    async with rt.db.session() as session:
        repo=DeliveryQueueRepository(session)
        await repo.enqueue(direction='max_to_telegram',target_chat_id='-100777',target_thread_id='52',payload=p,idempotency_key='max:0:123:max_to_telegram')
        with pytest.raises(DuplicateEnqueueError):await repo.enqueue(direction='max_to_telegram',target_chat_id='-100777',payload=p,idempotency_key='max:0:123:max_to_telegram')
    rt._tg=dest
    failed=True
    async def intermittent(source,a):
        if failed and a.payload['photo_id']==4:raise TimeoutError('fixture')
        return await fetch(source,a)
    rt._fetch_media=intermittent
    rec=await rt._claim_one(120)
    with pytest.raises(TimeoutError):await rt._deliver(rec)
    assert len(dest.calls[0][1]['media_refs'])==3
    async with rt.db.session() as session:
        from sqlalchemy import select
        from shmax.database.models import DeliveryQueueItem
        row=await session.scalar(select(DeliveryQueueItem));assert row.payload['album_progress']['next']==3
        await DeliveryQueueRepository(session).mark_retry(queue_id=row.id,last_error='fixture',next_attempt_at=row.created_at)
    failed=False
    # A fresh claim reconstructs persisted progress after a process restart.
    rec=await rt._claim_one(120);result=await rt._deliver(rec);await rt._record_mapping(rec,result)
    async with rt.db.session() as session:
        await DeliveryQueueRepository(session).mark_delivered(queue_id=rec.id)
        maps=MessageMapRepository(session)
        for ident in result.all_external_ids:
            mapped=await maps.find_by_telegram(telegram_account_id=1,telegram_chat_id='-100777',telegram_thread_id='52',telegram_message_id=ident)
            assert mapped.max_message_id=='123' and mapped.max_chat_id=='0'
        assert await maps.find_by_telegram(telegram_account_id=1,telegram_chat_id='-100777',telegram_thread_id='53',telegram_message_id='2') is None
    assert [len(kw['media_refs']) for kind,kw in dest.calls]==[3,3]


@pytest.mark.asyncio
async def test_explicit_group_rejection_falls_back_in_order_but_timeout_never_does(tmp_path):
    from shmax.media.diagnostics import MediaGroupRejected
    n,p,d,fetch,save,states=await setup(6,tmp_path)
    async def rejected(**kwargs):raise MediaGroupRejected('telegram.album_send','request_failed')
    d.send_media_group=rejected
    result=await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=fetch,save=save)
    assert len(result.all_external_ids)==6
    assert all(kind=='single' for kind,kw in d.calls)
    assert [Path(kw['media_ref'].partition(':')[2]).stem for kind,kw in d.calls]==['1','2','3','4','5','6']
    assert sum(bool(kw['caption']) for kind,kw in d.calls)==1


@pytest.mark.asyncio
async def test_long_text_chunks_are_checkpointed_without_resending_photos(tmp_path):
    n,p,d,fetch,save,states=await setup(2,tmp_path);p['text']='🦆'*3000
    await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=fetch,save=save)
    texts=[kw['text'] for kind,kw in d.calls if kind=='text']
    assert ''.join(texts)==p['text']
    assert all(len(t.encode('utf-16-le'))//2<=4096 for t in texts)
    before=len(d.calls)
    await deliver_album(payload=copy.deepcopy(states[-1]),chat_id='-100777',thread_id='52',reply_to=None,adapter=d,fetch=fetch,save=save)
    assert len(d.calls)==before


@pytest.mark.asyncio
async def test_single_photo_preserves_existing_engine_contract(tmp_path):
    engine=object.__new__(BridgeEngine)
    async def fetch(source,a):return NS(media_ref='photo:'+str(tmp_path/'one.jpg'),kind='photo',filename='one.jpg',mime='image/jpeg',size=1,sha256='fixture')
    engine._media_fetcher=fetch
    media,fallback=await engine._prepare_media(event(1),'max')
    assert media['media_ref'].endswith('/one.jpg') and media['media_kind']=='photo'
    assert 'media_items' not in media and fallback is None


@pytest.mark.asyncio
async def test_expired_url_refresh_uses_supported_sdk_get_messages_and_chat_zero(tmp_path):
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
    from shmax.media.diagnostics import MediaFailure
    adapter=object.__new__(PyMaxMaxAdapter);lookups=[];downloads=[]
    async def get_messages(chat,ids):lookups.append((chat,ids));return [NS(attaches=[NS(photo_id=1,base_url='https://cdn.max.ru/fresh')])]
    adapter._handle=NS(client=NS(get_messages=get_messages))
    async def download(url,dest):
        downloads.append(url)
        if url.endswith('expired'):raise MediaFailure('max.download','http_response_failed')
        return 3
    adapter._download_url=download
    size=await adapter.download_attachment({'url':'https://cdn.max.ru/expired','photo_id':1,'chat_id':0,'message_id':123},tmp_path/'p.jpg')
    assert size==3 and lookups==[(0,[123])]
    assert downloads==['https://cdn.max.ru/expired','https://cdn.max.ru/fresh']


@pytest.mark.asyncio
async def test_wrong_worker_cannot_checkpoint_or_renew_album(rt):
    from shmax.queue.repository import DeliveryQueueRepository,LeaseOwnershipLostError
    async with rt.db.session() as s:
        await DeliveryQueueRepository(s).enqueue(direction='max_to_telegram',target_chat_id='-100777',payload={'media_items':[]})
    row=await rt._claim_one(120)
    async with rt.db.session() as s:
        for operation in ('checkpoint','renew_lease'):
            kw={'queue_id':row.id,'worker_id':'unrelated-worker'}
            if operation=='checkpoint':kw['payload']={'album_progress':{'next':100}}
            with pytest.raises(LeaseOwnershipLostError):await getattr(DeliveryQueueRepository(s),operation)(**kw)


def test_legacy_host_cli_selects_only_the_accepted_release(tmp_path):
    from shmax.cli.main import _accepted_backend
    opt=tmp_path/'opt';backend=opt/'releases'/'accepted-011'/'backend'
    (backend/'shmax/cli').mkdir(parents=True)
    (backend/'shmax/VERSION').write_text('0.1.1')
    (backend/'shmax/cli/main.py').write_text('fixture')
    (opt/'.env').write_text('SHMAX_DEPLOYMENT_ID=accepted-011\n')
    assert _accepted_backend(opt)==backend.resolve()
    (opt/'.env').write_text('SHMAX_DEPLOYMENT_ID=../../outside\n')
    assert _accepted_backend(opt) is None


@pytest.mark.asyncio
async def test_album_rate_limit_sets_persistent_retry_deadline(rt,tmp_path):
    import datetime as dt
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.media.diagnostics import MediaFailure
    from sqlalchemy import select
    from shmax.database.models import DeliveryQueueItem
    n,p,dest,fetch,save,states=await setup(2,tmp_path)
    p.update(direction='max_to_telegram',max_account_id=1,max_chat_id='0',max_message_id='123',telegram_account_id=1,marker='fixture-rate-marker',target_chat_id='-100777')
    async def limited(**kwargs):
        exc=MediaFailure('telegram.album_send','request_failed');exc.retry_after=61;raise exc
    dest.send_media_group=limited;rt._tg=dest;rt._fetch_media=fetch
    async with rt.db.session() as session:
        await DeliveryQueueRepository(session).enqueue(direction='max_to_telegram',target_chat_id='-100777',payload=p)
    rec=await rt._claim_one(120);before=dt.datetime.now(dt.timezone.utc)
    await rt._drain_one(rec)
    async with rt.db.session() as session:
        row=await session.scalar(select(DeliveryQueueItem))
        assert row.status=='retry' and row.attempts==1
        deadline=row.next_attempt_at.replace(tzinfo=dt.timezone.utc)
        assert (deadline-before).total_seconds()>=61

@pytest.mark.asyncio
async def test_cancellation_during_download_removes_prepared_cache(tmp_path):
    import asyncio
    n,p,dest,fetch,save,saved=await setup(6,tmp_path)
    original=fetch
    async def cancelled_fetch(source, attachment):
        if attachment.payload['photo_id']==2:
            raise asyncio.CancelledError()
        return await original(source,attachment)
    with pytest.raises(asyncio.CancelledError):
        await deliver_album(payload=p,chat_id='-100777',thread_id='52',reply_to=None,adapter=dest,fetch=cancelled_fetch,save=save)
    assert not list(tmp_path.glob('*.jpg'))
    assert dest.calls==[]
    assert p['album_progress']['next']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', 'caption', '🦆' * 512, '🦆' * 513],
                         ids=['no-caption', 'caption', '1024-utf16', '1026-utf16'])
async def test_three_photos_without_sender_name_use_durable_queue(rt, tmp_path, text):
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.database.models import MaxAccount, TelegramAccount

    async with rt.db.session() as session:
        session.add_all([MaxAccount(id=1, label='fixture'), TelegramAccount(id=1, label='fixture')])
        await session.commit()
    n = replace(event(3), text=text)
    assert n.sender.display_name is None
    engine = object.__new__(BridgeEngine)
    engine._config = NS(max_account_id=1, telegram_account_id=1)
    engine._on_enqueue = None
    engine.journal = None
    media, fallback = await engine._prepare_media(n, 'max')
    async with rt.db.session() as session:
        engine._queue = DeliveryQueueRepository(session)
        await engine._enqueue_max_to_tg(
            event=n, text=text,
            route=NS(telegram_chat_id='-100777', telegram_thread_id='52'),
            reply_to_telegram_message_id=None, media=media, fallback=fallback,
        )
    rec = await rt._claim_one(120)
    assert rec is not None
    payload = rec.payload
    assert len(payload['attachments']) == 3
    fits = len(text.encode('utf-16-le')) // 2 <= 1024
    assert payload['album_caption'] == (text if fits else '')
    assert payload['text'] == ('' if fits else text)
    dest = Destination()
    async def fetch(source, attachment):
        path = tmp_path / f'{attachment.payload["photo_id"]}.jpg'
        path.write_bytes(b'fixture-image')
        return NS(media_ref='photo:' + str(path), kind='photo')
    rt._tg = dest
    rt._fetch_media = fetch
    result = await rt._deliver(rec)
    assert len(result.all_external_ids) == (3 if fits else 4)
    groups = [kw for kind, kw in dest.calls if kind == 'group']
    assert len(groups) == 1 and len(groups[0]['media_refs']) == 3
    assert (groups[0].get('caption') or '') == (text if fits else '')
    assert all(kw['thread_id'] == '52' for kind, kw in dest.calls)
    if not fits:
        assert ''.join(kw['text'] for kind, kw in dest.calls if kind == 'text') == text
